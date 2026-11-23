"""量化口径的唯一实现：月数与阈值一律由代码计算，模型只负责解释。

真实模型曾把「剩余现金 ÷ 养车增量」当成可支撑月数，620000÷1650=376 个月，
而同参数的正确口径 (800000-180000)÷((16000-4000)+1650)=45.4 个月。这里把公式写死：

    月必要开支 = max(月收入 - 月结余, 0)
    月消耗     = 月必要开支 + 月度增量支出
    可支撑月数 = (可动用存款 - 一次性支出) ÷ 月消耗
    可动用存款下限 = 一次性支出 + 6 × 月必要开支
    月结余下限     = 月度增量支出

预置剧本与真实模型共用本模块，避免两套口径漂移。
"""
import re

from .numbers_cn import fmt_int, fmt_yuan, parse_amount

ASSUMED = "（假设值）"
EMERGENCY_MONTHS = 6

DEFAULT_INCOME = 11500
DEFAULT_CASH = 1200000
DEFAULT_SAVING = 3000
DEFAULT_ONE_TIME = 200000
DEFAULT_DELTA = 1650

QUANT_DEFAULTS = {
    "emergency_months": float(EMERGENCY_MONTHS),
    "opportunity_low": 0.03,
    "opportunity_high": 0.05,
    "sensitivity_pct": 0.20,
    "monthly_income": float(DEFAULT_INCOME),
    "cash": float(DEFAULT_CASH),
    "monthly_saving": float(DEFAULT_SAVING),
    "max_one_time": float(DEFAULT_ONE_TIME),
    "monthly_delta": float(DEFAULT_DELTA),
}
PARAMS = dict(QUANT_DEFAULTS)


def configure(options=None) -> dict:
    """按 config.json 的 quant 段覆盖量化口径；未知键忽略，非法值直接报错。"""
    merged = dict(QUANT_DEFAULTS)
    for key, value in (options or {}).items():
        if key not in QUANT_DEFAULTS:
            continue
        if isinstance(value, bool):
            raise ValueError("quant.%s 必须是数字，当前为 %r" % (key, value))
        try:
            merged[key] = float(value)
        except (TypeError, ValueError):
            raise ValueError("quant.%s 必须是数字，当前为 %r" % (key, value))
    if merged["emergency_months"] <= 0 or merged["sensitivity_pct"] <= 0:
        raise ValueError("quant.emergency_months 与 quant.sensitivity_pct 必须大于 0")
    if not 0 <= merged["opportunity_low"] <= merged["opportunity_high"]:
        raise ValueError("quant.opportunity_low 不能大于 opportunity_high，且都不能为负")
    PARAMS.update(merged)
    return dict(PARAMS)


def default_amount(key: str) -> float:
    return PARAMS.get(key, QUANT_DEFAULTS.get(key, 0.0))


def parameters_text() -> str:
    """给 A4 的量化参数说明，保证模型解释与代码口径一致。"""
    return ("【本次量化参数】应急金按 %d 个月月必要开支；敏感性上下浮动 %d%%；"
            "机会成本按一次性支出的年化 %.1f%%～%.1f%%。" % (
                int(PARAMS["emergency_months"]), int(round(PARAMS["sensitivity_pct"] * 100)),
                PARAMS["opportunity_low"] * 100, PARAMS["opportunity_high"] * 100))

COST_HINTS = ("养车", "每年", "成本", "费用", "支出", "月供", "租金")

MONTH_PATTERNS = (re.compile(r"(?:每|/|／)\s*月"), re.compile(r"月均"), re.compile(r"每个月"))
YEAR_PATTERNS = (
    re.compile(r"(?:每|/|／)\s*年"),
    re.compile(r"一年"),
    re.compile(r"全年"),
    re.compile(r"(?<![明去今当本同次])年度"),
    re.compile(r"(?<![明去今当本同次])年成本"),
    re.compile(r"(?<![明去今当本同次])年费用"),
    re.compile(r"(?<![明去今当本同次])年租金"),
    re.compile(r"(?<![明去今当本同次])年薪"),
)
YEAR_PREFIX_EXCLUDE = frozenset("明去今当本同次")


def cost_period(text: str, anchor=None) -> str:
    """判断成本口径是每月还是每年。

    只要附近出现“每月/每个月”，一律按月，避免“明年开始每月租金 5000”
    因为一个“年”字被除以 12；anchor 指向关键词位置时还要识别“年养车成本”
    这种年份写在关键词前面的口语写法。
    """
    body = str(text or "")
    if anchor:
        start, end = anchor
        window = body[max(0, start - 6): end + 14]
    else:
        window = body
    if any(pattern.search(window) for pattern in MONTH_PATTERNS):
        return "month"
    if any(pattern.search(window) for pattern in YEAR_PATTERNS):
        return "year"
    if anchor:
        idx = anchor[0]
        if idx > 0 and body[idx - 1] == "年" and (
                idx < 2 or body[idx - 2] not in YEAR_PREFIX_EXCLUDE):
            return "year"
    return "month"


def monthly_amount(value, text: str = "", anchor=None):
    """把成本金额换算成元/月；无法判断时保持原值。"""
    if cost_period(text, anchor) == "year":
        return round(float(value) / 12.0, 2)
    return value

# 只纠偏明确指向“可支撑月数”的表述，避免误改“六个月应急金”这类正常写法。
MONTHS_CLAIM = re.compile(
    r"(可支撑|能支撑|可覆盖|能覆盖|可维持|维持)([^0-9\n。；]{0,12})(\d+(?:\.\d+)?)\s*个月")


def coerce_amount(value):
    """把档案里的数字转成元；兼容 "80万"、"1.15万"、"16,000 元" 这类写法。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    amount = parse_amount(text)
    return amount if amount > 0 else None


def resolve_finance(archive, monthly_delta=None):
    """返回 (财务四项, 月度增量, 假设值说明)；缺失或不可解析的项用假设值补足。"""
    fin = (archive or {}).get("finance") or {}
    notes = []
    resolved = {}
    for key, label in (
        ("monthly_income", "月收入"),
        ("cash", "可动用存款"),
        ("monthly_saving", "月结余"),
        ("max_one_time", "一次性支出"),
    ):
        value = coerce_amount(fin.get(key))
        if value is None:
            default = default_amount(key)
            value = float(default)
            notes.append("%s按 %d 元%s" % (label, default, ASSUMED))
        resolved[key] = value

    delta = coerce_amount(monthly_delta)
    if delta is None:
        # 用户质疑成本后修订过的口径写在档案里，全量重算必须沿用，不得回落到默认值
        delta = coerce_amount(fin.get("monthly_delta"))
    if delta is None:
        default = default_amount("monthly_delta")
        delta = float(default)
        notes.append("月度增量支出按 %d 元%s" % (default, ASSUMED))
    resolved["monthly_delta"] = round(float(delta))
    return resolved, resolved["monthly_delta"], notes


def measure(archive, monthly_delta=None) -> dict:
    """按固定公式算出量化卡片：现金流、阈值、敏感性与机会成本区间。"""
    fin, delta, notes = resolve_finance(archive, monthly_delta)
    income = fin["monthly_income"]
    cash = fin["cash"]
    saving = fin["monthly_saving"]
    one_time = fin["max_one_time"]

    monthly_need = max(income - saving, 0.0)
    remaining = cash - one_time
    burn = monthly_need + delta
    no_burn = burn <= 0
    months = 0.0 if no_burn else remaining / burn
    months_text = "不适用（月消耗为 0）" if no_burn else "%.1f 个月" % months
    emergency_months = PARAMS["emergency_months"]
    emergency = emergency_months * monthly_need
    boundary = "未越界" if remaining >= emergency else "越界"
    boundary_calc = "%s - %s = %s ≥ 应急金 %s = %d×%s" % (
        fmt_int(cash), fmt_int(one_time), fmt_int(remaining),
        fmt_int(emergency), int(emergency_months), fmt_int(monthly_need))
    assumed = [note for note in notes if ASSUMED in note and "月度增量" not in note]
    insufficient = len(assumed) >= 2

    calc = ("一次性支出 %s；月度增量 %s；月必要开支 %s - %s = %s；"
            "剩余 %s - %s = %s；可支撑月数 = %s ÷ (%s + %s) = %s%s" % (
                fmt_int(one_time), fmt_int(delta),
                fmt_int(income), fmt_int(saving), fmt_int(monthly_need),
                fmt_int(cash), fmt_int(one_time), fmt_int(remaining),
                fmt_int(remaining), fmt_int(monthly_need), fmt_int(delta), months_text,
                ("；" + "；".join(notes)) if notes else ""))

    def months_with(price, growth):
        rest = cash - price
        return rest / (monthly_need + growth) if (monthly_need + growth) > 0 else 0.0

    def boundary_with(price, growth):
        return "未越界" if cash - price >= emergency else "越界"

    sensitivity = PARAMS["sensitivity_pct"]
    p_up, p_down = one_time * (1 + sensitivity), one_time * (1 - sensitivity)
    m_up, m_down = delta * (1 + sensitivity), delta * (1 - sensitivity)
    flip_p = boundary_with(p_up, delta) != boundary_with(p_down, delta)
    flip_m = boundary_with(one_time, m_up) != boundary_with(one_time, m_down)

    opp_low = one_time * PARAMS["opportunity_low"]
    opp_high = one_time * PARAMS["opportunity_high"]
    return {
        "finance": fin,
        "monthly_delta": delta,
        "months": round(months, 1),
        "no_burn": no_burn,
        "insufficient": insufficient,
        "notes": notes,
        "cashflow": {
            "calc": calc,
            "one_time": int(one_time) if float(one_time).is_integer() else round(one_time, 2),
            "monthly_delta": int(delta) if float(delta).is_integer() else round(delta, 2),
            "months": round(months, 1),
            "month_text": months_text,
            "no_burn": no_burn,
            "insufficient": insufficient,
            "boundary": boundary,
            "boundary_calc": boundary_calc,
        },
        "opportunity": {
            "calc": "%s × 年化 %.1f%%～%.1f%%" % (
                fmt_int(one_time), PARAMS["opportunity_low"] * 100,
                PARAMS["opportunity_high"] * 100),
            "annual_low": round(opp_low),
            "annual_high": round(opp_high),
            "range": "%s～%s 元/年" % (fmt_int(opp_low), fmt_int(opp_high)),
        },
        "thresholds": [
            {"name": "可动用存款下限", "value": fmt_yuan(one_time + emergency),
             "derive": "一次性支出 %s + %d 个月应急金 %s = %s" % (
                 fmt_int(one_time), int(emergency_months), fmt_int(emergency),
                 fmt_int(one_time + emergency))},
            {"name": "月结余下限", "value": "%s 元/月" % fmt_int(delta),
             "derive": "月结余低于月度增量支出 %s 时，存量资金被逐月侵蚀" % fmt_int(delta)},
        ],
        "sensitivity": [
            {"var": "一次性支出", "up": "上浮 %d%% 至 %s，可支撑 %.1f 个月，边界%s" % (
                int(round(sensitivity * 100)), fmt_int(p_up), months_with(p_up, delta),
                boundary_with(p_up, delta)),
             "down": "下调 %d%% 至 %s，可支撑 %.1f 个月，边界%s" % (
                 int(round(sensitivity * 100)), fmt_int(p_down), months_with(p_down, delta),
                 boundary_with(p_down, delta)),
             "flip": flip_p},
            {"var": "月度增量支出", "up": "上浮 %d%% 至 %s，可支撑 %.1f 个月，边界%s" % (
                int(round(sensitivity * 100)), fmt_int(m_up), months_with(one_time, m_up),
                boundary_with(one_time, m_up)),
             "down": "下调 %d%% 至 %s，可支撑 %.1f 个月，边界%s" % (
                 int(round(sensitivity * 100)), fmt_int(m_down), months_with(one_time, m_down),
                 boundary_with(one_time, m_down)),
             "flip": flip_m},
        ],
    }


def cost_amount(text: str) -> float:
    """从用户原话里取成本金额。

    “反方说每年两万太高了，我实际一年一万二”这类反驳会同时出现两个数，
    用户用“实际”断言的那个才是新口径，必须优先取它。
    """
    body = str(text or "")
    marker = body.find("实际")
    if marker != -1:
        clause = re.split(r"[，。；、,;]", body[marker:])[0]
        amount = parse_amount(clause)
        if amount > 0:
            return amount
    return parse_amount(body)


def delta_from_neu(data, hint: str = "", fallback=None):
    """确定月度增量支出：成本类指令 > 上一轮权威值 > 模型字段。

    上一轮权威值优先于模型字段：非成本类质疑触发的重算，模型常随口给出新的
    monthly_delta（实测出现过 350000 元/月，把 27.7 个月压成 1.6 个月）。
    """
    text = str(hint or "")
    if text and any(key in text for key in COST_HINTS):
        amount = cost_amount(text)
        if amount > 0:
            anchor = None
            for key in COST_HINTS:
                pos = text.find(key)
                if pos != -1:
                    anchor = (pos, pos + len(key))
                    break
            return monthly_amount(amount, text, anchor)
    previous = coerce_amount(fallback)
    if previous is not None:
        return previous
    cashflow = (data or {}).get("cashflow") or {}
    return coerce_amount(cashflow.get("monthly_delta"))


def external_values(archive) -> dict:
    """外部条目里可核验的数值，按 [外N] 编号索引。"""
    values = {}
    ext = (archive or {}).get("external") or {}
    for item in ext.get("items") or []:
        try:
            item_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not item_id:
            continue
        numbers = []
        facts = item.get("facts") or {}
        for group in ("amounts", "rates"):
            for entry in facts.get(group) or []:
                try:
                    numbers.append(float(entry.get("value")))
                except (TypeError, ValueError):
                    continue
        values[item_id] = numbers
    return values


def validate_external_use(archive, data) -> list:
    """A4 的 external_use.cited 只保留能在对应条目要点里核到的数字。

    外部文章不能直接改量化公式，但可以作为校准依据被引用；凡是模型编造的
    编号、金额或利率，一律在这里丢弃，并随返回值进入日志。
    """
    use = (data or {}).get("external_use") if isinstance(data, dict) else None
    if not isinstance(use, dict):
        return []
    values = external_values(archive)
    accepted = []
    for entry in use.get("cited") or []:
        if not isinstance(entry, dict):
            continue
        try:
            item_id = int(entry.get("id") or 0)
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            continue
        numbers = values.get(item_id) or []
        if not any(abs(value - number) <= max(1.0, abs(number) * 0.005) for number in numbers):
            continue
        accepted.append({
            "id": item_id,
            "value": value,
            "unit": str(entry.get("unit") or ""),
            "key": str(entry.get("key") or ""),
            "how": str(entry.get("how") or ""),
        })
        if len(accepted) >= 6:
            break
    return accepted


def normalize_neu(archive, data, hint: str = "", fallback_delta=None) -> dict:
    """用代码口径覆盖 A4 输出：月数、安全边界、阈值、敏感性以代码结果为准。

    模型给的 side/round/params 等解释性字段保留，但所有会被看板与最终报告
    引用的量化数字一律重算，避免真实模型自行选择公式。
    """
    source = data if isinstance(data, dict) else {}
    measured = measure(archive, delta_from_neu(source, hint, fallback_delta))
    out = dict(source)
    cashflow = dict(source.get("cashflow") or {})
    cashflow.update(measured["cashflow"])
    out["cashflow"] = cashflow
    out["thresholds"] = measured["thresholds"]
    out["sensitivity"] = measured["sensitivity"]
    # 机会成本口径同样只能由代码给出：模型实测自造过「年化 2%～6%」「按全部存款测算」
    out["opportunity"] = dict(measured["opportunity"])
    out.setdefault("side", "NEU")
    return out


def authority(archive, neu=None) -> dict:
    """最终报告必须引用的权威量化结论（与看板同源）。"""
    measured = measure(archive, delta_from_neu(neu))
    th = measured["thresholds"]
    no_burn = measured["cashflow"].get("no_burn")
    insufficient = measured.get("insufficient")
    months = None if no_burn else measured["months"]
    months_text = "不适用（月消耗为 0）" if no_burn else "%.1f 个月" % months
    thresholds_text = "安全边界%s；支出后%s；阈值一 %s；阈值二 %s" % (
        measured["cashflow"]["boundary"],
        months_text if no_burn else "可支撑 " + months_text,
        th[0]["value"], th[1]["value"])
    prompt_text = "\n".join([
        "可支撑月数：%s" % months_text,
        "安全边界：%s" % measured["cashflow"]["boundary"],
        "现金流算式：%s" % measured["cashflow"]["calc"],
        "阈值一（%s）：%s" % (th[0]["name"], th[0]["value"]),
        "阈值二（%s）：%s" % (th[1]["name"], th[1]["value"]),
    ])
    if insufficient:
        prompt_text += ("\n数据缺口：财务四项缺失两项及以上，以上量化基于假设值，"
                        "只能作为方向性参考，不得据此给出强判定。")
    return {
        "months": months,
        "no_burn": bool(no_burn),
        "insufficient": bool(insufficient),
        "boundary": measured["cashflow"]["boundary"],
        "thresholds": th,
        "thresholds_text": thresholds_text,
        "prompt_text": prompt_text,
    }


def align_months_text(text, months):
    """把文本中“可支撑约 N 个月”的 N 纠偏为代码口径，已一致的原样保留。"""
    def repl(match):
        value = float(match.group(3))
        if abs(value - months) < 0.05:
            return match.group(0)
        return "%s%s%.1f 个月" % (match.group(1), match.group(2), months)

    return MONTHS_CLAIM.sub(repl, str(text))


def align_report(value, months):
    """递归纠偏最终报告里的可支撑月数，保证报告与看板同口径。"""
    if isinstance(value, dict):
        return {key: align_report(item, months) for key, item in value.items()}
    if isinstance(value, list):
        return [align_report(item, months) for item in value]
    if isinstance(value, str):
        return align_months_text(value, months)
    return value
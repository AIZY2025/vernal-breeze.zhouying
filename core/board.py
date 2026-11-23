"""纯文本看板与最终报告的渲染。

渲染是确定性的：一至四段、六段由三方结构化输出直接拼，五、七段用 A5 给出的
争议焦点与结论。四行速览放在最上面，投屏一眼能看完。
"""
import re

from .numbers_cn import fmt_int, fmt_yuan
from .quant import coerce_amount, default_amount

LINE = "─" * 62
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
CITE = re.compile(r"\[外\s*(\d+)\]")
CONDITION_LABEL = "失效条件"

FINANCE_FIELDS = (
    ("monthly_income", "月收入"),
    ("cash", "可动用存款"),
    ("monthly_saving", "月结余"),
    ("max_one_time", "一次性支出上限"),
)


def _get(d, *path, default=""):
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def finance_line(archive: dict) -> str:
    fin = archive.get("finance") or {}
    parts = []
    for key, label in FINANCE_FIELDS:
        value = fin.get(key)
        if coerce_amount(value) is None:
            parts.append("%s 未提供（按假设值 %s）" % (label, fmt_yuan(default_amount(key))))
        else:
            parts.append("%s %s" % (label, fmt_yuan(value)))
    return " ｜ ".join(parts)


def default_brief(archive: dict, pro: dict, con: dict, neu: dict) -> list:
    args = pro.get("arguments") or []
    risks = con.get("risks") or []
    cf = neu.get("cashflow") or {}
    th = neu.get("thresholds") or []
    first_arg = args[0].get("claim", "") if args else "（正方未返回）"
    top_risk = max(risks, key=lambda r: {"高": 3, "中": 2, "低": 1}.get(str(r.get("level", "低")), 0)) if risks else {}
    risk_txt = "（反方未返回）"
    if top_risk:
        risk_txt = "%s（%s）" % (top_risk.get("desc", ""), top_risk.get("level", ""))
    boundary = cf.get("boundary", "")
    threshold = th[0].get("name", "") + " " + str(th[0].get("value", "")) if th else ""
    quant_line = "%s ｜ %s" % (boundary or "未测算", threshold)
    if cf.get("insufficient"):
        quant_line += "（财务缺口，按假设值估算）"
    return [
        "问题：" + _clip(archive.get("question", ""), 60),
        "正方：" + _clip(first_arg, 60),
        "反方：" + _clip(risk_txt, 60),
        "量化：" + quant_line,
    ]


def _clip(text, width=70):
    text = CONTROL.sub("", str(text or "")).replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def cite_usage(pro: dict, con: dict) -> dict:
    """统计三方正文里真实出现的 [外N] 编号，以及被哪一条论据/风险引用。"""
    usage = {}
    for index, arg in enumerate(((pro or {}).get("arguments") or [])[:3], 1):
        for value in _cite_ids(arg.get("claim"), arg.get("basis"), arg.get("premise")):
            usage.setdefault(value, []).append("正方论据%s" % "一二三"[index - 1])
    for index, risk in enumerate(((con or {}).get("risks") or [])[:3], 1):
        for value in _cite_ids(risk.get("desc"), risk.get("trigger"), risk.get("level_basis")):
            usage.setdefault(value, []).append("反方风险%s" % "一二三"[index - 1])
    return usage


def _cite_ids(*texts) -> list:
    ids = []
    for text in texts:
        for match in CITE.finditer(str(text or "")):
            value = int(match.group(1))
            if value not in ids:
                ids.append(value)
    return ids


def _intel_note(payload: dict) -> str:
    use = (payload or {}).get("intel_use") or {}
    if not isinstance(use, dict):
        return ""
    return str(use.get("skipped") or "").strip()


NUMBER = re.compile(r"\d+(?:\.\d+)?")
HAN = re.compile(r"[\u4e00-\u9fa5]")


def _numbers(text) -> set:
    values = set()
    for match in NUMBER.finditer(str(text or "")):
        try:
            values.add(round(float(match.group(0)), 2))
        except ValueError:
            continue
    return values


def _grams(text) -> set:
    body = "".join(HAN.findall(str(text or "")))
    return {body[index:index + 2] for index in range(len(body) - 1)}


def _item_reference(item: dict):
    facts = (item or {}).get("facts") or {}
    numbers = set()
    for entry in (facts.get("amounts") or []) + (facts.get("rates") or []):
        try:
            numbers.add(round(float(entry.get("value")), 2))
        except (TypeError, ValueError):
            continue
    keywords = set(str(word) for word in (facts.get("keywords") or []) if str(word).strip())
    return numbers, keywords, _grams(" ".join(
        str((item or {}).get(key) or "") for key in ("title", "excerpt", "content")))


def _strong_cite(item: dict, text: str) -> bool:
    """编号必须落在条目里的数字或实体上，才算是实质引用。"""
    numbers, keywords, grams = _item_reference(item)
    body = str(text or "")
    if numbers & _numbers(body):
        return True
    if any(word in body for word in keywords):
        return True
    return len(grams & _grams(body)) >= 2


def cite_audit(pro: dict, con: dict, archive: dict) -> dict:
    """按 [外N] 统计引用位置，并标记是否命中条目中的数字/实体。"""
    items = {}
    for item in ((archive or {}).get("external") or {}).get("items") or []:
        try:
            items[int(item.get("id") or 0)] = item
        except (TypeError, ValueError):
            continue
    usage = {}

    def add(values, who, text):
        for value in values:
            entry = usage.setdefault(value, {"who": [], "strong": True})
            entry["who"].append(who)
            item = items.get(value)
            if item and not _strong_cite(item, text):
                entry["strong"] = False

    for index, arg in enumerate(((pro or {}).get("arguments") or [])[:3], 1):
        text = " ".join(str(arg.get(key) or "")
                        for key in ("claim", "basis", "premise"))
        add(_cite_ids(arg.get("claim"), arg.get("basis"), arg.get("premise")),
            "正方论据%s" % "一二三"[index - 1], text)
    for index, risk in enumerate(((con or {}).get("risks") or [])[:3], 1):
        text = " ".join(str(risk.get(key) or "")
                        for key in ("desc", "trigger", "level_basis"))
        add(_cite_ids(risk.get("desc"), risk.get("trigger"), risk.get("level_basis")),
            "反方风险%s" % "一二三"[index - 1], text)
    return usage


def _external_value(entry: dict) -> str:
    try:
        value = float(entry.get("value"))
    except (TypeError, ValueError):
        return str(entry.get("value") or "")
    if str(entry.get("unit") or "") == "%":
        return "%.1f%%" % value
    if value >= 10000:
        return "%.1f万元" % (value / 10000.0)
    return "%s元" % fmt_int(value)


def external_usage_text(archive: dict, pro: dict, con: dict, detail: bool = True) -> str:
    """外部情报是否被真正使用：谁引用、引用了哪条、没有引用的原因。"""
    ext = (archive or {}).get("external") or {}
    items = ext.get("items") or []
    if not items:
        return ""
    audit = cite_audit(pro, con, archive)
    lines = []
    for request in (ext.get("requests") or [])[:6]:
        lines.append("请求 %s：%s ｜ 命中 %d 条%s" % (
            _clip(request.get("agent") or "-", 4),
            _clip(request.get("query", ""), 34),
            int(request.get("hits") or 0),
            ("（%s）" % _clip(request.get("reason"), 30)) if request.get("reason") else ""))
    cited = sorted(audit)
    if cited:
        lines.append("引用：" + "；".join(
            "[外%d] → %s%s" % (
                value, "、".join(audit[value]["who"]),
                "" if audit[value]["strong"] else "（弱引用：未命中条目数字/实体）")
            for value in cited))
    else:
        lines.append("引用：无（三方均未把检索结果作为论据依据）")
    unused = [int(item.get("id") or 0) for item in items
              if int(item.get("id") or 0) not in audit]
    if unused:
        lines.append("未引用：" + "".join("[外%d]" % value for value in unused))
    if detail:
        for item in items[:5]:
            item_id = int(item.get("id") or 0)
            mark = (" ← " + "、".join(audit[item_id]["who"])) if item_id in audit else ""
            origin = str(item.get("origin") or "").strip()
            source = (" ｜ 发起 %s" % _clip(origin, 4)) if origin else ""
            lines.append("[外%s] %s ｜ %s ｜ %s%s%s" % (
                item.get("id", "-"), _clip(item.get("title", ""), 44),
                item.get("site") or "来源", item.get("date", ""), source, mark))
            facts = item.get("facts") or {}
            parts = [str(entry.get("raw") or "") for entry in
                     (facts.get("amounts") or [])[:2] + (facts.get("rates") or [])[:2]]
            parts.extend(str(word) for word in (facts.get("keywords") or [])[:2])
            if parts:
                lines.append("    要点：%s" % _clip("、".join(part for part in parts if part), 120))
            status = str(item.get("page_status") or "").strip()
            if status:
                lines.append("    正文：%s" % _clip(status, 60))
            if item_id in audit and item.get("excerpt"):
                lines.append("    摘要：%s" % _clip(item.get("excerpt"), 120))
    for side_label, payload in (("正方", pro), ("反方", con)):
        note = _intel_note(payload)
        if note:
            lines.append("%s说明：%s" % (side_label, _clip(note, 160)))
    return "\n".join(lines)


def _verdict_of(conclusion: dict):
    conclusion = conclusion or {}
    verdict = str(conclusion.get("verdict") or conclusion.get("lean") or "未给出").strip()
    reason = str(conclusion.get("reason") or "").strip()
    conditions = conclusion.get("conditions") or []
    return verdict, reason, conditions


def cross_note(pro: dict, con: dict) -> str:
    """看板用：显示交叉质询轮次、定向回应与收敛状态。"""
    lines = []
    triggered = False
    converged = None
    for label, payload in (("正方", pro), ("反方", con)):
        state = (payload or {}).get("cross_state") or {}
        if state.get("triggered"):
            triggered = True
        if state.get("converged") is not None:
            converged = bool(state.get("converged"))
        for round_data in (payload or {}).get("cross_rounds") or []:
            if round_data.get("source") != "respond":
                continue
            for item in round_data.get("responses") or []:
                lines.append("%s%s「%s」" % (
                    label, _clip(item.get("verdict") or "-", 4),
                    _clip(item.get("target") or "-", 12)))
    if not triggered:
        return "交叉质询：1 轮基础点评（无追加回应）"
    text = "交叉质询：第 1 轮基础点评 → 第 2 轮定向回应"
    if lines:
        text += "（" + "；".join(lines[:4]) + "）"
    text += " ｜ 收敛：" + ("是" if converged else "否")
    return text


def render_board(*, version, archive, pro, con, neu, brief=None, dispute=None,
                 conclusion=None, drift=None, syncs=None, log=None, intent_note="",
                 stale_sides=False) -> str:
    brief = brief or default_brief(archive, pro, con, neu)
    dispute = dispute or []
    conclusion = conclusion or {}
    verdict, verdict_reason, conditions = _verdict_of(conclusion)
    out = []
    out.append(LINE)
    out.append("《沙盘看板》V%d" % version)
    out.append(LINE)
    out.append(_clip("【仲裁判定】%s%s" % (
        verdict, (" ｜ " + verdict_reason) if verdict_reason else ""), 130))
    out.append("")
    out.append("【速览四行】")
    for i, line in enumerate(brief[:4], 1):
        out.append("%d  %s" % (i, _clip(line, 62)))
    out.append("")

    out.append("一、决策问题与背景摘要")
    out.append("1. 问题：%s（%s）" % (_clip(archive.get("question", ""), 60),
                                   _clip(archive.get("type", ""), 20)))
    out.append("2. 时间窗口：%s ｜ 信息完整度：%s" % (_clip(archive.get("window", "未提供"), 30),
                                                 _clip(archive.get("completeness", "未标注"), 20)))
    out.append("3. 财务：%s" % finance_line(archive))
    ext = archive.get("external") or {}
    ext_items = ext.get("items") or []
    if ext_items:
        out.append("4. 外部情报：%d 条（%s ｜ 抓取于 %s）" % (
            len(ext_items), ext.get("provider", ""), ext.get("retrieved_at", "")))
        for line in external_usage_text(archive, pro, con).splitlines():
            out.append("   " + line)
    elif ext:
        out.append("4. 外部情报：未获取 ｜ %s" % (ext.get("reason") or "未知原因"))
    out.append("")

    out.append("二、正方核心论据" + ("（数字为参数变更前口径）" if stale_sides else ""))
    for i, arg in enumerate((pro.get("arguments") or [])[:3], 1):
        out.append("%d. [%s] %s" % (i, arg.get("dimension", "-"), _clip(arg.get("claim", "")))
        )
        out.append("   依据：%s ｜ 前提：%s" % (_clip(arg.get("basis", ""), 46), _clip(arg.get("premise", ""), 40)))
    if not pro.get("arguments"):
        out.append("（正方未返回）")
    out.append("")

    out.append("三、反方核心风险" + ("（数字为参数变更前口径）" if stale_sides else ""))
    for i, risk in enumerate((con.get("risks") or [])[:3], 1):
        out.append("%d. [%s·%s] %s" % (i, risk.get("dimension", "-"), risk.get("level", "-"),
                                     _clip(risk.get("desc", "")))
        )
        out.append("   触发：%s ｜ 依据：%s" % (_clip(risk.get("trigger", ""), 46), _clip(risk.get("level_basis", ""), 40)))
    if not con.get("risks"):
        out.append("（反方未返回）")
    out.append("")

    out.append("四、量化关键结论")
    cf = neu.get("cashflow") or {}
    if cf.get("no_burn"):
        out.append("安全边界：%s ｜ 月消耗为 0，可支撑月数不适用" % cf.get("boundary", "未测算"))
    else:
        out.append("安全边界：%s ｜ 支出后可支撑 %s 个月" % (
            cf.get("boundary", "未测算"), cf.get("months", "-")))
    if cf.get("calc"):
        out.append("现金流算式：" + _clip(cf.get("calc"), 70))
    if cf.get("insufficient"):
        out.append("数据说明：财务四项缺失两项及以上，以上数字按假设值估算，仅作方向参考")
    for i, th in enumerate((neu.get("thresholds") or [])[:2], 1):
        out.append("阈值%s：%s = %s" % ("一二"[i - 1], th.get("name", ""), th.get("value", "")))
    if not neu.get("thresholds"):
        out.append("（量化未给出阈值）")
    external_use = (neu.get("external_use") or {}).get("cited") or []
    if external_use:
        out.append("外部参数校准：" + "；".join(
            "[外%s] %s %s%s" % (
                entry.get("id"), _clip(entry.get("key") or "外部参数", 12),
                _external_value(entry),
                ("（" + _clip(entry.get("how"), 30) + "）") if entry.get("how") else "")
            for entry in external_use[:3]))
    out.append("")

    out.append("五、争议焦点")
    for i, item in enumerate(dispute[:2], 1):
        out.append("%d. %s（根源：%s）— %s" % (i, _clip(item.get("point", ""), 40),
                                            item.get("root", "未判定"), _clip(item.get("detail", ""), 50)))
    if not dispute:
        out.append("（A5 未给出争议焦点）")
    out.append(cross_note(pro, con))
    out.append("")

    out.append("六、可执行交互项")
    out.append("A 质疑正方：针对某条论据提出反驳或补充")
    out.append("B 质疑反方：针对某条风险提出反驳或补充")
    out.append("C 修改参数：调整收入、存款、结余、支出上限等数字")
    out.append("D 补充背景：追加此前未提及的事实或约束")
    out.append("E 结束沙盘：进入收尾，输出最终报告")
    out.append("")

    out.append("七、仲裁判定")
    out.append("判定：%s" % verdict)
    if verdict_reason:
        out.append("理由：%s" % _clip(verdict_reason, 90))
    for cond in conditions[:3]:
        out.append("  %s：%s" % (CONDITION_LABEL, _clip(cond, 60)))
    out.append("")

    if drift:
        out.append("【观点漂移日志】")
        for item in drift[-6:]:
            name = item.get("item") or ""
            out.append("V%s ｜ %s ｜ %s%s → %s ｜ 依据：%s" % (
                item.get("version", "-"), item.get("side", "-"),
                (name + "：") if name else "",
                _clip(item.get("from", ""), 16), _clip(item.get("to", ""), 16),
                _clip(item.get("basis", ""), 28)))
        out.append("")

    if syncs:
        out.append("【口径同步记录】")
        for item in syncs[-4:]:
            out.append("V%s ｜ %s→%s ｜ 摘要（%s字）：%s ｜ 对手方回应：%s" % (
                item.get("version", "-"), item.get("from", "-"), item.get("to", "-"),
                item.get("length", "-"), item.get("summary", ""), item.get("reply", "")))
        out.append("")

    if log:
        out.append("【本版变更】")
        for line in log[-4:]:
            out.append("· " + line)
        if intent_note:
            out.append("· 路由：" + intent_note)
    out.append(LINE)
    return "\n".join(out)


def render_archive(archive: dict) -> str:
    fin = archive.get("finance") or {}
    options = archive.get("options") or []
    opt_text = "；".join("%s %s" % (o.get("id", ""), o.get("name", "")) for o in options) or "未提供"
    needs = archive.get("needs") or []
    facts = archive.get("facts") or []
    unknowns = archive.get("unknowns") or []
    out = [
        LINE,
        "《决策背景档案》",
        LINE,
        "决策问题：%s" % _clip(archive.get("question", ""), 60),
        "决策类型：%s" % _clip(archive.get("type", ""), 20),
        "时间窗口：%s" % _clip(archive.get("window", ""), 30),
        "可选方案：%s" % opt_text,
        "财务约束：%s" % finance_line(archive),
        "核心诉求：%s" % "；".join(_clip(str(n), 30) for n in needs),
        "风险容忍度：%s" % _clip(archive.get("risk_tolerance", ""), 60),
        "已确认事实：%d 条" % len(facts),
    ]
    for fact in facts[:6]:
        out.append("  · " + _clip(fact, 62))
    external = archive.get("external") or {}
    items = external.get("items") or []
    if external:
        if items:
            out.append("外部情报：%d 条 ｜ %s ｜ 抓取于 %s" % (
                len(items), external.get("provider", ""), external.get("retrieved_at", "")))
            for item in items[:5]:
                origin = str(item.get("origin") or "").strip()
                out.append("  [外%d] %s ｜ %s ｜ %s%s" % (
                    item.get("id", "-"), _clip(item.get("title", ""), 44),
                    item.get("site") or "来源", item.get("date", ""),
                    (" ｜ 发起 " + _clip(origin, 4)) if origin else ""))
                facts = item.get("facts") or {}
                bits = [str(entry.get("raw") or "") for entry in
                        (facts.get("amounts") or [])[:2] + (facts.get("rates") or [])[:2]]
                bits.extend(str(word) for word in (facts.get("keywords") or [])[:2])
                if bits:
                    out.append("        要点：%s" % _clip(
                        "、".join(part for part in bits if part), 100))
                status = str(item.get("page_status") or "").strip()
                if status:
                    out.append("        正文：%s" % _clip(status, 60))
                out.append("        %s" % item.get("url", ""))
        else:
            out.append("外部情报：未获取（%s）" % (external.get("reason") or "未知原因"))
    out.append("待确认变量：%s" % ("；".join(str(u) for u in unknowns) if unknowns else "无"))
    out.append("信息完整度：%s%s" % (archive.get("completeness", ""),
                                ("（缺口：%s）" % archive.get("gap")) if archive.get("gap") and archive.get("gap") != "无" else ""))
    out.append("_BACKGROUND_READY=true")
    out.append(LINE)
    return "\n".join(out)


def render_report(report: dict, meta: dict) -> str:
    verdict = str(report.get("verdict") or "未给出").strip()
    verdict_reason = str(report.get("reason") or "").strip()
    out = [LINE, "《最终沙盘报告》%s" % _clip(meta.get("title", ""), 80), LINE]
    out.append(_clip("【仲裁判定】%s%s" % (
        verdict, (" ｜ " + verdict_reason) if verdict_reason else ""), 130))
    out.append("")
    summary = report.get("summary") or []
    out.append("一、决策摘要")
    for line in summary[:3]:
        out.append("· " + _clip(line, 66))
    out.append("")

    out.append("二、正反观点对比")
    for i, row in enumerate((report.get("compare") or [])[:3], 1):
        out.append("%d. 正方：%s" % (i, _clip(row.get("pro", ""), 50)))
        out.append("   反方：%s" % _clip(row.get("con", ""), 50))
        out.append("   分歧：%s" % _clip(row.get("gap", ""), 50))
    out.append("")

    out.append("三、量化阈值与安全边界")
    out.append(str(report.get("thresholds", "未提供"))[:200])
    out.append("")

    out.append("四、两套行动方案")
    for plan in (report.get("plans") or [])[:2]:
        out.append("· %s" % plan.get("name", ""))
        out.append("   适用条件：%s" % _clip(plan.get("condition", ""), 54))
        out.append("   代价：%s" % _clip(plan.get("cost", ""), 54))
    out.append("")

    out.append("五、方案切换条件")
    for item in (report.get("switch") or [])[:4]:
        out.append("· 当%s 时 → %s" % (_clip(item.get("indicator", ""), 40), _clip(item.get("action", ""), 28)))
    out.append("")

    out.append("六、风险兜底建议")
    for item in (report.get("fallback") or [])[:4]:
        out.append("· " + _clip(item, 66))
    out.append("")
    external_note = str(meta.get("external_note") or "").strip()
    if external_note:
        out.append("附、外部情报使用")
        out.extend(str(external_note).splitlines())
        out.append("")
    out.append("_SESSION=CLOSED")
    out.append(LINE)
    return "\n".join(out)

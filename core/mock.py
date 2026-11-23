"""预置剧本版五人团队：不调用任何模型，全部本地确定性生成。

用途有三个：没配 Key 时把链路跑通、现场断网兜底、彩排录屏。
量化部分是真实算术（不是写死的数字），改参数会真的重算。
"""
import copy

from . import quant
from .numbers_cn import fmt_int, fmt_yuan, parse_amount
from .router import _extract_params



# ---------------------------------------------------------------- 量化算术
def compute(archive: dict, monthly_delta: float = None) -> dict:
    """算术全部来自 core.quant，与真实模型共用同一套口径。"""
    measured = quant.measure(archive, monthly_delta)
    result = {
        "side": "NEU",
        "round": 1,
        "cashflow": measured["cashflow"],
        "opportunity": measured["opportunity"],
        "thresholds": measured["thresholds"],
        "sensitivity": measured["sensitivity"],
        "params": {"changed": []},
        "notes": measured["notes"],
    }
    items = ((archive or {}).get("external") or {}).get("items") or []
    cited = []
    for item in items[:1]:
        facts = (item or {}).get("facts") or {}
        fin = archive.get("finance") or {}
        cap = quant.coerce_amount(fin.get("max_one_time")) or quant.coerce_amount(fin.get("cash")) or 0
        rows = []
        for row in facts.get("amounts") or []:
            try:
                value = float(row.get("value"))
            except (TypeError, ValueError):
                continue
            if value <= 0 or (cap and value > cap * 2):
                continue
            rows.append((row, value))
        entry = next(((row, value) for row, value in rows if any(
            hint in str(row.get("text") or "") for hint in quant.COST_HINTS)),
            rows[0] if rows else None)
        if not entry:
            continue
        value = entry[1]
        cited.append({"id": item.get("id"), "value": value, "unit": "元",
                      "key": "外部金额口径", "how": "用于校准预算与月供区间"})
        if cited:
            break
    if cited:
        result["external_use"] = {"cited": cited}
    return result


MOCK_QUESTIONS = [
    "1. 你打算在多久之内定下来？有没有硬性截止时间？",
    "2. 目前月收入、可动用存款、每月固定结余、能接受的最大一次性支出，大概各是多少？",
    "3. 除了当前倾向的方案，还有哪些备选（比如继续打车、租车、暂缓）？最想解决的问题和最坏能接受的结果是什么？",
]


# ---------------------------------------------------------------- 团队
class MockTeam:
    """接口与 LLMTeam 完全一致，编排层无需区分。"""

    def __init__(self, *args, **kwargs):
        self.demo_answers = []
        self.round = 1

    def stats(self):
        return {"calls": 0, "seconds": 0.0}

    # ---- 入口接待
    def intent_probe(self, text):
        """预置剧本不猜语义，交给 core.intent 的规则与默认放行。"""
        return {}

    # ---- A1
    def a1_questions(self, query, history_text, asked_total):
        if asked_total >= len(MOCK_QUESTIONS):
            return {"kind": "archive", "background_ready": True}
        return {
            "kind": "questions",
            "understanding": ("你的问题是：%s，需要在几个关键前提上先对齐口径。" % query)
            if asked_total == 0 else "",
            "questions": [MOCK_QUESTIONS[asked_total]],
            "missing": [["时间窗口"], ["财务约束"], ["核心诉求"]][asked_total],
        }

    def a1_archive(self, query, history_text, asked_total, new_facts="", prev_archive=None):
        if prev_archive:
            archive = copy.deepcopy(prev_archive)
            if new_facts:
                archive.setdefault("facts", []).append(new_facts)
                params = _extract_params(new_facts)
                if params:
                    archive.setdefault("finance", {}).update(params)
                archive["unknowns"] = []
                archive["completeness"] = "充足"
                archive["gap"] = "无"
            return {"kind": "archive", "background_ready": True, "archive": archive,
                    "research_requests": self._mock_requests(archive)}
        archive = build_archive(query, history_text)
        return {"kind": "archive", "background_ready": True, "archive": archive,
                "research_requests": self._mock_requests(archive)}

    @staticmethod
    def _mock_requests(archive):
        """预置剧本也走“Agent 提请求”的路径，供演示与回归。"""
        question = str(archive.get("question") or "")
        if "车" in question:
            return [
                {"query": "20万 家用车 2026 补贴 政策", "purpose": "补全地域政策与补贴",
                 "expect": "补贴金额、截止日期", "target": "unknowns"},
                {"query": "20万 家用车 年度 养车成本 保险 保养", "purpose": "核实持有成本",
                 "expect": "年度费用区间", "target": "finance"},
            ]
        return [
            {"query": "%s 2026 最新 政策 数据" % question[:16], "purpose": "补全外部事实",
             "expect": "价格、政策或行业数据", "target": "unknowns"},
        ]

    # ---- A2
    def judge_pro(self, archive, round_no=1):
        fin = archive.get("finance") or {}
        cash = fin.get("cash") or 0
        one_time = fin.get("max_one_time") or 0
        ratio = (one_time / cash * 100) if cash else 0
        arguments = [
            {"dimension": "效率",
             "claim": "一次性支出只占可动用存款的 %.1f%%，不改变资金安全垫" % ratio,
             "basis": "档案载明可动用存款 %s，一次性支出上限 %s" % (fmt_yuan(cash), fmt_yuan(one_time)),
             "premise": "月收入与月结余保持当前水平，无大额突发支出"},
            {"dimension": "长期价值",
             "claim": "通勤与周末家庭出行由不确定性变为可安排，长期时间成本下降",
             "basis": "核心诉求第 1 条为通勤，第 2 条为周末带父母出行",
             "premise": "用车频率达到每周三次以上，否则长期价值被持有成本抵消"},
            {"dimension": "机会窗口",
             "claim": "在既定时间窗口内完成决策，避免把决策成本拖成持续消耗",
             "basis": "档案时间窗口为%s" % archive.get("window", "既定窗口"),
             "premise": "窗口内价格与补贴政策不发生大幅变化"},
        ]
        cited = self._apply_intel(arguments[0], archive, 0, "basis")
        return {
            "side": "PRO", "round": round_no,
            "arguments": arguments,
            "limited": "无",
            "revision": None,
            "intel_use": self._intel_use(cited),
        }

    @staticmethod
    def _intel_items(archive):
        return ((archive or {}).get("external") or {}).get("items") or []

    @staticmethod
    def _cite_note(item):
        facts = (item or {}).get("facts") or {}
        bits = []
        for group in ("amounts", "rates"):
            for entry in (facts.get(group) or [])[:1]:
                bits.append(str(entry.get("raw") or ""))
        bits.extend(str(word) for word in (facts.get("keywords") or [])[:1])
        return "、".join(bit for bit in bits if bit)

    def _apply_intel(self, row, archive, index, field):
        items = self._intel_items(archive)
        if not items:
            return []
        item = items[index] if index < len(items) else items[0]
        try:
            item_id = int(item.get("id") or 1)
        except (TypeError, ValueError):
            item_id = 1
        note = self._cite_note(item)
        if note:
            row[field] = "%s；外部依据：%s [外%d]" % (row.get(field, ""), note, item_id)
        else:
            row[field] = "%s；参见 [外%d]" % (row.get(field, ""), item_id)
        return [{"id": item_id, "how": "用外部条目校准本条论据"}]

    @staticmethod
    def _intel_use(cited):
        if cited:
            return {"cited": cited, "skipped": "其余条目与本次决策无直接关系，未采用"}
        return {"cited": [], "skipped": "预置剧本未获取外部情报"}

    @staticmethod
    def _cited_ids(pro, con):
        ids = []
        for payload in (pro, con):
            for entry in ((payload or {}).get("intel_use") or {}).get("cited") or []:
                try:
                    value = int(entry.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if value and value not in ids:
                    ids.append(value)
        return ids

    # ---- A3
    def judge_con(self, archive, round_no=1):
        fin = archive.get("finance") or {}
        saving = fin.get("monthly_saving") or 0
        delta = 1650
        share = (delta / saving * 100) if saving else 0
        risks = [
            {"dimension": "现金流压力",
             "desc": "月度增量支出占月结余的 %.0f%%，结余缓冲被明显压缩" % share,
             "trigger": "月收入下降、或月结余低于 %s 元" % fmt_int(delta),
             "level": "中",
             "level_basis": "月结余 %s，月度增量 %s，尚有正向结余但缓冲变薄" % (
                 fmt_int(saving), fmt_int(delta))},
            {"dimension": "隐性持有成本",
             "desc": "保险、保养、停车与折旧构成每年约两万元的隐性成本",
             "trigger": "用车频率低于每周三次，或停车位需另行租用",
             "level": "中",
             "level_basis": "该成本不随使用频率同比下降，属于固定支出"},
            {"dimension": "机会成本",
             "desc": "二十万元资金被锁定在折旧资产上，放弃其理财收益与流动性",
             "trigger": "未来一年内出现需要大额现金的机会或风险",
             "level": "低",
             "level_basis": "存款规模足以覆盖该笔占用，且无近期大额支出计划"},
        ]
        cited = self._apply_intel(risks[1], archive, 1, "level_basis")
        return {
            "side": "CON", "round": round_no,
            "risks": risks,
            "revision": None,
            "intel_use": self._intel_use(cited),
        }

    # ---- 交叉质询
    def cross_pro(self, archive, own, opponent, round_no=1, draft=None):
        if draft:
            return draft
        risks = (opponent.get("risks") or [])[:3]
        verdicts = ("接受", "限定", "反驳")
        points = (
            "该风险在档案数字下成立，正方不否认其对结余缓冲的影响",
            "只有在触发条件同时出现时成立，当前档案未显示该条件已发生",
            "该风险的后果量级缺少档案依据，不宜按最高档处理",
        )
        return {"side": "PRO", "cross": [
            {"target": "反方风险%s" % "一二三"[i], "verdict": verdicts[i], "point": points[i],
             "basis": "档案：%s" % (risk.get("level_basis") or risk.get("trigger") or "已确认事实")}
            for i, risk in enumerate(risks)
        ], "evidence_requests": [
            {"query": "家用车 年度 养车成本 保险 保养",
             "purpose": "核实反方隐性成本量级", "target": "反方风险二"}]}

    def cross_con(self, archive, own, opponent, round_no=1, draft=None):
        if draft:
            return draft
        args = (opponent.get("arguments") or [])[:3]
        verdicts = ("限定", "反驳", "接受")
        points = (
            "该论据只在使用频率达到每周三次以上时成立，档案尚未确认该频率",
            "该论据把一次性支出占比低等同于风险低，忽略了月度现金流的持续性",
            "该论据引用的时间窗口确实存在，反方接受其对决策节奏的判断",
        )
        return {"side": "CON", "cross": [
            {"target": "正方论据%s" % "一二三"[i], "verdict": verdicts[i], "point": points[i],
             "basis": "档案：%s" % (arg.get("premise") or arg.get("basis") or "已确认事实")}
            for i, arg in enumerate(args)
        ], "evidence_requests": [
            {"query": "20万 家用车 停车费 保养 费用",
             "purpose": "核实正方低估的持有成本", "target": "正方论据一"}]}

    # ---- 第 2 轮定向回应
    def respond_pro(self, archive, own, opponent, own_cross, opponent_cross,
                    conflicts, round_no=2):
        premise = (own.get("arguments") or [{}])[0].get("premise") or "已确认事实"
        return {"side": "PRO", "round": round_no, "responses": [
            {"target": target, "verdict": "限定",
             "point": "接受对方对前提的质疑，维持论据方向，但收窄成立条件",
             "basis": "档案：%s" % premise}
            for target in (conflicts or [])]}

    def respond_con(self, archive, own, opponent, own_cross, opponent_cross,
                    conflicts, round_no=2):
        basis = (own.get("risks") or [{}])[0].get("level_basis") or "已确认事实"
        return {"side": "CON", "round": round_no, "responses": [
            {"target": target, "verdict": "维持",
             "point": "风险量级有档案数字支撑，维持当前等级并保留触发条件",
             "basis": "档案：%s" % basis}
            for target in (conflicts or [])]}

    # ---- A4
    def judge_neu(self, archive, round_no=1, hint=""):
        delta = 1650
        m = None
        if hint:
            amount = parse_amount(hint)
            if amount > 0:
                m = amount / 12 if "年" in hint or "每年" in hint else amount
        res = compute(archive, m)
        res["round"] = round_no
        return res

    def revise_pro(self, archive, prev, user_text, round_no):
        res = copy.deepcopy(prev)
        res["round"] = round_no
        amount = quant.cost_amount(user_text)
        revision = None
        for i, arg in enumerate(res.get("arguments", [])):
            if any(k in user_text for k in ("效率", "通勤", "时间", "论据")):
                old = arg.get("claim", "")
                new = old.split("，")[0] + "，但该论据在通勤距离缩短后权重下调，需重新校准"
                res["arguments"][i]["claim"] = new
                revision = {"item": arg.get("dimension", "论据"), "from": old, "to": new,
                            "basis": user_text}
                break
        res["revision"] = revision
        return res

    def revise_con(self, archive, prev, user_text, round_no):
        res = copy.deepcopy(prev)
        res["round"] = round_no
        revision = None
        annual = quant.cost_amount(user_text)
        if annual <= 0:
            annual = 20000
        delta = annual / 12
        saving = (archive.get("finance") or {}).get("monthly_saving") or 0
        share = (delta / saving * 100) if saving else 0
        for risk in res.get("risks", []):
            if risk.get("dimension") == "现金流压力":
                risk["desc"] = "月度增量支出 %s 元，占月结余的 %.0f%%" % (fmt_int(delta), share)
                risk["trigger"] = "月收入下降、或月结余低于 %s 元" % fmt_int(delta)
                risk["level_basis"] = "月结余 %s，月度增量 %s，占比 %.0f%%" % (
                    fmt_int(saving), fmt_int(delta), share)
                risk["level"] = "中" if share >= 30 else "低"
            if risk.get("dimension") == "隐性持有成本":
                old_level = risk.get("level", "中")
                risk["desc"] = "保险、保养、停车与折旧构成每年约 %s 的隐性成本" % fmt_yuan(annual)
                risk["level"] = "低"
                risk["level_basis"] = "按用户提供的年度成本 %s 重算，占月结余比例下降" % fmt_yuan(annual)
                revision = {"item": "隐性持有成本", "from": old_level,
                            "to": "低", "basis": user_text, "annual_cost": annual}
        res["revision"] = revision
        return res

    def recompute_neu(self, archive, prev, user_text, changed, round_no):
        delta = None
        if prev:
            delta = (prev.get("cashflow") or {}).get("monthly_delta")
        amount = quant.cost_amount(user_text)
        if amount > 0 and any(k in user_text for k in ("养车", "每年", "成本", "费用")):
            delta = amount / 12
        res = compute(archive, delta)
        res["round"] = round_no
        res["params"] = {"changed": changed}
        if prev:
            before = (prev.get("cashflow") or {}).get("months")
            after = res["cashflow"]["months"]
            if before and after:
                pct = (after - before) / before * 100
                res["cashflow"]["calc"] += "；修改前可支撑 %s 个月，修改后 %.1f 个月，变化 %+.1f%%" % (
                    before, after, pct)
        return res

    # ---- A5
    def arbitrate(self, archive, pro, con, neu, version, drift, syncs, intent_note):
        return self._board_payload(archive, pro, con, neu, version, drift, syncs)

    def _board_payload(self, archive, pro, con, neu, version, drift, syncs, closed=False):
        fin = archive.get("finance") or {}
        cash = fin.get("cash") or 0
        th = neu.get("thresholds") or []
        t1 = th[0].get("value", "") if th else ""
        first_arg = (pro.get("arguments") or [{}])[0].get("claim", "")
        risk = (con.get("risks") or [{}])[1] if len(con.get("risks") or []) > 1 else (con.get("risks") or [{}])[0]
        brief = [
            "问题：" + str(archive.get("question", ""))[:26],
            "正方：" + str(first_arg)[:28],
            "反方：" + ("%s（%s）" % (risk.get("desc", ""), risk.get("level", "")))[:28],
            "量化：%s ｜ %s" % ((neu.get("cashflow") or {}).get("boundary", ""), t1)[:32],
        ]
        dispute = [
            {"point": "隐性持有成本的口径", "root": "事实差异",
             "detail": "反方初版按每年两万元计，用户提供实测数据后下调，正方仍按旧口径对比的假设已消除"},
            {"point": "存款占用与家庭出行价值", "root": "权重差异",
             "detail": "正方按时间成本与出行质量加权，反方按资金效率加权，同一组数字得出不同偏向"},
        ]
        if version > 1:
            dispute[0]["detail"] = "本轮已完成口径同步，双方基于同一套成本数字继续讨论"
        boundary = (neu.get("cashflow") or {}).get("boundary", "")
        no_burn = bool((neu.get("cashflow") or {}).get("no_burn"))
        months_text = "不适用（月消耗为 0）" if no_burn else "%s 个月" % (
            (neu.get("cashflow") or {}).get("months", "-"))
        verdict = "暂缓" if boundary == "越界" else "执行主方案"
        reason = "量化口径：安全边界%s，支出后%s，可动用存款下限 %s" % (
            boundary or "未测算", months_text if no_burn else "可支撑 " + months_text,
            t1 or "待测算")
        cited = self._cited_ids(pro, con)
        if cited:
            reason += "；外部参考 " + "".join("[外%d]" % value for value in cited)
        conclusion = {
            "verdict": verdict,
            "reason": reason,
            "conditions": [
                "可动用存款跌破 %s，或月结余低于月度增量支出" % (t1 or "测算阈值"),
                "用车频率连续低于每周两次，隐性成本无法被实际使用摊薄",
                "未来十二个月内出现需要动用大额现金的支出或风险",
            ],
        }
        payload = {"version": version, "brief": brief, "dispute": dispute,
                   "conclusion": conclusion, "session_closed": False}
        if closed:
            payload["session_closed"] = True
            payload["report"] = self._report(archive, pro, con, neu, cash)
        return payload

    def _report(self, archive, pro, con, neu, cash):
        cf = neu.get("cashflow") or {}
        th = neu.get("thresholds") or []
        t1 = th[0].get("value", "") if th else ""
        t2 = th[1].get("value", "") if len(th) > 1 else ""
        boundary = cf.get("boundary", "未测算")
        no_burn = bool(cf.get("no_burn"))
        months_text = "不适用（月消耗为 0）" if no_burn else "%s 个月" % cf.get("months", "-")
        verdict = "暂缓" if boundary == "越界" else "执行主方案"
        reason = "安全边界%s，支出后%s；判定以阈值一 %s 为失效线" % (
            boundary, months_text if no_burn else "可支撑 " + months_text, t1 or "待测算")
        cited = self._cited_ids(pro, con)
        if cited:
            reason += "；外部参考 " + "".join("[外%d]" % value for value in cited)
        return {
            "verdict": verdict,
            "reason": reason,
            "summary": [
                "当前参数下偏向方案A，但偏向依赖三个条件同时成立。",
                "一次性支出占存款比例不高，安全边界未越界，主要风险在隐性持有成本与用车频率。",
                "若存款或月结余跌破阈值，应改选方案二暂缓。",
            ],
            "compare": [
                {"pro": (pro.get("arguments") or [{}])[0].get("claim", ""),
                 "con": (con.get("risks") or [{}])[0].get("desc", ""),
                 "gap": "分歧在月度增量支出的口径与对结余缓冲的容忍度"},
                {"pro": (pro.get("arguments") or [{}, {}])[-1].get("claim", ""),
                 "con": (con.get("risks") or [{}, {}, {}])[-1].get("desc", ""),
                 "gap": "同一笔资金，一方按时间价值加权，一方按资金效率加权"},
            ],
            "thresholds": quant.authority(archive, neu)["thresholds_text"],
            "plans": [
                {"name": "方案一 立即执行",
                 "condition": "存款 ≥ %s 且月结余 ≥ %s，用车频率每周三次以上" % (t1, t2),
                 "cost": "一次性支出 %s，月度增量支出 %s，放弃年度理财收益约 %s" % (
                     fmt_yuan((archive.get("finance") or {}).get("max_one_time")),
                     fmt_yuan(cf.get("monthly_delta")),
                     (neu.get("opportunity") or {}).get("range", ""))},
                {"name": "方案二 暂缓等待",
                 "condition": "存款或月结余跌破阈值，或未来一年存在大额现金需求",
                 "cost": "继续承担打车与租车的即时成本，决策窗口被压缩"},
            ],
            "switch": [
                {"indicator": "可动用存款跌破 %s" % t1, "action": "改选方案二暂缓"},
                {"indicator": "月结余连续两个月低于 %s" % t2, "action": "改选方案二，并重算现金流"},
                {"indicator": "用车频率低于每周两次", "action": "改选方案二，隐性成本无法被摊薄"},
            ],
            "fallback": [
                "若已购置且收入下滑：先压缩非必要用车，把年度成本拉回预算内",
                "若存款接近阈值：停止新增分期与订阅类支出，优先恢复六个月应急金",
                "若决策窗口关闭：接受方案二，并把决策条件写入下一次复盘的触发指标",
            ],
        }

    def final_report(self, archive, pro, con, neu, version, rounds_text):
        return self._board_payload(archive, pro, con, neu, version, [], [], closed=True)

    def sync_reply(self, side, own, summary):
        return "口径已对齐，无需调整。"


# ---------------------------------------------------------------- 档案构造
def build_archive(query: str, history_text: str) -> dict:
    """从问答文本里抠出财务数字，构造一份可推演的档案。"""
    answers = [line.strip()[2:] for line in (history_text or "").splitlines()
               if line.strip().startswith("答：")]
    params = _extract_params("\n".join(answers))
    window = ""
    for line in answers:
        if any(k in line for k in ("月", "周", "年", "春节", "季度", "之前", "截止")):
            window = line.strip()[:24]
            break
    return {
        "question": query[:30],
        "type": "消费支出",
        "window": window or "未明确（用户未提供硬性截止时间）",        "options": [
            {"id": "A", "name": "按当前倾向执行主方案", "is_main": True},
            {"id": "B", "name": "继续现有替代方式（如打车、租车）", "is_main": False},
            {"id": "C", "name": "暂缓决策，继续等待与观察", "is_main": False},
        ],
        "finance": {
            "monthly_income": params.get("monthly_income"),
            "cash": params.get("cash"),
            "monthly_saving": params.get("monthly_saving"),
            "max_one_time": params.get("max_one_time"),
        },
        "needs": ["1. 降低日常通勤的时间成本", "2. 周末家庭出行更可控", "3. 不显著削弱资金安全垫"],
        "risk_tolerance": "存款可小幅下降，但不接受跌破六个月应急金",
        "facts": answers[:6],
        "unknowns": [],
        "completeness": "充足" if params else "基本充足",
        "gap": "无" if params else "财务四项中至少一项未提供，量化将使用假设值",
    }

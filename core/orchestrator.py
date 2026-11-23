"""编排层：六阶段状态机 + 硬编码路由 + 跨立场口径同步。

这里刻意不把“这条指令是什么意思”交给模型判断：classify() 是纯规则，
模型只在被定向调用时做它该做的论证与汇总。
"""
import copy
import datetime
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import board as render
from . import intent
from . import research
from .jsonx import ParseError
from .numbers_cn import fmt_int, fmt_yuan
from .quant import COST_HINTS, align_months_text, authority, coerce_amount, cost_amount
from .router import classify, extract_params
from .search import archive_queries, archive_query, render_intel


HISTORY_CHARS = 12000
CROSS_SECOND_ROUND = True

RESEARCH_HINTS = (
    "价格", "政策", "补贴", "利率", "行情", "市场", "行业", "首付", "月供",
    "保险", "保养", "税", "优惠", "涨价", "降价", "趋势", "数据", "最新",
    "当地", "本地",
)

PARAM_LIMITS = {
    "monthly_income": (0.0, 1e7),
    "cash": (0.0, 1e9),
    "monthly_saving": (0.0, 1e7),
    "max_one_time": (0.0, 1e9),
    "monthly_delta": (0.0, 1e6),
}


def _coerce_number(key, value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    low, high = PARAM_LIMITS.get(key, (0.0, 1e9))
    if num < low or num > high:
        return None
    return int(num) if num.is_integer() else num


def _differs(before, after) -> bool:
    """数值口径是否真的变了；两侧都缺省（None/非数字）时视为未变。"""
    try:
        return float(before) != float(after)
    except (TypeError, ValueError):
        return before != after


def _delta_text(value, missing: str) -> str:
    try:
        return "%s 元/月" % fmt_int(float(value))
    except (TypeError, ValueError):
        return missing


def _history_text(lines, limit: int = HISTORY_CHARS) -> str:
    """A1 的问答历史按字数窗口截断，超长时保留最近记录并注明省略条数。"""
    items = [str(line) for line in (lines or []) if str(line).strip()]
    text = "\n".join(items)
    if len(text) <= limit:
        return text
    kept = []
    total = 0
    for line in reversed(items):
        if kept and total + len(line) + 1 > limit:
            break
        kept.append(line)
        total += len(line) + 1
    dropped = len(items) - len(kept)
    return ("（较早的 %d 行记录已省略，仅保留最近问答）\n" % dropped
            + "\n".join(reversed(kept)))


def _brief(text: str, limit: int = 18) -> str:
    """漂移日志里保留首尾，正文太长时中间省略，避免两条看起来一模一样。"""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head - 1
    return text[:head] + "…" + text[-tail:]


_QUESTION_PREFIXES = (
    re.compile(r"^第?\s*\d+\s*[.、)）:：]"),
    re.compile(r"^[（(]\s*\d+\s*[)）]"),
    re.compile(r"^[-*•·]"),
)
_QUESTION_QUOTES = "「」\"'“”"


def _normalize_question(text: str) -> str:
    """剥离模型自带的序号、项目符号与包裹引号：界面编号只由系统给出。"""
    if text is None:
        return ""
    raw = str(text).strip()
    out = raw
    while True:
        stripped = out.strip()
        for pattern in _QUESTION_PREFIXES:
            stripped = pattern.sub("", stripped, count=1)
        stripped = stripped.strip()
        if stripped == out:
            break
        out = stripped
    out = out.strip(_QUESTION_QUOTES).strip()
    return out or raw


def _question_key(text: str) -> str:
    """只用于判断 A1 是否在重复问同一题：去掉编号与句末标点。"""
    key = str(text).replace("\r", "").replace("\n", "").strip()
    for ch in ("？", "?", "！", "!", "。", "，", ",", "；", ";"):
        key = key.replace(ch, "")
    return key.lstrip("0123456789.、 )（）")


class SandboxError(Exception):
    pass


class SandboxCancelled(Exception):
    """用户主动停止推演：不生成最终报告，已产出内容照常落盘。"""


class Sandbox:
    def __init__(self, team, ui, store, demo: bool = False, research=None, cancel_event=None):
        from . import research as research_plan
        self.team = team
        self.ui = ui
        self.store = store
        self.demo = demo
        self.research = research
        self.cancel_event = cancel_event
        self.query = ""
        self.archive = {}
        self.pro = {}
        self.con = {}
        self.neu = {}
        self.version = 0
        self.round = 0
        self.conclusion = {}
        self.drift = []
        self.syncs = []
        self.routes = []
        self.asked_total = 0
        self.history_lines = []
        self.close_reason = ""
        self.intel = {}
        self.research_state = research_plan.new_state()
        self._pending_requests = []
        self._last_research_at = 0.0
        self.topic = ""
        self.refused = False
        self.refuse_reason = ""

    # ------------------------------------------------------------ 主流程
    def run(self, query: str):
        try:
            self._run(query)
        except ParseError as exc:
            raise SandboxError("模型输出不符合 JSON 契约，链路中止：%s" % exc)

    def run_paste(self, archive: dict, query: str = ""):
        """直接载入外部档案，跳过 A1 进入并行研判（CLI --paste / Web 粘档案）。"""
        if not isinstance(archive, dict) or not archive:
            raise SandboxError("载入的档案必须是 JSON 对象")
        self._check_cancel()
        self.archive = copy.deepcopy(archive)
        self.query = str(archive.get("question") or query or "")
        self.topic = str(archive.get("topic") or self.query)
        self.store.log("系统", "载入外部档案，跳过 A1", level="line")
        self.ui.info("已载入外部档案，跳过 A1 直接进入推演组（降级预案）")
        self.ui.show(render.render_archive(self.archive))
        self.store.save_text("背景档案.json",
                             json.dumps(self.archive, ensure_ascii=False, indent=2))
        self.ui.info("阶段 3/6 ｜ 三个智能体并行研判")
        self._judge_parallel(round_no=1)
        self._check_cancel()
        self.ui.info("阶段 3.5/6 ｜ 交叉质询：先一轮基础点评，出现未回应冲突时追加定向回应")
        self._cross_examine(round_no=1)
        self._check_cancel()
        self.ui.info("阶段 4/6 ｜ 仲裁汇总 Agent 生成第一版看板")
        self._refresh_board(log=["外部档案载入，三方并行研判"], intent_note="降级预案·直接推演")
        self.ui.info("阶段 5/6 ｜ 已开放介入入口，可反复质疑、改参数、补背景")
        self._check_cancel()
        self._interact()

    def _run(self, query: str):
        self.query = query
        self.store.log("你", query)
        self._check_cancel()

        gate = intent.judge(query, getattr(self.team, "intent_probe", None))
        self.store.log("入口接待", gate.describe(), level="line")
        if not gate.is_decision:
            self.refused = True
            self.refuse_reason = gate.reason
            self.ui.info("入口接待 ｜ %s ｜ 本场不进入推演" % gate.describe())
            self.store.save_json("入口判定.json", {
                "query": query, "is_decision": False, "source": gate.source,
                "reason": gate.reason, "topic": gate.topic,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            return
        self.ui.info("入口接待 ｜ %s" % gate.describe())
        self._check_cancel()

        topic = gate.topic or query
        self.topic = topic
        if self.research is not None and self.research.active:
            self.ui.info("阶段 1/6 ｜ 只唤起背景探询 Agent；档案确认后再按城市、金额与时间窗口检索情报")
        else:
            self.ui.info("阶段 1/6 ｜ 只唤起背景探询 Agent，其余四个待命")
        self.archive = self._probe(query)
        self._backfill_cost()
        self._check_cancel()
        if self.research is not None:
            self.ui.info("阶段 2/6 ｜ 按 A1 的检索请求取证，缺失或超时都不阻断链路")
            self._seed_research()
        self.ui.show(render.render_archive(self.archive))
        self.store.save_text("背景档案.json", json.dumps(self.archive, ensure_ascii=False, indent=2))

        self.ui.info("阶段 3/6 ｜ 三个智能体先并行独立研判")
        self._judge_parallel(round_no=1)
        self._check_cancel()
        self._enrich_a4(1)
        self._check_cancel()

        self.ui.info("阶段 3.5/6 ｜ 交叉质询：先一轮基础点评，出现未回应冲突时追加定向回应")
        self._cross_examine(round_no=1)
        self._check_cancel()

        self.ui.info("阶段 4/6 ｜ 仲裁汇总 Agent 生成第一版看板")
        self._refresh_board(log=["三方并行研判完成，生成本版看板"], intent_note="初始研判")

        self.ui.info("阶段 5/6 ｜ 已开放介入入口，可反复质疑、改参数、补背景")
        self._check_cancel()
        self._interact()

    # ------------------------------------------------------------ 阶段一：情报
    def _seed_research(self, requests=None, force: bool = False, extra: str = ""):
        """A1 建档（或补充事实）后，按它提出的检索请求统一取证。"""
        if self.research is None:
            return 0
        selected = list(requests if requests is not None else self._pending_requests)
        self._pending_requests = []
        if not selected:
            queries = archive_queries(self.archive, extra,
                                      kind=str(self.archive.get("type") or ""))
            selected = [{"agent": "A1", "query": query, "purpose": "自动补全档案",
                         "expect": "", "target": ""} for query in queries[:2]]
        gained = self._execute_requests(selected, force=force)
        research.ensure(self.archive)
        return gained

    def _execute_requests(self, requests, force: bool = False) -> int:
        """逐条执行检索请求：校验预算、去重、编号并入证据池。"""
        if self.research is None:
            return 0
        gained = 0
        for request in requests or []:
            ok, reason = research.check(self.research_state, request)
            if not ok:
                research.record_skip(self.archive, request, reason)
                self.ui.info("检索请求跳过 ｜ %s ｜ %s" % (request.get("query", ""), reason))
                continue
            research.accept(self.research_state, request)
            limit = min(int(getattr(self.research, "max_items", 5) or 5),
                        research.PER_REQUEST_ITEMS,
                        research.remaining_items(self.research_state))
            if limit <= 0:
                research.record_skip(self.archive, request, "正文抓取预算已用完")
                continue
            if hasattr(self.research, "search_queries"):
                intel = self.research.search_queries(
                    [request["query"]], extra=request.get("expect") or "",
                    force=force, max_items=limit, terms_text=request["query"])
            else:
                intel = self.research.collect(
                    request["query"], extra=request.get("expect") or "",
                    archive=self.archive)
            hits = research.merge(self.archive, intel, request)
            self.research_state["items"] += hits
            gained += hits
            self.store.log("情报侦察", "请求 %s ｜ %s ｜ %d 条" % (
                request.get("agent", "-"), request["query"], hits), level="line")
            if hits:
                self.intel = self.archive.get("external") or {}
                self.store.save_json("外部情报.json", self.intel)
                recent = (self.intel.get("items") or [])[-hits:]
                self.ui.agent("外部情报 · 元宝搜索",
                              "%s 取证 %d 条" % (request.get("agent", "-"), hits),
                              render_intel({"provider": self.intel.get("provider", ""),
                                            "retrieved_at": self.intel.get("retrieved_at", ""),
                                            "items": recent}))
            else:
                self.ui.info("外部情报未获取 ｜ %s ｜ %s" % (
                    request["query"], intel.get("reason") or "未知原因"))
        research.ensure(self.archive)
        return gained

    def _research(self, query: str, extra: str = "", kind: str = "",
                  force: bool = False) -> dict:
        """A1 的外部情报工具：拿不到就返回空包，绝不阻断推演。"""
        if self.research is None:
            return {}
        self._last_research_at = time.monotonic()
        intel = self.research.collect(query, extra, kind,
                                      archive=self.archive, force=force)
        self.store.log("情报侦察", render_intel(intel))
        if intel.get("items"):
            self.store.save_json("外部情报.json", intel)
            self.ui.agent("外部情报 · 元宝搜索", "已获取 %d 条" % len(intel["items"]),
                          render_intel(intel))
        else:
            self.ui.info("外部情报未获取 ｜ %s ｜ 链路继续" % (intel.get("reason") or "未知原因"))
        return intel

    def _needs_research(self, text: str) -> bool:
        """质疑里点到价格、政策、利率等外部变量时，才值得重新联网。"""
        if self.research is None:
            return False
        return any(word in str(text or "") for word in RESEARCH_HINTS)

    def _refresh_intel(self, extra: str = "", min_interval: float = 60.0) -> bool:
        """交互阶段按需刷新外部情报，60 秒内不重复联网。"""
        if self.research is None:
            return False
        now = time.monotonic()
        if self._last_research_at and now - self._last_research_at < min_interval:
            return False
        query = archive_query(self.archive, extra)
        intel = self._research(query, extra,
                               kind=str(self.archive.get("type") or ""), force=True)
        if not intel:
            return False
        request = {"agent": "USER", "query": query,
                   "purpose": "交互阶段按需刷新", "expect": "", "target": ""}
        hits = research.merge(self.archive, intel, request)
        self.intel = self.archive.get("external") or {}
        if hits:
            self.store.save_json("外部情报.json", self.intel)
            self.ui.info("外部情报已按本轮指令刷新 ｜ 新增 %d 条" % hits)
        return True

    # ------------------------------------------------------------ 阶段二：探询
    def _probe(self, query: str) -> dict:
        """一次只问一题：答案进入历史后再让 A1 决定下一题。

        不设题数上限，退出由 A1 返回 archive 或问题为空触发；
        问题正文只在 A1 卡片出现一次，提问通道关闭回显。
        连续重复同一题时提前归档，避免模型原地绕圈。
        """
        last_key = ""
        repeats = 0
        while True:
            self._check_cancel()
            res = self.team.a1_questions(query, _history_text(self.history_lines), self.asked_total)
            if res.get("kind") == "archive" and res.get("archive"):
                self._pending_requests = research.normalize("A1", res.get("research_requests"))
                return res["archive"]
            qs = [str(q) for q in (res.get("questions") or []) if str(q).strip()][:1]
            if not qs:
                return self._force_archive(query)

            raw_question = qs[0]
            question = _normalize_question(raw_question) or raw_question
            key = _question_key(_normalize_question(raw_question))
            repeats = repeats + 1 if key == last_key else 0
            if repeats >= 2:
                self.ui.info("A1 连续重复同一问题，提前归档")
                return self._force_archive(query)
            last_key = key

            if self.asked_total == 0 and res.get("understanding"):
                self.ui.agent("A1 背景探询", "理解", res["understanding"])
            self.ui.agent("A1 背景探询", "第 %d 问" % (self.asked_total + 1), question)
            self.store.log("A1 背景探询", question)

            answer = self.ui.ask(question, echo=False)
            self._check_cancel()
            if classify(answer).intent == "STOP":
                self.close_reason = "user_stop"
                raise SandboxCancelled("用户停止推演")
            self.store.log("你", answer, level="line")
            self.asked_total += 1
            self.history_lines.append("问：" + raw_question)
            self.history_lines.append("答：" + answer)


    def _force_archive(self, query: str) -> dict:
        self._check_cancel()
        res = self.team.a1_archive(query, _history_text(self.history_lines), self.asked_total)
        archive = res.get("archive")
        if not archive:
            raise SandboxError("A1 未返回《决策背景档案》，链路中止")
        self._pending_requests = research.normalize("A1", res.get("research_requests"))
        self.ui.info("信息收集结束 ｜ 累计提问 %d 个｜_BACKGROUND_READY=true" % self.asked_total)
        self.store.log("A1 背景探询", "_BACKGROUND_READY=true", level="line")
        return archive

    # ------------------------------------------------------------ 阶段三：并行
    def _judge_parallel(self, round_no: int):
        self._check_cancel()
        for key in ("A2", "A3", "A4"):
            self.ui.info("%s 已启动" % key)
        errors = []
        pool = ThreadPoolExecutor(max_workers=3)
        try:
            futures = {
                pool.submit(self.team.judge_pro, copy.deepcopy(self.archive), round_no): "A2",
                pool.submit(self.team.judge_con, copy.deepcopy(self.archive), round_no): "A3",
                pool.submit(self.team.judge_neu, copy.deepcopy(self.archive), round_no): "A4",
            }
            for future in as_completed(futures):
                self._check_cancel()
                key = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # 任一缺失不得生成看板
                    errors.append("%s 调用失败：%s" % (key, exc))
                    continue
                self._set_side(key, result)
                self.ui.agent(self._name(key), "研判完成", self._render_side(key, result))
                self.store.log(self._name(key), self._render_side(key, result))
        finally:
            self._shutdown_pool(pool)
        if errors:
            raise SandboxError("；".join(errors) + "；三方未齐备，按流转规则不得生成看板")
        for key in ("A2", "A3", "A4"):
            self.store.log(self._name(key), "_SIDE=" + {"A2": "PRO", "A3": "CON", "A4": "NEU"}[key],
                           level="line")

    def _enrich_a4(self, round_no: int):
        """A4 只在关键参数缺失时提补证请求；命中后二次测算，仍由代码口径覆盖。"""
        requests = research.normalize("A4", (self.neu or {}).get("evidence_requests"))
        if not requests or self.research is None:
            return
        gained = self._execute_requests(requests, force=True)
        if not gained:
            return
        self._check_cancel()
        self.neu = self.team.recompute_neu(
            copy.deepcopy(self.archive), copy.deepcopy(self.neu),
            "外部参数补证", [], round_no)
        self.ui.agent("A4 量化测算", "补证后重算", self._render_side("A4", self.neu))
        self.store.log("A4 量化测算", self._render_side("A4", self.neu))

    def _cross_examine(self, round_no: int):
        """两阶段交叉质询：先提证据请求，编排层统一取证后再定稿。

        双方都基于对方首轮输出并行评审，因此不会读到对方的质询内容，
        既保留独立性，又给 A5 提供真实的争议依据。质询失败降级为“未完成”，
        不阻断看板生成。
        """
        drafts, errors = {}, []
        requests = []
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            futures = {
                pool.submit(self.team.cross_pro, copy.deepcopy(self.archive),
                            copy.deepcopy(self.pro), copy.deepcopy(self.con), round_no): "A2",
                pool.submit(self.team.cross_con, copy.deepcopy(self.archive),
                            copy.deepcopy(self.con), copy.deepcopy(self.pro), round_no): "A3",
            }
            for future in as_completed(futures):
                self._check_cancel()
                key = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    errors.append("%s 交叉质询失败：%s" % (key, exc))
                    continue
                drafts[key] = result
                requests.extend(research.normalize(key, result.get("evidence_requests")))
        finally:
            self._shutdown_pool(pool)

        if requests and self.research is not None:
            gained = self._execute_requests(requests, force=True)
            if gained:
                self._check_cancel()
                pool = ThreadPoolExecutor(max_workers=2)
                try:
                    futures = {}
                    for key, result in drafts.items():
                        if key == "A2":
                            futures[pool.submit(
                                self.team.cross_pro, copy.deepcopy(self.archive),
                                copy.deepcopy(self.pro), copy.deepcopy(self.con),
                                round_no, draft=result)] = key
                        else:
                            futures[pool.submit(
                                self.team.cross_con, copy.deepcopy(self.archive),
                                copy.deepcopy(self.con), copy.deepcopy(self.pro),
                                round_no, draft=result)] = key
                    for future in as_completed(futures):
                        self._check_cancel()
                        key = futures[future]
                        try:
                            refined = future.result()
                        except Exception as exc:
                            errors.append("%s 质询定稿失败：%s" % (key, exc))
                            continue
                        if len(refined.get("cross") or []) == len(drafts[key].get("cross") or []):
                            drafts[key] = refined
                        else:
                            errors.append("%s 定稿改动了条目数，保留初稿" % key)
                finally:
                    self._shutdown_pool(pool)

        conflicts = self._find_conflicts(drafts)
        for key, result in drafts.items():
            target = self.pro if key == "A2" else self.con
            target["cross"] = result.get("cross") or []
            target["cross_requests"] = research.normalize(
                key, result.get("evidence_requests"))
            target["cross_rounds"] = [{
                "round": round_no,
                "source": "initial",
                "cross": target["cross"],
                "evidence_requests": target["cross_requests"],
            }]
            target["cross_state"] = {
                "triggered": False,
                "reasons": conflicts.get("reasons") or [],
                "converged": None,
                "conflicts": conflicts,
            }
        responses = {}
        if CROSS_SECOND_ROUND and (conflicts.get("pro") or conflicts.get("con")):
            responses = self._cross_respond(round_no, drafts, conflicts)
        triggered = bool(responses)
        verdicts = [str(item.get("verdict") or "")
                    for result in responses.values()
                    for item in result.get("responses") or []]
        converged = bool(verdicts) and all(v in ("接受", "限定") for v in verdicts)
        for key in drafts:
            target = self.pro if key == "A2" else self.con
            target["cross_state"].update({
                "triggered": triggered,
                "converged": converged if triggered else None,
            })
            text = self._render_cross(target["cross"])
            if key in responses:
                text += "\n" + self._render_respond(responses[key].get("responses"))
            self.ui.agent(self._name(key), "交叉质询", text)
            self.store.log(self._name(key), text)
        if errors:
            self.ui.info("交叉质询未完成 ｜ %s ｜ 看板继续生成" % "；".join(errors))
            self.store.log("交叉质询", "未完成：" + "；".join(errors), level="line")

    def _find_conflicts(self, drafts: dict) -> dict:
        """保守触发第 2 轮：每方最多一条，宁可少触发。

        R1：A2 接受反方风险，但 A3 风险等级未降级且未修订；
        R2：A3 反驳正方论据，但 A2 论据未修订。
        """
        result = {"pro": [], "con": [], "reasons": []}
        numerals = "一二三"
        pro = self.pro or {}
        con = self.con or {}
        pro_revision = str((pro.get("revision") or {}).get("item") or "")
        con_revision = str((con.get("revision") or {}).get("item") or "")

        for item in (drafts.get("A2") or {}).get("cross") or []:
            if item.get("verdict") != "接受":
                continue
            target = str(item.get("target") or "")
            if not target.startswith("反方风险"):
                continue
            index = numerals.find(target[-1:])
            risks = con.get("risks") or []
            if index < 0 or index >= len(risks):
                continue
            revised = target in con_revision or ("风险" + numerals[index]) in con_revision
            if revised or str(risks[index].get("level")) not in ("高", "中"):
                continue
            result["con"].append(target)
            result["reasons"].append("%s 已被正方接受，反方未回应是否降级" % target)
            break

        for item in (drafts.get("A3") or {}).get("cross") or []:
            if item.get("verdict") != "反驳":
                continue
            target = str(item.get("target") or "")
            if not target.startswith("正方论据"):
                continue
            index = numerals.find(target[-1:])
            args = pro.get("arguments") or []
            if index < 0 or index >= len(args):
                continue
            revised = target in pro_revision or ("论据" + numerals[index]) in pro_revision
            if revised:
                continue
            result["pro"].append(target)
            result["reasons"].append("%s 已被反方反驳，正方未回应是否修订" % target)
            break

        return result

    def _cross_respond(self, round_no: int, drafts: dict, conflicts: dict) -> dict:
        """第 2 轮定向回应：只调用被点名的一方，不检索、不新增条目。"""
        if not (hasattr(self.team, "respond_pro") and hasattr(self.team, "respond_con")):
            return {}
        responses, errors = {}, []
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            futures = {}
            if conflicts.get("pro") and "A2" in drafts:
                futures[pool.submit(
                    self.team.respond_pro, copy.deepcopy(self.archive),
                    copy.deepcopy(self.pro), copy.deepcopy(self.con),
                    copy.deepcopy(drafts["A2"]), copy.deepcopy(drafts.get("A3") or {}),
                    list(conflicts["pro"]), round_no + 1)] = "A2"
            if conflicts.get("con") and "A3" in drafts:
                futures[pool.submit(
                    self.team.respond_con, copy.deepcopy(self.archive),
                    copy.deepcopy(self.con), copy.deepcopy(self.pro),
                    copy.deepcopy(drafts["A3"]), copy.deepcopy(drafts.get("A2") or {}),
                    list(conflicts["con"]), round_no + 1)] = "A3"
            for future in as_completed(futures):
                self._check_cancel()
                key = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    errors.append("%s 第 2 轮回应失败：%s" % (key, exc))
                    continue
                responses[key] = result
        finally:
            self._shutdown_pool(pool)
        for key, result in responses.items():
            target = self.pro if key == "A2" else self.con
            target.setdefault("cross_rounds", []).append({
                "round": round_no + 1,
                "source": "respond",
                "responses": result.get("responses") or [],
            })
        if errors:
            self.ui.info("第 2 轮定向回应未完成 ｜ %s ｜ 保留第 1 轮" % "；".join(errors))
            self.store.log("定向回应", "未完成：" + "；".join(errors), level="line")
        return responses

    @staticmethod
    def _render_respond(responses: list) -> str:
        lines = ["《定向回应》"]
        for item in (responses or [])[:3]:
            lines.append("· %s ｜ %s：%s" % (
                item.get("target", "-"), item.get("verdict", "-"),
                item.get("point", "")))
            if item.get("basis"):
                lines.append("  依据：%s" % item.get("basis"))
        return "\n".join(lines)

    @staticmethod
    def _render_cross(cross: list) -> str:
        lines = ["《交叉质询》"]
        for item in (cross or [])[:3]:
            lines.append("· %s ｜ %s：%s" % (item.get("target", "-"), item.get("verdict", "-"),
                                            item.get("point", "")))
            if item.get("basis"):
                lines.append("  依据：%s" % item.get("basis"))
        return "\n".join(lines)

    # ------------------------------------------------------------ 阶段四：看板
    def _refresh_board(self, log: list, intent_note: str, stale_sides: bool = False):
        self._check_cancel()
        self.version += 1
        payload = self.team.arbitrate(
            copy.deepcopy(self.archive), copy.deepcopy(self.pro), copy.deepcopy(self.con),
            copy.deepcopy(self.neu), self.version, self.drift, self.syncs, intent_note)
        if payload.get("_cite_filled"):
            note = "A5 判定理由漏标外部引用，已按真实条目补注：%s" % "、".join(
                "[外%d]" % item for item in payload["_cite_filled"])
            self.ui.info(note)
            self.store.log("系统", note, level="line")
        self.conclusion = payload.get("conclusion") or {}
        months = authority(self.archive, self.neu)["months"]
        if months is not None:
            if self.conclusion.get("reason"):
                self.conclusion["reason"] = align_months_text(self.conclusion["reason"], months)
            if isinstance(payload.get("brief"), list):
                payload["brief"] = [align_months_text(line, months) for line in payload["brief"]]
        text = render.render_board(
            version=self.version,
            archive=self.archive,
            pro=self.pro,
            con=self.con,
            neu=self.neu,
            brief=payload.get("brief"),
            dispute=payload.get("dispute"),
            conclusion=payload.get("conclusion"),
            drift=self.drift,
            syncs=self.syncs,
            log=log,
            intent_note=intent_note,
            stale_sides=stale_sides,
        )
        self.ui.show(text)
        self.store.save_text("看板_V%d.txt" % self.version, text)
        self.store.log("A5 仲裁汇总", "《沙盘看板》V%d" % self.version, level="line")

    # ------------------------------------------------------------ 阶段五：交互
    def _interact(self):
        unknown_asked = False
        while True:
            self._check_cancel()
            raw = self.ui.ask("指令 >")
            raw = (raw or "").strip()
            if not raw:
                continue
            route = classify(raw)
            self.round += 1
            self.routes.append({"round": self.round, "raw": raw, "intent": route.intent,
                                "target": route.target, "params": route.params})
            self.store.log("你", raw, level="line")
            self.store.log("路由", route.describe(), level="line")
            self.ui.info("路由判定 ｜ " + route.describe())

            if route.intent == "STOP":
                self.close_reason = "user_stop"
                raise SandboxCancelled("用户停止推演")
            if route.intent == "CLOSE":
                self._final()
                return
            if route.intent == "CHALLENGE_PRO":
                self._handle_challenge("A2", raw)
                unknown_asked = False
                continue
            if route.intent == "CHALLENGE_CON":
                self._handle_challenge("A3", raw)
                unknown_asked = False
                continue
            if route.intent == "PARAM":
                if self._handle_param(raw, route.params):
                    unknown_asked = False
                continue
            if route.intent == "NEW_FACT":
                self._handle_new_fact(raw)
                unknown_asked = False
                continue

            if not unknown_asked:
                unknown_asked = True
                self.ui.info("这条指令没能归入交互项。可选：\n"
                             "A 质疑正方　B 质疑反方　C 修改参数（说清哪项、改成多少）　"
                             "D 补充背景　E 结束出结论　F 停止推演")
                continue
            self.ui.info("仍然无法归类。示例：B 反方说的养车成本每年两万太高了")
            unknown_asked = True

    def _handle_challenge(self, key: str, raw: str):
        self._check_cancel()
        if self._needs_research(raw):
            self._refresh_intel(raw, min_interval=0)
        self.ui.info("定向分发 → %s，仅该智能体重算，另一方与量化保持原结论" % key)
        prev = self.pro if key == "A2" else self.con
        if key == "A2":
            result = self.team.revise_pro(copy.deepcopy(self.archive), copy.deepcopy(prev), raw, self.round + 1)
            self.pro = result
        else:
            result = self.team.revise_con(copy.deepcopy(self.archive), copy.deepcopy(prev), raw, self.round + 1)
            self.con = result
        self.ui.agent(self._name(key), "重算完成", self._render_side(key, result))
        self.store.log(self._name(key), self._render_side(key, result))

        revision = result.get("revision")
        if revision and str(revision.get("from", "")).strip() == str(revision.get("to", "")).strip():
            # 等级/结论没有真变化（实测模型会回 from=中 to=中）：不记漂移、不发同步摘要
            revision = None
        logs = []
        if revision:
            side_label = "正方" if key == "A2" else "反方"
            self.drift.append({
                "version": self.version + 1,
                "side": side_label,
                "item": str(revision.get("item", "")),
                "from": _brief(str(revision.get("from", ""))),
                "to": _brief(str(revision.get("to", ""))),
                "basis": _brief(str(revision.get("basis", "")), 28),
            })
            logs.append("%s完成修订：%s → %s" % (side_label, revision.get("from", ""), revision.get("to", "")))
            self._sync(key, revision)

        # 修订若改动了量化用到的成本口径，A4 同步重算（文档演示脚本第 5 步）
        # 成本质疑即使 A2/A3 没给出实质修订，也要按用户原话重算一次
        cost_hint = any(k in raw for k in COST_HINTS) and cost_amount(raw) > 0
        numeric = bool(revision and revision.get("annual_cost")) or cost_hint
        if numeric:
            # 只从用户原话取成本口径；revision.basis 里的措辞可能带出别的数字
            hint = raw
            annual_cost = (revision or {}).get("annual_cost")
            if annual_cost:
                try:
                    annual_value = float(annual_cost)
                    hint = "养车成本每年 %d 元，折合 %d 元/月" % (
                        int(round(annual_value)), int(round(annual_value / 12.0)))
                except (TypeError, ValueError):
                    pass
            before_months = (self.neu.get("cashflow") or {}).get("months")
            before_delta = (self.neu.get("cashflow") or {}).get("monthly_delta")
            self.neu = self.team.recompute_neu(copy.deepcopy(self.archive), copy.deepcopy(self.neu),
                                               hint, [], self.round + 1)
            after_months = (self.neu.get("cashflow") or {}).get("months")
            after_delta = (self.neu.get("cashflow") or {}).get("monthly_delta")
            # 修订后的成本口径写回档案，后续全量重算沿用同一口径
            self._remember_delta(self.neu)
            self.ui.agent("A4 量化测算", "同步重算", self._render_side("A4", self.neu))
            self.store.log("A4 量化测算", self._render_side("A4", self.neu))
            if _differs(before_delta, after_delta):
                self.drift.append({"version": self.version + 1, "side": "量化",
                                   "item": "月度增量支出",
                                   "from": _delta_text(before_delta, "未记录"),
                                   "to": _delta_text(after_delta, "未记录"),
                                   "basis": "成本口径质疑后同步重算"})
                logs.append("量化随成本口径重算：月度增量 %s → %s"
                            % (_delta_text(before_delta, "未记录"), _delta_text(after_delta, "未记录")))
            if before_months and after_months and before_months != after_months:
                self.drift.append({"version": self.version + 1, "side": "量化",
                                   "item": "可支撑月数",
                                   "from": "%s个月" % before_months, "to": "%s个月" % after_months,
                                   "basis": "成本口径质疑后同步重算"})
                logs.append("量化随成本口径重算：%s → %s 个月" % (before_months, after_months))
            locked = (self.archive.get("finance") or {}).get("monthly_delta")
            if locked:
                logs.append("月度增量口径锁定为 %s 元/月，后续补充背景沿用该口径" % fmt_int(locked))

        if not revision:
            logs.append("%s认为原结论无需修订" % self._name(key))

        self._refresh_board(log=logs or ["本轮无实质修订"], intent_note="质疑定向重算")

    def _sync(self, revised_key: str, revision: dict):
        """只抄送不超过五十字的摘要，抄送一次，对手方只回一句。"""
        target = "A3" if revised_key == "A2" else "A2"
        side_label = "正方" if revised_key == "A2" else "反方"
        item = str(revision.get("item", "论据"))
        from_s = str(revision.get("from", ""))
        to_s = str(revision.get("to", ""))
        basis = str(revision.get("basis", ""))
        if len(from_s) <= 12 and len(to_s) <= 12:
            summary = "%s·%s：%s→%s（依据：%s）" % (side_label, item, from_s, to_s, basis)
        else:
            summary = "%s·%s已修订（依据：%s）" % (side_label, item, basis)
        summary = summary[:50]
        own = self.con if target == "A3" else self.pro
        reply = self.team.sync_reply("PRO" if target == "A2" else "CON", copy.deepcopy(own), summary)
        self.syncs.append({
            "version": self.version + 1,
            "from": side_label,
            "to": "正方" if target == "A2" else "反方",
            "summary": summary,
            "length": len(summary),
            "reply": reply,
        })
        self.store.log("A5 口径同步", "%s→%s：%s" % (side_label, "正方" if target == "A2" else "反方", summary),
                       level="line")
        self.ui.info("口径同步（%d字）→ %s ｜ 回应：%s" % (len(summary), target, reply))

    def _handle_param(self, raw: str, params: dict) -> bool:
        self._check_cancel()
        if not params:
            if not getattr(self, "_param_asked", False):
                self._param_asked = True
                self.ui.info("要改哪一项？可改：月收入 / 可动用存款 / 月结余 / 一次性支出上限，例如“存款改成150万”")
            else:
                self.ui.info("仍未识别到改哪一项，本轮不作变更。")
            return False
        self._param_asked = False
        cleaned = {}
        for key, value in params.items():
            num = _coerce_number(key, value)
            if num is None:
                self.ui.info("参数值不合法，本轮不作变更。数字应在 0 到 %s 之间。"
                             % format(int(PARAM_LIMITS.get(key, (0, 1e9))[1]), ","))
                return False
            cleaned[key] = num
        changed = []
        for key, value in cleaned.items():
            if key == "monthly_delta":
                before = (self.neu.get("cashflow") or {}).get("monthly_delta")
                self.archive.setdefault("finance", {})["monthly_delta"] = value
                changed.append({"key": "月度增量支出", "before": before, "after": value, "delta_pct": "-"})
                continue
            before = (self.archive.get("finance") or {}).get(key)
            self.archive.setdefault("finance", {})[key] = value
            pct = "-"
            if before:
                pct = "%+.1f%%" % ((value - before) / before * 100)
            changed.append({"key": {"cash": "可动用存款", "monthly_income": "月收入",
                                    "monthly_saving": "月结余", "max_one_time": "一次性支出上限"
                                    }.get(key, key),
                            "before": before, "after": value, "delta_pct": pct})
        self.ui.info("参数已更新 ｜ " + "；".join(
            "%s %s → %s" % (c["key"], fmt_yuan(c["before"]), fmt_yuan(c["after"])) for c in changed))
        if self.research is not None and any(
                c["key"] in ("可动用存款", "一次性支出上限", "月度增量支出") for c in changed):
            self._refresh_intel(raw, min_interval=30.0)
        self.neu = self.team.recompute_neu(copy.deepcopy(self.archive), copy.deepcopy(self.neu),
                                           raw, changed, self.round + 1)
        self.ui.agent("A4 量化测算", "重算完成（仅量化变更，两方结论保持不变）",
                      self._render_side("A4", self.neu))
        self.store.log("A4 量化测算", self._render_side("A4", self.neu))
        self.drift.append({"version": self.version + 1, "side": "量化",
                           "item": "参数",
                           "from": "原参数",
                           "to": "；".join("%s=%s" % (c["key"], fmt_yuan(c["after"])) for c in changed),
                           "basis": _brief(raw, 28)})
        self._refresh_board(log=["量化按新参数重算：" + "；".join(c["key"] for c in changed),
                                 "两方结论按规则 3 保持不变，其原文引用的数字仍为变更前口径"],
                            intent_note="参数变更仅重算", stale_sides=True)
        return True

    def _handle_new_fact(self, raw: str):
        self._check_cancel()
        self.ui.info("先更新档案，再三方全量重算（不得只让一方更新）")
        prev_archive = copy.deepcopy(self.archive)
        prev_months = (self.neu.get("cashflow") or {}).get("months")
        res = self.team.a1_archive(self.query, _history_text(self.history_lines), self.asked_total,
                                   new_facts=raw, prev_archive=copy.deepcopy(self.archive))
        archive = res.get("archive")
        if not archive:
            raise SandboxError("A1 未能更新档案，链路中止")
        # 用户已确认的财务口径（尤其质疑后修订的成本）不得被全量重算刷回默认值
        self._keep_confirmed_finance(prev_archive, archive)
        self.archive = archive
        self.history_lines.append("补充事实：" + raw)
        if self.research is not None:
            requests = research.normalize("A1", res.get("research_requests"))
            self._seed_research(requests, force=True, extra=raw)
        self.ui.agent("A1 背景探询", "档案已更新", render.render_archive(self.archive))
        self.store.log("A1 背景探询", render.render_archive(self.archive))
        self._judge_parallel(round_no=self.round + 1)
        self._enrich_a4(self.round + 1)
        self._cross_examine(round_no=self.round + 1)
        self._remember_delta(self.neu)
        notes = ["档案更新后三方全量重算", "新事实：" + raw[:40]]
        after_months = (self.neu.get("cashflow") or {}).get("months")
        if prev_months and after_months and prev_months != after_months:
            self.drift.append({"version": self.version + 1, "side": "量化",
                               "item": "可支撑月数",
                               "from": "%s个月" % prev_months, "to": "%s个月" % after_months,
                               "basis": "补充背景后全量重算"})
            notes.append("量化随背景更新：%s → %s 个月" % (prev_months, after_months))
        self._refresh_board(log=notes, intent_note="补充背景全量重算")

    def _remember_delta(self, neu: dict):
        """把当前权威月度增量写回档案，后续全量重算必须沿用同一口径。"""
        delta = (neu.get("cashflow") or {}).get("monthly_delta")
        try:
            value = float(delta)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        self.archive.setdefault("finance", {})["monthly_delta"] = (
            int(value) if value.is_integer() else round(value, 2))

    def _backfill_cost(self):
        """A1 建档漏写月度增量时，按用户原话补齐成本口径。"""
        if not isinstance(self.archive.get("finance"), dict):
            self.archive["finance"] = {}
        finance = self.archive["finance"]
        if coerce_amount(finance.get("monthly_delta")) is not None:
            return
        answers = [line[len("答："):] for line in self.history_lines
                   if str(line).startswith("答：")]
        for text in answers[::-1] + [self.query or ""]:
            delta = extract_params(text).get("monthly_delta")
            if delta and delta > 0:
                finance["monthly_delta"] = (int(delta) if float(delta).is_integer()
                                            else delta)
                self.store.log("系统", "档案补记月度增量 %s 元/月（A1 建档漏写）"
                               % fmt_int(delta), level="line")
                return

    @staticmethod
    def _keep_confirmed_finance(prev: dict, current: dict):
        """A1 全量重建档案时，回填它漏掉的已确认财务项，避免口径静默回退。"""
        old = (prev or {}).get("finance") or {}
        if not isinstance(current.get("finance"), dict):
            current["finance"] = {}
        new = current["finance"]
        for key in ("monthly_income", "cash", "monthly_saving", "max_one_time", "monthly_delta"):
            if coerce_amount(new.get(key)) is None and coerce_amount(old.get(key)) is not None:
                new[key] = old[key]

    # ------------------------------------------------------------ 阶段六：收尾
    def _final(self):
        self._check_cancel()
        self.ui.info("阶段 6/6 ｜ 结束推演，仲裁汇总 Agent 输出最终报告")
        result = self.team.final_report(copy.deepcopy(self.archive), copy.deepcopy(self.pro),
                                        copy.deepcopy(self.con), copy.deepcopy(self.neu),
                                        self.version, self._rounds_text())
        report = result.get("report")
        if not report:
            raise SandboxError("A5 未返回最终报告结构")
        if result.get("_cite_filled"):
            note = "收尾报告理由漏标外部引用，已按真实条目补注：%s" % "、".join(
                "[外%d]" % item for item in result["_cite_filled"])
            self.ui.info(note)
            self.store.log("系统", note, level="line")
        self._lock_report_verdict(report)
        meta = {
            "title": "｜ V%d ｜ %s" % (self.version, self.archive.get("question", "")),
            "external_note": render.external_usage_text(
                self.archive, self.pro, self.con, detail=False),
        }
        text = render.render_report(report, meta)
        self.ui.show(text, final=True)
        self.store.save_text("最终报告.txt", text)
        self.store.save_json("最终报告.json", report)
        self.store.save_json("复查清单.json", self._review_checklist(report))
        self.close_reason = "user_close"

    def _review_checklist(self, report: dict) -> dict:
        """把报告里的切换条件与兜底建议转成到期复查清单，供后续复盘。"""
        conditions = []
        for item in (report.get("switch") or []):
            if isinstance(item, dict):
                conditions.append("%s → %s" % (item.get("indicator", ""),
                                               item.get("action", "")))
            else:
                conditions.append(str(item))
        today = datetime.date.today()
        return {
            "version": "V%d" % self.version,
            "verdict": str(report.get("verdict") or ""),
            "reason": str(report.get("reason") or ""),
            "due_at": (today + datetime.timedelta(days=30)).isoformat(),
            "conditions": conditions,
            "fallback": [str(item) for item in (report.get("fallback") or [])],
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "reviewed_at": "",
        }

    def _lock_report_verdict(self, report: dict):
        """收尾报告不得静默改判：没有新增事实时，判定沿用最近一版看板。"""
        reason = str(report.get("reason") or "").strip()
        if reason.startswith(("改判：", "改判:")):
            return
        board_verdict = str((self.conclusion or {}).get("verdict") or "").strip()
        report_verdict = str(report.get("verdict") or "").strip()
        if not board_verdict:
            return
        if not report_verdict:
            report["verdict"] = board_verdict
            return
        if report_verdict == board_verdict:
            return
        old_reason = str(report.get("reason") or "").strip()
        report["verdict"] = board_verdict
        report["reason"] = ("沿用最近看板 V%d 判定，收尾未出现新事实，不静默改判；原报告理由：%s"
                            % (self.version, old_reason))

    def _rounds_text(self) -> str:
        lines = []
        for item in self.routes:
            lines.append("轮%d %s：%s" % (item["round"], item["intent"],
                                       _brief(str(item["raw"]), 40)))
        return "\n".join(lines)

    # ------------------------------------------------------------ 工具
    def snapshot(self, provider: str = "", stopped: bool = False) -> dict:
        """导出可续跑状态：档案、三方输出、路由与轮次历史都在这里。"""
        return {
            "query": self.query,
            "topic": self.topic,
            "archive": self.archive,
            "pro": self.pro,
            "con": self.con,
            "neu": self.neu,
            "routes": self.routes,
            "drift": self.drift,
            "sync": self.syncs,
            "conclusion": self.conclusion,
            "history": self.history_lines,
            "asked_total": self.asked_total,
            "round": self.round,
            "version": self.version,
            "close_reason": self.close_reason,
            "research": self.research_state,
            "stopped": bool(stopped),
            "provider": provider,
        }

    def restore(self, snapshot: dict):
        """从状态快照恢复现场；缺档案说明快照不可用，直接报错。"""
        data = snapshot if isinstance(snapshot, dict) else {}
        archive = data.get("archive")
        if not isinstance(archive, dict) or not archive:
            raise SandboxError("状态快照缺少《决策背景档案》，无法续跑")
        self.archive = copy.deepcopy(archive)
        self.query = str(data.get("query") or archive.get("question") or "")
        self.topic = str(data.get("topic") or self.query)
        for key in ("pro", "con", "neu"):
            value = data.get(key)
            setattr(self, key, copy.deepcopy(value) if isinstance(value, dict) else {})
        conclusion = data.get("conclusion")
        self.conclusion = copy.deepcopy(conclusion) if isinstance(conclusion, dict) else {}
        self.routes = [dict(item) for item in (data.get("routes") or []) if isinstance(item, dict)]
        self.drift = [dict(item) for item in (data.get("drift") or []) if isinstance(item, dict)]
        self.syncs = [dict(item) for item in (data.get("sync") or []) if isinstance(item, dict)]
        self.history_lines = [str(item) for item in (data.get("history") or [])]
        self.asked_total = int(data.get("asked_total") or 0)
        rounds = [int(item.get("round") or 0) for item in self.routes]
        self.round = max(rounds) if rounds else int(data.get("round") or 0)
        self.version = int(data.get("version") or 0)
        self.close_reason = ""
        self.refused = False
        self.refuse_reason = ""
        self._param_asked = False
        self._last_research_at = 0.0
        self._pending_requests = []
        state = data.get("research")
        self.research_state = research.new_state()
        if isinstance(state, dict):
            self.research_state["used"] = int(state.get("used") or 0)
            self.research_state["items"] = int(state.get("items") or 0)
            self.research_state["queries"] = [
                str(item) for item in (state.get("queries") or [])]
            self.research_state["agents"] = {
                str(key): int(value) for key, value in (state.get("agents") or {}).items()}
        return self

    def resume(self):
        """从快照恢复后重新进入交互阶段，后续版本继续在同一场次递增。"""
        self._check_cancel()
        if self.version <= 0:
            raise SandboxError("该场次还没有看板版本，无法续跑")
        self.ui.info("已恢复场次 V%d ｜ 可继续质疑、改参数、补背景，或说结束出结论" % self.version)
        self.store.log("系统", "续跑场次 V%d" % self.version, level="line")
        self._interact()

    def _check_cancel(self):
        event = self.cancel_event
        if event is not None and event.is_set():
            if not self.close_reason:
                self.close_reason = "user_stop"
            raise SandboxCancelled("用户停止推演")

    def _shutdown_pool(self, pool):
        """取消时不等仍在跑的模型线程，未取消时保持原等待语义。"""
        cancelled = self.cancel_event is not None and self.cancel_event.is_set()
        if not cancelled:
            pool.shutdown(wait=True)
            return
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # Python 3.8 没有 cancel_futures
            pool.shutdown(wait=False)

    def _set_side(self, key: str, result: dict):
        if key == "A2":
            self.pro = result
        elif key == "A3":
            self.con = result
        else:
            self.neu = result

    @staticmethod
    def _name(key: str) -> str:
        return {"A2": "A2 支持方", "A3": "A3 反对方", "A4": "A4 量化测算"}[key]

    def _render_side(self, key: str, data: dict) -> str:
        if key == "A2":
            lines = ["《支持方观点》第%s轮" % data.get("round", 1)]
            for arg in (data.get("arguments") or [])[:3]:
                lines.append("· [%s] %s" % (arg.get("dimension", "-"), arg.get("claim", "")))
                lines.append("  依据：%s" % arg.get("basis", ""))
                lines.append("  前提：%s" % arg.get("premise", ""))
            lines.append("结论受限项：%s" % (data.get("limited") or "无"))
            if data.get("cross"):
                lines.extend(self._render_cross(data["cross"]).splitlines())
            lines.append("_SIDE=PRO")
            return "\n".join(lines)
        if key == "A3":
            lines = ["《反方观点》第%s轮" % data.get("round", 1)]
            for risk in (data.get("risks") or [])[:3]:
                lines.append("· [%s·%s] %s" % (risk.get("dimension", "-"), risk.get("level", "-"),
                                             risk.get("desc", "")))
                lines.append("  触发：%s" % risk.get("trigger", ""))
                lines.append("  依据：%s" % risk.get("level_basis", ""))
            if data.get("cross"):
                lines.extend(self._render_cross(data["cross"]).splitlines())
            lines.append("_SIDE=CON")
            return "\n".join(lines)
        cf = data.get("cashflow") or {}
        lines = ["《量化测算》第%s轮" % data.get("round", 1),
                 "一、现金流测算", "算式：%s" % cf.get("calc", ""),
                 "结果：可支撑 %s 个月" % cf.get("months", "-"),
                 "安全边界判断：%s" % cf.get("boundary", ""),
                 "二、机会成本：%s" % (data.get("opportunity") or {}).get("range", "")]
        for th in (data.get("thresholds") or [])[:2]:
            lines.append("阈值 %s：%s（%s）" % (th.get("name", ""), th.get("value", ""), th.get("derive", "")))
        for s in (data.get("sensitivity") or [])[:2]:
            lines.append("敏感性·%s：上浮 → %s；下调 → %s；反转=%s" % (
                s.get("var", ""), s.get("up", ""), s.get("down", ""), "是" if s.get("flip") else "否"))
        changed = (data.get("params") or {}).get("changed") or []
        if changed:
            lines.append("参数变更记录：" + "；".join(
                "%s %s→%s（%s）" % (c.get("key", ""), fmt_yuan(c.get("before")), fmt_yuan(c.get("after")),
                                   c.get("delta_pct", "")) for c in changed))
        lines.append("_SIDE=NEU")
        return "\n".join(lines)

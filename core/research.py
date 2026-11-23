"""检索请求的校验、去重、预算与证据池合并。

Agent 只提出“想搜什么”，执行统一由编排层完成：既能按论据需求取证，
又能控制总次数、避免重复查询，并保证所有 Agent 共享同一证据池。
"""
import re

MAX_REQUESTS = 6
MAX_ITEMS = 10
PER_REQUEST_ITEMS = 3
AGENT_LIMITS = {"A1": 2, "A2": 1, "A3": 1, "A4": 1}
MIN_QUERY = 4
MAX_QUERY = 40
SPACE = re.compile(r"\s+")
PUNCT = re.compile(r"[，。；！？、,.!?;:\"'“”‘’（）()【】]+")


def _clean(text, limit) -> str:
    return SPACE.sub(" ", PUNCT.sub(" ", str(text or ""))).strip()[:limit]


def normalize(agent: str, requests) -> list:
    """只保留有效请求，并按 Agent 上限截断。超预算在编排层再判。"""
    kept = []
    for raw in requests or []:
        if not isinstance(raw, dict):
            continue
        query = _clean(raw.get("query"), MAX_QUERY)
        if len(query) < MIN_QUERY:
            continue
        kept.append({
            "agent": str(agent or ""),
            "query": query,
            "purpose": _clean(raw.get("purpose"), 60),
            "expect": _clean(raw.get("expect"), 40),
            "target": _clean(raw.get("target"), 30),
        })
        if len(kept) >= AGENT_LIMITS.get(agent, 2):
            break
    return kept


def new_state() -> dict:
    return {"used": 0, "items": 0, "queries": [], "agents": {}}


def check(state: dict, request: dict):
    """返回 (是否允许, 原因)。"""
    if not request or not request.get("query"):
        return False, "请求为空"
    if state["used"] >= MAX_REQUESTS:
        return False, "本场检索请求已达上限 %d 次" % MAX_REQUESTS
    if request["query"] in state["queries"]:
        return False, "重复检索词"
    agent = request.get("agent") or ""
    if state["agents"].get(agent, 0) >= AGENT_LIMITS.get(agent, 2):
        return False, "%s 的检索请求已达上限" % (agent or "该方")
    if state["items"] >= MAX_ITEMS:
        return False, "本场正文抓取已达上限 %d 条" % MAX_ITEMS
    return True, ""


def accept(state: dict, request: dict):
    state["used"] += 1
    state["agents"][request["agent"]] = state["agents"].get(request["agent"], 0) + 1
    state["queries"].append(request["query"])


def remaining_items(state: dict) -> int:
    return max(0, MAX_ITEMS - state["items"])


def _external(archive: dict) -> dict:
    ext = archive.get("external")
    if not isinstance(ext, dict):
        ext = {}
        archive["external"] = ext
    ext.setdefault("status", "未获取")
    ext.setdefault("provider", "元宝搜索")
    ext.setdefault("retrieved_at", "")
    ext.setdefault("queries", [])
    ext.setdefault("items", [])
    ext.setdefault("requests", [])
    ext.setdefault("reason", "")
    return ext


def ensure(archive: dict) -> dict:
    return _external(archive)


def merge(archive: dict, intel: dict, request: dict) -> int:
    """把一次检索结果并入 run 级证据池，统一编号并记录发起方。"""
    ext = _external(archive)
    items = [item for item in (intel or {}).get("items") or [] if isinstance(item, dict)]
    start = max([int(item.get("id") or 0) for item in ext["items"]] or [0]) + 1
    for offset, item in enumerate(items):
        entry = dict(item)
        entry["id"] = start + offset
        entry["origin"] = request.get("agent") or ""
        entry["request"] = request.get("purpose") or ""
        ext["items"].append(entry)
    if items:
        ext["status"] = "已获取"
        ext["provider"] = intel.get("provider") or ext["provider"]
        ext["retrieved_at"] = intel.get("retrieved_at") or ext["retrieved_at"]
        ext["reason"] = ""
    elif ext["status"] != "已获取":
        ext["reason"] = intel.get("reason") or "未获取"
    query = request.get("query") or ""
    if query and query not in ext["queries"]:
        ext["queries"].append(query)
    ext["requests"].append({
        "agent": request.get("agent") or "",
        "query": query,
        "purpose": request.get("purpose") or "",
        "expect": request.get("expect") or "",
        "target": request.get("target") or "",
        "hits": len(items),
        "reason": "" if items else (intel.get("reason") or "未获取"),
    })
    return len(items)


def record_skip(archive: dict, request: dict, reason: str):
    ext = _external(archive)
    ext["requests"].append({
        "agent": request.get("agent") or "",
        "query": request.get("query") or "",
        "purpose": request.get("purpose") or "",
        "expect": request.get("expect") or "",
        "target": request.get("target") or "",
        "hits": 0,
        "reason": reason,
    })


def budget_text(state: dict) -> str:
    parts = []
    for agent, used in sorted((state or {}).get("agents", {}).items()):
        parts.append("%s %d" % (agent, used))
    return "请求 %d/%d ｜ 正文 %d/%d%s" % (
        (state or {}).get("used", 0), MAX_REQUESTS,
        (state or {}).get("items", 0), MAX_ITEMS,
        (" ｜ " + "、".join(parts)) if parts else "")
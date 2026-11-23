"""模型输出契约校验：字段缺失时立刻报错，由调用方补一次修正请求，避免半成品进看板。"""
import re

from .jsonx import ParseError


class ContractError(ParseError):
    pass


CITE = re.compile(r"\[外\s*(\d+)\]")


def _need_dict(data, key, agent):
    value = data.get(key) if isinstance(data, dict) else None
    if not isinstance(value, dict) or not value:
        raise ContractError("%s 输出缺少有效字段 %s" % (agent, key))
    return value


def _need_list(data, key, agent):
    value = data.get(key) if isinstance(data, dict) else None
    if not isinstance(value, list) or not value:
        raise ContractError("%s 输出缺少有效字段 %s" % (agent, key))
    return value


def _need_text(data, key, agent):
    value = str(data.get(key) or "").strip() if isinstance(data, dict) else ""
    if not value:
        raise ContractError("%s 缺少有效字段 %s" % (agent, key))
    return value


def _need_listish(data, key, agent):
    value = data.get(key) if isinstance(data, dict) else None
    if not isinstance(value, list):
        raise ContractError("%s 的 %s 必须是数组" % (agent, key))
    return value


def _check_intel_use(data, agent):
    """intel_use 允许缺省，但给了就必须是对象且 cited 为数组。"""
    use = data.get("intel_use") if isinstance(data, dict) else None
    if use is None:
        return
    if not isinstance(use, dict):
        raise ContractError("%s 的 intel_use 必须是对象" % agent)
    if not isinstance(use.get("cited", []), list):
        raise ContractError("%s 的 intel_use.cited 必须是数组" % agent)


def _check_external_use(data, agent):
    """A4 的 external_use 允许缺省，但给了 cited 必须是数组。"""
    use = data.get("external_use") if isinstance(data, dict) else None
    if use is None:
        return
    if not isinstance(use, dict):
        raise ContractError("%s 的 external_use 必须是对象" % agent)
    if not isinstance(use.get("cited", []), list):
        raise ContractError("%s 的 external_use.cited 必须是数组" % agent)


def reason_cite_ids(archive, sides) -> list:
    """正反方实际引用、且确实存在于档案里的 [外N] 编号（按首次出现顺序去重）。"""
    items = ((archive or {}).get("external") or {}).get("items") or []
    valid = set()
    for item in items:
        try:
            valid.add(int(item.get("id") or 0))
        except (TypeError, ValueError):
            continue
    used = []
    for side in sides or ():
        use = (side or {}).get("intel_use") or {}
        for entry in use.get("cited") or []:
            if isinstance(entry, dict):
                try:
                    used.append(int(entry.get("id") or 0))
                except (TypeError, ValueError):
                    continue
    return [value for value in dict.fromkeys(used) if value in valid]


def check_reason_cite(payload, archive, sides):
    """正反方已引用外部条目时，判定理由必须标注真实存在的 [外N]。"""
    ids = reason_cite_ids(archive, sides)
    if not ids:
        return
    reason = str((payload or {}).get("reason") or "").strip()
    cited = [int(match.group(1)) for match in CITE.finditer(reason)]
    if not any(value in set(ids) for value in cited):
        raise ContractError("A5 判定理由使用了外部情报，但 reason 未标注真实存在的 [外N]")


def ensure_reason_cite(payload, archive, sides) -> list:
    """reason 漏标引用时，用正反方真实引用过的编号补注，绝不编造条目。

    真实模型（deepseek）出现过 brief/dispute 引用了 [外1]、[外2]，reason 却没写，
    两次修正请求后仍失败，整场被中止。判定正文不动，只在 reason 末尾追加
    「（依据 [外N]）」并返回补注编号，供编排层记日志。
    """
    ids = reason_cite_ids(archive, sides)
    if not ids or not isinstance(payload, dict):
        return []
    reason = str(payload.get("reason") or "").strip()
    cited = [int(match.group(1)) for match in CITE.finditer(reason)]
    if any(value in set(ids) for value in cited):
        return []
    picked = ids[:2]
    cites = "、".join("[外%d]" % value for value in picked)
    payload["reason"] = ("%s（依据 %s）" % (reason, cites)) if reason else ("依据 %s" % cites)
    return picked


def _check_requests(data, key, agent):
    """检索请求允许缺省，但给了就必须是数组，且每条都要有 query。"""
    value = data.get(key) if isinstance(data, dict) else None
    if value is None:
        return
    if not isinstance(value, list):
        raise ContractError("%s 的 %s 必须是数组" % (agent, key))
    for item in value:
        if not isinstance(item, dict) or not str(item.get("query") or "").strip():
            raise ContractError("%s 的 %s 条目缺少 query" % (agent, key))


VAGUE = ("视情况", "看情况", "不确定", "待定", "进一步观察", "无法判断", "还需观察")


def _need_verdict(data, agent):
    verdict = str(data.get("verdict") or "").strip() if isinstance(data, dict) else ""
    if not verdict:
        raise ContractError("%s 结论缺少 verdict（明确判定）" % agent)
    if any(word in verdict for word in VAGUE):
        raise ContractError("%s 结论模糊，必须给出明确判定：%s" % (agent, verdict))
    return verdict


def _need_reason(data, agent):
    reason = str(data.get("reason") or "").strip() if isinstance(data, dict) else ""
    if not reason:
        raise ContractError("%s 结论缺少 reason（判定理由）" % agent)
    return reason


def check_a1(data):
    if not isinstance(data, dict):
        raise ContractError("A1 输出不是对象")
    _check_requests(data, "research_requests", "A1")
    if data.get("kind") == "archive":
        check_archive(data)
        return
    archive = data.get("archive")
    if isinstance(archive, dict) and archive:
        check_archive(data)
        return
    _need_list(data, "questions", "A1")


def check_archive(data):
    archive = _need_dict(data, "archive", "A1")
    for key in ("question", "type", "window", "completeness"):
        _need_text(archive, key, "A1 档案")
    _need_dict(archive, "finance", "A1 档案")
    for key in ("options", "needs", "facts", "unknowns"):
        _need_listish(archive, key, "A1 档案")
    if not archive["needs"]:
        raise ContractError("A1 档案的 needs 不能为空")
    return archive


def check_pro(data):
    for item in _need_list(data, "arguments", "A2"):
        if not isinstance(item, dict) or not str(item.get("claim", "")).strip():
            raise ContractError("A2 论据缺少 claim")
        if not str(item.get("basis", "")).strip():
            raise ContractError("A2 论据缺少 basis")
    _check_intel_use(data, "A2")


def check_con(data):
    for item in _need_list(data, "risks", "A3"):
        if not isinstance(item, dict) or not str(item.get("desc", "")).strip():
            raise ContractError("A3 风险缺少 desc")
        if not str(item.get("level", "")).strip():
            raise ContractError("A3 风险缺少 level")
    _check_intel_use(data, "A3")


def check_cross(data):
    side = str((data or {}).get("side", "")) or "A2/A3"
    _check_requests(data, "evidence_requests", side)
    for item in _need_list(data, "cross", side):
        if not isinstance(item, dict):
            raise ContractError("%s 交叉质询条目不是对象" % side)
        for key in ("target", "verdict", "point"):
            if not str(item.get(key, "")).strip():
                raise ContractError("%s 交叉质询缺少 %s" % (side, key))


def check_respond(data, allowed_targets):
    """第 2 轮定向回应：只允许回应被点名的条目，不得新增论据/风险。"""
    if not isinstance(data, dict):
        raise ContractError("第 2 轮回应输出不是对象")
    if str(data.get("side") or "") not in ("PRO", "CON"):
        raise ContractError("第 2 轮回应缺少 side")
    responses = data.get("responses")
    if not isinstance(responses, list) or not responses:
        raise ContractError("第 2 轮回应缺少 responses")
    allowed = set(str(target) for target in (allowed_targets or []))
    for item in responses:
        if not isinstance(item, dict):
            raise ContractError("第 2 轮回应条目不是对象")
        target = str(item.get("target") or "").strip()
        if target not in allowed:
            raise ContractError("第 2 轮回应了未被点名的条目：%s" % target)
        if str(item.get("verdict") or "").strip() not in ("接受", "限定", "维持"):
            raise ContractError("第 2 轮回应 verdict 必须是 接受/限定/维持")
        if not str(item.get("point") or "").strip():
            raise ContractError("第 2 轮回应缺少 point")


def check_neu(data):
    cash = _need_dict(data, "cashflow", "A4")
    if "months" not in cash:
        raise ContractError("A4 现金流缺少 months")
    _need_list(data, "thresholds", "A4")
    _check_external_use(data, "A4")
    _check_requests(data, "evidence_requests", "A4")


def check_board(data):
    _need_list(data, "brief", "A5")
    _need_list(data, "dispute", "A5")
    conclusion = _need_dict(data, "conclusion", "A5")
    _need_verdict(conclusion, "A5")
    _need_reason(conclusion, "A5")


def check_report(data):
    payload = _need_dict(data, "report", "A5")
    for key in ("summary", "compare", "plans", "switch", "fallback"):
        _need_list(payload, key, "A5")
    _need_verdict(payload, "A5")
    _need_reason(payload, "A5")
    return payload
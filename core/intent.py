"""入口接待：判断用户是不是真的在纠结一个个人决策。

这里只回答“要不要开一场沙盘”，与 router 回答“沙盘内这条指令属于哪一类”
是两件事：前者判错只是多跑或少跑一次，后者判错会改错参数，所以这里允许
规则兜不住时交给模型判一次，而 router 永远只走硬编码。

判定策略固定为 规则优先 → 模型兜底 → 默认放行：
漏判（把真决策挡在门外）的代价远大于误判（多跑一轮推演）。
"""
import re
from dataclasses import dataclass

DECISION_WORDS = (
    "要不要", "该不该", "应不应该", "是否应该", "是否要", "是不是该", "有必要吗",
    "选哪个", "选哪家", "哪个好", "选哪一个", "二选一", "两个方案",
    "值不值", "值得吗", "划算吗", "合算吗", "买不买", "去不去", "留不留", "走不走",
    "纠结", "拿不定主意", "犹豫", "难以取舍",
)

DOI_WORDS = (
    "离职", "跳槽", "换工作", "裸辞", "考研", "读研", "出国", "留学",
    "买房", "买车", "租房", "换城市", "搬去", "结婚", "生孩子", "创业",
)

STRONG_NON_DECISION = (
    "帮我写", "写代码", "写个代码", "写脚本", "改代码", "debug", "报错了", "编译",
    "怎么安装", "怎么配置", "翻译一下", "总结一下", "讲个笑话", "天气怎么样",
    "几点了", "今天几号", "你是谁", "你叫什么", "什么是", "解释一下",
)

GREETING = ("你好", "您好", "谢谢", "再见", "在吗", "早上好", "晚上好", "哈喽", "hello", "hi")

TOPIC_PREFIX = re.compile(
    r"^(?:我|俺|咱|本人)?(?:现在|目前|最近|正在|一直)?(?:到底)?"
    r"(?:要不要|该不该|应不应该|是否应该|是否要|是不是该|要不要考虑|纠结要不要|纠结|犹豫)\s*"
)
PUNCT = re.compile(r"[，。；！？、,.!?;:：\"'“”‘’（）()\[\]【】\s]+")


@dataclass
class Gate:
    is_decision: bool
    reason: str
    source: str = "rule"
    topic: str = ""

    def describe(self) -> str:
        if self.is_decision:
            return "判定为决策问题（%s）" % self.source + (
                "｜主题：%s" % self.topic if self.topic else "")
        return "未进入沙盘（%s）｜%s" % (self.source, self.reason)


def topic_of(text: str) -> str:
    """把“我要不要买一台二十万级的家用车”压成“买一台二十万级的家用车”。"""
    body = PUNCT.sub(" ", str(text or "")).strip()
    body = TOPIC_PREFIX.sub("", body).strip()
    return PUNCT.sub(" ", body).strip()[:40]


def rule_gate(text: str):
    """纯规则判定；返回 None 表示规则拿不准，交给模型或默认放行。"""
    body = str(text or "").strip()
    if not body:
        return Gate(False, "没有输入内容")

    has_decision = any(word in body for word in DECISION_WORDS)
    has_doi = any(word in body for word in DOI_WORDS)
    strong_wrong = any(word in body for word in STRONG_NON_DECISION)

    if strong_wrong and not has_decision:
        return Gate(False, "这句话是在请求写代码、查知识或闲聊，不是要做选择")
    if has_decision:
        return Gate(True, "命中决策表达", topic=topic_of(body))
    if has_doi:
        return Gate(True, "命中决策主题（%s）" % next(w for w in DOI_WORDS if w in body),
                    topic=topic_of(body))
    if len(body) <= 10 and any(word in body.lower() for word in GREETING):
        return Gate(False, "寒暄或闲聊，没有具体决策")
    return None


def judge(text: str, probe=None) -> Gate:
    """probe(text) -> {"is_decision": bool, "reason": str, "topic": str}。

    只有模型明确说“不是决策”时才拦；判空、判不出、抛异常一律放行。
    """
    gate = rule_gate(text)
    if gate is not None:
        return gate

    if probe is None:
        return Gate(True, "规则未覆盖，按放行处理", source="fallback", topic=topic_of(text))

    try:
        data = probe(text)
    except Exception:
        return Gate(True, "入口判定失败，按放行处理", source="fallback", topic=topic_of(text))
    if not isinstance(data, dict) or not data:
        return Gate(True, "入口判定未返回结论，按放行处理", source="fallback",
                    topic=topic_of(text))

    topic = str(data.get("topic") or "").strip()[:40] or topic_of(text)
    if data.get("is_decision") is False:
        reason = str(data.get("reason") or "模型判定为非决策").strip()[:60]
        return Gate(False, reason, source="model", topic=topic)
    return Gate(True, "模型判定为决策", source="model", topic=topic)
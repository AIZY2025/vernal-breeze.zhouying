"""指令路由：全部硬编码在编排层，不交给模型做意图识别。

文档里的六类指令在这里变成确定性的判定顺序：
CLOSE > CHALLENGE(正方/反方) > PARAM > NEW_FACT > UNKNOWN
"""
from dataclasses import dataclass, field

import re

from .numbers_cn import parse_amount
from .quant import monthly_amount

STOP_WORDS = ("停止推演", "停止决策", "停止沙盘", "不继续了", "别继续了", "不要继续了",
              "放弃推演", "退出沙盘", "中断推演", "中止推演", "取消推演")
STOP_EXACT = ("停止", "停下", "停", "中止", "中断", "退出", "放弃")
CLOSE_WORDS = ("结束", "收尾", "给结论", "差不多了", "出报告", "收工", "就这样", "结束沙盘", "定稿", "给个结论",
               "不买了", "不去了", "不选了", "先这样", "就到这", "不用了", "不纠结了")
CHALLENGE_WORDS = ("质疑", "反驳", "不同意", "不认同", "不对", "反对", "站不住", "不成立", "凭什么",
                   "有问题", "太高", "太低", "偏高", "偏低", "夸大", "危言耸听", "不接受", "不靠谱", "错了",
                   "不准确", "没道理", "不现实", "想当然")
PRO_WORDS = ("正方", "支持方", "支持", "论据", "收益", "好处", "效率", "长期价值", "机会窗口", "乐观", "正面")
CON_WORDS = ("反方", "风险", "成本", "养车", "现金流", "机会成本", "退出", "外部环境", "悲观", "负面", "夸大")
NEW_FACT_WORDS = ("补充", "另外", "还有", "新情况", "新增", "追加", "事实是", "其实", "刚接到",
                  "家里", "公司", "政策", "变动", "新信息", "新的事实",
                  "我刚想到", "还有个事", "更新一下", "更正一下", "情况有变", "最新消息")
CHANGE_VERBS = ("改成", "改到", "改为", "改回", "调整成", "调整到", "调整为",
                "变成", "变为", "设成", "设为", "降到", "提到", "涨到", "提高到")
NEGATION_WORDS = ("别", "先别", "不要", "不用", "还没", "没有", "先不", "暂不", "甭", "不是")
CONDITION_TAILS = ("之前", "以前", "前先", "前再", "前把")

PARAM_KEYS = (
    (("存款", "现金", "本金", "积蓄", "可动用", "手头"), "cash"),
    (("月收入", "收入", "月薪", "工资", "到手"), "monthly_income"),
    (("结余", "每月能存", "存钱"), "monthly_saving"),
    (("一次性支出", "预算", "总价", "车价", "支出上限", "首付", "上限"), "max_one_time"),
    (("养车", "月供", "用车费用", "每月花", "每月成本", "租金", "房租"), "monthly_delta"),
)


@dataclass
class Route:
    intent: str
    target: str = ""
    params: dict = field(default_factory=dict)
    note: str = ""
    raw: str = ""

    def describe(self) -> str:
        label = {
            "STOP": "停止推演 → 保留已产出，不出最终报告",
            "CLOSE": "结束沙盘 → A5 收尾",
            "CHALLENGE_PRO": "质疑正方 → A2 定向重算",
            "CHALLENGE_CON": "质疑反方 → A3 定向重算",
            "PARAM": "参数变更 → A4 仅重算",
            "NEW_FACT": "补充背景 → 先更新档案，再三方全量重算",
            "UNKNOWN": "无法归类 → 回问一次",
        }.get(self.intent, self.intent)
        if self.params:
            label += "｜参数：" + "、".join("%s=%s" % (k, v) for k, v in self.params.items())
        return label


def _count(text: str, words) -> int:
    return sum(text.count(w) for w in words)


PUNCT = r"[，。；、,;\n]"

NEGATIVE_PREFIX = ("不变", "没变", "没动", "不动", "维持", "照旧", "保持", "不调整", "暂不")
AMOUNT_HEAD = r"^(?:[0-9]|[零〇一幺二两三四五六七八九十百千万亿])"
LEAD_PREFIX = re.compile(r"^(?:约|大概|差不多|也就|改成|调整到|调整成|变为|变成|是|为|到|至|[:：])")
NEGATIVE_HEAD = re.compile(r"^(?:约|大概|差不多|也就|改成|调整为|调整到|调整成|变为|变成|是|为|到|至|[:：\s])*(?:[-－−]|负)\s*")


def _clause(text: str) -> str:
    return re.split(PUNCT, text)[0]


def _negative_value(clause: str):
    """显式负数（-100 / 负一百）返回负金额，交由上层按非法参数拒绝。"""
    match = NEGATIVE_HEAD.match(clause)
    if not match:
        return None
    amount = parse_amount(clause[match.end():])
    return -amount if amount > 0 else None


def _tail_value(text: str, idx: int, key_len: int):
    """关键词后面的值；返回 None 表示用户明确说了“不变”，不应当作新值。"""
    clause = _clause(text[idx + key_len: idx + key_len + 30])
    if clause.startswith(NEGATIVE_PREFIX):
        return None
    negative = _negative_value(clause)
    if negative is not None:
        return negative
    return parse_amount(clause)


def _head_value(text: str, idx: int) -> float:
    """关键词前面的值，只认“150万存款”这种紧贴写法，绝不跨标点回捞。"""
    if idx <= 0 or re.match(PUNCT, text[idx - 1]):
        return 0.0
    head = re.split(PUNCT, text[max(0, idx - 12): idx])[-1].strip()
    head = LEAD_PREFIX.sub("", head).strip()
    if not head or not re.match(AMOUNT_HEAD, head):
        return 0.0
    return parse_amount(head)



def extract_params(text: str) -> dict:
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)  # 兼容 150,000 这种千分位写法
    params = {}
    for keys, field_name in PARAM_KEYS:
        if field_name in params:
            continue
        for key in keys:
            idx = text.find(key)
            while idx != -1:
                value = _tail_value(text, idx, len(key))
                if value is None:
                    break
                if value < 0:
                    # 显式负数原样上送，由编排层按非法参数回问，绝不静默取绝对值
                    params[field_name] = value
                    break
                if value <= 0:
                    value = _head_value(text, idx)
                if value > 0:
                    if field_name == "monthly_delta":
                        value = monthly_amount(value, text, (idx, idx + len(key)))
                    params[field_name] = value
                    break
                idx = text.find(key, idx + len(key))
            if field_name in params:
                break
    return params


# 编排层与测试沿用旧名；语义与新名完全一致
_extract_params = extract_params


def _negated(text: str, idx: int, word: str) -> bool:
    """否定或条件从句里的“停止/结束”不是指令：别结束、还没结束、结束之前……"""
    prefix = text[max(0, idx - 5): idx]
    if any(token in prefix for token in NEGATION_WORDS):
        if not word.startswith(("不", "别")):
            return True
    tail = text[idx + len(word): idx + len(word) + 4].lstrip()
    return tail.startswith(CONDITION_TAILS)


def _hits(text: str, words) -> bool:
    for word in words:
        idx = text.find(word)
        while idx != -1:
            if not _negated(text, idx, word):
                return True
            idx = text.find(word, idx + len(word))
    return False


def classify(text: str) -> Route:
    text = (text or "").strip()
    if not text:
        return Route("UNKNOWN", note="空指令")

    if text in STOP_EXACT or _hits(text, STOP_WORDS):
        return Route("STOP", raw=text)

    if _hits(text, CLOSE_WORDS):
        return Route("CLOSE", target="A5", raw=text)

    params = extract_params(text)
    challenge = any(w in text for w in CHALLENGE_WORDS)
    if challenge:
        if params and any(verb in text for verb in CHANGE_VERBS):
            return Route("PARAM", target="A4", params=params,
                         note="句中同时含质疑表述，按参数变更优先", raw=text)
        pro_score = _count(text, PRO_WORDS)
        con_score = _count(text, CON_WORDS)
        if pro_score > con_score:
            return Route("CHALLENGE_PRO", target="A2", raw=text)
        if con_score > pro_score:
            return Route("CHALLENGE_CON", target="A3", raw=text)
        return Route("UNKNOWN", note="质疑未指向具体一方", raw=text)

    if params:
        return Route("PARAM", target="A4", params=params, raw=text)

    if any(w in text for w in NEW_FACT_WORDS):
        return Route("NEW_FACT", target="ALL", raw=text)

    if parse_amount(text) > 0:
        return Route("PARAM", target="A4", params={}, note="只说了数字没说改哪项", raw=text)

    return Route("UNKNOWN", raw=text)

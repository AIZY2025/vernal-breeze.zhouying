"""中文金额解析：把“一百五十万”“150万”“1.15万”这类写法换算成元。"""
import re

CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "幺": 1, "二": 2, "两": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
SMALL_UNIT = {"十": 10, "百": 100, "千": 1000}
BIG_UNIT = {"万": 10 ** 4, "亿": 10 ** 8}

_AMOUNT_RE = re.compile(
    r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?|[零〇一幺二两三四五六七八九十百千万亿]+)"
    r"\s*(亿|万|千|百)?\s*(元|块|人民币|RMB)?"
)


def _cn_int(s: str) -> float:
    """解析不含亿/万的中文整数，例如 一百五十六、三千、十五。"""
    total = 0.0
    section = 0.0
    number = 0.0
    has = False
    for ch in s:
        if ch in CN_DIGIT:
            number = CN_DIGIT[ch]
            has = True
        elif ch in SMALL_UNIT:
            unit = SMALL_UNIT[ch]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
            has = True
        else:
            return 0.0
    section += number
    total += section
    return total if has else 0.0


def cn_to_number(text: str) -> float:
    """支持 一百五十万、一万二、十二万五千 等常见写法。"""
    if not text:
        return 0.0
    text = text.replace(" ", "")
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
        return float(text)

    total = 0.0
    rest = text
    # 依次吃掉 亿 / 万 段
    for unit, value in (("亿", BIG_UNIT["亿"]), ("万", BIG_UNIT["万"])):
        idx = rest.find(unit)
        if idx != -1:
            head = rest[:idx]
            num = _cn_int(head) if head else 1.0
            total += num * value
            rest = rest[idx + 1:]
    if rest:
        # 口语省略写法：“一万二”表示 12000
        if len(rest) == 1 and rest in CN_DIGIT and total > 0:
            tail_unit = 10 ** (len(str(int(total))) - 1) // 10
            total += CN_DIGIT[rest] * max(tail_unit, 1)
        else:
            total += _cn_int(rest)
    return total


def parse_amount(text: str) -> float:
    """从一段文本里取最像金额的一个数，返回元；取不到返回 0。

    打分规则：带“万/亿”最高，带“元/块”次之，光秃秃的数字居中，
    “一年”“一次”这种量词前的单字中文数字直接判为噪音。
    """
    best_value = 0.0
    best_score = -99
    for m in _AMOUNT_RE.finditer(text or ""):
        num_part = m.group(1)
        big = m.group(2)
        currency = m.group(3)
        tail = text[m.end(): m.end() + 3].lstrip()[:1]
        if tail in ("%", "％", "成"):
            continue
        if num_part.replace(",", "").replace(".", "", 1).isdigit():
            value = float(num_part.replace(",", ""))
        else:
            value = cn_to_number(num_part)
        if not value:
            continue
        score = 0
        if big in ("亿", "万"):
            score += 3
        elif big:
            score += 1
        if currency:
            score += 2
        if len(num_part) == 1 and num_part in CN_DIGIT and not big and not currency:
            score -= 2
        if score > best_score:
            best_score = score
            best_value = value
            if big:
                best_value *= BIG_UNIT.get(big, SMALL_UNIT.get(big, 1))
    if best_score < 0:
        return 0.0
    return float(best_value)


def fmt_yuan(value) -> str:
    """把元格式化成便于投屏的写法：1500000 -> 150万元 / 3000 -> 3000元。"""
    if value is None:
        return "未提供"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(v) >= 10000:
        wan = v / 10000
        if abs(wan - round(wan)) < 1e-9:
            return "%d万元" % int(round(wan))
        return ("%.2f" % wan).rstrip("0").rstrip(".") + "万元"
    return "%d元" % int(round(v))


def fmt_int(value) -> str:
    if value is None:
        return "未提供"
    try:
        return "{:,}".format(int(round(float(value))))
    except (TypeError, ValueError):
        return str(value)

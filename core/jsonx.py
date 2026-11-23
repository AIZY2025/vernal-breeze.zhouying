"""把模型返回的文本尽量稳地解析成 JSON 对象。"""
import json
import re


class ParseError(Exception):
    pass


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
MAX_TEXT = 200_000
MAX_ATTEMPTS = 50


def parse_json_object(text: str) -> dict:
    if not text:
        raise ParseError("空响应")
    if len(text) > MAX_TEXT:
        raise ParseError("响应超过 %d 字符上限" % MAX_TEXT)
    cleaned = _FENCE.sub("", text).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    start = cleaned.find("{")
    attempts = 0
    while start != -1 and attempts < MAX_ATTEMPTS:
        attempts += 1
        try:
            obj, _ = decoder.raw_decode(cleaned, start)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        start = cleaned.find("{", start + 1)
    raise ParseError("响应不是合法 JSON：%s" % text[:200].replace("\n", " "))

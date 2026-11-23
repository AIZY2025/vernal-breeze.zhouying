"""外部情报层：腾讯云元宝搜索（原联网搜索 API）客户端 + 情报包 + 本地缓存。

只用标准库，不引入腾讯云 SDK。支持两种接入方式：
1. SecretId + SecretKey：TC3-HMAC-SHA256 签名，走 wsa.tencentcloudapi.com；
2. 服务 API KEY（sk- 开头）：Bearer 认证，走 api.wsa.cloud.tencent.com。
两者都优先从 config.json 读，其次读腾讯云标准环境变量。

三条硬规则写死在代码里，不交给模型：
1. 只有同时带 url 和 date 的条目才算情报，缺一条直接丢弃；
2. 没有凭据、超时、被限流、返回空，都只记为“未获取”，绝不阻断主链路；
3. 情报包原样落盘并带抓取时间，看板/档案里的每个来源都能倒推。
"""
import difflib
import hashlib
import hmac
import html
import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_ENDPOINT = "https://api.wsa.cloud.tencent.com/SearchPro"
SIGNED_ENDPOINT = "https://wsa.tencentcloudapi.com"
SIGNED_ACTION = "SearchPro"
SIGNED_VERSION = "2025-05-08"
SIGNED_SERVICE = "wsa"
HTML_TAG = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")
PUNCT = re.compile(r"[，。；！？、,.!?;:：\"'“”‘’（）()【】]+")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DATE_SHAPE = re.compile(r"^[0-9TZ:\-. /]{4,32}$")

MAX_QUERY = 60
QUERY_MAX = 42
MAX_EXCERPT = 220
FETCH_CHARS = 1200
FETCH_TIMEOUT = 6.0
FETCH_BYTES = 200000
MODE_CHOICES = ("auto", "live", "cache", "off")

REGIONS = (
    "北京", "上海", "广州", "深圳", "杭州", "成都", "重庆", "武汉", "南京", "西安",
    "长沙", "苏州", "天津", "郑州", "青岛", "宁波", "合肥", "佛山", "东莞", "厦门",
    "福州", "济南", "大连", "沈阳", "昆明", "南昌", "贵阳", "南宁", "海口", "石家庄",
    "太原", "长春", "哈尔滨", "兰州", "乌鲁木齐", "呼和浩特", "银川", "西宁", "拉萨",
    "珠海", "中山", "惠州", "无锡", "常州", "温州", "嘉兴", "绍兴", "金华", "泉州",
    "烟台", "徐州", "潍坊", "香港", "澳门", "台北",
)

POLICY_WORDS = (
    "补贴", "购置税", "减免", "利率", "首付", "月供", "贷款利率", "公积金",
    "落户", "社保", "政策", "优惠", "限购", "摇号", "牌照", "保险", "保养",
    "停车费", "充电", "新能源", "以旧换新", "截止", "实施", "征求意见",
)

AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万元|万|元)")
RATE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
DATE_RE = re.compile(r"20\d{2}\s*[-/年]\s*\d{1,2}(?:\s*[-/月]\s*\d{1,2})?")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 口语/疑问/虚词：从检索主题里剥掉，只留真正决定相关度的实义词
STOPWORDS = (
    "要不要", "该不该", "应不应该", "是不是", "是否", "能不能", "可不可以", "值不值得",
    "我想", "我在考虑", "我在纠结", "纠结", "考虑", "犹豫", "怎么办", "如何", "怎么样",
    "选哪个", "哪一个", "还是", "或者", "的话", "现在", "目前", "比较", "一个", "这个",
    "那个", "我们", "你们", "他们", "自己", "可以", "应该", "需要", "想要", "打算", "准备",
    "到底", "究竟", "其实", "帮我", "我要", "我在", "我的", "你看",
    "的", "了", "吗", "呢", "吧", "啊",
    # 单字介词/副词：中文长句没有空格，去掉它们才能把“从深圳回长沙发展”切成实义词
    "从", "回", "去", "到", "在", "来", "往", "向", "对", "给", "把", "被",
    "与", "或", "就", "都", "也", "很", "最", "再", "又", "还", "让", "使",
)
CJK = re.compile(r"[\u4e00-\u9fa5]")
NON_WORD = re.compile(r"[^\u4e00-\u9fa50-9a-zA-Z]")


class SearchError(Exception):
    pass


def _sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sign_request(secret_id: str, secret_key: str, host: str, payload: bytes,
                 timestamp=None, content_type="application/json; charset=utf-8",
                 action: str = SIGNED_ACTION, version: str = SIGNED_VERSION,
                 service: str = SIGNED_SERVICE, region: str = "") -> dict:
    """腾讯云 API 3.0 签名（TC3-HMAC-SHA256），签名算法用标准库实现。"""
    ts = int(timestamp if timestamp is not None else time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(ts))
    canonical_headers = "content-type:%s\nhost:%s\n" % (content_type, host)
    signed_headers = "content-type;host"
    canonical_request = "\n".join([
        "POST", "/", "", canonical_headers, signed_headers, _sha256_hex(payload)])
    credential_scope = "%s/%s/tc3_request" % (date, service)
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256", str(ts), credential_scope, _sha256_hex(canonical_request)])
    secret_date = _hmac(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac(secret_date, service)
    secret_signing = _hmac(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    headers = {
        "Authorization": ("TC3-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                          % (secret_id, credential_scope, signed_headers, signature)),
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Timestamp": str(ts),
    }
    if region:
        headers["X-TC-Region"] = region
    return headers


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _clean(text, limit=MAX_EXCERPT) -> str:
    text = CONTROL.sub("", HTML_TAG.sub("", str(text or "")))
    text = SPACE.sub(" ", text).strip()
    text = text.replace("<", "＜").replace(">", "＞")
    return text[:limit]


def _private_host(host: str) -> bool:
    name = str(host or "").strip("[]").lower()
    if name in ("localhost", "localhost.localdomain"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast)


def _safe_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    if parsed.username or parsed.password or _private_host(parsed.hostname):
        return ""
    return parsed.geturl()


def _check_endpoint(endpoint: str) -> str:
    parsed = urlparse(str(endpoint or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise SearchError("搜索 endpoint 必须是 https:// 地址")
    if parsed.username or parsed.password or _private_host(parsed.hostname):
        raise SearchError("搜索 endpoint 不得指向私有或环回地址")
    return endpoint.rstrip("/")


QUERY_SUFFIX = {
    "消费支出": "价格 政策 补贴 最新",
    "职业变动": "行业 薪酬 招聘 最新",
    "资产配置": "收益率 政策 风险 最新",
    "生活安排": "政策 成本 最新",
}
DEFAULT_SUFFIX = "最新 数据 趋势 政策"


def plan_queries(query: str, extra: str = "", kind: str = "") -> list:
    """规则生成检索词，不额外调用模型：一条主体、一条按决策类型定制的趋势词。"""
    base = PUNCT.sub(" ", str(query or "")).strip()
    base = SPACE.sub(" ", base)
    if extra:
        base = (base + " " + PUNCT.sub(" ", str(extra)).strip()).strip()
    base = base[:MAX_QUERY].strip()
    if not base:
        return []
    queries = [base]
    suffix = QUERY_SUFFIX.get(str(kind or "").strip(), DEFAULT_SUFFIX)
    trend = (base + " " + suffix)[:MAX_QUERY].strip()
    if trend and trend != base:
        queries.append(trend)
    return queries


def _amount_wan(value) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    if amount <= 0:
        return ""
    if amount >= 10000:
        return "%d万" % int(round(amount / 10000.0))
    return "%d" % int(round(amount))


def _region_of(archive) -> str:
    """从问题、时间窗口、事实与方案名里找地域，检索时地域必须优先保留。"""
    texts = [str(archive.get("question") or ""), str(archive.get("window") or "")]
    texts.extend(str(fact) for fact in (archive.get("facts") or []))
    texts.extend(str((option or {}).get("name") or "")
                 for option in (archive.get("options") or []))
    for text in texts:
        for city in REGIONS:
            if city in text:
                return city
    return ""


def _topic_of(archive) -> str:
    """把「我要不要买一台二十万级的家用车」收成「买 家用车」。"""
    text = str(archive.get("question") or "")
    text = re.sub(r"(我要不要|我该不该|该不该|应不应该|值不值得|要不要|是否|能不能|可不可以)", " ", text)
    text = re.sub(r"\d+(?:\.\d+)?\s*(?:万元|万|元)?(?:级|左右)?", " ", text)
    text = re.sub(r"[零一二三四五六七八九十百千两]+(?:万元|万|元)(?:级|左右)?", " ", text)
    text = re.sub(r"(一台|一辆|一套|一份|一次|一个|这个|那个)", " ", text)
    text = re.sub(r"[的之]", " ", text)
    text = PUNCT.sub(" ", text)
    return _clean(SPACE.sub(" ", text).strip(), 24)


def _budget_of(archive) -> str:
    fin = archive.get("finance") or {}
    for key in ("max_one_time", "cash"):
        text = _amount_wan(fin.get(key))
        if text:
            return "预算" + text
    return ""


def _window_of(archive) -> str:
    text = _clean(archive.get("window"), 16)
    if not text or any(word in text for word in ("未明确", "未提供", "暂无", "无硬性")):
        return ""
    return PUNCT.sub(" ", text).strip()[:12]


def _join_query(*parts) -> str:
    text = SPACE.sub(" ", " ".join(str(part or "").strip()
                                  for part in parts if str(part or "").strip()))
    return text[:QUERY_MAX].strip()


def archive_queries(archive, extra="", kind="") -> list:
    """按地域、品类、预算、政策与时间窗口拆成多条短查询。

    原来把档案压成 60 字长句会被截断，城市与预算常被挤掉；这里每条查询只带
    一两个限定词，既保留地域，也让元宝能分别召回价格、政策与成本信息。
    """
    if not isinstance(archive, dict):
        return plan_queries(str(extra or ""), "", kind)
    topic = _topic_of(archive) or _clean(archive.get("question"), 24)
    if not topic:
        return plan_queries(str(extra or ""), "", kind)
    region = _region_of(archive)
    budget = _budget_of(archive)
    window = _window_of(archive)
    suffix = QUERY_SUFFIX.get(str(kind or "").strip(), DEFAULT_SUFFIX)
    queries = [
        _join_query(region, topic, budget, "价格 行情"),
        _join_query(region, topic, "政策 补贴", window),
        _join_query(topic, budget, "成本 费用 月供"),
        _join_query(region, topic, suffix),
    ]
    if extra:
        queries.insert(1, _join_query(region, topic, extra, budget))
    kept = []
    for text in queries:
        text = _join_query(text)
        if text and text not in kept:
            kept.append(text)
    return kept[:5]


def archive_query(archive, extra="") -> str:
    """把《决策背景档案》压成一条检索主题，地域优先、避免关键限定词被截掉。"""
    if not isinstance(archive, dict):
        return str(extra or "")[:MAX_QUERY].strip()
    parts = []
    region = _region_of(archive)
    if region:
        parts.append(region)
    topic = _topic_of(archive) or _clean(archive.get("question"), 18)
    if topic:
        parts.append(topic)
    budget = _budget_of(archive)
    if budget:
        parts.append(budget)
    window = _window_of(archive)
    if window:
        parts.append(window)

    facts = [str(fact) for fact in (archive.get("facts") or [])]
    facts.sort(key=lambda text: 0 if any(
        word in text for word in ("万", "元", "首付", "月供", "租")) else 1)
    for fact in facts[:1]:
        text = _clean(fact, 16)
        if text:
            parts.append(text)
    if extra:
        text = _clean(extra, 18)
        if text:
            parts.append(text)

    base = PUNCT.sub(" ", " ".join(parts))
    base = SPACE.sub(" ", base).strip()
    return base[:MAX_QUERY].strip()


def _page_dict(item):
    if isinstance(item, dict):
        return item
    if isinstance(item, str):
        try:
            data = json.loads(item)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None
    return None


def normalize_items(pages, source_query: str = "") -> list:
    """只保留有标题、有 http 链接、有发布日期的条目。"""
    items = []
    for raw in pages or []:
        page = _page_dict(raw)
        if not page or page.get("vr"):
            continue
        url = _safe_url(page.get("url"))
        # 元宝搜索实际返回 “2026/11/12 00:00:00”，统一成连字符日期与看板一致
        date = str(page.get("date") or "").strip().replace("/", "-")
        title = _clean(page.get("title"), 80)
        if not url or not DATE_SHAPE.match(date) or not title:
            continue
        try:
            score = float(page.get("score"))
        except (TypeError, ValueError):
            score = 0.0
        items.append({
            "title": title,
            "url": url,
            "date": date[:19],
            "site": _clean(page.get("site"), 20),
            "score": score,
            "excerpt": _clean(page.get("passage") or page.get("content")),
            "query": source_query,
        })
    return items


NAV_WORDS = (
    "首页", "登录", "注册", "推荐", "更多", "下载", "客户端", "微博", "导航",
    "版权", "广告", "收藏", "分享", "评论", "举报", "关于我们", "联系我们",
    "网站地图", "浏览器", "主办", "人民日报",
)


def _nav_like(text) -> bool:
    """导航条、频道列表与页脚通常是大量短词拼接，正文段落不会长这样。"""
    body = str(text or "").strip()
    if any(word in body for word in NAV_WORDS) and len(body) < 120:
        return True
    words = [word for word in re.split(r"\s+", body) if word]
    if len(words) >= 6 and sum(len(word) for word in words) / float(len(words)) <= 4.5 \
            and not re.search(r"\d", body) \
            and not any(word in body for word in POLICY_WORDS):
        return True
    return False


class _TextParser(HTMLParser):
    """只取可见正文：脚本、样式、导航容器一律跳过。"""

    SKIP = ("script", "style", "noscript", "svg", "head")

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.skip:
            return
        text = html.unescape(data).strip()
        if text:
            self.parts.append(text)


def _article_text(text, limit=FETCH_CHARS) -> str:
    """丢掉导航短句，只保留带数字、政策词或足够长的正文句。"""
    raw = SPACE.sub(" ", str(text or "")).strip()
    sentences = re.split(r"(?<=[。！？!?])", raw)
    kept, total = [], 0
    for sentence in sentences:
        body = sentence.strip()
        if not body:
            continue
        if len(body) < 12 and not re.search(r"\d", body) and \
                not any(word in body for word in POLICY_WORDS):
            continue
        if len(body) < 24 and any(word in body for word in NAV_WORDS):
            continue
        kept.append(body)
        total += len(body)
        if total >= limit:
            break
    return SPACE.sub(" ", " ".join(kept)).strip()[:limit] or raw[:limit]


def _html_text(markup, limit=FETCH_CHARS) -> str:
    parser = _TextParser()
    try:
        parser.feed(str(markup or ""))
        parser.close()
    except (ValueError, AssertionError):
        return ""
    parts = []
    for part in parser.parts:
        body = str(part or "").strip()
        if not body or _nav_like(body):
            continue
        if len(body) < 8 and not re.search(r"\d", body) and \
                not any(word in body for word in POLICY_WORDS):
            continue
        parts.append(body)
    return _article_text(" ".join(parts), limit)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_page(url, timeout=FETCH_TIMEOUT, max_bytes=FETCH_BYTES) -> str:
    """抓取网页正文，最多跟两次重定向，每次跳转都重新校验目标地址。"""
    target = _safe_url(url)
    if not target:
        raise SearchError("链接不可抓取")
    opener = urllib.request.build_opener(_NoRedirect)
    raw = b""
    for _ in range(3):
        req = urllib.request.Request(target, headers={"User-Agent": UA}, method="GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read(max_bytes + 1)
                charset = resp.headers.get_content_charset() or "utf-8"
                break
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location", "") if exc.headers else ""
            if exc.code in (301, 302, 303, 307, 308):
                nxt = _safe_url(urllib.parse.urljoin(target, location))
                if not nxt or nxt == target:
                    raise SearchError("重定向地址不可抓取")
                target = nxt
                continue
            raise SearchError("HTTP %s" % exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SearchError("正文抓取失败：%s" % exc)
    else:
        raise SearchError("重定向次数过多")
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
    for codec in (charset, "utf-8", "gb18030"):
        try:
            return raw.decode(codec, errors="replace")
        except LookupError:
            continue
    return raw.decode("utf-8", errors="replace")


def _context(text, start, end, width=12) -> str:
    return _clean(text[max(0, start - width):end + width], 48)


def extract_facts(text, limit=6) -> dict:
    """从正文抽取可核验的金额、利率、日期与政策词，供 A4 引用与看板展示。"""
    body = str(text or "")
    amounts, rates, dates = [], [], []
    seen = set()
    for match in AMOUNT_RE.finditer(body):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        unit = match.group(2)
        yuan = value * 10000 if unit in ("万", "万元") else value
        key = ("amount", round(yuan, 2))
        if key in seen:
            continue
        seen.add(key)
        amounts.append({"text": _context(body, match.start(), match.end()),
                        "value": round(yuan, 2), "unit": "元", "raw": match.group(0)})
        if len(amounts) >= limit:
            break
    for match in RATE_RE.finditer(body):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        key = ("rate", round(value, 2))
        if key in seen:
            continue
        seen.add(key)
        rates.append({"text": _context(body, match.start(), match.end()),
                      "value": round(value, 2), "unit": "%", "raw": match.group(0)})
        if len(rates) >= limit:
            break
    for match in DATE_RE.finditer(body):
        text_value = match.group(0).strip()
        if text_value not in dates:
            dates.append(text_value)
        if len(dates) >= 3:
            break
    keywords = [word for word in POLICY_WORDS if word in body][:limit]
    return {"amounts": amounts, "rates": rates, "dates": dates, "keywords": keywords}


def fact_summary(facts: dict, limit=4) -> str:
    """把结构化要点压成一行人话，用于看板与终端回显。"""
    if not isinstance(facts, dict):
        return ""
    parts = []
    for item in (facts.get("amounts") or [])[:2]:
        parts.append(str(item.get("raw") or "%.0f元" % item.get("value", 0)))
    for item in (facts.get("rates") or [])[:2]:
        parts.append(str(item.get("raw") or "%.1f%%" % item.get("value", 0)))
    for item in (facts.get("dates") or [])[:1]:
        parts.append(str(item))
    for item in (facts.get("keywords") or [])[:2]:
        parts.append(str(item))
    return "、".join(part for part in parts[:limit] if part)


def _enrich_items(items, timeout=FETCH_TIMEOUT, chars=FETCH_CHARS, fetcher=None) -> list:
    """并发抓取入选条目的正文；任何一条失败都只降级，不影响其它条目。"""
    fetcher = fetcher or _fetch_page
    result = list(items or [])
    if not result:
        return result

    def work(item):
        try:
            markup = fetcher(str(item.get("url") or ""), timeout, FETCH_BYTES)
        except Exception as exc:
            return item, "", "未抓取：%s" % str(exc)[:48]
        text = _html_text(markup, chars)
        if not text:
            return item, "", "未抓取：正文为空"
        return item, text, "已抓取 %d 字" % len(text)

    with ThreadPoolExecutor(max_workers=min(3, len(result))) as pool:
        futures = [pool.submit(work, item) for item in result]
        for future in as_completed(futures):
            item, text, status = future.result()
            item["page_status"] = status
            if text:
                item["content"] = text
                item["excerpt"] = _clean(text, MAX_EXCERPT)
                item["facts"] = extract_facts(text)
    return result


def core_terms(query: str) -> list:
    """抽出决定相关度的实义词：去标点、去口语虚词，保留长度 ≥2 的片段。"""
    text = PUNCT.sub(" ", str(query or ""))
    for word in STOPWORDS:
        text = text.replace(word, " ")
    terms = []
    for token in text.split():
        if len(token) < 2 or token in STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)
    if terms:
        return terms
    fallback = SPACE.sub(" ", PUNCT.sub(" ", str(query or ""))).strip()
    return [fallback] if fallback else []


def _match_units(terms: list) -> list:
    """短词直接匹配；长句（中文无空格分词）展开成二元组，避免整段在标题里永不命中。"""
    units = []
    for term in terms or []:
        body = _normalized_title(term)
        if not body:
            continue
        pieces = [body] if len(body) <= 4 else [body[i:i + 2] for i in range(len(body) - 1)]
        for piece in pieces:
            if piece not in units:
                units.append(piece)
    return units


def relevance(item: dict, terms: list) -> float:
    """标题命中占比权重更高，摘要只做补充；无实义词时视为不相关。"""
    units = _match_units(terms)
    if not units:
        return 0.0
    title = _normalized_title(item.get("title"))
    text = title + " " + _normalized_title(item.get("excerpt"))
    title_cov = sum(1 for unit in units if unit in title) / float(len(units))
    text_cov = sum(1 for unit in units if unit in text) / float(len(units))
    return round(min(1.0, max(0.0, 0.7 * title_cov + 0.3 * text_cov)), 4)


def _recency(date_text) -> float:
    text = str(date_text or "").strip()[:10]
    try:
        stamp = time.mktime(time.strptime(text, "%Y-%m-%d"))
    except (TypeError, ValueError, OverflowError):
        return 0.9
    age = (time.time() - stamp) / 86400.0
    if age <= 365:
        return 1.0
    if age <= 1095:
        return 0.9
    return 0.8


def _normalized_title(text) -> str:
    return NON_WORD.sub("", str(text or "")).lower()


def _bigrams(text) -> set:
    body = "".join(CJK.findall(str(text or "")))
    return {body[i:i + 2] for i in range(len(body) - 1)}


def _near_duplicate(a, b) -> bool:
    """同一篇文章被多站转载时标题几乎一致，这里只做廉价的字面判重。"""
    left, right = _normalized_title(a), _normalized_title(b)
    if not left or not right:
        return False
    if left == right:
        return True
    grams_a, grams_b = _bigrams(left), _bigrams(right)
    if not grams_a or not grams_b or min(len(grams_a), len(grams_b)) < 8:
        return False
    if len(grams_a & grams_b) / float(len(grams_a | grams_b)) >= 0.6:
        return True
    # 改写型转载：标题前半段被换掉，但保留了同一段长标题核（实测 12 字公共串）
    if min(len(left), len(right)) < 8:
        return False
    match = difflib.SequenceMatcher(None, left, right).find_longest_match(
        0, len(left), 0, len(right))
    return match.size >= 10 and match.size / float(min(len(left), len(right))) >= 0.35


def _site_of(item: dict) -> str:
    site = str(item.get("site") or "").strip().lower()
    if site:
        return site
    return (urlparse(str(item.get("url") or "")).hostname or "").lower()


def rank_items(items, terms, max_items=5, per_site=2) -> list:
    """按「检索分×词权重 + 词面相关度 + 时效」重排，并做 url/标题/站点三重去重。

    不改写 item["score"]：原始检索分仍可用于排查，重排结果写在 rank_score/relevance。
    """
    try:
        limit = max(1, int(max_items))
    except (TypeError, ValueError):
        limit = 5
    try:
        site_cap = max(1, int(per_site))
    except (TypeError, ValueError):
        site_cap = 2

    candidates = []
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        try:
            weight = float(item.get("query_weight") or 1.0)
        except (TypeError, ValueError):
            weight = 1.0
        rel = relevance(item, terms)
        rank_score = round(0.55 * score * weight + 0.35 * rel + 0.10 * _recency(item.get("date")), 4)
        item["rank_score"] = rank_score
        item["relevance"] = round(rel, 3)
        candidates.append((index, item, rank_score, score))

    candidates.sort(key=lambda row: (-row[2], -row[3], row[0]))

    urls, titles, site_counts, kept = set(), [], {}, []
    for _index, item, _rank, _score in candidates:
        url = str(item.get("url") or "")
        if url in urls:
            continue
        title = str(item.get("title") or "")
        if any(_near_duplicate(title, other) for other in titles):
            continue
        site = _site_of(item)
        if site and site_counts.get(site, 0) >= site_cap:
            continue
        urls.add(url)
        titles.append(title)
        if site:
            site_counts[site] = site_counts.get(site, 0) + 1
        kept.append(item)
        if len(kept) >= limit:
            break
    return kept


def _urllib_transport(url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise SearchError("HTTP %s" % exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SearchError("网络错误：%s" % exc)


class YuanbaoSearch:
    def __init__(self, api_key: str = "", endpoint: str = "",
                 timeout: float = 8.0, transport=None,
                 secret_id: str = "", secret_key: str = "", region: str = "",
                 version: str = SIGNED_VERSION):
        self.api_key = str(api_key or "").strip()
        self.secret_id = str(secret_id or "").strip()
        self.secret_key = str(secret_key or "").strip()
        self.region = str(region or "").strip()
        self.version = version or SIGNED_VERSION
        self.signed = bool(self.secret_id and self.secret_key)
        self.endpoint = _check_endpoint(
            endpoint or (SIGNED_ENDPOINT if self.signed else DEFAULT_ENDPOINT))
        self.timeout = timeout
        self.transport = transport or _urllib_transport

    @property
    def has_credential(self) -> bool:
        return self.signed or bool(self.api_key)

    def search(self, query: str) -> list:
        if not self.has_credential:
            raise SearchError("未配置搜索密钥（SecretId/SecretKey 或服务 API KEY）")
        body = json.dumps({"Query": query}, ensure_ascii=False).encode("utf-8")
        if self.signed:
            host = urlparse(self.endpoint).hostname or ""
            headers = sign_request(self.secret_id, self.secret_key, host, body,
                                   version=self.version, region=self.region)
        else:
            headers = {
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json; charset=utf-8",
            }
        status, text = self.transport(self.endpoint, headers, body, self.timeout)
        try:
            status = int(status)
        except (TypeError, ValueError):
            raise SearchError("搜索返回状态异常")
        if not 200 <= status < 300:
            raise SearchError("HTTP %s" % status)
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            raise SearchError("搜索返回不是合法 JSON：%s" % str(text)[:120])
        if not isinstance(data, dict):
            raise SearchError("搜索返回结构异常")
        error = data.get("Error")
        if isinstance(error, dict) and error.get("Code"):
            raise SearchError("%s：%s" % (error.get("Code"), error.get("Message", "")))
        response = data.get("Response")
        if not isinstance(response, dict):
            raise SearchError("搜索返回缺少 Response")
        error = response.get("Error")
        if isinstance(error, dict) and error.get("Code"):
            raise SearchError("%s：%s" % (error.get("Code"), error.get("Message", "")))
        return normalize_items(response.get("Pages") or [], query)


class IntelService:
    """按配置决定检索、读缓存或跳过；任何异常都收敛成一份“未获取”情报包。"""

    def __init__(self, api_key: str = "", endpoint: str = "",
                 timeout: float = 8.0, max_items: int = 5, cache_hours: float = 24,
                 mode: str = "auto", cache_dir=None, per_site: int = 2,
                 secret_id: str = "", secret_key: str = "", region: str = "",
                 fetch_pages: bool = False, fetch_timeout: float = FETCH_TIMEOUT,
                 fetch_chars: int = FETCH_CHARS, refresh_hours: float = 6.0,
                 page_fetcher=None):
        self.mode = str(mode or "auto").lower()
        self.max_items = max(1, int(max_items or 5))
        self.per_site = self._positive(per_site, 2)
        self.cache_hours = float(cache_hours if cache_hours is not None else 24)
        self.refresh_hours = max(0.0, float(refresh_hours or 0.0))
        self.fetch_pages = bool(fetch_pages)
        self.fetch_timeout = max(1.0, float(fetch_timeout or FETCH_TIMEOUT))
        self.fetch_chars = max(200, int(fetch_chars or FETCH_CHARS))
        self.page_fetcher = page_fetcher
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.client = YuanbaoSearch(api_key, endpoint, timeout,
                                    secret_id=secret_id, secret_key=secret_key,
                                    region=region)
        self.reason = ""

    @staticmethod
    def _positive(value, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(1, number)

    @property
    def active(self) -> bool:
        if self.mode == "cache":
            return True
        if self.mode in ("auto", "live"):
            return self.client.has_credential
        return False

    # ---------------------------------------------------------- 缓存
    def _cache_path(self, queries, max_items=None):
        limit = self._positive(max_items or self.max_items, self.max_items)
        key = "\n".join(queries) + "\n%d\n%d" % (limit, self.per_site)
        name = _sha256_hex(key)[:16]
        return (self.cache_dir / ("%s.json" % name)) if self.cache_dir else None

    def _read_cache(self, path, max_age_hours=None):
        if not path or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            saved = float(data.get("saved_at") or 0)
        except (OSError, ValueError, TypeError):
            return None
        hours = self.cache_hours if max_age_hours is None else max_age_hours
        if hours and (time.time() - saved) > hours * 3600:
            return None
        intel = data.get("intel")
        if not isinstance(intel, dict) or intel.get("rank") != "v2":
            return None
        return intel

    def _write_cache(self, path, intel):
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"saved_at": time.time(), "intel": intel},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---------------------------------------------------------- 主流程
    def search_queries(self, queries, extra: str = "", force: bool = False,
                       max_items=None, terms_text: str = "") -> dict:
        """执行指定的短查询；不重新生成 query，也不分配全局编号。"""
        queries = [str(text).strip() for text in (queries or []) if str(text).strip()]
        limit = self._positive(max_items or self.max_items, self.max_items)
        empty = {
            "status": "未获取",
            "provider": "元宝搜索",
            "retrieved_at": "",
            "queries": queries,
            "items": [],
            "reason": "",
        }
        if self.mode == "off":
            empty["reason"] = "情报层已关闭"
            return empty
        if self.mode not in ("auto", "live", "cache"):
            empty["reason"] = "未知的情报模式：%s" % self.mode
            return empty
        if not queries:
            empty["reason"] = "没有可检索的主题"
            return empty
        if self.mode in ("auto", "live") and not self.client.has_credential:
            empty["reason"] = "未配置搜索凭据（SecretId/SecretKey 或服务 API KEY）"
            return empty

        cache_path = self._cache_path(queries, limit)
        if self.mode == "cache":
            cached = self._read_cache(cache_path)
        elif self.mode == "auto" and not force:
            cached = self._read_cache(cache_path, self.refresh_hours)
        else:
            cached = None
        if cached:
            cached = dict(cached)
            cached["status"] = "缓存"
            cached["queries"] = queries
            return cached
        if self.mode == "cache":
            empty["reason"] = "缓存未命中（当前为缓存模式）"
            return empty

        merged, errors = [], []
        weights = {text: (1.0 if index == 0 else 0.85)
                   for index, text in enumerate(queries)}
        for text in queries:
            try:
                merged.extend(self.client.search(text))
            except SearchError as exc:
                errors.append(str(exc))

        for item in merged:
            item["query_weight"] = weights.get(item.get("query"), 0.85)
        terms = terms_text or " ".join(queries)
        items = rank_items(merged, core_terms(terms + " " + str(extra or "")),
                           limit, self.per_site)
        if self.fetch_pages:
            items = _enrich_items(items, self.fetch_timeout, self.fetch_chars,
                                  self.page_fetcher)

        if not items:
            empty["reason"] = "；".join(errors)[:120] or "搜索没有返回可用条目（需同时有链接与日期）"
            return empty

        intel = {
            "status": "已获取",
            "provider": "元宝搜索",
            "rank": "v2",
            "retrieved_at": _now(),
            "queries": queries,
            "items": items,
            "reason": "",
        }
        self._write_cache(cache_path, intel)
        return intel

    def collect(self, query: str, extra: str = "", kind: str = "",
                archive=None, force: bool = False) -> dict:
        if isinstance(archive, dict):
            queries = archive_queries(archive, extra, kind)
        else:
            queries = plan_queries(query, extra, kind)
        if not queries:
            queries = plan_queries(query, extra, kind)
        intel = self.search_queries(queries, extra=extra, force=force,
                                    terms_text=query)
        for idx, item in enumerate(intel.get("items") or [], 1):
            item["id"] = idx
        intel["queries"] = queries
        return intel


def render_intel(intel: dict, detail: bool = True) -> str:
    items = (intel or {}).get("items") or []
    if not items:
        return "外部情报：未获取 ｜ %s" % ((intel or {}).get("reason") or "未知原因")
    lines = ["外部情报：%d 条 ｜ %s ｜ 抓取于 %s" % (
        len(items), _clean(intel.get("provider", ""), 20),
        _clean(intel.get("retrieved_at", ""), 20))]
    for item in items:
        lines.append("[外%s] %s" % (_clean(item.get("id", "-"), 8), _clean(item.get("title", ""), 80)))
        lines.append("      %s ｜ %s" % (_clean(item.get("site") or "来源", 20),
                                       _clean(item.get("date", ""), 20)))
        if detail and item.get("excerpt"):
            lines.append("      摘要：%s" % _clean(item["excerpt"], 120))
        status = str(item.get("page_status") or "").strip()
        summary = fact_summary(item.get("facts") or {})
        if status or summary:
            lines.append("      正文：%s%s" % (
                status or "未抓取", (" ｜ 要点：" + summary) if summary else ""))
        lines.append("      %s" % _clean(item.get("url", ""), 300))
    return "\n".join(lines)
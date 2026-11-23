"""OpenAI 兼容协议的模型调用（火山方舟 / DeepSeek / 通义 / 自建 vLLM 均可用）。"""
import ipaddress
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


class LLMError(Exception):
    pass


def _loopback(host: str) -> bool:
    name = str(host or "").strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _check_base_url(base_url: str) -> str:
    parsed = urlparse(str(base_url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LLMError("base_url 必须是 http:// 或 https:// 地址")
    if parsed.scheme == "http" and not _loopback(parsed.hostname):
        raise LLMError("base_url 使用 http 时必须指向本机回环地址；公网地址请用 https")
    return base_url.rstrip("/")


class OpenAICompatible:
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 90, max_tokens: int = 3000):
        if not base_url:
            raise LLMError("base_url 为空，无法调用模型")
        if not api_key:
            raise LLMError("api_key 为空，请填写 config.json 或环境变量")
        if not model:
            raise LLMError("model 为空，请填写模型 ID")
        self.base_url = _check_base_url(base_url)
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    def chat(self, system: str, messages: list, temperature: float = 0.4) -> str:
        url = self.base_url + "/chat/completions"
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
            "messages": [{"role": "system", "content": system}] + messages,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.api_key,
        }
        last_err = None
        for attempt in range(3):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise LLMError("模型返回不是合法 JSON：%s" % str(exc)[:120])
                if not isinstance(data, dict):
                    raise LLMError("模型返回结构异常")
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError("模型返回结构异常：%s" % raw[:300])
                msg = choices[0].get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):  # 部分兼容实现返回分段内容
                    content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                if not content:
                    raise LLMError("模型返回空内容")
                return content
            except urllib.error.HTTPError as exc:
                last_err = LLMError("HTTP %s" % exc.code)
                if exc.code in (408, 429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise last_err
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = LLMError("网络错误：%s" % exc)
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise last_err
        raise last_err or LLMError("调用失败")

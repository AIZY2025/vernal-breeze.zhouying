"""配置加载与团队装配：命令行入口和 Web 入口共用同一套选择逻辑。"""
import json
import os
import re
from pathlib import Path

from .agents import LLMTeam
from .llm import OpenAICompatible
from .mock import MockTeam
from .search import IntelService
from . import quant

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*(?:KEY|SK|WSA|_ID)$")


def _env_name(value, default: str) -> str:
    name = str(value or default).strip()
    if not ENV_NAME.match(name):
        raise SystemExit("配置里的环境变量名不合法：只能包含大写字母、数字与下划线，"
                         "并以 KEY / SK / WSA / _ID 结尾")
    return name


MAX_CONFIG_BYTES = 1024 * 1024


def load_config(path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    if not path.is_file():
        raise SystemExit("配置文件不是普通文件：%s" % path.name)
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise SystemExit("配置文件超过 %d KB 上限" % (MAX_CONFIG_BYTES // 1024))
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SystemExit("config.json 不是合法 JSON：%s" % exc)
    if not isinstance(data, dict):
        raise SystemExit("config.json 顶层必须是 JSON 对象")
    return data


def _as_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SystemExit("config.json 的 %s 必须是整数，当前为 %r" % (name, value))


def build_team(cfg: dict, force: str = None):
    provider = force or cfg.get("provider", "auto")
    key_env = _env_name(cfg.get("api_key_env"), "SANDBOX_API_KEY")
    key = str(cfg.get("api_key") or os.environ.get(key_env, "")).strip()
    if provider == "mock" or (provider == "auto" and not key):
        return MockTeam(), "预置剧本（不调用模型）"
    if not key:
        raise SystemExit("选择了真实模型但 api_key 为空：填 config.json 的 api_key，"
                         "或设置环境变量 %s" % key_env)
    client = OpenAICompatible(
        base_url=cfg.get("base_url", ""),
        api_key=key,
        model=cfg.get("model", ""),
        timeout=_as_int(cfg.get("timeout") or 90, "timeout"),
        max_tokens=_as_int(cfg.get("max_tokens") or 3000, "max_tokens"),
    )
    return LLMTeam(client, cfg.get("temperature") or {}), "真实模型 %s" % cfg.get("model", "")

def _as_float(value, name: str, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SystemExit("config.json 的 %s 必须是数字，当前为 %r" % (name, value))


def build_quant(cfg: dict):
    """装配量化口径：config.json 的 quant 段可选，缺省即代码内置默认值。"""
    conf = cfg.get("quant")
    if not isinstance(conf, dict):
        return quant.configure()
    try:
        return quant.configure(conf)
    except ValueError as exc:
        raise SystemExit("config.json 的 %s" % exc)


def build_research(cfg: dict, cache_dir=None):
    """装配外部情报层；配置里没有 search 段就完全不启用。

    凭据优先级：SecretId + SecretKey（腾讯云标准签名）> 服务 API KEY（Bearer）。
    """
    conf = cfg.get("search")
    if not isinstance(conf, dict):
        return None
    if conf.get("enabled") is False:
        return None
    mode = str(conf.get("mode") or "auto").lower()
    if mode not in ("auto", "live", "cache", "off"):
        raise SystemExit("config.json 的 search.mode 只能是 auto / live / cache / off，当前为 %r" % mode)
    if mode == "off":
        return None
    api_key = str(conf.get("api_key") or os.environ.get(
        _env_name(conf.get("api_key_env"), "TENCENTCLOUD_WSA_APIKEY"), "")).strip()
    secret_id = str(conf.get("secret_id") or os.environ.get("TENCENTCLOUD_SECRET_ID", "")).strip()
    secret_key = str(conf.get("secret_key") or os.environ.get(
        _env_name(conf.get("secret_key_env"), "TENCENTCLOUD_SECRET_KEY"), "")).strip()
    return IntelService(
        api_key=api_key,
        secret_id=secret_id,
        secret_key=secret_key,
        region=conf.get("region") or "",
        endpoint=conf.get("endpoint") or "",
        timeout=_as_float(conf.get("timeout"), "search.timeout", 8.0),
        max_items=_as_int(conf.get("max_items") or 5, "search.max_items"),
        per_site=_as_int(conf.get("per_site") or 2, "search.per_site"),
        cache_hours=_as_float(conf.get("cache_hours"), "search.cache_hours", 24.0),
        refresh_hours=_as_float(conf.get("refresh_hours"), "search.refresh_hours", 6.0),
        fetch_pages=conf.get("fetch_pages", True) is not False,
        fetch_timeout=_as_float(conf.get("fetch_timeout"), "search.fetch_timeout", 6.0),
        fetch_chars=_as_int(conf.get("fetch_chars") or 1200, "search.fetch_chars"),
        mode=mode,
        cache_dir=cache_dir,
    )
"""加载五个 Agent 的提示词。

提示词正文放在 agents/*.md，既给本程序当 system prompt，也能直接整段复制到
码道 CodeArts 的智能体配置框里用。改提示词不用改代码。
"""
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "agents"

FILES = {
    "A1": "a1_背景探询.md",
    "A2": "a2_支持方.md",
    "A3": "a3_反对方.md",
    "A4": "a4_量化测算.md",
    "A5": "a5_仲裁汇总.md",
}

NAMES = {
    "A1": "背景探询 Agent",
    "A2": "支持方 Agent",
    "A3": "反对方 Agent",
    "A4": "量化测算 Agent",
    "A5": "仲裁汇总 Agent",
}


def load_prompt(key: str) -> str:
    path = AGENT_DIR / FILES[key]
    if not path.exists():
        raise FileNotFoundError(f"提示词文件缺失：{path.name}")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    # 去掉首行 markdown 标题，只把正文当 system prompt
    while lines and (lines[0].startswith("# ") or not lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip()


def all_prompts() -> dict:
    return {k: load_prompt(k) for k in FILES}

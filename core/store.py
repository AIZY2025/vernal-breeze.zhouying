"""运行记录落盘：每场推演一个目录，保留全轮次文本与结构化状态。"""
import datetime
import json
import os
import re
from pathlib import Path

MAX_LINE = 500
MAX_BLOCK = 8000
MAX_NAME = 120
NAME_SUFFIXES = (".json", ".txt", ".md", ".log")
USER_SPEAKER = "你"
RUN_NAME = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _safe_name(name: str) -> str:
    raw = str(name or "").strip()
    if not raw or len(raw) > MAX_NAME:
        raise ValueError("文件名不合法")
    if raw in (".", "..") or "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError("文件名不合法")
    if Path(raw).name != raw or Path(raw).is_absolute():
        raise ValueError("文件名不合法")
    if Path(raw).suffix.lower() not in NAME_SUFFIXES:
        raise ValueError("文件名后缀不在白名单内")
    return raw


def _write(path: Path, text: str):
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _sanitize(speaker: str, text: str, level: str) -> str:
    """用户输入压成单行并截断，防止伪造记录结构或把记录文件撑爆。"""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    if speaker == USER_SPEAKER or level == "line":
        text = " ".join(part.strip() for part in text.split("\n") if part.strip())
        return _clip(text, MAX_LINE)
    return _clip(text, MAX_BLOCK)


class SessionStore:
    def __init__(self, root: Path, reuse: str = ""):
        self._prefix = ""
        reuse_name = str(reuse or "").strip()
        if reuse_name:
            if not RUN_NAME.match(reuse_name):
                raise ValueError("场次编号不合法")
            self.dir = Path(root) / "runs" / reuse_name
            if not self.dir.is_dir():
                raise ValueError("该场次不存在")
            if self.dir.is_symlink():
                raise RuntimeError("拒绝写入符号链接目录")
            transcript = self.dir / "transcript.md"
            if transcript.is_file():
                self._prefix = transcript.read_text(encoding="utf-8")
            try:
                meta = json.loads((self.dir / "session.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            self._meta = meta if isinstance(meta, dict) else {}
            self._meta.setdefault(
                "started_at", datetime.datetime.now().isoformat(timespec="seconds"))
            self._meta["resumed_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        else:
            base = Path(root) / "runs" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            self.dir = base
            seq = 1
            while True:
                try:
                    self.dir.mkdir(parents=True)
                    break
                except FileExistsError:
                    seq += 1
                    self.dir = Path("%s-%d" % (base, seq))
            if self.dir.is_symlink():
                raise RuntimeError("拒绝写入符号链接目录")
            try:
                os.chmod(self.dir, 0o700)
            except OSError:
                pass
            self._meta = {"started_at": datetime.datetime.now().isoformat(timespec="seconds")}
        self._transcript = []

    def meta(self, **kwargs):
        self._meta.update(kwargs)

    def log(self, speaker: str, text: str, level: str = "block"):
        self._transcript.append((datetime.datetime.now().strftime("%H:%M:%S"), speaker,
                                 level, _sanitize(speaker, text, level)))

    def save_text(self, name: str, text: str):
        _write(self.dir / _safe_name(name), text)

    def save_json(self, name: str, obj):
        _write(self.dir / _safe_name(name),
               json.dumps(obj, ensure_ascii=False, indent=2))

    def finish(self, extra: dict = None):
        extra = extra or {}
        self._meta.update(extra)
        lines = ["# 春风 · 全轮次记录", ""]
        lines.append("开始时间：%s" % self._meta.get("started_at", ""))
        if self._meta.get("resumed_at"):
            lines.append("续跑时间：%s" % self._meta["resumed_at"])
        for key, value in extra.items():
            lines.append("%s：%s" % (_sanitize("系统", str(key), "line"),
                                    _sanitize("系统", str(value), "line")))
        lines.append("")
        for stamp, speaker, level, text in self._transcript:
            text = _sanitize(speaker, text, level)
            if level == "block":
                lines.append("## [%s] %s" % (stamp, speaker))
                lines.append("")
                lines.append(text)
                lines.append("")
            else:
                lines.append("- [%s] %s：%s" % (stamp, speaker, text))
        body = "\n".join(lines)
        if self._prefix:
            body = self._prefix.rstrip() + "\n\n---\n\n" + body
        self.save_text("transcript.md", body)
        self._meta["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        self.save_json("session.json", self._meta)
        return self.dir

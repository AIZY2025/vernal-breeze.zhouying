"""春风 · 本地 Web 对话服务。

只用标准库：http.server 提供页面与接口，SSE 把智能体的发言推给浏览器，
浏览器提交的回答通过队列回灌给阻塞中的 Sandbox.ask()。
编排层一行不改，换的只是 ui 的实现。
"""
import argparse
import datetime
import json
import logging
import mimetypes
import os
import queue
import re
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.board import external_usage_text  # noqa: E402
from core.llm import LLMError  # noqa: E402
from core.orchestrator import Sandbox, SandboxCancelled, SandboxError  # noqa: E402
from core.settings import build_quant, build_research, build_team, load_config  # noqa: E402
from core.store import SessionStore  # noqa: E402

STATIC = ROOT / "web" / "static"
ANSWER_TIMEOUT = 1800
STOP_SENTINEL = object()
MAX_BODY = 256 * 1024
MAX_DRAIN_BODY = 4 * 1024 * 1024
MAX_QUERY = 500
MAX_ANSWER = 2000
RUN_NAME_RE = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}
SECURITY_HEADERS = (
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
     "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)


class AnswerTimeout(Exception):
    pass


class Busy(Exception):
    pass


class NotAwaiting(ValueError):
    pass


class EventBus:
    MAX_EVENTS = 400

    def __init__(self):
        self._cond = threading.Condition()
        self._events = []
        self._seq = 0
        self._answers = queue.Queue()
        self._awaiting = False

    def emit(self, kind, **payload):
        with self._cond:
            self._seq += 1
            event = {"id": self._seq, "type": kind}
            event.update(payload)
            self._events.append(event)
            if len(self._events) > self.MAX_EVENTS:
                del self._events[: len(self._events) - self.MAX_EVENTS]
            self._cond.notify_all()

    def read(self, since, timeout=12.0):
        with self._cond:
            if since < 0:
                since = 0
            if since > self._seq:
                since = self._seq
            if self._seq <= since:
                self._cond.wait(timeout)
            return [event for event in self._events if event["id"] > since]

    def reset(self):
        with self._cond:
            self._events = []
            self._seq = 0
            self._awaiting = False
            self._cond.notify_all()

    def history(self):
        with self._cond:
            return self._seq

    def ask(self, prompt, echo=True):
        with self._cond:
            self._awaiting = True
        self.emit("ask", prompt=prompt, echo=bool(echo))

    def awaiting(self):
        with self._cond:
            return self._awaiting

    def answer(self, text):
        with self._cond:
            if not self._awaiting:
                raise NotAwaiting("当前没有待回答的提问，本次回答已忽略")
            self._awaiting = False
        self.emit("user", text=text)
        self._answers.put(text)

    def wake(self):
        """清掉尚未消费的回答并投停止哨兵，让阻塞中的 ask() 立刻返回。"""
        self.drop_answers()
        self._answers.put(STOP_SENTINEL)

    def drop_answers(self):
        while True:
            try:
                self._answers.get_nowait()
            except queue.Empty:
                break
        with self._cond:
            self._awaiting = False

    def wait_answer(self, timeout):
        try:
            try:
                value = self._answers.get(timeout=timeout)
            except queue.Empty:
                raise AnswerTimeout("等待你的回复超过 %d 秒，本场推演已中止" % timeout)
        finally:
            with self._cond:
                self._awaiting = False
        if value is STOP_SENTINEL:
            raise SandboxCancelled("用户停止推演")
        return value


class WebUI:
    def __init__(self, bus):
        self.bus = bus

    def agent(self, name, tag, text):
        self.bus.emit("agent", name=name, tag=tag, text=text)

    def info(self, text):
        self.bus.emit("info", text=text)

    def show(self, text, final=False):
        self.bus.emit("show", text=text, final=bool(final))

    def ask(self, prompt, echo=True):
        self.bus.ask(prompt, echo=bool(echo))
        return self.bus.wait_answer(ANSWER_TIMEOUT)


class Session:
    def __init__(self, root, config_path):
        self.root = Path(root)
        self.config_path = Path(config_path)
        self.bus = EventBus()
        self.active = False
        self.mode = ""
        self.run_dir = ""
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            run_id = Path(self.run_dir).name if self.run_dir else ""
            return {"active": self.active, "mode": self.mode,
                    "run_id": run_id, "history": self.bus.history()}

    def list_runs(self, limit: int = 20):
        runs_dir = self.root / "runs"
        items = []
        if not runs_dir.is_dir():
            return items
        for path in sorted((p for p in runs_dir.iterdir() if p.is_dir()), reverse=True):
            if not RUN_NAME_RE.match(path.name):
                continue
            meta = {}
            try:
                meta = json.loads((path / "session.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            has_report = (path / "最终报告.txt").exists()
            version_text = meta.get("最终看板版本", "")
            items.append({
                "id": path.name,
                "started_at": meta.get("started_at", ""),
                "query": meta.get("决策问题") or meta.get("query", ""),
                "version": version_text,
                "status": meta.get("状态", "已完成"),
                "has_report": has_report,
                "resumable": bool(version_text) and (path / "状态快照.json").is_file(),
            })
            if len(items) >= limit:
                break
        return items

    def _run_base(self, run_id: str):
        if not RUN_NAME_RE.match(str(run_id or "")):
            raise ValueError("场次编号不合法")
        base = self.root / "runs" / run_id
        if not base.is_dir():
            raise ValueError("该场次不存在")
        return base

    @staticmethod
    def _board_files(base):
        items = []
        for path in Path(base).glob("看板_V*.txt"):
            match = re.fullmatch(r"看板_V(\d+)\.txt", path.name)
            if match:
                items.append((int(match.group(1)), path))
        items.sort(key=lambda item: item[0])
        return items

    def list_run_files(self, run_id: str):
        base = self._run_base(run_id)
        files = []
        if (base / "最终报告.txt").is_file():
            files.append({"kind": "report", "label": "最终报告"})
        for version, _path in reversed(self._board_files(base)):
            files.append({"kind": "board", "label": "看板 V%d" % version, "version": version})
        if (base / "transcript.md").is_file():
            files.append({"kind": "transcript", "label": "全轮次记录"})
        if (base / "复查清单.json").is_file():
            files.append({"kind": "review", "label": "复查清单"})
        return files

    def list_reviews(self):
        """列出所有带复查清单的场次；到期的排在前面。"""
        runs_dir = self.root / "runs"
        if not runs_dir.is_dir():
            return []
        today = datetime.date.today().isoformat()
        items = []
        for path in sorted((p for p in runs_dir.iterdir() if p.is_dir()), reverse=True):
            if not RUN_NAME_RE.match(path.name):
                continue
            review_path = path / "复查清单.json"
            if not review_path.is_file():
                continue
            try:
                data = json.loads(review_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            meta = {}
            try:
                meta = json.loads((path / "session.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            due_at = str(data.get("due_at") or "")
            reviewed = bool(data.get("reviewed_at"))
            items.append({
                "id": path.name,
                "query": meta.get("决策问题") or meta.get("query", ""),
                "due_at": due_at,
                "verdict": data.get("verdict", ""),
                "reviewed": reviewed,
                "overdue": bool(due_at and due_at <= today and not reviewed),
            })
            if len(items) >= 50:
                break
        items.sort(key=lambda item: (not item["overdue"], item["due_at"]))
        return items

    def review_run(self, run_id: str):
        base = self._run_base(run_id)
        path = base / "复查清单.json"
        if not path.is_file():
            raise ValueError("该场次没有复查清单")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("复查清单不是合法 JSON")
        if not isinstance(data, dict):
            raise ValueError("复查清单结构异常")
        data["reviewed_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    def export_run(self, run_id: str):
        """导出一场的结构化结果：档案、每版看板、最终报告、漂移与全轮次记录。"""
        base = self._run_base(run_id)

        def read_json(name):
            try:
                return json.loads((base / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

        transcript = base / "transcript.md"
        return {
            "id": run_id,
            "meta": read_json("session.json") or {},
            "snapshot": read_json("状态快照.json"),
            "report": read_json("最终报告.json"),
            "intel": read_json("外部情报.json"),
            "boards": [{"version": version, "text": path.read_text(encoding="utf-8")}
                       for version, path in self._board_files(base)],
            "transcript": transcript.read_text(encoding="utf-8")
            if transcript.is_file() else "",
        }

    def read_run(self, run_id: str, kind: str = "", version: int = 0):
        base = self._run_base(run_id)
        kind = str(kind or "").strip().lower()
        if kind == "report":
            path = base / "最终报告.txt"
            if not path.is_file():
                raise ValueError("该场次没有最终报告")
            return {"id": run_id, "kind": "report", "text": path.read_text(encoding="utf-8")}
        if kind == "transcript":
            path = base / "transcript.md"
            if not path.is_file():
                raise ValueError("该场次没有全轮次记录")
            return {"id": run_id, "kind": "transcript", "text": path.read_text(encoding="utf-8")}
        if kind == "board":
            try:
                number = int(version)
            except (TypeError, ValueError):
                raise ValueError("看板版本不合法")
            path = base / ("看板_V%d.txt" % number)
            if number < 1 or not path.is_file():
                raise ValueError("该看板版本不存在")
            return {"id": run_id, "kind": "board", "version": number,
                    "text": path.read_text(encoding="utf-8")}
        if kind == "review":
            path = base / "复查清单.json"
            if not path.is_file():
                raise ValueError("该场次没有复查清单")
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ValueError("复查清单不是合法 JSON")
            return {"id": run_id, "kind": "review",
                    "text": json.dumps(data, ensure_ascii=False, indent=2)}
        for name, name_kind in (("最终报告.txt", "report"), ("transcript.md", "transcript")):
            path = base / name
            if path.is_file():
                return {"id": run_id, "kind": name_kind, "text": path.read_text(encoding="utf-8")}
        boards = sorted(base.glob("看板_V*.txt"))
        if boards:
            return {"id": run_id, "kind": "board", "text": boards[-1].read_text(encoding="utf-8")}
        raise ValueError("该场次没有可读记录")

    def read_intel(self, run_id: str):
        """历史详情用：返回外部情报与引用关系，供前端独立面板渲染。"""
        base = self._run_base(run_id)

        def read_json(name):
            try:
                return json.loads((base / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

        intel = read_json("外部情报.json")
        snapshot = read_json("状态快照.json") or {}
        usage = ""
        if intel and intel.get("items"):
            archive = dict(snapshot.get("archive") or {}, external=intel)
            usage = external_usage_text(archive, snapshot.get("pro") or {},
                                        snapshot.get("con") or {})
        return {"id": run_id, "intel": intel, "usage": usage}

    def reset(self):
        with self._lock:
            if self.active:
                raise Busy("推演进行中，不能重置")
            self.mode = ""
            self.run_dir = ""
            self.bus.reset()

    def answer(self, text):
        with self._lock:
            if not self.active or self._cancel.is_set():
                raise NotAwaiting("当前没有进行中的推演，本次回答已忽略")
        self.bus.answer(text)

    def stop(self):
        """请求停止当前推演；后台线程收到取消后自行落盘并释放 active。"""
        with self._lock:
            if not self.active:
                return False
            self._cancel.set()
        self.bus.wake()
        self.bus.emit("info", text="已收到停止请求，正在结束当前步骤…")
        return True

    def start(self, query, provider, archive=None):
        if provider not in (None, "", "auto", "llm", "mock"):
            raise ValueError("provider 只能是 auto / llm / mock")
        with self._lock:
            if self.active:
                raise Busy("上一场推演还在进行，先结束它再开新的")
            self.active = True
            self.mode = ""
            self.run_dir = ""
            self._cancel.clear()
            self.bus.reset()
            self.bus.drop_answers()
            threading.Thread(target=self._run, args=(query, provider, archive),
                             daemon=True).start()

    def start_resume(self, run_id):
        run_id = str(run_id or "").strip()
        base = self._run_base(run_id)
        self._load_snapshot(base)
        with self._lock:
            if self.active:
                raise Busy("上一场推演还在进行，先结束它再续跑")
            self.active = True
            self.mode = ""
            self.run_dir = ""
            self._cancel.clear()
            self.bus.reset()
            self.bus.drop_answers()
            threading.Thread(target=self._run_resume, args=(run_id,), daemon=True).start()

    @staticmethod
    def _read_board(base, version):
        try:
            number = int(version or 0)
        except (TypeError, ValueError):
            return ""
        path = Path(base) / ("看板_V%d.txt" % number)
        return path.read_text(encoding="utf-8") if number > 0 and path.is_file() else ""

    @staticmethod
    def _load_snapshot(base):
        try:
            data = json.loads((Path(base) / "状态快照.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("该场次没有可续跑的状态快照")
        if (not isinstance(data, dict) or not isinstance(data.get("archive"), dict)
                or not data["archive"]):
            raise ValueError("该场次的状态快照不完整，无法续跑")
        return data

    def _run_resume(self, run_id):
        bus = self.bus
        store = None
        sandbox = None
        team = None
        finished = False
        query = ""
        try:
            try:
                cfg = load_config(self.config_path)
                build_quant(cfg)
                team, mode = build_team(cfg, None)
                research = build_research(cfg, self.root / "cache" / "search")
            except SystemExit as exc:
                bus.emit("error", text=str(exc),
                         hint="在 config.json 里填 api_key，或把 provider 改成 mock")
                return

            base = self._run_base(run_id)
            snapshot = self._load_snapshot(base)
            store = SessionStore(self.root, reuse=run_id)
            sandbox = Sandbox(team, WebUI(bus), store, research=research,
                              cancel_event=self._cancel)
            sandbox.restore(snapshot)
            query = sandbox.query
            self.mode = mode
            self.run_dir = str(store.dir)
            store.meta(provider=mode, query=query, channel="web")
            bus.emit("status", active=True, mode=mode, run_id=store.dir.name)
            bus.emit("info", text="续跑场次 %s ｜ 当前 V%d ｜ %s" % (
                run_id, sandbox.version, query))
            board = self._read_board(store.dir, sandbox.version)
            if board:
                bus.emit("show", text=board, final=False)
            sandbox.resume()

            self._dump_snapshot(store, sandbox, mode)
            store.finish({"运行模式": mode, "决策问题": query,
                          "最终看板版本": "V%d" % sandbox.version,
                          "通道": "web", "续跑": "是"})
            finished = True
            bus.emit("done", ok=True, run_id=store.dir.name,
                     version=sandbox.version)
        except SandboxCancelled:
            finished = True
            self._cancel.clear()
            if store is not None and sandbox is not None:
                store.log("系统", "用户停止推演", level="line")
                try:
                    self._dump_snapshot(store, sandbox, self.mode, stopped=True)
                    store.finish({"运行模式": self.mode, "决策问题": query,
                                  "最终看板版本": ("V%d" % sandbox.version) if sandbox.version else "",
                                  "状态": "已停止", "通道": "web", "续跑": "是"})
                except Exception:
                    logging.getLogger("sandbox").exception("停止落盘失败")
                bus.emit("done", ok=True, stopped=True,
                         run_id=store.dir.name, version=sandbox.version)
            else:
                bus.emit("done", ok=True, stopped=True, version=0)
        except AnswerTimeout as exc:
            bus.emit("error", text=str(exc))
        except (SandboxError, ValueError) as exc:
            bus.emit("error", text="续跑失败：%s" % exc)
        except LLMError as exc:
            bus.emit("error", text="模型调用失败：%s" % exc,
                     hint="检查 config.json 的 base_url / api_key / model，或先用预置剧本")
        except Exception as exc:
            rid = uuid.uuid4().hex[:8]
            logging.getLogger("sandbox").exception("内部错误 rid=%s", rid)
            bus.emit("error", text="内部错误（编号 %s）：%s。详情已记录在服务端终端。" % (
                rid, type(exc).__name__))
        finally:
            if store is not None and not finished:
                try:
                    if sandbox is not None:
                        self._dump_snapshot(store, sandbox, self.mode)
                    store.finish({"运行模式": self.mode, "决策问题": query,
                                  "状态": "中止", "通道": "web", "续跑": "是"})
                except Exception:
                    pass
            with self._lock:
                self.active = False
            bus.emit("stats", **self._stats(team))
            bus.emit("status", active=False, mode=self.mode,
                     run_id=Path(self.run_dir).name if self.run_dir else "")

    @staticmethod
    def _stats(team):
        if team is None or not hasattr(team, "stats"):
            return {"calls": 0, "seconds": 0.0}
        return team.stats()

    @staticmethod
    def _dump_snapshot(store, sandbox, mode, stopped=False):
        store.save_json("状态快照.json", sandbox.snapshot(mode, stopped))

    def _run(self, query, provider, archive=None):
        bus = self.bus
        store = None
        sandbox = None
        team = None
        finished = False
        bus.emit("user", text=query)
        try:
            try:
                cfg = load_config(self.config_path)
                build_quant(cfg)
                team, mode = build_team(cfg, provider or None)
                research = build_research(cfg, self.root / "cache" / "search")
            except SystemExit as exc:
                bus.emit("error", text=str(exc), hint="在 config.json 里填 api_key，或把 provider 改成 mock")
                return

            self.mode = mode
            store = SessionStore(self.root)
            self.run_dir = str(store.dir)
            store.meta(provider=mode, query=query, channel="web")
            bus.emit("status", active=True, mode=mode, run_id=store.dir.name)
            bus.emit("info", text="运行模式：%s ｜ 记录目录：runs/%s" % (mode, store.dir.name))

            sandbox = Sandbox(team, WebUI(bus), store, research=research,
                              cancel_event=self._cancel)
            if archive:
                sandbox.run_paste(archive, query)
                query = sandbox.query or query
                store.meta(query=query)
            else:
                sandbox.run(query)

            if sandbox.refused:
                store.finish({"运行模式": mode, "决策问题": query,
                              "状态": "未进入推演", "原因": sandbox.refuse_reason,
                              "通道": "web"})
                finished = True
                bus.emit("done", ok=True, refused=True, reason=sandbox.refuse_reason,
                         run_id=store.dir.name, version=0)
                return

            self._dump_snapshot(store, sandbox, mode)
            store.finish({"运行模式": mode, "决策问题": query,
                          "最终看板版本": "V%d" % sandbox.version, "通道": "web"})
            finished = True
            bus.emit("done", ok=True, run_id=store.dir.name,
                     version=sandbox.version)
        except SandboxCancelled:
            finished = True
            self._cancel.clear()
            if store is not None and sandbox is not None:
                store.log("系统", "用户停止推演", level="line")
                try:
                    self._dump_snapshot(store, sandbox, self.mode, stopped=True)
                    store.finish({"运行模式": self.mode, "决策问题": query,
                                  "最终看板版本": ("V%d" % sandbox.version) if sandbox.version else "",
                                  "状态": "已停止", "通道": "web"})
                except Exception:
                    logging.getLogger("sandbox").exception("停止落盘失败")
                bus.emit("done", ok=True, stopped=True,
                         run_id=store.dir.name, version=sandbox.version)
            else:
                bus.emit("done", ok=True, stopped=True, version=0)
        except AnswerTimeout as exc:
            bus.emit("error", text=str(exc))
        except SandboxError as exc:
            bus.emit("error", text="链路中止：%s" % exc)
        except LLMError as exc:
            bus.emit("error", text="模型调用失败：%s" % exc,
                     hint="检查 config.json 的 base_url / api_key / model，或先用预置剧本演示")
        except Exception as exc:
            rid = uuid.uuid4().hex[:8]
            logging.getLogger("sandbox").exception("内部错误 rid=%s", rid)
            bus.emit("error", text="内部错误（编号 %s）：%s。详情已记录在服务端终端。" % (rid, type(exc).__name__))
        finally:
            if store is not None and not finished:
                try:
                    if sandbox is not None:
                        self._dump_snapshot(store, sandbox, self.mode)
                    store.finish({"运行模式": self.mode, "决策问题": query,
                                  "状态": "中止", "通道": "web"})
                except Exception:
                    pass
            with self._lock:
                self.active = False
            bus.emit("stats", **self._stats(team))
            bus.emit("status", active=False, mode=self.mode,
                     run_id=Path(self.run_dir).name if self.run_dir else "")


class Handler(BaseHTTPRequestHandler):
    server_version = "sandbox"
    sys_version = ""

    def version_string(self):
        return self.server_version

    protocol_version = "HTTP/1.1"
    app = None

    def log_message(self, fmt, *args):
        pass

    def _loopback_client(self):
        if getattr(self.app, "allow_remote", False):
            return True
        addr = getattr(self, "client_address", None)
        if not addr:
            return True
        ip = str(addr[0])
        return ip in ("127.0.0.1", "::1") or ip.startswith("127.") or ip.startswith("::ffff:127.")

    @staticmethod
    def _host_name(value):
        """取出不带端口的 host 名，兼容 IPv6 方括号写法，并统一为小写。

        端口必须是数字才会被剥离；非法端口保留在结果里，从而在 allow-list
        比对时被拒绝，而不是被误当成合法 host。
        """
        name = str(value or "").strip()
        if not name:
            return ""
        if name.startswith("["):
            end = name.find("]")
            return name[1:end].lower() if end > 0 else ""
        if name.count(":") == 1:
            host, _, port = name.partition(":")
            if port.isdigit():
                return host.lower()
        return name.lower()

    def _fetch_site(self):
        return (self.headers.get("Sec-Fetch-Site") or "").strip().lower()

    def _deny(self, message, code=403):
        """拒绝请求并关闭连接，避免未读完的请求体污染同一条 keep-alive 连接。"""
        self.close_connection = True
        self._json({"error": message}, code)
        return False

    def _guard(self):
        """只接受本机 Host / Origin 与回环客户端，挡住 DNS rebinding 与跨站调用。"""
        allowed = {str(host).lower() for host in getattr(
            self.app, "allowed_hosts", {"127.0.0.1", "localhost", "::1"})}
        if not self._loopback_client():
            return self._deny("forbidden client")
        if self._host_name(self.headers.get("Host")) not in allowed:
            return self._deny("forbidden host")
        origin = self.headers.get("Origin")
        if origin:
            if self._host_name(urlparse(origin).hostname) not in allowed:
                return self._deny("forbidden origin")
        fetch_site = self._fetch_site()
        if fetch_site and fetch_site not in ("same-origin", "none"):
            return self._deny("forbidden origin")
        return True

    def _guard_write(self):
        """写操作额外要求 JSON 请求体与同源发起点，挡住 file:// 页面与跨站表单。"""
        media = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            return self._deny("unsupported content type", 415)
        fetch_site = self._fetch_site()
        if fetch_site and fetch_site != "same-origin":
            return self._deny("forbidden origin")
        return True

    def do_GET(self):
        if not self._guard():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/stream":
            since = 0
            raw = parse_qs(parsed.query).get("since", ["0"])[0]
            if raw.isdigit():
                since = int(raw)
            return self._stream(since)
        if path == "/api/status":
            return self._json(self.app.session.snapshot())
        if path == "/api/runs":
            return self._json({"runs": self.app.session.list_runs()})
        if path == "/api/reviews":
            return self._json({"reviews": self.app.session.list_reviews()})
        if path.startswith("/api/runs/"):
            rest = path[len("/api/runs/"):]
            query = parse_qs(parsed.query)
            if rest.endswith("/files"):
                run_id = rest[:-len("/files")]
                try:
                    return self._json({"files": self.app.session.list_run_files(run_id)})
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 404)
            if rest.endswith("/export"):
                run_id = rest[:-len("/export")]
                try:
                    return self._json(self.app.session.export_run(run_id))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 404)
            if rest.endswith("/intel"):
                run_id = rest[:-len("/intel")]
                try:
                    return self._json(self.app.session.read_intel(run_id))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 404)
            kind = query.get("kind", [""])[0]
            raw_version = query.get("version", ["0"])[0]
            version = int(raw_version) if raw_version.isdigit() else 0
            try:
                return self._json(self.app.session.read_run(rest, kind, version))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 404)
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard():
            return
        if not self._guard_write():
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        if path == "/api/start":
            archive = body.get("archive")
            if archive is not None and not isinstance(archive, dict):
                return self._json({"error": "archive 必须是 JSON 对象"}, 400)
            query = str(body.get("query") or "").strip()
            if archive and not query:
                query = str(archive.get("question") or "").strip()
            if not query:
                return self._json({"error": "先说出你正在纠结的决策"}, 400)
            if len(query) > MAX_QUERY:
                return self._json({"error": "问题描述请控制在 %d 字以内" % MAX_QUERY}, 400)
            provider = body.get("provider") or None
            try:
                self.app.session.start(query, provider, archive or None)
            except Busy as exc:
                return self._json({"error": str(exc)}, 409)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"ok": True})
        if path == "/api/resume":
            run_id = str(body.get("run_id") or "").strip()
            try:
                self.app.session.start_resume(run_id)
            except Busy as exc:
                return self._json({"error": str(exc)}, 409)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"ok": True})
        if path.startswith("/api/runs/") and path.endswith("/review"):
            run_id = path[len("/api/runs/"):-len("/review")]
            try:
                return self._json({"ok": True, "review": self.app.session.review_run(run_id)})
            except ValueError as exc:
                return self._json({"error": str(exc)}, 404)
        if path == "/api/answer":
            text = str(body.get("text") or "").strip()
            if not text:
                return self._json({"error": "回答不能为空"}, 400)
            if len(text) > MAX_ANSWER:
                return self._json({"error": "单次回答请控制在 %d 字以内" % MAX_ANSWER}, 400)
            try:
                self.app.session.answer(text)
            except NotAwaiting as exc:
                return self._json({"error": str(exc)}, 409)
            return self._json({"ok": True})
        if path == "/api/stop":
            stopped = self.app.session.stop()
            return self._json({"ok": True, "stopped": bool(stopped)})
        if path == "/api/reset":
            try:
                self.app.session.reset()
            except Busy as exc:
                return self._json({"error": str(exc)}, 409)
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            if length <= MAX_DRAIN_BODY:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            self.close_connection = True
            raise ValueError("请求体超过 %d KB 上限" % (MAX_BODY // 1024))
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求体不是合法 JSON")

    def _security_headers(self):
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel):
        target = (STATIC / rel).resolve()
        root = STATIC.resolve()
        if root not in target.parents and target != root or not target.is_file():
            return self._json({"error": "not found"}, 404)
        data = target.read_bytes()
        ctype = STATIC_TYPES.get(target.suffix.lower())
        if ctype is None:
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, since):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self._security_headers()
        self.end_headers()
        bus = self.app.session.bus
        try:
            while True:
                events = bus.read(since)
                if events:
                    for event in events:
                        self._chunk("id: %d\ndata: %s\n\n" % (
                            event["id"], json.dumps(event, ensure_ascii=False)))
                        since = event["id"]
                else:
                    self._chunk(": ping\n\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
            return

    def _chunk(self, text):
        data = text.encode("utf-8")
        self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description="春风 · Web 对话界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--allow-remote", action="store_true",
                        help="允许非回环客户端访问（默认只接受本机连接）")
    args = parser.parse_args()

    if args.allow_remote and os.environ.get("SANDBOX_ALLOW_REMOTE") != "1":
        print("拒绝启动：--allow-remote 需要同时设置环境变量 SANDBOX_ALLOW_REMOTE=1")
        print("本服务没有登录鉴权，请勿暴露到不可信网络。")
        return 2

    class App:
        def __init__(self, allowed_hosts, allow_remote):
            self.session = Session(ROOT, args.config)
            self.allowed_hosts = allowed_hosts
            self.allow_remote = allow_remote

    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    if args.host not in ("0.0.0.0", "::", ""):
        allowed_hosts.add(args.host)

    Handler.app = App(allowed_hosts, args.allow_remote)
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print("端口 %d 无法监听：%s" % (args.port, exc))
        print("换一个端口再试：python run_web.py --port 8766")
        return 1
    httpd.daemon_threads = True
    url = "http://%s:%d/" % (args.host, args.port)
    print("春风 · Web 对话界面 · 遇事不决，可问春风")
    print("─" * 52)
    print("地址：%s" % url)
    if args.allow_remote:
        print("警告：已允许非本机客户端访问，本服务没有登录鉴权，请勿暴露到不可信网络")
    elif args.host not in ("127.0.0.1", "localhost", "::1", ""):
        print("提示：已绑定 %s，但默认只接受本机连接；确需远程访问请加 --allow-remote" % args.host)
    print("按 Ctrl+C 停止服务")
    print("提示：本服务只应在本机使用，没有登录鉴权，不要穿透到公网。")
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
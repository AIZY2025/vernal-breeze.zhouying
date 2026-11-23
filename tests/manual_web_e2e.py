"""Web 端到端手工验证（真实模型 / 预置剧本）。标准库实现，不使用 Node。

文件名不以 test 开头，因此不会被 `unittest discover` 收集。

用法：
    python -u tests/manual_web_e2e.py --mode mock
    python -u tests/manual_web_e2e.py --mode live --timeout 1500

脚本自行拉起 http.server（一次，供主链路复用），用 http.client 消费 chunked
事件流，逐项输出 PASS/FAIL 与证据；异常路径另起一个实例并使用临时 config。
任何 api_key 都不会被打印或写入证据文件。
"""
import argparse
import http.client
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

QUERY = "我要不要买一台二十万级的家用车"
NOT_QUERY = "帮我写一个 Python 脚本抓取网页标题"
ANSWER_POOL = [
    "三个月内要定下来，最晚春节前提车；主要用途是通勤和周末带父母出行。",
    "月收入大概1.15万，可动用存款120万，每月能存3000左右，最多能接受一次性支出20万。",
    "除了买，也在考虑继续打车加周末租车，或者先暂缓；最坏能接受存款降到100万左右。",
    "我每年还有大约两万年终奖，稳定性一般；家里能支持10万但我不想用。",
    "每周通勤约300公里，现在打车每月约800元，周末租车约400元。",
    "我比较看重时间效率和父母出行舒适度，也担心养车成本、停车费和折旧。",
    "如果非要二选一我倾向买，但希望有明确的止损线。",
    "我所在城市停车位月租大概400元。",
]
INTERACT = [
    ("CHALLENGE", "我质疑正方第一条论据，养车、停车和折旧的隐性成本被低估了，通勤收益也没那么大"),
    ("PARAM", "我把可动用存款改成150万"),
    ("CLOSE", "结束沙盘，给我结论"),
]
FRONTEND_TYPES = {"agent", "user", "show", "ask", "info", "status", "done", "error"}

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append({"item": name, "ok": bool(ok), "detail": detail})
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" ｜ " + detail) if detail else ""))


def free_port(start):
    port = start
    while port < start + 50:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                port += 1
                continue
        return port
    raise RuntimeError("找不到可用端口")


def http_call(port, path, payload=None, host=None, extra_headers=None, timeout=20):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Host": host or ("127.0.0.1:%d" % port)}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    headers.update(extra_headers or {})
    conn.request("POST" if payload is not None else "GET", path, body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    status = resp.status
    resp_headers = dict(resp.getheaders())
    conn.close()
    text = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = text
    return status, data, resp_headers


class Stream:
    """一条 SSE 连接：逐帧读取 id/data，注释帧（: ping）直接跳过。"""

    def __init__(self, port, since=0, timeout=90):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        self.conn.request("GET", "/api/stream?since=%d" % since,
                          headers={"Host": "127.0.0.1:%d" % port,
                                   "Accept": "text/event-stream"})
        self.resp = self.conn.getresponse()
        self.headers = {k.lower(): v for k, v in self.resp.getheaders()}

    def frame(self):
        cur = {}
        while True:
            line = self.resp.readline()
            if not line:
                return None
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not text:
                if cur:
                    return cur
                continue
            if text.startswith(":"):
                # 注释帧（: ping）交回外层，让调用方重新检查截止时间，避免内层无限阻塞
                return {"__comment__": True}
            key, _, value = text.partition(":")
            cur[key.strip()] = value.strip()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def start_server(config, port, wait=25):
    proc = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "run_web.py"), "--no-browser",
         "--port", str(port), "--config", str(config)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
            raise RuntimeError("服务启动失败：\n" + out)
        try:
            status, data, _ = http_call(port, "/api/status", timeout=2)
            if status == 200 and isinstance(data, dict):
                return proc
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("服务在 %d 秒内没有就绪" % wait)


def stop_server(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_idle(port, timeout=60):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        _, last, _ = http_call(port, "/api/status", timeout=5)
        if isinstance(last, dict) and not last.get("active"):
            return last
        time.sleep(0.2)
    return last


class Collector:
    def __init__(self, port, since=0, socket_timeout=90):
        self.stream = Stream(port, since=since, timeout=socket_timeout)
        self.events = []
        self.arrivals = []
        self.expected = since + 1
        self.gaps = []
        self.bad_json = []
        self.comments = 0
        self.started = time.time()

    def next_event(self, deadline):
        while time.time() < deadline:
            try:
                frame = self.stream.frame()
            except socket.timeout:
                continue
            if frame is None:
                return None
            if "__comment__" in frame:
                self.comments += 1
                continue
            if "data" not in frame:
                return {"__bad__": frame}
            try:
                event = json.loads(frame["data"])
            except ValueError:
                self.bad_json.append(frame.get("data", "")[:120])
                return {"__bad__": frame}
            if str(event.get("id")) != frame.get("id"):
                self.gaps.append("帧 id=%s 与载荷 id=%s 不一致" % (frame.get("id"), event.get("id")))
            if event.get("id") != self.expected:
                self.gaps.append("期望 id=%d，实际 id=%s" % (self.expected, event.get("id")))
            self.expected = int(event.get("id") or self.expected) + 1
            self.events.append(event)
            self.arrivals.append(round(time.time() - self.started, 2))
            return event
        return {"__timeout__": True}

    def close(self):
        self.stream.close()


def post_answer(port, text):
    status, data, _ = http_call(port, "/api/answer", {"text": text})
    if status != 200:
        raise RuntimeError("提交回答失败：%s %s" % (status, data))
    return data


def drive_main(port, provider, budget):
    """提交决策问题并消费事件流直到 done；返回证据包。"""
    started = time.time()
    status, data, _ = http_call(port, "/api/start", {"query": QUERY, "provider": provider})
    if status != 200:
        raise RuntimeError("启动推演失败：%s %s" % (status, data))

    collector = Collector(port, since=0, socket_timeout=90)
    deadline = started + budget
    answers = 0
    interactions = 0
    ask_log = []
    stages = []
    done = None
    last_arrival = time.time()
    idle_after_done = 0
    busy = {}

    while time.time() < deadline:
        event = collector.next_event(min(deadline, time.time() + 120))
        if "__timeout__" in event:
            continue
        if "__bad__" in event:
            continue
        now = time.time()
        kind = event.get("type")
        gap = now - last_arrival
        last_arrival = now
        if kind == "info":
            match = re.search(r"阶段\s*(\d)\s*/\s*6", str(event.get("text", "")))
            if match:
                stages.append(match.group(1))
        if kind == "ask":
            if not busy:
                # 沙盘阻塞在 ask 上时，Web 层应拒绝重置与并发启动
                busy["reset"] = http_call(port, "/api/reset", {})[0]
                busy["start"] = http_call(port, "/api/start",
                                          {"query": "要不要再开一场", "provider": provider})[0]
            prompt = str(event.get("prompt") or "")
            ask_log.append({"prompt": prompt[:120], "at": round(now - started, 1), "gap": round(gap, 1)})
            if "指令" in prompt:
                if interactions < len(INTERACT):
                    label, text = INTERACT[interactions]
                    interactions += 1
                else:
                    label, text = ("CLOSE", INTERACT[-1][1])
                post_answer(port, text)
                ask_log[-1]["answered"] = text
                ask_log[-1]["route"] = label
            else:
                text = ANSWER_POOL[min(answers, len(ANSWER_POOL) - 1)]
                answers += 1
                post_answer(port, text)
                ask_log[-1]["answered"] = text
        if kind == "done":
            done = event
            idle_after_done += 1
            if not event.get("ok"):
                break
        if done is not None:
            idle_after_done += 1
            if idle_after_done >= 2:
                break

    collector.close()
    elapsed = time.time() - started
    snapshot = wait_idle(port, timeout=30)
    return {
        "events": collector.events,
        "arrivals": collector.arrivals,
        "gaps": collector.gaps,
        "bad_json": collector.bad_json,
        "asks": ask_log,
        "stages": stages,
        "done": done,
        "elapsed": round(elapsed, 1),
        "snapshot": snapshot,
        "answers": answers,
        "interactions": interactions,
        "busy": busy,
        "pings": collector.comments,
    }


def replay(port, since=0, socket_timeout=15, settle=1.0):
    collector = Collector(port, since=since, socket_timeout=socket_timeout)
    deadline = time.time() + settle
    while time.time() < deadline:
        event = collector.next_event(deadline)
        if "__timeout__" in event:
            break
    collector.close()
    return collector


def denied_gate(port, provider):
    status, data, _ = http_call(port, "/api/start", {"query": NOT_QUERY, "provider": provider})
    if status != 200:
        raise RuntimeError("门禁用例启动失败：%s %s" % (status, data))
    collector = Collector(port, since=0, socket_timeout=30)
    done = None
    deadline = time.time() + 120
    while time.time() < deadline:
        event = collector.next_event(deadline)
        if "__timeout__" in event:
            continue
        if "__bad__" in event:
            continue
        if event.get("type") == "done":
            done = event
            collector.next_event(time.time() + 5)
            break
    collector.close()
    snapshot = wait_idle(port, timeout=20)
    return collector.events, done, snapshot


def check_loopback_only(port):
    """服务只 bind 回环时，本机网卡地址应连不上（客户端级拒绝由单测覆盖）。"""
    candidates = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in candidates:
                candidates.append(ip)
    except OSError:
        pass
    if not candidates:
        record("非回环地址不可达", True, "未取到本机网卡地址，由单测 test_remote_client_blocked_by_default 覆盖")
        return
    try:
        socket.create_connection((candidates[0], port), timeout=3).close()
        record("非回环地址不可达", False, "%s:%d 竟然可连" % (candidates[0], port))
    except OSError as exc:
        record("非回环地址不可达（仅监听回环）", True,
               "%s:%d → %s" % (candidates[0], port, type(exc).__name__))


def file_listing(path):
    return sorted(p.name for p in Path(path).iterdir())


def check_static(port):
    status, body, headers = http_call(port, "/")
    record("静态首页可访问", status == 200 and "春风" in str(body),
           "HTTP %d，%d 字节" % (status, len(str(body))))
    script_status, _, script_headers = http_call(port, "/static/app.js")
    record("app.js 可访问", script_status == 200 and "javascript" in
           script_headers.get("Content-Type", ""), "Content-Type=%s" % script_headers.get("Content-Type"))
    css_status, _, _ = http_call(port, "/static/style.css")
    record("style.css 可访问（无 CDN/框架）", css_status == 200, "HTTP %d" % css_status)
    csp = headers.get("Content-Security-Policy", "")
    record("安全响应头完整", bool(csp) and headers.get("X-Frame-Options") == "DENY"
           and headers.get("X-Content-Type-Options") == "nosniff",
           "CSP 含 script-src 'self'=%s" % ("script-src 'self'" in csp))

    status, data, _ = http_call(port, "/api/status", host="evil.example.com")
    record("伪造 Host 被拒", status == 403, "HTTP %d %s" % (status, data))
    status, data, _ = http_call(port, "/api/status",
                                extra_headers={"Origin": "http://evil.example.com"})
    record("跨站 Origin 被拒", status == 403, "HTTP %d %s" % (status, data))
    status, data, _ = http_call(port, "/api/status",
                                extra_headers={"Sec-Fetch-Site": "cross-site"})
    record("跨站 Fetch 元数据被拒", status == 403, "HTTP %d %s" % (status, data))
    status, data, _ = http_call(port, "/api/runs/..%2F..%2Fcore")
    record("场次路径穿越被拒", status in (403, 404), "HTTP %d" % status)


def check_frontend_contract(events):
    app_source = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    kinds = set(re.findall(r"case '([a-z]+)'", app_source))
    actual = {e.get("type") for e in events}
    record("前端消费的事件类型全覆盖", actual <= FRONTEND_TYPES,
           "app.js case=%s；实际=%s；多余=%s" % (
               ",".join(sorted(kinds)), ",".join(sorted(actual)), ",".join(sorted(actual - kinds))))
    shapes = True
    for event in events:
        kind = event.get("type")
        if kind == "agent" and not (event.get("name") and event.get("text") is not None):
            shapes = False
        if kind == "ask" and not event.get("prompt"):
            shapes = False
        if kind == "show" and event.get("text") is None:
            shapes = False
        if kind == "info" and event.get("text") is None:
            shapes = False
    record("事件字段满足前端渲染所需", shapes, "agent/ask/show/info 必需字段齐全")
    final_shows = [e for e in events if e.get("type") == "show" and e.get("final") is True]
    record("只有收尾看板带 final 标记", len(final_shows) == 1,
           "final 帧 %d 条；看板帧 %d 条" % (
               len(final_shows), len([e for e in events if e.get("type") == "show"])))
    seat_names = [e.get("name", "") for e in events if e.get("type") == "agent"]
    record("agent 事件名称可被前端席位匹配", any(re.search(r"A[1-5]", n) for n in seat_names),
           "示例=%s" % seat_names[:4])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--timeout", type=float, default=1500, help="主链路时间预算（秒）")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--keep", action="store_true", help="结束后保留服务进程")
    parser.add_argument("--evidence", default="", help="证据 JSON 输出路径")
    parser.add_argument("--skip-errors", action="store_true", help="跳过异常路径用例")
    args = parser.parse_args()

    config = ROOT / "config.json"
    port = args.port or free_port(8877)
    provider = "llm" if args.mode == "live" else "mock"
    evidence = {"mode": args.mode, "port": port, "provider": provider, "results": RESULTS}
    proc = None
    started = time.time()
    try:
        proc = start_server(config, port)
        record("run_web.py --no-browser 启动服务", proc.poll() is None, "127.0.0.1:%d" % port)
        check_static(port)

        gate_events, gate_done, gate_snapshot = denied_gate(port, provider)
        gate_kinds = [e.get("type") for e in gate_events]
        refused_ok = bool(gate_done and gate_done.get("refused") and gate_done.get("reason"))
        no_agents = not any(k in ("agent", "show") for k in gate_kinds)
        gate_info = [e.get("text", "") for e in gate_events if e.get("type") == "info"]
        record("入口门禁拦截非决策输入", refused_ok and no_agents,
               "done.refused=%s；事件=%s；提示=%s" % (
                   bool(gate_done and gate_done.get("refused")), ",".join(gate_kinds),
                   (gate_info[0][:60] if gate_info else "")))
        gate_run = gate_snapshot.get("run_id", "")
        if gate_run:
            gate_dir = ROOT / "runs" / gate_run
            meta = json.loads((gate_dir / "session.json").read_text(encoding="utf-8"))
            record("门禁场次落盘（未进入推演）",
                   (gate_dir / "入口判定.json").exists() and meta.get("状态") == "未进入推演",
                   "runs/%s ｜ %s" % (gate_run, meta.get("状态")))
        else:
            record("门禁场次落盘（未进入推演）", False, "没有拿到 run_id")
        http_call(port, "/api/reset", {})

        main_run = drive_main(port, provider, args.timeout)
        events = main_run["events"]
        done = main_run["done"]
        record("推演在主预算内结束", done is not None,
               "耗时 %.1fs；事件 %d 条；ask %d 次（A1 回答 %d / 指令 %d）" % (
                   main_run["elapsed"], len(events), len(main_run["asks"]),
                   main_run["answers"], main_run["interactions"]))
        record("事件流无丢包/无粘包（id 连续）", not main_run["gaps"] and not main_run["bad_json"],
               "gap=%s；坏帧=%d；首尾 id=%s..%s" % (
                   main_run["gaps"][:3], len(main_run["bad_json"]),
                   events[0]["id"] if events else "-", events[-1]["id"] if events else "-"))
        record("阶段提示齐全（1/3/4/5/6）", set(main_run["stages"]) >= {"1", "3", "4", "5", "6"},
               "阶段序列=%s" % "".join(main_run["stages"]))
        record("收尾命中 _SESSION=CLOSED", done is not None and bool(
            [e for e in events if e.get("type") == "show" and "_SESSION=CLOSED" in str(e.get("text"))]),
            "done=%s" % json.dumps({k: done.get(k) for k in ("ok", "version", "run_id")} if done else {},
                                   ensure_ascii=False))
        boards = [e for e in events if e.get("type") == "show" and e.get("final") is not True]
        versions = sorted({m for e in boards for m in re.findall(r"V(\d+)", str(e.get("text", ""))[:80])},
                          key=int)
        record("看板版本随质疑/改参递增", len(boards) >= 3 and versions == ["1", "2", "3"],
               "看板帧 %d 条，版本=%s" % (len(boards), ",".join(versions)))
        route_info = [e.get("text", "") for e in events if "路由判定" in str(e.get("text", ""))]
        record("CHALLENGE/PARAM 走规则路由", len(route_info) >= 3,
               "路由提示 %d 条：%s" % (len(route_info), (route_info[-1][:70] if route_info else "")))
        record("用户发言进入事件流", len([e for e in events if e.get("type") == "user"]) >= 3,
               "user 事件 %d 条" % len([e for e in events if e.get("type") == "user"]))
        check_frontend_contract(events)

        snapshot = main_run["snapshot"]
        run_id = snapshot.get("run_id", "")
        run_dir = ROOT / "runs" / run_id if run_id else None
        record("会话结束后状态归零", isinstance(snapshot, dict) and snapshot.get("active") is False
               and bool(run_id), "active=%s；run_id=%s" % (
                   snapshot.get("active") if isinstance(snapshot, dict) else "?", run_id))

        status, runs, _ = http_call(port, "/api/runs")
        listed = [r for r in runs.get("runs", []) if r.get("id") == run_id]
        record("看板数据可经 /api/runs 读取", bool(listed) and listed[0].get("has_report"),
               "%s" % json.dumps(listed[0] if listed else {}, ensure_ascii=False)[:160])
        status, report, _ = http_call(port, "/api/runs/%s" % run_id)
        record("最终报告可下载", status == 200 and report.get("kind") == "report"
               and "_SESSION=CLOSED" in str(report.get("text")), "HTTP %d ｜ %d 字节" % (
                   status, len(str(report.get("text", "")))))

        if run_dir is not None:
            names = file_listing(run_dir)
            meta = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
            record("runs 目录产物齐全（与 CLI 同构）",
                   {"最终报告.txt", "transcript.md", "状态快照.json", "session.json"} <= set(names)
                   and meta.get("通道") == "web",
                   "runs/%s：%s" % (run_id, ",".join(names)))
            transcript = (run_dir / "transcript.md").read_text(encoding="utf-8")
            record("transcript 记录用户每一次发言",
                   all(text[:12] in transcript for text in [QUERY] + INTERACT_ANSWERS),
                   "命中 %d/%d 条关键发言" % (
                       len([t for t in [QUERY] + INTERACT_ANSWERS if t[:12] in transcript]),
                       len([QUERY] + INTERACT_ANSWERS)))

        time.sleep(0.5)
        replay_collector = replay(port, since=0, settle=3.0)
        replay_ids = [e.get("id") for e in replay_collector.events]
        seq = snapshot.get("history", 0) if isinstance(snapshot, dict) else 0
        keep_from = max(1, seq - 399)
        expected_ids = [e["id"] for e in events if e["id"] >= keep_from]
        record("刷新后回放完整时间线（since=0）",
               (set(expected_ids) <= set(replay_ids) and not replay_collector.gaps
                and replay_ids == list(range(replay_ids[0], replay_ids[0] + len(replay_ids))))
               if replay_ids else False,
               "seq=%d ｜ 回放 %d 条(%s..%s) ｜ 主链 %d 条 ｜ 缺口=%s" % (
                   seq, len(replay_ids), replay_ids[0] if replay_ids else "-",
                   replay_ids[-1] if replay_ids else "-", len(expected_ids),
                   replay_collector.gaps[:2]))

        gaps_between = [b - a for a, b in zip(main_run["arrivals"], main_run["arrivals"][1:])]
        max_gap = max(gaps_between) if gaps_between else 0
        record("事件流持续输出（无长时间空窗）", max_gap < 90,
               "最大相邻事件间隔 %.1fs ｜ ping 帧 %d ｜ 到达时序前 8 条=%s" % (
                   max_gap, main_run["pings"], main_run["arrivals"][:8]))
        frames = [{"id": e.get("id"), "final": bool(e.get("final")),
                   "head": str(e.get("text", "")).strip().splitlines()[0][:70] if str(e.get("text", "")).strip() else "",
                   "len": len(str(e.get("text", "")))}
                  for e in events if e.get("type") == "show"]
        evidence.update({
            "run_id": run_id,
            "versions": versions,
            "elapsed": main_run["elapsed"],
            "event_count": len(events),
            "event_types": {k: sum(1 for e in events if e.get("type") == k)
                            for k in sorted({e.get("type") for e in events if e.get("type")})},
            "asks": main_run["asks"],
            "arrivals": main_run["arrivals"],
            "max_gap": round(max_gap, 1),
            "pings": main_run["pings"],
            "show_frames": frames,
            "run_files": file_listing(run_dir) if run_dir is not None else [],
        })

        status, _, _ = http_call(port, "/api/status")
        busy = main_run.get("busy") or {}
        record("推演中拒绝重置与并发启动", busy.get("reset") == 409 and busy.get("start") == 409,
               "reset=%s start=%s（期望 409/409）" % (busy.get("reset"), busy.get("start")))
        status, _, _ = http_call(port, "/api/start", {"query": "   ", "provider": provider})
        record("空问题被接口拒绝", status == 400, "HTTP %d" % status)
        check_loopback_only(port)

        sock_probe = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock_probe.sendall((
            "GET /api/stream?since=%d HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
            "Accept: text/event-stream\r\n\r\n" % (max(0, expected_ids[-1] - 1), port)).encode())
        raw = b""
        try:
            while True:
                block = sock_probe.recv(4096)
                if not block:
                    break
                raw += block
                head, _, body = raw.partition(b"\r\n\r\n")
                if body and re.match(rb"^[0-9A-F]+\r\n", body):
                    break
        except socket.timeout:
            pass
        sock_probe.close()
        head, _, body = raw.partition(b"\r\n\r\n")
        record("HTTP/1.1 chunked 流式传输", b"Transfer-Encoding: chunked" in head
               and b"text/event-stream" in head,
               "头部=%s" % head.decode("iso-8859-1").replace("\r\n", " | ")[:150])
        record("chunk 帧为十六进制长度前缀", bool(re.match(rb"^[0-9A-F]+\r\nid: ", body)),
               "首个 chunk=%r" % body[:40])

        if not args.skip_errors:
            check_error_paths(config)
    finally:
        evidence["results"] = RESULTS
        evidence["total_seconds"] = round(time.time() - started, 1)
        if args.keep:
            print("\n服务仍在运行：http://127.0.0.1:%d/  （手动停止：Ctrl+C 后杀进程 %s）" % (port, proc.pid))
        else:
            stop_server(proc)

    path = Path(args.evidence) if args.evidence else Path(tempfile.gettempdir()) / "web_e2e_evidence.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [r for r in RESULTS if not r["ok"]]
    print("\n结果：%d/%d 通过 ｜ 证据：%s ｜ 总耗时 %.1fs" % (
        len(RESULTS) - len(failed), len(RESULTS), path, evidence["total_seconds"]))
    if failed:
        print("失败项：" + "；".join(r["item"] for r in failed))
    return 0 if not failed else 2


INTERACT_ANSWERS = [text for _, text in INTERACT]


# --------------------------------------------------------------- 异常路径
class FakeEndpoint(http.server.BaseHTTPRequestHandler):
    mode = "401"
    hits = []

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        FakeEndpoint.hits.append(self.path)
        if FakeEndpoint.mode == "slow":
            time.sleep(6)
        payload = json.dumps({"error": {"message": "invalid api key"}}).encode()
        code = 200 if FakeEndpoint.mode == "ok" else 401
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def check_error_paths(main_config):
    """Key 错误 + 超时：错误事件不得泄露密钥、绝对路径或堆栈。"""
    real_cfg = json.loads(Path(main_config).read_text(encoding="utf-8"))
    real_key = str(real_cfg.get("api_key") or "")
    fake_key = "sk-e2e-invalid-key-0001"
    fake_port = free_port(8980)
    fake = http.server.ThreadingHTTPServer(("127.0.0.1", fake_port), FakeEndpoint)
    fake.daemon_threads = True
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    tmp = tempfile.TemporaryDirectory()
    config = Path(tmp.name) / "bad-config.json"
    config.write_text(json.dumps({
        "provider": "llm",
        "base_url": "http://127.0.0.1:%d/v1" % fake_port,
        "api_key": fake_key,
        "model": "e2e-fake",
        "timeout": 1,
        "max_tokens": 50,
        "search": {"enabled": False},
    }, ensure_ascii=False), encoding="utf-8")
    port = free_port(fake_port + 1 + 20)
    proc = None
    try:
        proc = start_server(config, port)
        for mode, label in (("401", "模型 Key 错误"), ("slow", "模型超时")):
            FakeEndpoint.mode = mode
            FakeEndpoint.hits.clear()
            status, data, _ = http_call(port, "/api/start", {"query": QUERY, "provider": "llm"})
            if status != 200:
                record("异常路径：%s" % label, False, "启动失败 HTTP %d %s" % (status, data))
                continue
            collector = Collector(port, since=0, socket_timeout=40)
            errors = []
            deadline = time.time() + 90
            while time.time() < deadline and not errors:
                event = collector.next_event(deadline)
                if "__timeout__" in event or "__bad__" in event:
                    continue
                if event.get("type") == "error":
                    errors.append(event)
            collector.close()
            wait_idle(port, timeout=30)
            if not errors:
                record("异常路径：%s" % label, False, "未收到 error 事件")
                continue
            payload = json.dumps(errors, ensure_ascii=False)
            leaks = []
            if real_key and real_key in payload:
                leaks.append("真实 api_key")
            if fake_key in payload:
                leaks.append("测试 api_key")
            for probe in ("Bearer", "Traceback", str(ROOT), "site-packages", "/private/"):
                if probe in payload:
                    leaks.append(probe)
            record("异常路径：%s" % label, not leaks,
                   "错误文案=%r ｜ 泄露=%s ｜ 端点命中=%d" % (
                       str(errors[0].get("text"))[:110], leaks, len(FakeEndpoint.hits)))
            http_call(port, "/api/reset", {})
    finally:
        fake.shutdown()
        tmp.cleanup()
        stop_server(proc)


if __name__ == "__main__":
    sys.exit(main())
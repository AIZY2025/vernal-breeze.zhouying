"""春风（个人决策沙盘 Agent Team）· 命令行入口。

python run.py                 交互模式（真实模型或预置剧本）
python run.py --demo          预置剧本全链路自动演示（20 万级家用车）
python run.py --demo-full     演示并额外跑通“补充背景 + 质疑正方”
python run.py --check         自检：提示词、配置、算术、路由
python run.py --paste a.json   粘贴档案直接进入推演组（降级预案）
"""
import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import board as render  # noqa: E402
from core import quant  # noqa: E402
from core.jsonx import ParseError  # noqa: E402
from core.llm import LLMError  # noqa: E402
from core.mock import build_archive, compute  # noqa: E402
from core.numbers_cn import parse_amount  # noqa: E402
from core.orchestrator import Sandbox, SandboxCancelled, SandboxError  # noqa: E402
from core.prompts import all_prompts  # noqa: E402
from core.router import _extract_params, classify  # noqa: E402
from core.settings import build_quant, build_research, build_team, load_config  # noqa: E402
from core.store import SessionStore  # noqa: E402

DEMO_QUERY = "我要不要买一台二十万级的家用车"
MAX_QUERY = 500
MAX_ANSWER = 2000
MAX_PASTE_BYTES = 1024 * 1024


def _safe_paste(raw):
    target = Path(raw).resolve()
    try:
        target.relative_to(ROOT)
    except ValueError:
        raise SystemExit("--paste 路径必须位于项目目录内")
    if not target.is_file():
        raise SystemExit("--paste 指向的文件不存在")
    if target.stat().st_size > MAX_PASTE_BYTES:
        raise SystemExit("--paste 文件超过 1 MB 上限")
    return target

DEMO_ANSWERS = [
    "三个月内要定下来，最晚春节前提车",
    "月收入大概1.15万，可动用存款120万，每月能存3000左右，最多能接受一次性支出20万",
    "除了买，也在考虑继续打车加周末租车，或者先暂缓；主要是通勤和周末带父母出行，最坏能接受存款降到100万左右",
    "我实际一年养车大概一万二，反方说的每年两万太高了",
    "我把可动用存款改成150万",
    "结束沙盘，给我结论",
]

DEMO_FULL_EXTRA = DEMO_ANSWERS[:3] + [
    "我实际一年养车大概一万二，反方说的每年两万太高了",
    "我把可动用存款改成150万",
    "补充一个情况：公司明年可能搬到南山，通勤距离缩短一半",
    "我质疑正方第一条论据，搬家后通勤距离缩短，效率提升没那么大",
    "结束沙盘，给我结论",
]


class ConsoleUI:
    def __init__(self, demo_answers=None, fast=False):
        self.queue = list(demo_answers or [])
        self.fast = fast

    def _pause(self, seconds: float):
        if not self.fast:
            time.sleep(seconds)

    def agent(self, name, tag, text):
        print("\n" + "═" * 62)
        print("【%s】%s" % (name, tag))
        print("─" * 62)
        print(text)
        self._pause(0.4)

    def info(self, text):
        print("\n· " + text)

    def show(self, text, final=False):
        print("\n" + text)
        self._pause(0.6)

    def ask(self, prompt, echo=True):
        if self.queue:
            answer = self.queue.pop(0)
            if echo:
                print("\n" + prompt)
            print("你（预置）> " + answer)
            self._pause(0.9)
            return answer
        try:
            if echo:
                print("\n" + prompt)
            return input("你 > ").strip()[:MAX_ANSWER]
        except EOFError:
            return "结束沙盘"



def run_check(cfg: dict) -> int:
    ok = True
    print("春风 · 自检")
    print("─" * 62)
    try:
        prompts = all_prompts()
        for key, text in prompts.items():
            print("[提示词] %s：%d 字 %s" % (key, len(text), "OK" if len(text) > 500 else "过短"))
            ok = ok and len(text) > 500
    except Exception as exc:
        print("[提示词] 失败：%s" % exc)
        ok = False

    cases = [("一百五十万", 1500000), ("1.15万", 11500), ("一万二", 12000),
             ("3000", 3000), ("二十万", 200000), ("每年两万", 20000)]
    for text, expect in cases:
        got = parse_amount(text)
        flag = abs(got - expect) < 1e-6
        print("[金额解析] %s → %s %s" % (text, int(got), "OK" if flag else "不符（期望 %s）" % expect))
        ok = ok and flag

    param_cases = [
        ("我把可动用存款改成150万", {"cash": 1500000}),
        ("我养车每年1.5万", {"monthly_delta": 1250}),
        ("月收入1.5万，存款不变", {"monthly_income": 15000}),
    ]
    for text, expect in param_cases:
        got = _extract_params(text)
        flag = got == expect
        print("[参数抽取] %s → %s %s" % (text, got, "OK" if flag else "不符（期望 %s）" % expect))
        ok = ok and flag

    archive = build_archive("要不要买一台二十万级的家用车",
                            "答：三个月内要定下来 / 答：月收入1.15万，存款120万，月存3000，一次性支出20万")
    try:
        result = compute(archive, 1650)
        print("[量化算术] 可支撑 %s 个月，边界 %s，阈值 %s %s" % (
            result["cashflow"]["months"], result["cashflow"]["boundary"],
            result["thresholds"][0]["value"], result["thresholds"][1]["value"]))
        print("[量化参数] 应急金 %d 个月 ｜ 敏感性 ±%d%% ｜ 机会成本 %.1f%%～%.1f%%" % (
            int(quant.PARAMS["emergency_months"]),
            int(round(quant.PARAMS["sensitivity_pct"] * 100)),
            quant.PARAMS["opportunity_low"] * 100,
            quant.PARAMS["opportunity_high"] * 100))
    except Exception as exc:
        print("[量化算术] 失败：%s" % exc)
        ok = False

    routes = [("结束沙盘，给我结论", "CLOSE"), ("反方说的养车成本每年两万太高了", "CHALLENGE_CON"),
              ("我质疑正方第一条论据", "CHALLENGE_PRO"), ("我把存款改成150万", "PARAM"),
              ("补充一个情况：公司明年搬到南山", "NEW_FACT"), ("今天天气不错", "UNKNOWN")]
    for text, expect in routes:
        got = classify(text).intent
        flag = got == expect
        print("[路由] %s → %s %s" % (text, got, "OK" if flag else "不符（期望 %s）" % expect))
        ok = ok and flag

    key = cfg.get("api_key") or os.environ.get(cfg.get("api_key_env", "SANDBOX_API_KEY"), "")
    print("[模型] %s" % ("已配置，可切换真实模型" if key else "未配置，默认预置剧本模式"))
    conf = cfg.get("search")
    if not isinstance(conf, dict) or conf.get("enabled") is False:
        print("[外部情报] 未配置 search 段，情报层跳过")
    elif str(conf.get("mode") or "auto").lower() == "off":
        print("[外部情报] 已关闭（search.mode=off）")
    else:
        search_key = conf.get("api_key") or os.environ.get(
            conf.get("api_key_env", "TENCENTCLOUD_WSA_APIKEY"), "")
        secret_id = conf.get("secret_id") or os.environ.get("TENCENTCLOUD_SECRET_ID", "")
        secret_key = conf.get("secret_key") or os.environ.get(
            conf.get("secret_key_env", "TENCENTCLOUD_SECRET_KEY"), "")
        if secret_id and secret_key:
            print("[外部情报] 已配置（SecretId/SecretKey 签名方式）")
        elif search_key:
            print("[外部情报] 已配置（服务 API KEY 方式）")
        else:
            print("[外部情报] 未配置密钥，情报层将跳过（不影响推演）")
    print("─" * 62)
    print("自检结果：%s" % ("全部通过" if ok else "存在失败项，请先修正"))
    return 0 if ok else 1


def _write_snapshot(store, sandbox, mode, stopped=False):
    store.save_json("状态快照.json", sandbox.snapshot(mode, stopped))


def _print_stats(team):
    stats = team.stats() if hasattr(team, "stats") else {"calls": 0, "seconds": 0.0}
    if stats.get("calls"):
        print("模型调用 %d 次，累计 %.1f 秒" % (stats["calls"], stats["seconds"]))


def main():
    parser = argparse.ArgumentParser(description="春风 · 遇事不决，可问春风")
    parser.add_argument("--demo", action="store_true", help="预置剧本全链路演示")
    parser.add_argument("--demo-full", action="store_true", help="演示并额外跑通补充背景与质疑正方")
    parser.add_argument("--fast", action="store_true", help="去掉演示停顿")
    parser.add_argument("--check", action="store_true", help="自检")
    parser.add_argument("--paste", metavar="ARCHIVE_JSON", help="直接载入档案进入推演组")
    parser.add_argument("--provider", choices=["auto", "llm", "mock"], help="覆盖 config 的 provider")
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="配置文件路径")
    parser.add_argument("--query", help="直接给出决策问题，跳过开场输入")
    parser.add_argument("--debug", action="store_true", help="内部错误时打印完整堆栈")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    build_quant(cfg)
    if args.check:
        return run_check(cfg)

    team, mode = build_team(cfg, args.provider or ("mock" if (args.demo or args.demo_full) else None))
    research = build_research(cfg, ROOT / "cache" / "search")

    if args.demo or args.demo_full:
        answers = DEMO_FULL_EXTRA if args.demo_full else DEMO_ANSWERS
        ui = ConsoleUI(demo_answers=answers, fast=args.fast)
        query = DEMO_QUERY
    else:
        ui = ConsoleUI(fast=args.fast)
        query = args.query
        if query:
            query = query.strip()[:MAX_QUERY]
        if not query:
            print("春风 ｜ 五智能体协作推演 · 遇事不决，可问春风")
            print("─" * 62)
            print("用一句话说出你正在纠结的决策，例如：我要不要买一台二十万级的家用车。")
            query = input("\n你 > ").strip()[:MAX_QUERY]
            if not query:
                print("没有输入决策问题，退出。")
                return 1

    store = SessionStore(ROOT)
    store.meta(provider=mode, query=query)
    sandbox = Sandbox(team, ui, store, demo=bool(args.demo or args.demo_full),
                      research=research)
    print("\n运行模式：%s ｜ 记录目录：runs/%s" % (mode, store.dir.name))

    try:
        if args.paste:
            archive = json.loads(_safe_paste(args.paste).read_text(encoding="utf-8"))
            if not isinstance(archive, dict):
                raise SystemExit("--paste 文件顶层必须是 JSON 对象")
            sandbox.run_paste(archive, query)
        else:
            sandbox.run(query)
            if sandbox.refused:
                print("\n未进入沙盘：%s" % sandbox.refuse_reason)
                print("本沙盘只处理「要不要 / 该不该 / 选哪个」这类个人决策。")
                store.finish({"状态": "未进入推演", "决策问题": query,
                              "原因": sandbox.refuse_reason})
                return 0
    except SandboxCancelled:
        _print_stats(sandbox.team)
        print("\n已停止推演：不生成最终报告，已产出的档案与看板保留在 runs/%s。" % store.dir.name)
        _write_snapshot(store, sandbox, mode, stopped=True)
        store.finish({"状态": "已停止", "决策问题": query,
                      "最终看板版本": ("V%d" % sandbox.version) if sandbox.version else ""})
        return 0
    except SandboxError as exc:
        print("\n链路中止：%s" % exc)
        store.finish({"状态": "中止", "原因": str(exc)})
        return 2
    except LLMError as exc:
        print("\n模型调用失败：%s" % exc)
        print("可先用 python run.py --demo 走预置剧本，或检查 config.json。")
        store.finish({"状态": "中止", "原因": str(exc)})
        return 2
    except ParseError as exc:
        print("\n模型输出不符合 JSON 契约：%s" % exc)
        store.finish({"状态": "中止", "原因": str(exc)})
        return 2
    except Exception as exc:
        print("\n内部错误：%s" % exc)
        if args.debug:
            traceback.print_exc()
        else:
            print("如需完整堆栈排查，重跑时加 --debug。")
        store.finish({"状态": "中止", "原因": str(exc)})
        return 2
    except KeyboardInterrupt:
        print("\n已停止推演。")
        _write_snapshot(store, sandbox, mode, stopped=True)
        store.finish({"状态": "已停止", "决策问题": query,
                      "最终看板版本": ("V%d" % sandbox.version) if sandbox.version else ""})
        return 130

    _print_stats(sandbox.team)
    _write_snapshot(store, sandbox, mode)
    store.finish({"运行模式": mode, "决策问题": query, "最终看板版本": "V%d" % sandbox.version})
    print("\n全轮次记录已保存：runs/%s" % store.dir.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())

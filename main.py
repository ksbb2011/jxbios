"""命令行入口：不依赖 GUI 也能跑完整流程（远程桌面/无人值守场景）。

用法：
    python main.py check                      配置与设备自检（不动作）
    python main.py routes "A路线：苏州-广州"    解析路线并写入 config/routes.json
    python main.py scan                        抓一屏列表并打印命中（调试切卡与字段）
    python main.py run                         完整流程：设置路线 → 扫描抢单
    python main.py run --no-setup              跳过路线设置，直接开始监听

所有参数都在 config/ 里，命令行只管「做什么」，不管「怎么做」。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# 后台运行（stdout 重定向到文件）时，Python 默认按 GBK 编码 stdout；
# 日志里设备返回的乱码字符（\ufffd）会触发 UnicodeEncodeError 使进程崩溃。
# 统一强制 UTF-8 + errors=replace，保证无人值守/后台跑日志不崩。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.config_store import ConfigStore, ConfigError  # noqa: E402
from core.domain.route_plan import RoutePlanError, parse_routes_text  # noqa: E402

# 本轮日志文件（run 时设置）。循环测试要靠它逐轮分析——终端滚过去就没了。
_LOG_FILE: Optional[Path] = None


def _log(level: str, msg: str) -> None:
    prefix = {"info": "[信息]", "warning": "[警告]", "error": "[错误]", "debug": "[调试]"}.get(
        level, "[信息]"
    )
    line = f"{prefix} {msg}"
    print(line, flush=True)
    if _LOG_FILE is not None:
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        except OSError:
            pass  # 落盘失败绝不能影响运行


def cmd_check(args: argparse.Namespace) -> int:
    """自检：依赖完整性 → 配置校验 → 模板完整性 → 设备连接。"""
    from tools.check_deps import check_deps
    if check_deps(install=getattr(args, "install_deps", False)) != 0:
        print("[自检] 依赖不完整：OCR 类缺失仅影响字段识别（降级不崩溃），"
              "PyQt5 缺失将导致图形界面无法启动。建议补齐后再继续。\n")
    try:
        store = ConfigStore(strict=True)
    except ConfigError as exc:
        print(f"[错误] 配置校验失败:\n{exc}")
        return 1
    print(store.describe())

    missing = [n for n in _all_template_names(store) if not store.template(n).exists()]
    if missing:
        print(f"[警告] 缺失模板 {len(missing)} 个: {', '.join(missing)}")

    if args.no_device:
        return 0
    from core.devices.frame_source import create_frame_source

    source_name = store.get("vision", "capture.source", "camera")
    source = create_frame_source(store, log=_log)
    ok_src = source.open()
    source.close()
    print(f"[信息] 取帧源({source_name}): {'可用' if ok_src else '不可用'}")

    from core.devices.arm import RobotArm

    arm = RobotArm(store, log=_log)
    ok_arm = arm.open(restart_service=not args.no_restart)
    arm.close()
    print(f"[信息] 机械臂: {'可用' if ok_arm else '不可用'}")
    return 0 if (ok_src and ok_arm) else 2


def _all_template_names(store: ConfigStore) -> list:
    raw = store.section("templates").get("items") or {}
    return [n for n, item in raw.items() if item.get("enabled", True)]


def cmd_routes(args: argparse.Namespace) -> int:
    """解析粘贴的路线文本并写入 config/routes.json。"""
    store = ConfigStore(strict=True)
    from core.domain.regions import RegionStore

    regions = RegionStore(store)
    text = args.text or sys.stdin.read()
    try:
        plan = parse_routes_text(text, regions, max_stops_per_side=args.max_stops)
    except RoutePlanError as exc:
        print(f"[错误] {exc}")
        return 1
    from core.domain.route_plan import validate_plan

    for warn in validate_plan(plan, regions):
        print(f"[警告] {warn}")
    print(plan.describe())
    if args.save:
        path = store.config_dir() / "routes.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(plan.as_dict())
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[信息] 已写入 {path}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """抓一屏列表，打印切卡与字段探测结果（不抢单、不点击）。"""
    from core.flow.context import FlowContext
    from core.flow.probe_shared import build_kit
    from core.flow.scanner import ListScanner

    store = ConfigStore(strict=True)
    kit, fs = build_kit(store, args.image)
    res = kit.detect(fs)
    print(f"页面判定: {res.summary()}")

    from core.domain.capacity import Capacity
    from core.domain.regions import RegionStore
    from core.domain.rules import RuleEngine

    ctx = FlowContext(
        store=store,
        source=kit.source,  # type: ignore[arg-type]  只读扫描，取帧源为 None
        arm=kit.arm,  # type: ignore[arg-type]
        kit=kit,
        regions=RegionStore(store),
        rules=RuleEngine(store),
        capacity=Capacity.from_store(store),
        log=_log,
    )
    hits = ListScanner(ctx).scan_once(fs)
    print(f"命中 {len(hits)} 张")
    for card in hits:
        print(f"  - {card.describe()} 指纹={card.fingerprint} 点击点={card.click_point}")
    return 0


def _write_run_summary(store: ConfigStore, task, started: float, log_file: Optional[Path]) -> None:
    """本轮结束后写结构化摘要（供逐轮分析：哪里卡、命中多少、是否监护触发）。"""
    if log_file is None:
        return
    ctx = task.ctx
    try:
        wd = ctx.watchdog.describe() if getattr(ctx, "watchdog", None) is not None else ""
    except Exception:
        wd = ""
    data = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
        "ended_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - started, 1),
        "route": ctx.plan.current.describe() if (ctx.plan and ctx.plan.current) else "",
        "route_index": ctx.plan.current_index if ctx.plan else None,
        "routes_total": len(ctx.plan.routes) if ctx.plan else 0,
        "stop_reason": getattr(ctx, "stop_reason", ""),
        "watchdog": wd,
        "counters": dict(ctx.counters),
        "summary": task.summary(),
    }
    path = Path(log_file).with_suffix(".summary.json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[信息] 本轮摘要已保存: {path}")


def _maybe_advance_route(task, args: argparse.Namespace) -> None:
    """本轮「定时收工」才自动切下一条路线并落盘，使每轮指令完全固定。

    循环测试的流程是「跑一轮 → 分析 → 优化 → 再跑」，每轮指令必须一样，路线由程序
    自己往前推（A→B→C→D→A）。若本轮是异常/人工停止，则**停在原路线**下轮重跑，
    避免把没测明白的路线跳过去。
    """
    if args.route:
        return  # 显式指定路线时不自动前进
    reason = getattr(task.ctx, "stop_reason", "") or ""
    if "round_time_limit" not in reason:
        _log("info", "本轮非定时收工（异常/人工停止），下轮仍跑同一条路线")
        return
    plan = task.plan
    if plan is None or not plan.routes:
        return
    if plan.advance() is None:
        plan.reset()
        _log("info", f"所有路线已跑完，下轮回到第 1 条：{plan.current.describe()}")
    else:
        _log("info", f"下轮自动切到：{plan.current.describe()}")
    path = task.store.config_dir() / "routes.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(plan.as_dict())
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_run(args: argparse.Namespace) -> int:
    """跑完整流程。"""
    global _LOG_FILE
    from core.config_store import DATA_DIR
    from core.flow.task import RobotTask

    store = ConfigStore(strict=True)

    # 每轮一个日志文件：循环测试要逐轮对比，终端滚过去就没了
    log_dir = Path(DATA_DIR) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = log_dir / f"run_{time.strftime('%Y%m%d_%H%M%S')}.log"
    print(f"[信息] 本轮日志: {_LOG_FILE}")

    task = RobotTask(store, log=_log, max_run_sec=(args.max_run_sec or None))

    # 清理上一轮残留的停止标志：stop_run.py 写的标志可能因上轮进程已退出而
    # 没被消费，残留会导致本轮一启动就被「外部停止标志」立刻停掉。
    # 2026-09-12 实测：残留 stop.flag 让 GUI「开始」后主循环 3s 即停。
    _stop_flag = Path(DATA_DIR) / "stop.flag"
    if _stop_flag.exists():
        _stop_flag.unlink(missing_ok=True)
        print("[信息] 已清理上一轮残留的停止标志")

    # 指定本轮跑哪条路线（多路线轮流测试用）
    if args.route:
        want = str(args.route).strip().upper()
        idx = next(
            (
                i
                for i, r in enumerate(task.plan.routes)
                if str(getattr(r, "name", "")).upper() == want
            ),
            None,
        )
        if idx is None:
            names = [str(getattr(r, "name", "")) for r in task.plan.routes]
            print(f"[错误] 未找到路线 {args.route}；可选用: {names}")
            return 2
        task.plan.current_index = idx
        _log("info", f"本轮指定路线: {task.plan.current.describe()}")

    if args.max_run_sec:
        _log("info", f"本轮限时 {args.max_run_sec}s（到点自动收工并输出摘要）")

    print("[信息] 正在打开设备…")
    if not task.open():
        print("[错误] 设备打开失败")
        return 2
    print("[信息] 设备就绪，开始运行（Ctrl+C 停止）")
    started = time.time()
    try:
        task.run(setup_first=not args.no_setup)
    except KeyboardInterrupt:
        print("\n[信息] 收到中断，停止中…")
        task.stop()
    finally:
        task.close()
    print(f"[信息] 运行结束 | {task.summary()}")
    _write_run_summary(store, task, started, _LOG_FILE)
    _maybe_advance_route(task, args)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="抢单机器人命令行入口")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="配置与设备自检")
    p_check.add_argument("--no-device", action="store_true", help="只校验配置，不连设备")
    p_check.add_argument("--no-restart", action="store_true", help="开串口前不重启服务")
    p_check.add_argument("--install-deps", action="store_true", help="依赖缺失时自动 pip 安装")
    p_check.set_defaults(func=cmd_check)

    p_routes = sub.add_parser("routes", help="解析路线文本")
    p_routes.add_argument("text", nargs="?", help="路线文本，不给则从标准输入读")
    p_routes.add_argument("--save", action="store_true", help="写入 config/routes.json")
    p_routes.add_argument("--max-stops", type=int, default=3, help="每侧最多选择数")
    p_routes.set_defaults(func=cmd_routes)

    p_scan = sub.add_parser("scan", help="扫描当前列表页一屏（只读）")
    p_scan.add_argument("--image", default="", help="用截图代替真机取帧")
    p_scan.set_defaults(func=cmd_scan)

    p_run = sub.add_parser("run", help="跑完整流程")
    p_run.add_argument("--no-setup", action="store_true", help="跳过路线设置直接监听")
    p_run.add_argument(
        "--max-run-sec",
        type=int,
        default=0,
        help="本轮最长运行秒数，到点自动收工并输出摘要（0=不限，默认取配置 1200）",
    )
    p_run.add_argument("--route", default="", help="指定本轮跑哪条路线（按路线名，如 A/B/C/D）")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())

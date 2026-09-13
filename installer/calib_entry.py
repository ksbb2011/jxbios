"""校准工具（打包版）—— 控制台入口：把 GUI 之外的所有标定 / 探活工具收进一个菜单。

为什么是这个结构（而不是把 tools/*.py 编译成 exe）：
    标定工具的代码还在演进（2026-09-13 一天内就改了三处判据）。如果把它们编译进
    exe，每次调判据都要重新打包（5~15 分钟）。所以这里反过来做：
        * tools/*.py **作为普通文件**随包拷到 exe 同级（dist\\tools\\），保持可编辑；
        * 本入口只负责「选工具 → 摆好 argv / cwd / sys.path → exec 那个文件」；
        * 用 runpy.run_path 跑磁盘上的真实文件 —— 改完存盘即生效，**无需重新打包**。
    还有一条硬原因：所有工具都用 Path(__file__).parent.parent 当项目根，并读写
    config/hardware.json、data/... 这些相对路径。只有文件真的躺在 <exe目录>\\tools\\
    下，__file__ 推算出来的根才是对的（塞进 _internal 会算成 _internal，全盘错位）。

用法：
    双击 校准工具.exe                     -> 菜单（推荐，页面由人工摆好再选目标）
    校准工具.exe tap                      -> 直接跑 UI 落点校准（它自带菜单）
    校准工具.exe tap --list               -> 参数透传给工具（传了就以你的为准）
    校准工具.exe ink --verify-only        -> 映射复检
    校准工具.exe grid --dry               -> 整屏网格标定：只验手机连通性（不驱臂）
    校准工具.exe gridselftest             -> 整屏网格：离线自测（假手机+假机械臂）
    校准工具.exe run calibrate_z_press.py -> 万能通道：跑 tools 下任意脚本

权限：本入口**不提权** —— 标定要反复跑，每次都弹 UAC 太折磨；真正需要管理员的动作
    （重启 JxbService 释放被占的 COM4）由工具内部自己 Start-Process -Verb RunAs 提权。
    若整包放在 Program Files 这类受保护目录、导致 config 写不进去，右键「以管理员身份运行」。
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import traceback
from pathlib import Path

# 冻结后 sys.executable = <dist>\校准工具.exe → 包根就是它所在目录；
# 源码运行（py -3.11 installer\calib_entry.py）时用本文件的上上级目录。
APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
TOOLS_DIR = APP_DIR / "tools"
HARDWARE_JSON = APP_DIR / "config" / "hardware.json"

# 菜单项 key -> (脚本文件名, 菜单文字, 不带附加参数时的固定参数)
TOOLS: dict[str, tuple[str, str, list[str]]] = {
    "home": ("arm_home.py", "机械臂归位（救急：卡住 / 串口被占）", []),
    "scanz": (
        "calibrate_arm_ink.py",
        "测下压深度 z_press（换笔、换手机后必做）",
        # ⚠️ 三个值都是 2026-09-13 实测校正过的，别改回旧值：
        #   --z-start 4.8：旧值 5.5 一上来就触发 → 扫不到真实最低点，算出偏浅的 z_press
        #   --z-max   6.2：旧值 8.0 超过 Z 硬上限，工具会直接拒绝执行
        #   （上浮值在下面 _menu_args 里问，默认 0.3；旧默认 1.5 会算出 ≈7.1，是历史事故）
        ["--scan-z", "--z-start", "4.8", "--z-max", "6.2", "--z-step", "0.1"],
    ),
    "ink": ("calibrate_arm_ink.py", "跑映射标定（墨点版，3~10 分钟）", []),
    "verify": (
        "calibrate_arm_ink.py",
        "复检映射精度（只验证，不重新标定）",
        ["--verify-only"],
    ),
    "probe": (
        "calibrate_arm_ink.py",
        "单步探针（戳一下，看能否检测出墨点）",
        ["--probe-one"],
    ),
    "imouse": (
        "imouse_probe.py",
        "iMouse 探活与实测（版本 / 端口 / 帧率 / 插件）",
        ["--repeat", "30"],
    ),
    "tap": (
        "calibrate_tap_ui.py",
        "UI 落点校准（补墨点覆盖不到的区域）",
        # 必须显式 --menu：该工具不带参数时只打印目标清单就退出，
        # 交互菜单只在 --menu 下才起来（源码侧的 界面按钮标定.bat 也是这么调的）。
        ["--menu"],
    ),
    # ---- 整屏网格标定（2026-09-13 新增）：手机 App 当传感器，整屏 45 点自动测
    "gridcheck": (
        "calibrate_full_grid.py",
        "整屏网格 · 只验连通（--dry，不驱臂；先跑这个）",
        ["--dry"],
    ),
    "gridselftest": (
        "calib_mock_phone.py",
        "整屏网格 · 离线自测（假手机+假机械臂，不动真机）",
        [],
    ),
    "grid": (
        "calibrate_full_grid.py",
        "整屏网格标定（整轮测量，约 6 分钟；默认只测不写配置）",
        # 默认不带 --verify：第一轮先看数据合不合理；确认后到命令行加
        # `--verify --apply` 复压验收并写配置。
        [],
    ),
}

# 菜单顺序（key 列表）
# 8/9/10 是 2026-09-13 新增的整屏网格标定三件套：先 gridcheck 验连通 → gridselftest
# 离线自测（不动真机）→ grid 正式跑一轮（默认只测量不写配置）。
MENU_ORDER = ["home", "scanz", "ink", "verify", "probe", "imouse", "tap",
              "gridcheck", "gridselftest", "grid"]


# --------------------------------------------------------------------- 小工具
def _read_z_press() -> float | None:
    """读当前 z_press（只读，用于菜单显示与默认值）。读不到返回 None。"""
    try:
        data = json.loads(HARDWARE_JSON.read_text(encoding="utf-8"))
        val = (data.get("calibration") or {}).get("z_press")
        return float(val) if val is not None else None
    except Exception:  # noqa: BLE001 - 只是显示用，读不到不影响主流程
        return None


def _has_actuation() -> bool:
    try:
        data = json.loads(HARDWARE_JSON.read_text(encoding="utf-8"))
        return bool((data.get("calibration") or {}).get("actuation"))
    except Exception:  # noqa: BLE001
        return False


class _Eof:
    """输入流结束的哨兵。

    为什么需要：用管道 / 重定向喂输入时（例如自动化验证 `echo 0 | 校准工具.exe tap`），
    input() 会抛 EOFError。如果只是"返回默认值"，菜单里那个空字符串会被当成
    "无效输入" → 无限循环刷屏。所以这里用哨兵显式区分"用户按了回车"与"没有输入了"。
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "<EOF>"


EOF = _Eof()


def _prompt(text: str, default: str = "") -> str:
    """交互输入。正常返回字符串；EOF / Ctrl+C 返回 EOF 哨兵（不抛）。"""
    tip = f"{text}（回车用 {default}）: " if default else f"{text}: "
    try:
        raw = input(tip).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return EOF  # type: ignore[return-value]
    return raw or default


def _float_prompt(text: str, default: float) -> float:
    raw = _prompt(text, str(default))
    if not isinstance(raw, str):
        return float(default)
    try:
        return float(raw)
    except ValueError:
        print(f"  （{raw!r} 不是数字，改用默认 {default}）")
        return float(default)


def _int_prompt(text: str, default: int) -> int:
    raw = _prompt(text, str(default))
    if not isinstance(raw, str):
        return int(default)
    try:
        return int(float(raw))
    except ValueError:
        print(f"  （{raw!r} 不是整数，改用默认 {default}）")
        return int(default)


# --------------------------------------------------------------------- 执行
def run_tool(script: str, extra: list[str]) -> int:
    """跑 tools/<script>，参数为 extra。返回退出码（异常也转成码，不往外抛）。"""
    path = TOOLS_DIR / script
    if not path.is_file():
        print(f"[错误] 找不到工具脚本：{path}")
        print(f"       包根是否完整？tools\\ 必须与 校准工具.exe 同级。")
        return 2

    os.chdir(APP_DIR)  # 工具大量使用 config/... data/... 这类相对路径
    sys.path.insert(0, str(APP_DIR))  # 让 `import tools.*` / `import core` 都能解析
    sys.argv = [str(path), *extra]

    print()
    print("=" * 70)
    print(f">>> {path.name} {' '.join(extra)}".rstrip())
    print("=" * 70)
    try:
        runpy.run_path(str(path), run_name="__main__")
        return 0
    except SystemExit as exc:  # 工具正常 sys.exit(code)
        return int(exc.code or 0)
    except KeyboardInterrupt:
        print("\n[中断] 已交由工具自身的退出保护收尾（抬笔 / 复位 / 释放串口）")
        return 130
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，把真实堆栈打给用户
        print(f"\n[失败] {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1


def _menu_args(key: str) -> list[str] | None:
    """菜单模式下按需向用户问参数。返回 None 表示用户取消。"""
    _script, _desc, fixed = TOOLS[key]
    if key == "scanz":
        # ⚠️ 目标是让算出来的 z_press 落在 5.9~6.0（实测 5.8 不触发；6.2 是碎屏红线）。
        #    旧默认 1.5 会算出 min_z+1.5≈7.1 —— config/hardware.json 的 _note_z_press
        #    记录的"历史错误"就是这个，故默认改 0.3。
        up = _float_prompt("上浮值(建议 0.3；目标 z_press 5.9~6.0)", 0.3)
        return [*fixed, "--up-float", str(up)]
    if key == "ink":
        cur = _read_z_press()
        zp = _float_prompt("z_press（下压深度）", cur if cur is not None else 5.9)
        grid = _int_prompt("网格密度(6=36点约3分钟 / 8=64点约10分钟)", 8)
        rounds = _int_prompt("校准轮数(1=快 / 2=更准)", 2)
        return ["--z-press", str(zp), "--grid", str(grid), "--rounds", str(rounds),
                "--auto-restart"]
    if key == "probe":
        cur = _read_z_press()
        zp = _float_prompt("z_press（下压深度）", cur if cur is not None else 5.9)
        return ["--probe-one", "--z-press", str(zp)]
    return list(fixed)


def _print_status() -> None:
    zp = _read_z_press()
    print(f"  包根      = {APP_DIR}")
    print(f"  tools     = {TOOLS_DIR}  ({'存在' if TOOLS_DIR.is_dir() else '★缺失'})")
    print(f"  z_press   = {zp if zp is not None else '未标定'}")
    print(f"  actuation = {'已标定' if _has_actuation() else '未标定'}")


def menu() -> int:
    """交互菜单：页面由人工摆好，选一项就跑一轮。"""
    while True:
        print()
        print("=" * 70)
        print("    校准工具    robot_order_picker_imouse")
        print("=" * 70)
        print("  [当前状态]")
        _print_status()
        print()
        print("-" * 70)
        for idx, key in enumerate(MENU_ORDER, start=1):
            print(f"  {idx}. {TOOLS[key][1]}")
        print("  " + "-" * 66)
        print("  h. 工具清单 / 高级用法（直接给工具传参数）")
        print("  0. 退出")
        print("=" * 70)

        choice = _prompt("请输入数字后回车", "")
        if choice is EOF:  # 没有输入了（管道/重定向）→ 直接退出，别刷屏
            print("（输入结束）已退出。")
            return 0
        choice = choice.lower()
        if choice in ("0", "q", "quit", "exit"):
            print("已退出。")
            return 0
        if choice in ("h", "?", "--help"):
            _print_help()
            _prompt("按回车返回菜单", "")
            continue
        if not choice.isdigit() or not (1 <= int(choice) <= len(MENU_ORDER)):
            continue

        key = MENU_ORDER[int(choice) - 1]
        script, _, _ = TOOLS[key]
        args = _menu_args(key)
        if args is None:
            continue
        print()
        print(f"[{choice}] {TOOLS[key][1]}")
        if key in ("ink", "scanz", "probe"):
            print("  提示：机械臂会真的下压。手机请按该工具要求先摆好页面 / 打开画布。")
        if key == "scanz":
            print("  ⚠️ 若映射可能已失效（点不准 / 刚挪过机械臂），请**先做菜单 3 再回来测 Z**：")
            print("     否则笔会戳在画布外，界面变化会被误判成墨点，算出的 z_press 不可信。")
        print("  Ctrl+C 可随时中断（工具自带复位与释放串口保护）。")
        if _prompt("按回车开始", "") is EOF:
            return 0
        run_tool(script, args)
        if _prompt("（已结束）按回车返回菜单", "") is EOF:
            return 0


def _print_help() -> None:
    print()
    print("=" * 70)
    print("  校准工具 —— 用法")
    print("=" * 70)
    print("  双击 exe（或不带参数）          打开菜单")
    print("  校准工具.exe <key> [参数...]    直接跑某个工具（参数原样透传）")
    print("  校准工具.exe run <脚本名> [参数] 跑 tools\\ 下任意脚本")
    print()
    print("  key 一览：")
    for key in MENU_ORDER:
        script, desc, fixed = TOOLS[key]
        print(f"    {key:8s} -> {script:24s} {desc}")
        if fixed:
            print(f"    {'':8s}    默认参数: {' '.join(fixed)}")
    print()
    print("  示例：校准工具.exe tap --list")
    print("        校准工具.exe ink --z-press 5.9 --grid 8 --rounds 2")
    print()
    print("  tools\\ 目录下的全部脚本：")
    if TOOLS_DIR.is_dir():
        for p in sorted(TOOLS_DIR.glob("*.py")):
            print(f"    {p.name}")
    else:
        print(f"    ★目录不存在：{TOOLS_DIR}")
    print("=" * 70)


def main(argv: list[str] | None = None) -> int:
    try:  # 控制台可能被 chcp 936，中文输出不炸
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    args = list(sys.argv[1:] if argv is None else argv)

    if not args:
        return menu()
    if args[0] in ("-h", "--help", "help", "--list", "list", "?"):
        _print_help()
        return 0
    if args[0] == "run":
        if len(args) < 2:
            print("[错误] 用法：校准工具.exe run <脚本名> [参数...]")
            return 2
        return run_tool(args[1], args[2:])

    key = args[0]
    if key not in TOOLS:
        print(f"[错误] 未知工具 '{key}'。可用的 key：{', '.join(MENU_ORDER)}")
        print("       想跑其它脚本请用：校准工具.exe run <脚本名> [参数...]")
        return 2

    script, _, fixed = TOOLS[key]
    # CLI 下不交互：没给参数就用默认参数（tap 无参数 == 它自己的菜单）
    extra = args[1:] if len(args) > 1 else list(fixed)
    return run_tool(script, extra)


if __name__ == "__main__":
    sys.exit(main())

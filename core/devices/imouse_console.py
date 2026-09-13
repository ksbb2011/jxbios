"""iMouse 控制台窗口：自动拉起 + 与 GUI 并排（用户 2026-09-13 要求）。

为什么需要它：
    人工沟通核对时，**真正能鼠标+键盘操作手机的是 iMouse 自带的控制台窗口**——
    我们的 HTTP 客户端只有「截图 / 找色 / 键盘（且不支持中文）」，没有任何
    点击/触摸接口（见 imouse_client.py 开头的边界说明）。所以人工操作必须依赖
    那个控制台窗口。
    以前是三步：手动开控制台 → 调窗口大小 → 再开我们的软件。现在合成一步：
    GUI 启动即拉起控制台摆到左边，GUI 自己摆到右边。

边界（重要）：
    * 只做「拉起进程 + 摆窗口位置」，**不做任何点击/键鼠注入**；
    * 任何一步失败都静默降级（exe 找不到、没权限、窗口迟迟不出现），
      绝不抛异常、绝不阻塞——GUI 起不来远比窗口没对齐严重。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

# 控制台窗口标题（实测：iMouseManager.exe 的主窗口标题）
CONSOLE_TITLE = "iMouse自动化测试控制台"
# 本机实测路径（由开始菜单快捷方式 iMouse控制台专业版.lnk 解析得到）
DEFAULT_EXE = r"D:\iMousePro\iMouseManager.exe"

Rect = Tuple[int, int, int, int]
LogFn = Optional[Callable[[str, str], None]]

_user32 = ctypes.windll.user32

# 显式声明签名：64 位下 HWND/指针不声明会被按 int 截断，SetWindowPos 会失败
_user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
_user32.EnumWindows.restype = wt.BOOL
_user32.GetWindowTextLengthW.argtypes = [wt.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.SetWindowPos.argtypes = [
    wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
_user32.SetWindowPos.restype = wt.BOOL
_user32.SystemParametersInfoW.argtypes = [
    wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT,
]
_user32.SystemParametersInfoW.restype = wt.BOOL

SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
SPI_GETWORKAREA = 0x0030


def find_window(title: str) -> int:
    """按**完整标题**精确查顶层窗口句柄，找不到返回 0。"""
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)  # type: ignore[misc]
    def _cb(hwnd, _lparam):  # noqa: ANN001
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value == title:
            hits.append(int(hwnd))
            return False  # 找到就停
        return True

    _user32.EnumWindows(ctypes.cast(_cb, ctypes.c_void_p), 0)
    return hits[0] if hits else 0


def work_area() -> Rect:
    """主屏工作区（已排除任务栏），Win32 物理像素。"""
    r = wt.RECT()
    _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    return (int(r.left), int(r.top), int(r.right), int(r.bottom))


def compute_layout(work: Rect, ratio: float = 0.32) -> Tuple[Rect, Rect]:
    """按比例把工作区切成左右两块：返回 (控制台矩形, GUI 矩形)。

    ratio 是控制台占的宽度比例（clamp 到 15%~80%，防止手滑填出个没法用的布局）。
    """
    left, top, right, bottom = work
    width = max(1, right - left)
    ratio = min(0.8, max(0.15, float(ratio)))
    split = left + int(width * ratio)
    return (left, top, split, bottom), (split + 1, top, right, bottom)


def move(hwnd: int, rect: Rect) -> bool:
    """把窗口挪到指定矩形（不激活、不改 Z 序，避免抢焦点）。"""
    if not hwnd:
        return False
    left, top, right, bottom = rect
    return bool(_user32.SetWindowPos(
        wt.HWND(hwnd), wt.HWND(0),
        int(left), int(top),
        int(max(1, right - left)), int(max(1, bottom - top)),
        SWP_NOACTIVATE | SWP_NOZORDER,
    ))


def launch(exe: str) -> bool:
    """拉起控制台进程（返回 False 表示文件不存在/启动失败）。"""
    path = Path(exe)
    if not path.exists():
        return False
    try:
        subprocess.Popen([str(path)], cwd=str(path.parent))
        return True
    except OSError:
        return False


def show_and_tile(exe: str, gui_hwnd: int, ratio: float = 0.32,
                  wait_sec: float = 15.0, log: LogFn = None) -> None:
    """确保控制台窗口存在，然后把「控制台 | GUI」并排铺满工作区。

    已在运行就只摆位置，不重复启动；启动后轮询等窗口出现（最长 wait_sec）。
    """
    def _log(level: str, msg: str) -> None:
        if log is None:
            return
        try:
            log(level, msg)
        except Exception:  # noqa: BLE001 日志失败不影响摆窗口
            pass

    hwnd = find_window(CONSOLE_TITLE)
    if hwnd:
        _log("info", "iMouse 控制台已在运行，直接并排窗口")
    else:
        exe = exe or DEFAULT_EXE
        if not launch(exe):
            _log("warning", f"iMouse 控制台未运行且找不到可执行文件：{exe}（跳过并排）")
            return
        _log("info", f"已拉起 iMouse 控制台：{exe}，等待窗口出现…")
        deadline = time.time() + float(wait_sec)
        while time.time() < deadline and not hwnd:
            time.sleep(0.5)
            hwnd = find_window(CONSOLE_TITLE)
        if not hwnd:
            _log("warning", "等待 iMouse 控制台窗口超时（跳过并排，不影响使用）")
            return

    console_rect, gui_rect = compute_layout(work_area(), ratio)
    ok_console = move(hwnd, console_rect)
    ok_gui = move(gui_hwnd, gui_rect)
    _log(
        "info",
        f"窗口并排完成：控制台={console_rect}（{'ok' if ok_console else '失败'}） "
        f"GUI={gui_rect}（{'ok' if ok_gui else '失败'}）",
    )


__all__ = [
    "CONSOLE_TITLE",
    "DEFAULT_EXE",
    "compute_layout",
    "find_window",
    "launch",
    "move",
    "show_and_tile",
    "work_area",
]

"""JxbService（机械臂 WCF 服务宿主）管理。

背景（血泪教训）：
    机械臂服务进程被强杀时不会执行 close_port，WCF 服务会一直占用串口，
    导致下次 open_port 返回资源号 0（表现为「机械臂不动但不报错」）。
    因此在任何需要打开串口的程序启动前，必须先重启该服务释放串口。

注意：Restart-Service 需要管理员权限。若当前进程无权限，本模块会返回 False
并在日志里给出明确提示（由调用方决定是否继续），绝不静默失败。
"""

from __future__ import annotations

import subprocess
import time
from typing import Callable, Optional

LogFn = Optional[Callable[[str, str], None]]

_PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]


def _log(log: LogFn, level: str, msg: str) -> None:
    if log:
        log(level, msg)


def _run_ps(script: str, timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [*_PS, script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", str(exc)


def service_exists(name: str) -> bool:
    code, out, _ = _run_ps(f"(Get-Service -Name '{name}' -ErrorAction SilentlyContinue) -ne $null")
    return code == 0 and out.lower() == "true"


def is_running(name: str) -> bool:
    code, out, _ = _run_ps(
        f"(Get-Service -Name '{name}' -ErrorAction SilentlyContinue).Status -eq 'Running'"
    )
    return code == 0 and out.lower() == "true"


def restart_service(name: str = "JxbService", log: LogFn = None, wait_sec: float = 3.0) -> bool:
    """重启机械臂服务以释放串口。需要管理员权限。

    返回 True 表示重启成功（或本就无需重启）；返回 False 表示失败，
    调用方应提示用户以管理员身份运行。
    """
    if not service_exists(name):
        _log(log, "warning", f"未找到服务 {name}，跳过重启（请确认机械臂服务已安装）")
        return False

    _log(log, "info", f"正在重启服务 {name} 以释放串口...")
    code, out, err = _run_ps(f"Restart-Service -Name '{name}' -Force -ErrorAction Stop")
    if code != 0:
        _log(log, "error", f"重启服务 {name} 失败（可能需要管理员权限）: {err or out}")
        return False

    time.sleep(wait_sec)
    if is_running(name):
        _log(log, "info", f"服务 {name} 已重启并运行中")
        return True
    _log(log, "error", f"服务 {name} 重启后未处于运行状态")
    return False


def kill_stale_process(process_name: str = "WindowsService1", log: LogFn = None) -> bool:
    """强杀残留的宿主进程（服务名与进程名不同名时的兜底）。"""
    code, out, err = _run_ps(
        f"Get-Process -Name '{process_name}' -ErrorAction SilentlyContinue | Stop-Process -Force"
    )
    if code != 0:
        _log(log, "warning", f"清理残留进程 {process_name} 失败: {err or out}")
        return False
    _log(log, "info", f"已清理残留进程 {process_name}")
    return True

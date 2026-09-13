"""优雅停止运行中的测试：写 data/stop.flag，主循环检测到即收工（释放串口）。

替代 Stop-Process -Force 强杀——强杀会跳过 arm.close() 导致串口句柄泄漏、
COM4 卡死需手动重启 JxbService。用法：py -3.11 tools/stop_run.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config_store import PROJECT_ROOT  # noqa: E402

flag = Path(PROJECT_ROOT) / "data" / "stop.flag"
flag.write_text("stop", encoding="utf-8")
print(f"已写停止标志 {flag}，测试进程将在下一轮循环优雅收工（释放串口）")

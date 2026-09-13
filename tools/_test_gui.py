"""offscreen 启动 GUI，用属性名检查关键控件。"""
import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5 import QtWidgets
from gui.app import MainWindow

app = QtWidgets.QApplication(sys.argv)
w = MainWindow()
print("MainWindow 实例化 OK")

for name in ("chk_setup", "btn_start", "btn_stop", "btn_check",
             "tabs", "log_view", "candidate_table", "route_list",
             "chk_dry_run", "chk_review"):
    obj = getattr(w, name, None)
    print(f"  {name}: {'OK' if obj is not None else '缺失'}")

print("RESULT: 检查完成")
w.close()

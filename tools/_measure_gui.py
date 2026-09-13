"""offscreen 测 GUI 窗口真实最小高度，定位是哪个控件撑大。"""
import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5 import QtWidgets
from gui.app import MainWindow, GLOBAL_QSS

app = QtWidgets.QApplication(sys.argv)
app.setStyleSheet(GLOBAL_QSS)
win = MainWindow()

hint = win.minimumSizeHint()
print(f"窗口 minimumSizeHint = {hint.width()}x{hint.height()}")
print(f"窗口 minimumHeight = {win.minimumHeight()}")
print(f"窗口 size = {win.size().width()}x{win.size().height()}")

# 递归找高 minimumSizeHint 的控件
def walk(w, depth=0, path=""):
    h = w.minimumSizeHint().height()
    mh = w.minimumHeight()
    sh = w.sizeHint().height()
    if max(h, mh, sh) >= 100:
        print(f"{'  '*depth}{type(w).__name__} minHint={h} minH={mh} sizeHint={sh} [{path}]")
    for child in w.findChildren(QtWidgets.QWidget):
        pass  # findChildren 会重复，改用 children()
    for child in w.children():
        if isinstance(child, QtWidgets.QWidget):
            walk(child, depth+1, path + "/" + type(child).__name__)

print("\n=== 高度 >= 100 的控件 ===")
walk(win)
win.close()

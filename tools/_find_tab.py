import cv2
import numpy as np
from pathlib import Path

p = sorted(Path("docs/_probe/shots").glob("home_*.png"))[-1]
img = cv2.imread(str(p))
g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
roi = g[700:800, 90:290]
m = (roi > 190).astype(np.uint8) * 255
k = cv2.getStructuringElement(cv2.MORPH_RECT, (4, 4))
m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k, iterations=1)
cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
out = []
for c in cnts:
    x, y, w, h = cv2.boundingRect(c)
    a = cv2.contourArea(c)
    if 300 < a < 8000 and 20 < w < 130 and 8 < h < 55:
        out.append((x + 90, y + 700, w, h, a))
for x, y, w, h, a in sorted(out):
    print(f"box x={x} y={y} w={w} h={h} area={a} center=({x + w // 2},{y + h // 2})")
cv2.imwrite("docs/_probe/shots/tab_debug.png", cv2.cvtColor(m, cv2.COLOR_GRAY2BGR))

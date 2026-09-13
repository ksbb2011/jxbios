import cv2
import numpy as np
from pathlib import Path

p = sorted(Path("docs/_probe/shots").glob("home_*.png"))[-1]
img = cv2.imread(str(p))
H, W = img.shape[:2]
print(f"img size: {W}x{H}")

# 底部 tab 区域：取 y 770~830（避开 home indicator）
roi = img[770:830, :]
g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
# 放宽阈值
m = (g > 150).astype(np.uint8) * 255
# 横向投影，看 4/5 列分布
col_sum = m.sum(axis=0) // 255
print("non-white per col (downsampled every 10):")
for x in range(0, W, 10):
    print(f"  x={x:3d} : {col_sum[x]}")

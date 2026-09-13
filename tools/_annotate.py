import cv2
import numpy as np
from pathlib import Path

p = sorted(Path("docs/_probe/shots").glob("home_*.png"))[-1]
img = cv2.imread(str(p))
H, W = img.shape[:2]
out = img.copy()

# 1) 底部 5 等分竖线（看清中间列）
for i in range(1, 5):
    x = W * i // 5
    cv2.line(out, (x, 760), (x, H), (0, 255, 0), 1)

# 2) 候选框：5 等分中间列 x≈187, 底部栏 y≈778~822
cx, cy = 187, 800
cv2.rectangle(out, (cx - 35, 778), (cx + 35, 822), (0, 0, 255), 2)
cv2.drawMarker(out, (cx, cy), (0, 0, 255), cv2.MARKER_CROSS, 20, 2)
cv2.putText(out, "全国货源候选(188,800)", (cx - 70, 770),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

# 3) 整条底部栏描边
cv2.rectangle(out, (0, 765), (W, H), (255, 0, 0), 1)

dst = Path("docs/_probe/shots/home_annotated.png")
cv2.imwrite(str(dst), out)
print("saved", dst)

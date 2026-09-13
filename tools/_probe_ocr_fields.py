import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2

from core.config_store import ConfigStore
from core.vision.ocr import OcrEngine

D = Path("F:/JXBproject/robot_order_picker_new/data/regression/detail")
files = sorted(D.glob("*.png"))[:6]

s = ConfigStore()
ocr = OcrEngine(s)

lines = []
for f in files:
    img = cv2.imread(str(f))
    if img is None:
        continue
    h, w = img.shape[:2]
    # 裁 screen_roi（去黑边）
    crop = img[int(0.098 * h):int(0.898 * h), int(0.177 * w):int(0.846 * w)]
    boxes = ocr.ocr(crop) or []
    texts = [b.text for b in boxes]
    lines.append("%s | %s" % (f.name, " / ".join(texts)))

Path("data/logs/detail_ocr.txt").write_text("\n".join(lines), encoding="utf-8")
print("WROTE", len(lines), flush=True)

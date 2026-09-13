"""OCR 最近几张详情截图，看两个 km（距装货约/估里程）的坐标分布。"""
import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cv2
from core.vision.ocr import OcrEngine
from core.config_store import ConfigStore
from core.vision.imgio import imread_unicode

oc = OcrEngine(ConfigStore())
print("OCR:", oc.available)

shots = sorted(Path("data/traces").glob("*_detail_*.jpg"))[-6:]
print(f"最近 {len(shots)} 张详情截图\n")

KM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:km|公里|KM)")

for f in shots:
    img = imread_unicode(str(f), cv2.IMREAD_COLOR)
    if img is None:
        continue
    h, w = img.shape[:2]
    boxes = oc.ocr(img)
    kms = []
    for b in boxes:
        m = KM_RE.search(b.text)
        if m:
            val = float(m.group(1).replace(",", ""))
            x, y, bw, bh = b.rect
            kms.append((val, x/w, y/h, b.text))
    kms.sort(key=lambda t: t[1])  # 按 x 排序
    line = " | ".join(f"{v:g}km@x{x:.2f},y{y:.2f}" for v, x, y, _ in kms)
    print(f"{f.name[-30:]:<32} {line}")

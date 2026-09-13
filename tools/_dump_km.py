"""dump 一张详情截图 OCR，看距离字段（km）在哪。"""
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

f = Path("data/traces/20260911_103745_detail_reject_南京市_江宁区.jpg")
img = imread_unicode(str(f), cv2.IMREAD_COLOR)
h, w = img.shape[:2]
print(f"{f.name} {w}x{h}\n")

boxes = oc.ocr(img)
for b in boxes:
    x, y, bw, bh = b.rect
    nx, ny = x / w, y / h
    flag = ""
    if re.search(r"km|公里|KM", b.text) or re.search(r"\d+(\.\d+)?", b.text):
        flag = " <== 数字/km"
    print(f"  ({nx:.2f},{ny:.2f}) [{x},{y}] '{b.text}'{flag}")

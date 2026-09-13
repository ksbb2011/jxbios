"""验证 dx=-90 后 tonnage/volume 能否命中车型行。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.config_store import ConfigStore
from core.flow.scanner import CardSplitter
from core.vision.imgio import imread_unicode
from core.vision.matcher import TemplateMatcher
from core.vision.ocr import OcrEngine


class FS:
    def __init__(self, img):
        self.fast = img
        self.raw = img


class Kit:
    def __init__(self, matcher, ocr):
        self.matcher = matcher
        self.ocr = ocr


def main():
    s = ConfigStore()
    img = imread_unicode("data/shots/list_current.png")
    matcher = TemplateMatcher(s)
    ocr = OcrEngine(s)
    boxes = ocr.ocr(img)
    w = img.shape[1]

    splitter = CardSplitter(s, Kit(matcher, ocr))
    cards = splitter.split(FS(img), boxes)
    offs = s.get("fields", "card.offsets", {}) or {}

    for i, (top, bottom) in enumerate(cards[:3]):
        print(f"卡#{i} bottom={bottom}:")
        for name in ("che_length", "tonnage", "volume"):
            cfg = offs.get(name, {})
            dx = int(cfg.get("dx", 0) or 0)
            ww = int(cfg.get("w", 60) or 60)
            x0 = w // 2 + dx
            x1 = x0 + ww
            bt = bottom - 100
            bb = bottom - 12
            cand = [b for b in boxes if x0 <= b.center[0] <= x1 and bt <= b.center[1] <= bb]
            print(f"  {name} x[{x0},{x1}]: {[b.text for b in cand]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
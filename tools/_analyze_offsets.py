"""离线分析：用列表页截图，跑 avatar_icon 切卡 + 字段探测，诊断"未取到车型"根因并给出重标定值。"""
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


def main() -> int:
    s = ConfigStore()
    img = imread_unicode("data/shots/list_current.png")
    if img is None:
        print("FAIL: 无列表页截图")
        return 1
    matcher = TemplateMatcher(s)
    ocr = OcrEngine(s)
    boxes = ocr.ocr(img)
    w = img.shape[1]

    splitter = CardSplitter(s, Kit(matcher, ocr))
    cards = splitter.split(FS(img), boxes)
    print(f"切卡 {len(cards)} 张，帧宽 {w}，锚点x={w//2}")

    offs = s.get("fields", "card.offsets", {}) or {}
    for i, (top, bottom) in enumerate(cards):
        print(f"\n=== 卡#{i} top={top} bottom={bottom} ===")
        # 模拟 che_length 搜索：x [anchor+dx, anchor+dx+w]，band [bottom-100, bottom-12]
        for name in ("che_length", "tonnage"):
            cfg = offs.get(name, {})
            dx = int(cfg.get("dx", 0) or 0)
            ww = int(cfg.get("w", 60) or 60)
            x0 = w // 2 + dx
            x1 = x0 + ww
            bt = bottom - 100
            bb = bottom - 12
            cand = [b for b in boxes if x0 <= b.center[0] <= x1 and bt <= b.center[1] <= bb]
            print(f"  {name}: x[{x0},{x1}] band[{bt},{bb}] -> 命中 {len(cand)} 框:")
            for b in cand:
                print(f"    [{b.text}] center=({b.center[0]},{b.center[1]})")
        # 实际车型行（含"米"）相对锚点的偏移
        for b in boxes:
            if top <= b.center[1] <= bottom and "米" in b.text:
                dx_real = b.center[0] - w // 2
                dy_real = b.center[1] - bottom
                print(f"  车型行 [{b.text}] center=({b.center[0]},{b.center[1]}) -> 实际 dx={dx_real} dy={dy_real}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

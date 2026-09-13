"""字段偏移重标定：截列表页，用 avatar_icon 切卡，OCR 定位车型行/结算行，
计算车型行相对锚点（卡片水平中心 + 卡下边界）的实际 dx/dy，与 fields.json 对比。

用法：真机停在列表页时运行，输出建议的 card.offsets 值。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.config_store import ConfigStore
from core.devices.imouse_source import ImouseFrameSource
from core.flow.scanner import CardSplitter
from core.vision.matcher import TemplateMatcher
from core.vision.ocr import OcrEngine


class Kit:
    def __init__(self, matcher, ocr):
        self.matcher = matcher
        self.ocr = ocr


def main() -> int:
    s = ConfigStore()
    src = ImouseFrameSource()
    if not src.open():
        print("FAIL open imouse")
        return 1
    fs = src.read()
    matcher = TemplateMatcher(s)
    ocr = OcrEngine(s)
    boxes = ocr.ocr(fs.raw)
    w = fs.fast.shape[1]

    splitter = CardSplitter(s, Kit(matcher, ocr))
    cards = splitter.split(fs, boxes)
    print(f"切卡 {len(cards)} 张，帧宽 {w}")
    print(f"当前配置 card.offsets 关键值:")
    offs = s.get("fields", "card.offsets", {}) or {}
    for k in ("che_length", "che_type", "tonnage", "volume"):
        c = offs.get(k, {})
        print(f"  {k}: dx={c.get('dx')} w={c.get('w')} dy={c.get('dy')} h={c.get('h')}")

    print("\n--- 每张卡实测（车型行相对锚点 = 卡片中心x + 卡底y）---")
    for i, (top, bottom) in enumerate(cards[:4]):
        anchor_x = w // 2
        print(f"\n卡#{i} top={top} bottom={bottom} 锚点x={anchor_x}")
        # 卡内 + band（结算行上方 12~100px）内的 OCR 框
        band_top = bottom - 100
        band_bot = bottom - 12
        for b in boxes:
            if top <= b.center[1] <= bottom:
                tag = ""
                if band_top <= b.center[1] <= band_bot and ("米" in b.text or "吨" in b.text or "方" in b.text):
                    tag = "  <== 参数行候选"
                elif "结算" in b.text:
                    tag = "  <== 结算行"
                print(f"  [{b.text}] center=({b.center[0]},{b.center[1]}) rect={b.rect}{tag}")
    src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

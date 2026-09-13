"""临时标定工具：交互框选「已被抢/已下架/货源已定」弹窗的关闭 × 按钮，
输出 × 中心相对 OCR 标题中心的偏移，用于校正 config/runtime.json 的
dialog_close_points.offset_from_center。

用法：
    py -3.11 tools/_calibrate_dialog_x.py            # 交互框选 + 询问写入
    py -3.11 tools/_calibrate_dialog_x.py --probe    # 仅 OCR + 输出推荐偏移（不弹窗）

为什么不直接 OCR × ：× 是图标，OCR 识别不出；只能靠人工框选。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cv2
import numpy as np

from core.config_store import ConfigStore
from core.devices.imouse_source import ImouseFrameSource
from core.vision.imgio import imwrite_unicode
from core.vision.ocr import OcrEngine

KEYWORDS = ("已被抢", "货源已定", "已下架")


def _ocr_title_center(img: np.ndarray, eng: OcrEngine):
    boxes = eng.ocr(img)
    for b in boxes:
        if any(k in b.text for k in KEYWORDS):
            return b
    return None


def _interactive_x(img: np.ndarray):
    """弹窗拖拽框选 × 区域，返回 (cx, cy) fast 帧像素。取消返回 None。"""
    sel = {"x1": -1, "y1": -1, "x2": -1, "y2": -2, "drawing": False}

    def on_mouse(evt, x, y, flags, _):
        if evt == cv2.EVENT_LBUTTONDOWN:
            sel["x1"], sel["y1"], sel["drawing"] = x, y, True
            sel["x2"], sel["y2"] = x, y
        elif evt == cv2.EVENT_MOUSEMOVE and sel["drawing"]:
            sel["x2"], sel["y2"] = x, y
        elif evt == cv2.EVENT_LBUTTONUP:
            sel["drawing"] = False

    try:
        cv2.namedWindow("calibrate_dialog_x", cv2.WINDOW_NORMAL)
        cv2.setMouseCallback("calibrate_dialog_x", on_mouse)
    except cv2.error as exc:
        print(f"FAIL: no GUI: {exc}")
        return None

    print("[SELECT] drag around the dialog close 'X' -> Enter confirm / Esc cancel")
    while True:
        vis = img.copy()
        if sel["x1"] >= 0:
            cv2.rectangle(vis, (sel["x1"], sel["y1"]), (sel["x2"], sel["y2"]),
                          (0, 255, 0), 2)
        cv2.imshow("calibrate_dialog_x", vis)
        k = cv2.waitKey(30) & 0xFF
        if k == 13:
            break
        if k == 27:
            cv2.destroyAllWindows()
            return None
    cv2.destroyAllWindows()

    x1, y1, x2, y2 = sel["x1"], sel["y1"], sel["x2"], sel["y2"]
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    if x2 - x1 < 3 or y2 - y1 < 3:
        print("CANCEL: selection too small")
        return None
    return (int((x1 + x2) / 2), int((y1 + y2) / 2))


def _main_probe() -> int:
    """仅 OCR 探测：输出标题中心 + 当前 offset_from_center 推荐值（假设 × 在右边缘）。"""
    store = ConfigStore()
    src = ImouseFrameSource()
    src.open()
    f = src.read().raw
    eng = OcrEngine(store)
    title = _ocr_title_center(f, eng)
    if not title:
        print("FAIL: no dialog title text found (need 该货源已被抢/货源已定/已下架 on screen)")
        src.close()
        return 1
    cfg = store.section("runtime").get("dialog_close_points", {}) or {}
    cur_off = int(cfg.get("offset_from_center", 136))
    print(f"TITLE  center=({title.center[0]}, {title.center[1]})  text='{title.text}'")
    print(f"CURRENT offset_from_center = {cur_off}")
    print(f"CURRENT would click at     = ({title.center[0] + cur_off}, {title.center[1]})")
    # 经验：× 通常靠近屏幕右边缘，且与标题同行
    print(f"SCREEN   width = {f.shape[1]}")
    src.close()
    return 0


def _main_interactive(write: bool) -> int:
    store = ConfigStore()
    src = ImouseFrameSource()
    src.open()
    f = src.read().raw
    print(f"FRAME {f.shape[1]}x{f.shape[0]}")
    imwrite_unicode(str(ROOT / "data" / "shots" / "dialog_grabbed_calib.png"), f)

    eng = OcrEngine(store)
    title = _ocr_title_center(f, eng)
    if not title:
        print("FAIL: no dialog title text; run on a frame showing 该货源已被抢/货源已定/已下架")
        src.close()
        return 1
    print(f"TITLE  text='{title.text}'  rect={title.rect}  center=({title.center[0]}, {title.center[1]})")

    x_pt = _interactive_x(f)
    if x_pt is None:
        print("CANCEL")
        src.close()
        return 0
    cx, cy = x_pt
    off_x = cx - title.center[0]
    off_y = cy - title.center[1]
    print(f"X      center=({cx}, {cy})")
    print(f"OFFSET dx={off_x}  dy={off_y}  (dialog_close_points.offset_from_center 用 dx)")

    cfg = store.section("runtime").get("dialog_close_points", {}) or {}
    cur = int(cfg.get("offset_from_center", 136))
    print(f"CURRENT offset_from_center = {cur}  (delta = {off_x - cur:+d})")

    if write:
        ans = input("write offset_from_center into config/runtime.json? [y/N]: ").strip().lower()
        if ans == "y":
            # 注意：save_section 是「整段替换」，不能只传 dialog_close_points 子段，
            # 否则会把 runtime.json 的其它字段全部抹掉。
            runtime = store.section("runtime")
            dp = dict(runtime.get("dialog_close_points", {}) or {})
            dp["offset_from_center"] = off_x
            # 固定坐标兜底同步为 × 的归一化坐标（否则 close_mode=fixed 时仍点偏）
            dp["detail_grabbed"] = [round(cx / f.shape[1], 4), round(cy / f.shape[0], 4)]
            runtime["dialog_close_points"] = dp
            store.save_section("runtime", runtime)
            print(f"WRITTEN offset_from_center={off_x}  detail_grabbed={dp['detail_grabbed']}")
        else:
            print("NOT WRITTEN")
    src.close()
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--probe" in args:
        return _main_probe()
    return _main_interactive(write=("--write" in args))


if __name__ == "__main__":
    sys.exit(main())

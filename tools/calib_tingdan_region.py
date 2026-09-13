"""听单颜色判定区域标定：在「听单已开启（红色）」的帧上自动算出 region。

为什么需要它：
    听单开/关不靠模板（灰度结构分不开，实测 on=0.60 / off=0.73 反而 off 更高），
    靠 hardware.calibration.tingdan.region 内的**红色像素占比**。
    region 必须贴合控件：
      * 框大了 → 红色被背景稀释，占比掉到阈值 0.08 以下 → 把「开着」判成「已关」，
        程序就不去关它，听单一直开着自动抢单（代价最高的一种误判）；
      * 框偏了 → 点击点取 region 中心，关听单会点歪。
    人工框选很难一次到位，而红色像素的分布边界是客观可测的，故做成工具。

判据与运行时完全一致（core/vision/page_state.py::_detect_tingdan）：
    red = (R > min_red) & (R > B * red_factor) & (R > G * red_factor)
    ratio = red.mean();  ratio >= red_ratio_threshold  →  听单中

用法：
    # 听单已开启时跑（默认现截当前屏）
    py -3.11 tools/calib_tingdan_region.py

    # 用已存好的截图
    py -3.11 tools/calib_tingdan_region.py --shot data/shots/tingdan_on.png

    # 确认无误后写入 hardware.json
    py -3.11 tools/calib_tingdan_region.py --write
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Tuple

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config_store import ConfigStore  # noqa: E402
from core.vision import imgio  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HW_JSON = ROOT / "config" / "hardware.json"

# 默认搜索区：列表页顶部右侧（听单控件所在的大致范围，宁可大一点再收紧）
SEARCH_NORM = (0.50, 0.00, 1.00, 0.20)
# bbox 外扩像素：贴边会截掉抗锯齿的过渡像素，留 2px 更稳
PAD_PX = 2


def _red_mask(roi: np.ndarray, min_red: float, factor: float) -> np.ndarray:
    b, g, r = (roi[:, :, c].astype(np.float32) for c in range(3))
    return (r > min_red) & (r > b * factor) & (r > g * factor)


def _ratio(frame: np.ndarray, region, min_red: float, factor: float) -> float:
    h, w = frame.shape[:2]
    x1, y1 = max(0, int(region[0] * w)), max(0, int(region[1] * h))
    x2, y2 = min(w, int(region[2] * w)), min(h, int(region[3] * h))
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return float(_red_mask(frame[y1:y2, x1:x2], min_red, factor).mean())


def _bbox(mask: np.ndarray) -> Tuple[int, int, int, int]:
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return (0, 0, 0, 0)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def _grab(shot: str) -> np.ndarray:
    if shot:
        img = imgio.imread_unicode(shot, cv2.IMREAD_COLOR)
        if img is None:
            print(f"[ERROR] cannot read {shot}")
            raise SystemExit(2)
        return img
    from core.devices.imouse_source import ImouseFrameSource

    src = ImouseFrameSource()
    src.open()
    try:
        return src.read().raw
    finally:
        try:
            src.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="calibrate tingdan red-pixel region")
    ap.add_argument("--shot", default="", help="screenshot to analyze (default: live capture)")
    ap.add_argument("--search", default="", help="l,t,r,b search window (normalized)")
    ap.add_argument("--pad", type=int, default=PAD_PX, help="bbox padding in px")
    ap.add_argument("--write", action="store_true", help="write region into hardware.json")
    ap.add_argument("--blocks", type=int, default=0,
                    help="print top N red connected components (bbox+area) to locate the widget")
    ap.add_argument("--save-frame", action="store_true", help="save the analyzed frame for eyeballing")
    ap.add_argument("--expect", default="on", choices=["on", "off"],
                    help="current tingdan state: on=ratio must be >= thr, off=must be < thr")
    args = ap.parse_args()

    store = ConfigStore()
    cal = store.get("hardware", "calibration", {}) or {}
    cfg = cal.get("tingdan", {}) or {}
    min_red = float(cfg.get("min_red", 90) or 90)
    factor = float(cfg.get("red_factor", 1.25) or 1.25)
    thr = float(cfg.get("red_ratio_threshold", 0.08) or 0.08)
    cur = cfg.get("region")

    frame = _grab(args.shot)
    h, w = frame.shape[:2]

    if args.save_frame:
        shots = ROOT / "data" / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        p = shots / f"tingdan_debug_{time.strftime('%Y%m%d_%H%M%S')}.png"
        cv2.imwrite(str(p), frame)
        print(f"frame saved: {p.relative_to(ROOT)}")

    # 防呆：听单控件在列表页**顶部区**，顶部被隐藏时控件不在画面上，
    # 此时量到的红色全是别的东西（按钮/标签/弹窗），标定出来必错。
    from core.vision.matcher import TemplateMatcher
    from core.vision.page_state import PageDetector

    res = PageDetector(store, TemplateMatcher(store)).detect(frame)
    print(f"page state  : {res.state}   top_visible={res.top_visible}   "
          f"hits={sorted(res.hits.keys())}")
    if not res.top_visible:
        print("[FAIL] top area is HIDDEN (find_records / driver_school not visible): "
              "the tingdan widget is off-screen, any measurement here is meaningless. "
              "Swipe down to expand the top bar, make sure the widget is on screen, then rerun.")
        return 1
    if "ORDER_LIST" not in str(res.state):
        print(f"[WARN] page is {res.state}, not ORDER_LIST - double check the screen")

    # 连通域：红色像素可能不止听单控件（红色按钮/标签都会命中），
    # 先看块的位置和大小，才能判断哪一块才是听单控件。
    if args.blocks:
        full = _red_mask(frame, min_red, factor).astype(np.uint8)
        n, _lab, stats, _cent = cv2.connectedComponentsWithStats(full, 8)
        if n > 1:
            order = np.argsort(-stats[1:, 4])[: args.blocks] + 1
            print("--- top red blocks (x1,y1,x2,y2 area) ---")
            for i in order:
                x1, y1, bw, bh, area = (int(v) for v in stats[i])
                print(f"  ({x1:>3},{y1:>3})-({x1 + bw:>3},{y1 + bh:>3})  area={area:>6}  "
                      f"norm=[{x1 / w:.3f},{y1 / h:.3f},{(x1 + bw) / w:.3f},{(y1 + bh) / h:.3f}]")

    search = tuple(float(v) for v in args.search.split(",")) if args.search else SEARCH_NORM
    sx1, sy1 = int(search[0] * w), int(search[1] * h)
    sx2, sy2 = int(search[2] * w), int(search[3] * h)
    sub = frame[sy1:sy2, sx1:sx2]
    mask = _red_mask(sub, min_red, factor)
    bx1, by1, bx2, by2 = _bbox(mask)

    print(f"frame {w}x{h}   search=({sx1},{sy1})-({sx2},{sy2})")
    print(f"red pixels in search: {int(mask.sum())}")
    if bx2 <= bx1 or by2 <= by1:
        print("[FAIL] no red pixels found - is tingdan actually ON (red)? "
              "or widen --search")
        return 1

    gx1, gy1 = sx1 + bx1 - args.pad, sy1 + by1 - args.pad
    gx2, gy2 = sx1 + bx2 + args.pad, sy1 + by2 + args.pad
    suggest = (round(max(0.0, gx1 / w), 4), round(max(0.0, gy1 / h), 4),
               round(min(1.0, gx2 / w), 4), round(min(1.0, gy2 / h), 4))
    r_bbox = float(mask[by1:by2, bx1:bx2].mean())
    r_sugg = _ratio(frame, suggest, min_red, factor)
    r_cur = _ratio(frame, cur, min_red, factor) if cur else 0.0
    cx, cy = (suggest[0] + suggest[2]) / 2, (suggest[1] + suggest[3]) / 2

    print(f"red bbox (px)  : x {sx1 + bx1}~{sx1 + bx2}   y {sy1 + by1}~{sy1 + by2}")
    print(f"tight ratio    : {r_bbox:.3f}  (red inside its own bbox)")
    # 期待开态时 ratio 必须够高；期待关态时必须够低——两种情况下「对」的方向相反，
    # 用同一个 OK/MISS 文案会在关态时把「正确」显示成失败，故按 expect 判定。
    want_high = args.expect == "on"
    ok = (lambda v: v >= thr) if want_high else (lambda v: v < thr)

    print(f"expect         : tingdan {args.expect.upper()} "
          f"({'ratio >= ' if want_high else 'ratio < '}{thr})")
    print(f"SUGGEST region : {list(suggest)}   ratio={r_sugg:.3f}  "
          f"margin={r_sugg - thr:+.3f}  {'PASS' if ok(r_sugg) else 'FAIL'}")
    if cur:
        verdict = "PASS" if ok(r_cur) else "FAIL"
        print(f"CURRENT region : {list(cur)}  ratio={r_cur:.3f}  "
              f"margin={r_cur - thr:+.3f}  {verdict}")
    print(f"click point    : norm=[{cx:.4f},{cy:.4f}]  (region center, used to turn it off)")

    if not want_high and r_cur < thr:
        print("[OK] off-state confirmed: region sees almost no red -> "
              "will NOT be misjudged as listening")
    if want_high and r_sugg < thr * 1.5:
        print(f"[WARN] margin too small: UI variation may drop it below {thr}")
    if not args.write:
        print("[dry-run] add --write to save into hardware.json")
        return 0

    if not want_high:
        print("[REFUSED] --write with --expect off: there is no red widget to measure "
              "in the off state. Turn tingdan ON and run without --expect off.")
        return 1

    hw = json.loads(HW_JSON.read_text(encoding="utf-8"))
    node = hw.setdefault("calibration", {}).setdefault("tingdan", {})
    node["region"] = list(suggest)
    node["_note_region_calibrated"] = (
        "2026-09-10 tools/calib_tingdan_region.py 自动标定：在听单开启（红色）帧上取红色像素 "
        f"bounding box 外扩 {args.pad}px；实测红色占比 {r_sugg:.3f}（阈值 {thr}）。"
        "旧值为人工框选且落在控件下方，会把「开着」判成「已关」。"
    )
    HW_JSON.write_text(json.dumps(hw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("hardware.json: UPDATED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        sys.exit(2)

"""坐标标注工具：截图 + 弹窗标注点/区域，输出归一化坐标，可选写入 coords.json。

用途：
    机械臂标定覆盖不了屏幕顶部（返回箭头等），这些控件的坐标靠人工标注，
    直接点/框选得到归一化坐标，AI 记录到配置，程序用固定坐标点击。

用法：
    # 标注一个点（单击目标点），打印归一化坐标
    py -3.11 tools/annotate_coords.py --name detail_back_arrow

    # 标注一个区域（拖拽框选），打印归一化 [l,t,r,b] + 中心
    py -3.11 tools/annotate_coords.py --name detail_back_arrow --region

    # 标注并写入 coords.json（作为固化控件）
    py -3.11 tools/annotate_coords.py --name detail_back_arrow --save

    # 用已存截图标注（不现截）
    py -3.11 tools/annotate_coords.py --name x --shot data/shots/xx.png
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2

from core.config_store import ConfigStore
from core.vision import imgio

ROOT = Path(__file__).resolve().parent.parent
COORDS_JSON = ROOT / "config" / "coords.json"


def _grab(shot: str):
    if shot:
        img = imgio.imread_unicode(shot, cv2.IMREAD_COLOR)
        if img is None:
            print("[ERROR] cannot read %s" % shot, flush=True)
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


def annotate_point(img):
    """单击标注一个点，返回 (px, py) 像素坐标；取消返回 None。"""
    pt = {"x": -1, "y": -1}

    def on_mouse(evt, x, y, flags, param):
        if evt == cv2.EVENT_LBUTTONDOWN:
            pt["x"], pt["y"] = x, y

    cv2.namedWindow("annotate", cv2.WINDOW_NORMAL)
    cv2.setMouseCallback("annotate", on_mouse)
    print("[点标注] 点击目标点 -> Enter 确认 / c 清除 / Esc 取消", flush=True)
    while True:
        vis = img.copy()
        if pt["x"] >= 0:
            cv2.circle(vis, (pt["x"], pt["y"]), 5, (0, 0, 255), 2)
            cv2.putText(vis, "(%d,%d)" % (pt["x"], pt["y"]), (pt["x"] + 8, pt["y"]),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        cv2.imshow("annotate", vis)
        k = cv2.waitKey(30) & 0xFF
        if k == 13:
            break
        if k == ord("c"):
            pt["x"] = pt["y"] = -1
        elif k == 27:
            cv2.destroyAllWindows()
            return None
    cv2.destroyAllWindows()
    return pt["x"], pt["y"]


def annotate_region(img):
    """拖拽框选区域，返回 (x, y, w, h) 像素；取消返回 None。"""
    sel = {"x1": -1, "y1": -1, "x2": -1, "y2": -1, "drawing": False}

    def on_mouse(evt, x, y, flags, param):
        if evt == cv2.EVENT_LBUTTONDOWN:
            sel["x1"], sel["y1"], sel["drawing"] = x, y, True
            sel["x2"], sel["y2"] = x, y
        elif evt == cv2.EVENT_MOUSEMOVE and sel["drawing"]:
            sel["x2"], sel["y2"] = x, y
        elif evt == cv2.EVENT_LBUTTONUP:
            sel["drawing"] = False

    cv2.namedWindow("annotate", cv2.WINDOW_NORMAL)
    cv2.setMouseCallback("annotate", on_mouse)
    print("[区域标注] 拖拽框选 -> Enter 确认 / c 清除 / Esc 取消", flush=True)
    while True:
        vis = img.copy()
        if sel["x1"] >= 0:
            cv2.rectangle(vis, (sel["x1"], sel["y1"]), (sel["x2"], sel["y2"]),
                          (0, 255, 0), 2)
        cv2.imshow("annotate", vis)
        k = cv2.waitKey(30) & 0xFF
        if k == 13:
            break
        if k == ord("c"):
            sel["x1"] = sel["y1"] = sel["x2"] = sel["y2"] = -1
        elif k == 27:
            cv2.destroyAllWindows()
            return None
    cv2.destroyAllWindows()
    x1, y1, x2, y2 = sel["x1"], sel["y1"], sel["x2"], sel["y2"]
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    if x2 - x1 < 2 or y2 - y1 < 2:
        print("[取消] 框选过小", flush=True)
        return None
    return x1, y1, x2 - x1, y2 - y1


def _save_coords(name: str, norm, desc: str) -> None:
    cfg = {}
    if COORDS_JSON.is_file():
        cfg = json.loads(COORDS_JSON.read_text(encoding="utf-8"))
    controls = cfg.setdefault("controls", {})
    controls[name] = {
        "template": "",
        "norm": [round(float(norm[0]), 4), round(float(norm[1]), 4)],
        "desc": desc or f"{name} 人工标注",
    }
    COORDS_JSON.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="坐标标注工具")
    ap.add_argument("--name", required=True, help="控件名")
    ap.add_argument("--region", action="store_true", help="框选区域（默认点标注）")
    ap.add_argument("--shot", default="", help="用已存截图")
    ap.add_argument("--save", action="store_true", help="写入 coords.json")
    ap.add_argument("--desc", default="", help="coords.json 描述")
    args = ap.parse_args()

    img = _grab(args.shot)
    h, w = img.shape[:2]

    if args.region:
        r = annotate_region(img)
        if r is None:
            return 0
        x, y, rw, rh = r
        l, t, rr, b = x / w, y / h, (x + rw) / w, (y + rh) / h
        cx, cy = (l + rr) / 2, (t + b) / 2
        print("REGION 像素=(%d,%d,%d,%d) 归一化=[%.4f,%.4f,%.4f,%.4f] 中心=[%.4f,%.4f]"
              % (x, y, rw, rh, l, t, rr, b, cx, cy), flush=True)
        norm = (cx, cy)
    else:
        p = annotate_point(img)
        if p is None:
            return 0
        px, py = p
        cx, cy = px / w, py / h
        print("POINT 像素=(%d,%d) 归一化=[%.4f,%.4f]" % (px, py, cx, cy), flush=True)
        norm = (cx, cy)

    if args.save:
        _save_coords(args.name, norm, args.desc)
        print("coords.json: 已写入 %s norm=[%.4f,%.4f]" % (args.name, norm[0], norm[1]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

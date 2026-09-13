"""模板重采工具：用 iMouse 原生截图，按 ROI 裁切并保存。

为什么新做：
    源项目 35 个模板都是按相机 fast 帧 (480x640) 采集的。投屏后截图分辨率变成
    屏幕原生 (375x812)，旧模板**全部作废**。但重采反而变简单：
    投屏截图 = 手机屏幕本身，无畸变/无反光/无压缩失真，按已知 UI 坐标一裁即可。

ROI 输入约定（逻辑点坐标，与手机一致）：
    --name 模板名（写入 templates.json 与文件名）
    --x --y  --w  --h    左上角 + 宽高（逻辑点，如 243 533 60 28）

用法：
    # 手动模式：截当前屏 → 按给定逻辑点 ROI 裁切并保存
    py -3.11 tools/capture_template.py --name 02_source_red --x 230 --y 525 --w 56 --h 28

    # 交互框选模式（推荐，AI 不可见截图时由用户直接框选）
    #   弹窗显示当前屏，鼠标拖拽框选目标控件 → Enter 确认 / c 清除 / Esc 取消
    #   自动换算 375x812 逻辑点 ROI 并写回 templates.json
    py -3.11 tools/capture_template.py --name detail_report --interactive

    # 干跑模式（不写文件，只输出裁切结果尺寸）
    py -3.11 tools/capture_template.py --dry-run --x 230 --y 525 --w 56 --h 28

    # 一次截多张（手动改 UI 后按 Enter 继续）
    py -3.11 tools/capture_template.py --batch --name detail_share --count 5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

# 允许从 tools/ 子目录直接运行时也能 import 到项目根的 core 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.devices.imouse_client import ImouseError
from core.devices.imouse_source import ImouseFrameSource

DEFAULT_TEMPLATES = Path("config/templates.json")
DEFAULT_DATA_DIR = Path("data/templates")


def _crop(img, x: int, y: int, w: int, h: int):
    h_max, w_max = img.shape[:2]
    x = max(0, min(int(x), w_max - 1))
    y = max(0, min(int(y), h_max - 1))
    w = max(1, min(int(w), w_max - x))
    h = max(1, min(int(h), h_max - y))
    return img[y:y + h, x:x + w]


def _next_path(tpl_name: str, out_dir: Path, ext: str = ".png") -> Path:
    """找未占用的文件名：02_source_red.png → 02_source_red.png（重名加 _1 _2）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{tpl_name}{ext}"
    i = 1
    while p.exists():
        p = out_dir / f"{tpl_name}_{i}{ext}"
        i += 1
    return p


def register_template(name: str, path: Path, templates_json: Path,
                      roi_norm: tuple[float, float, float, float],
                      threshold: float = 0.88, kind: str = "icon",
                      purpose: str = "") -> None:
    """向 templates.json 注册/更新一条模板。"""
    cfg = json.loads(templates_json.read_text(encoding="utf-8"))
    items = cfg.setdefault("items", {})
    rel = str(path).replace("\\", "/")
    entry = items.get(name, {})
    entry.update({
        "path": rel,
        "threshold": threshold,
        "roi": [round(v, 4) for v in roi_norm],
        "kind": kind,
        "enabled": True,
    })
    if purpose:
        entry["purpose"] = purpose
    items[name] = entry
    templates_json.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                              encoding="utf-8")


def _interactive_select(img, dev) -> tuple[int, int, int, int] | None:
    """鼠标拖拽框选 ROI，返回逻辑点 (x, y, w, h) 或 None（取消/失败）。"""
    h_img, w_img = img.shape[:2]
    sel = {"x1": -1, "y1": -1, "x2": -1, "y2": -1, "drawing": False}

    def on_mouse(evt, x, y, flags, param):
        if evt == cv2.EVENT_LBUTTONDOWN:
            sel["x1"], sel["y1"], sel["drawing"] = x, y, True
            sel["x2"], sel["y2"] = x, y
        elif evt == cv2.EVENT_MOUSEMOVE and sel["drawing"]:
            sel["x2"], sel["y2"] = x, y
        elif evt == cv2.EVENT_LBUTTONUP:
            sel["drawing"] = False

    try:
        cv2.namedWindow("capture_template_ROI", cv2.WINDOW_NORMAL)
        cv2.setMouseCallback("capture_template_ROI", on_mouse)
    except cv2.error as exc:
        print(f"[错误] 无法打开 GUI 窗口（无显示器环境？）：{exc}\n"
              f"  请改用手动模式：--x --y --w --h 指定逻辑点 ROI。")
        return None

    print("[框选] 弹窗里按住左键拖拽框选目标控件 → Enter 确认 / c 清除 / Esc 取消")
    while True:
        vis = img.copy()
        if sel["x1"] >= 0:
            cv2.rectangle(vis, (sel["x1"], sel["y1"]), (sel["x2"], sel["y2"]),
                          (0, 255, 0), 2)
        cv2.imshow("capture_template_ROI", vis)
        key = cv2.waitKey(30) & 0xFF
        if key == 13:   # Enter
            break
        elif key == ord("c"):
            sel["x1"] = sel["y1"] = sel["x2"] = sel["y2"] = -1
        elif key == 27:  # Esc
            cv2.destroyAllWindows()
            return None
    cv2.destroyAllWindows()

    x1, y1, x2, y2 = sel["x1"], sel["y1"], sel["x2"], sel["y2"]
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    if x2 - x1 < 3 or y2 - y1 < 3:
        print("[取消] 框选区域过小")
        return None
    # 像素 → 逻辑点（截图是投屏分辨率，dev 为逻辑 375x812）
    lx = int(round(x1 / w_img * dev.width))
    ly = int(round(y1 / h_img * dev.height))
    lw = int(round((x2 - x1) / w_img * dev.width))
    lh = int(round((y2 - y1) / h_img * dev.height))
    return lx, ly, lw, lh


def main() -> int:
    ap = argparse.ArgumentParser(description="模板重采（iMouse 原生截图）")
    ap.add_argument("--name", required=True, help="模板名（同时是文件名前缀）")
    ap.add_argument("--x", type=int, default=None, help="ROI 左上 x（逻辑点，交互模式可省略）")
    ap.add_argument("--y", type=int, default=None, help="ROI 左上 y（逻辑点，交互模式可省略）")
    ap.add_argument("--w", type=int, default=None, help="ROI 宽（逻辑点，交互模式可省略）")
    ap.add_argument("--h", type=int, default=None, help="ROI 高（逻辑点，交互模式可省略）")
    ap.add_argument("--threshold", type=float, default=0.88,
                    help="写入 templates.json 的阈值（默认 0.88；投屏后建议按基线重定）")
    ap.add_argument("--kind", default="icon", choices=["icon", "text"],
                    help="图标/文字，决定默认阈值 0.88/0.60")
    ap.add_argument("--purpose", default="", help="模板用途说明（写到 templates.json）")
    ap.add_argument("--templates", default=str(DEFAULT_TEMPLATES))
    ap.add_argument("--out-dir", default=str(DEFAULT_DATA_DIR / "_new"))
    ap.add_argument("--dry-run", action="store_true",
                    help="只截屏+裁切，不写文件，方便先看尺寸对不对")
    ap.add_argument("--interactive", action="store_true",
                    help="弹窗鼠标框选 ROI（无需手动报坐标，适合 AI 不可见截图场景）")
    ap.add_argument("--from-json", action="store_true",
                    help="按 templates.json 里该 name 已有的归一化 roi 自动裁切（ROI 已知时免框选）")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    src = ImouseFrameSource()
    src.open()
    dev = src.device
    img = src.read().raw
    W, H = img.shape[1], img.shape[0]

    if args.from_json:
        # 按 templates.json 里已有的归一化 ROI 自动裁切（iPhone 逻辑点归一化，
        # 在 iPhone X 上与投屏截图同比例，可直接 × 截图尺寸得到像素坐标）。
        # 适合「ROI 已知、旧模板图作废」的重采：免去重新框选。
        tj = Path(args.templates)
        cfg = json.loads(tj.read_text(encoding="utf-8"))
        item = cfg.get("items", {}).get(args.name)
        if not item or not item.get("roi"):
            print(f"[错误] templates.json 中 {args.name} 无 roi，无法 --from-json"
                  f"（改用 --interactive 框选）")
            return 1
        roi = item["roi"]
        x = int(round(roi[0] * W)); y = int(round(roi[1] * H))
        w = int(round((roi[2] - roi[0]) * W)); h = int(round((roi[3] - roi[1]) * H))
        cropped = _crop(img, x, y, w, h)
        print(f"设备：{dev.name} {dev.width}x{dev.height}  截图：{W}x{H}")
        print(f"按 json roi 裁切：({x},{y}) {w}x{h} → 输出 {cropped.shape[1]}x{cropped.shape[0]}")
        path = _next_path(args.name, out_dir)
        cv2.imwrite(str(path), cropped)
        register_template(args.name, path, tj, tuple(roi),
                          threshold=args.threshold, kind=args.kind,
                          purpose=item.get("purpose", ""))
        print(f"已保存：{path}  已更新 templates.json(path 改为 {path})")
        try:
            from core.config_store import ConfigStore
            from core.vision.matcher import TemplateMatcher
            store = ConfigStore()
            sc = TemplateMatcher(store).score(img, store.template(args.name))
            print(f"[粗验] 自帧匹配分={sc:.3f}（≈1.0 说明裁到了正确控件；"
                  f"低分请用 --interactive 重新框选）")
        except Exception as exc:  # noqa: BLE001
            print(f"[粗验跳过] {exc}")
        return 0

    if not args.interactive and None in (args.x, args.y, args.w, args.h):
        print("[错误] 非交互模式必须提供 --x --y --w --h"
              "（或加 --interactive 框选 / --from-json 按 json roi 裁）")
        return 1

    if args.interactive:
        sel = _interactive_select(img, dev)
        if sel is None:
            return 0
        args.x, args.y, args.w, args.h = sel

    cropped = _crop(img, args.x, args.y, args.w, args.h)
    print(f"设备：{dev.name} {dev.width}x{dev.height}")
    print(f"截图尺寸：{img.shape[1]}x{img.shape[0]}")
    print(f"裁切：({args.x}, {args.y}) {args.w}x{args.h} → 输出 {cropped.shape[1]}x{cropped.shape[0]}")
    print(f"归一化 ROI：[0/0/0/0]  →  按逻辑点归一化："
          f"[{args.x/dev.width:.3f}, {args.y/dev.height:.3f}, "
          f"{(args.x+args.w)/dev.width:.3f}, {(args.y+args.h)/dev.height:.3f}]")

    if args.dry_run:
        print("[dry-run] 未写文件")
        return 0

    path = _next_path(args.name, out_dir)
    cv2.imwrite(str(path), cropped)
    print(f"已保存：{path}")

    # 注册到 templates.json
    templates_json = Path(args.templates)
    roi_norm = (args.x / dev.width, args.y / dev.height,
                (args.x + args.w) / dev.width, (args.y + args.h) / dev.height)
    if not templates_json.exists():
        templates_json.parent.mkdir(parents=True, exist_ok=True)
        templates_json.write_text('{"items": {}}', encoding="utf-8")
    register_template(args.name, path, templates_json, roi_norm,
                      threshold=args.threshold, kind=args.kind, purpose=args.purpose)
    print(f"已注册到：{templates_json}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ImouseError, RuntimeError) as exc:
        print(f"[错误] {exc}")
        sys.exit(1)

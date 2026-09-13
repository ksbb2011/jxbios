"""模板采集登记 helper：裁一张 → 校验达标 → 写入 templates.json 与 coords.json。

为什么单独做这个工具：
    capture_template.py 只做「裁剪 + 注册 + 自帧粗验」，缺三件事：
      1. 不写 coords.json（ActionKit 固化点击要用的归一化坐标）；
      2. 不做「达标才写」的门禁——历史上多次出现模板采歪了也照样入库，
         结果 page_state 静默失效（教训见 templates.json 里 close_x/dialog_grabbed
         的 _withdrawn 说明：误命中比不命中更危险）；
      3. 不做负样本校验——在其他页面上会不会误命中，必须当场测出来。

门禁规则（默认）：
    * 正样本分 ≥ 阈值（icon 0.88 / text 0.60）；
    * 负样本（data/neg_frames 下的帧）全屏最高分 < 阈值；
    * 卡线预警：正样本分 − 阈值 < 0.04 → 报警（分数贴着线，UI 一改就失效）；
    * 未通过默认不写入（--force 才强写）。
    四者任一不满足都会打印原因，绝不含糊。

用法（PowerShell 参数必须是 ASCII，中文说明事后用编辑器补进 JSON）：
    # 采集并登记一张（现截当前屏，按归一化框裁剪）
    py -3.11 tools/register_template.py register --name detail_share --box 0.78,0.05,0.85,0.10

    # 用已存好的截图裁剪（不连真机也能重采）
    py -3.11 tools/register_template.py register --name source_red --shot data/shots/list.png --box 0.43,0.90,0.57,0.96

    # 逻辑点坐标（x,y,w,h）而不是归一化
    py -3.11 tools/register_template.py register --name find_records --xywh 49,43,48,44

    # 只校验已注册模板（不裁图不写入），可在任意帧上跑
    py -3.11 tools/register_template.py check --name detail_share --shot data/shots/detail.png

    # 采集进度：哪些已采、哪些还差
    py -3.11 tools/register_template.py status
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config_store import ConfigStore, TemplateSpec  # noqa: E402
from core.vision import imgio  # noqa: E402
from core.vision.matcher import TemplateMatcher  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TPL_JSON = ROOT / "config" / "templates.json"
COORDS_JSON = ROOT / "config" / "coords.json"
TPL_ROOT = ROOT / "data" / "templates"
NEG_DIR = ROOT / "data" / "neg_frames"
SHOTS_DIR = ROOT / "data" / "shots"

# 铁律：图标 0.88；文字类标签 0.60（文字模板不走图标铁律）
DEFAULT_THR = {"icon": 0.88, "text": 0.60}
# 卡线预警余量：正样本最低分与阈值之差小于它就要报警
MARGIN_WARN = 0.04


# ------------------------------------------------------------------ 基础工具
def _load_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] cannot parse {path.name}: {exc}")
        raise SystemExit(2) from exc


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    bak = path.with_suffix(path.suffix + ".bak")
    if path.is_file():
        shutil.copy2(path, bak)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _crop(frame, roi_norm: Tuple[float, float, float, float]):
    h, w = frame.shape[:2]
    x0 = max(0, int(round(roi_norm[0] * w)))
    y0 = max(0, int(round(roi_norm[1] * h)))
    x1 = min(w, int(round(roi_norm[2] * w)))
    y1 = min(h, int(round(roi_norm[3] * h)))
    return frame[y0:y1, x0:x1], (x0, y0, x1 - x0, y1 - y0)


def _next_path(name: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}.png"
    i = 1
    while p.exists():
        p = out_dir / f"{name}_{i}.png"
        i += 1
    return p


def _grab_frame(shot: str = "") -> Any:
    """取一帧：优先用 --shot 指定的截图，否则现截（iMouse）。"""
    if shot:
        img = imgio.imread_unicode(shot, cv2.IMREAD_COLOR)
        if img is None:
            print(f"[ERROR] cannot read shot: {shot}")
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


def _interactive_select(img, lw: int, lh: int):
    """弹窗拖拽框选 ROI，返回逻辑点 (x, y, w, h)；取消/失败返回 None。

    AI 看不到截图，坐标只能由用户框选产生——这是「边采边叫」流程里最省事的一环。
    """
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
        cv2.namedWindow("register_template_ROI", cv2.WINDOW_NORMAL)
        cv2.setMouseCallback("register_template_ROI", on_mouse)
    except cv2.error as exc:
        print(f"[ERROR] no GUI window available: {exc}")
        return None

    print("[SELECT] drag to select the control -> Enter confirm / c clear / Esc cancel")
    while True:
        vis = img.copy()
        if sel["x1"] >= 0:
            cv2.rectangle(vis, (sel["x1"], sel["y1"]), (sel["x2"], sel["y2"]),
                          (0, 255, 0), 2)
        cv2.imshow("register_template_ROI", vis)
        key = cv2.waitKey(30) & 0xFF
        if key == 13:      # Enter
            break
        if key == ord("c"):
            sel["x1"] = sel["y1"] = sel["x2"] = sel["y2"] = -1
        elif key == 27:    # Esc
            cv2.destroyAllWindows()
            return None
    cv2.destroyAllWindows()

    x1, y1, x2, y2 = sel["x1"], sel["y1"], sel["x2"], sel["y2"]
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    if x2 - x1 < 3 or y2 - y1 < 3:
        print("[CANCEL] selection too small")
        return None
    return (int(round(x1 / w_img * lw)), int(round(y1 / h_img * lh)),
            int(round((x2 - x1) / w_img * lw)), int(round((y2 - y1) / h_img * lh)))


def _existing_entry(name: str) -> Dict[str, Any]:
    """取 templates.json 里该模板的已有条目（沿用 kind / threshold / match_mode）。

    match_mode 尤其重要：detail_back / detail_share 等条目注册的是 shape 模式，
    若校验时按 gray 算分，分数根本不是运行时的分数（校验等于白做）。
    """
    return ((_load_json(TPL_JSON).get("items", {}) or {}).get(name) or {})


def _build_spec(name: str, path: str, roi_norm, threshold: float, kind: str,
                match_mode: str = "gray") -> TemplateSpec:
    return TemplateSpec(
        name=name,
        path=path,
        alts=(),
        threshold=threshold,
        roi=roi_norm,
        scales=None,
        kind=kind,
        match_mode=match_mode,
        enabled=True,
        purpose="",
    )


# ------------------------------------------------------------------ 校验
def verify(matcher: TemplateMatcher, frame, spec: TemplateSpec,
           roi_norm, threshold: float, neg_dir: Optional[Path] = None) -> Dict[str, Any]:
    """正样本（ROI 内）+ 全屏误命中 + 负样本，三项一起判。

    负样本必须按「该控件不该出现的页面」来选：用 --neg-dir 指定页面子目录
    （如 data/neg_frames/list）。若把所有页面混在一个目录里，采 detail_* 时
    拿详情页截图当负样本，等于把"本该命中"判成误命中——自伤。
    """
    pos_hit = matcher.find(frame, spec, roi=roi_norm, threshold=-2.0)
    pos_score = float(pos_hit.score) if pos_hit else -1.0

    # 全屏扫一遍：看模板在 ROI 之外有没有更高分（框错控件的典型信号）
    full_spec = _build_spec(spec.name, spec.path, None, threshold, spec.kind)
    full_hit = matcher.find(frame, full_spec, roi=None, threshold=-2.0)
    full_score = float(full_hit.score) if full_hit else -1.0
    outside = False
    if full_hit is not None:
        h, w = frame.shape[:2]
        cx, cy = full_hit.center
        outside = not (
            roi_norm[0] * w - 2 <= cx <= roi_norm[2] * w + 2
            and roi_norm[1] * h - 2 <= cy <= roi_norm[3] * h + 2
        )

    neg_max, neg_where = -1.0, ""
    nd = Path(neg_dir) if neg_dir else NEG_DIR
    if nd.is_dir():
        for p in sorted(nd.rglob("*.png")):
            img = imgio.imread_unicode(str(p), cv2.IMREAD_COLOR)
            if img is None:
                continue
            hit = matcher.find(img, full_spec, roi=None, threshold=-2.0)
            if hit is not None and hit.score > neg_max:
                neg_max, neg_where = float(hit.score), p.name

    pos_ok = pos_score >= threshold
    neg_ok = neg_max < threshold
    return {
        "pos_score": pos_score,
        "pos_ok": pos_ok,
        "full_score": full_score,
        "full_outside_roi": outside,
        "neg_max": neg_max,
        "neg_where": neg_where,
        "neg_ok": neg_ok,
        "margin": pos_score - threshold,
        "pass": pos_ok and neg_ok,
    }


# ------------------------------------------------------------------ 写入
def register_templates_json(name: str, rel_path: str, roi_norm, threshold: float,
                            kind: str, purpose: str, group: str) -> None:
    cfg = _load_json(TPL_JSON)
    items = cfg.setdefault("items", {})
    entry = items.get(name, {})
    entry["path"] = rel_path
    entry["roi"] = [round(float(v), 4) for v in roi_norm]
    entry["threshold"] = round(float(threshold), 3)
    entry["kind"] = kind
    entry["enabled"] = True
    # purpose 保留原有中文说明（命令行传中文会踩 GBK 坑，故不覆盖）
    if purpose:
        entry["purpose"] = purpose
    items[name] = entry
    _save_json(TPL_JSON, cfg)


def register_coords_json(control: str, template: str, norm: Tuple[float, float],
                         desc: str) -> None:
    cfg = _load_json(COORDS_JSON)
    controls = cfg.setdefault("controls", {})
    entry = controls.get(control, {})
    entry["template"] = template
    entry["norm"] = [round(float(norm[0]), 4), round(float(norm[1]), 4)]
    entry["desc"] = desc or f"{control} control"
    controls[control] = entry
    _save_json(COORDS_JSON, cfg)


# ------------------------------------------------------------------ 子命令
def cmd_register(args) -> int:
    frame = _grab_frame(args.shot)
    h, w = frame.shape[:2]

    if args.interactive:
        _lw, _lh = ConfigStore().logical_size
        sel = _interactive_select(frame, _lw, _lh)
        if sel is None:
            return 0
        args.xywh = f"{sel[0]},{sel[1]},{sel[2]},{sel[3]}"
        print(f"[SELECT] logical xywh = {args.xywh}")

    if args.xywh:
        x, y, bw, bh = [float(v) for v in args.xywh.split(",")]
        store = ConfigStore()
        lw, lh = store.logical_size
        roi_norm = (x / lw, y / lh, (x + bw) / lw, (y + bh) / lh)
    else:
        roi_norm = tuple(float(v) for v in args.box.split(","))
    if not (0 <= roi_norm[0] < roi_norm[2] <= 1 and 0 <= roi_norm[1] < roi_norm[3] <= 1):
        print(f"[ERROR] bad roi {roi_norm}: need 0<=l<r<=1 and 0<=t<b<=1")
        return 2

    cropped, rect = _crop(frame, roi_norm)
    if cropped.size == 0:
        print("[ERROR] crop is empty")
        return 2

    out_dir = TPL_ROOT / args.group
    path = _next_path(args.name, out_dir)
    if not cv2.imwrite(str(path), cropped):
        print(f"[ERROR] cannot write {path}")
        return 2
    rel_path = str(path.relative_to(ROOT)).replace("\\", "/")

    existing = _existing_entry(args.name)
    kind = args.kind or str(existing.get("kind", "icon"))
    match_mode = args.match_mode or str(existing.get("match_mode", "gray"))
    threshold = args.threshold if args.threshold is not None else float(
        existing.get("threshold", DEFAULT_THR.get(kind, 0.88)))
    spec = _build_spec(args.name, str(path), roi_norm, threshold, kind, match_mode)

    # 源帧留证：模板由这一帧裁出，存档便于回溯，日后也可直接当负样本用
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    shot_path = SHOTS_DIR / f"{args.name}_{time.strftime('%Y%m%d_%H%M%S')}.png"
    cv2.imwrite(str(shot_path), frame)

    store = ConfigStore()
    matcher = TemplateMatcher(store)
    res = verify(matcher, frame, spec, roi_norm, threshold, args.neg_dir)

    # 独立样本帧：自帧满分证明不了任何事（模板就是从它裁出来的），
    # 必须用没参与裁剪的同页面帧复验，否则「模板老化」这类问题根本测不出来
    # ——历史上 source_red 就是自帧好看、真列表页只有 0.79。
    samples: List[Tuple[str, float]] = []
    if args.extra_shots:
        for p in sorted(Path(args.extra_shots).rglob("*.png")):
            img = imgio.imread_unicode(str(p), cv2.IMREAD_COLOR)
            if img is None:
                continue
            hit = matcher.find(img, spec, roi=roi_norm, threshold=-2.0)
            samples.append((p.name, float(hit.score) if hit else -1.0))
    sample_min = min((s for _, s in samples), default=None)
    gate_pos = sample_min if sample_min is not None else res["pos_score"]
    pos_ok = gate_pos >= threshold
    res["pos_ok"] = pos_ok
    res["margin"] = gate_pos - threshold
    res["pass"] = pos_ok and res["neg_ok"]

    print(f"=== REGISTER {args.name} ===")
    print(f"frame      : {w}x{h}")
    print(f"crop rect  : {rect} -> saved {rel_path} ({cropped.shape[1]}x{cropped.shape[0]})")
    print(f"kind/thr   : {kind} / {threshold:.2f} / mode={match_mode}")
    print(f"POS  self   = {res['pos_score']:.3f}   (cut from this frame: proves nothing)")
    if samples:
        print(f"POS  samples= min {sample_min:.3f} over {len(samples)} frames  "
              f"{'PASS' if pos_ok else 'FAIL'}")
        for n, s in samples:
            tail = "" if s >= threshold else "   <-- BELOW THRESHOLD"
            print(f"       {n}: {s:.3f}{tail}")
    else:
        print("[WARN] no independent sample frames (--extra-shots): gate uses the "
              "self-frame only, which CANNOT detect template aging")
    print(f"FULL screen = {res['full_score']:.3f}  outside_roi={res['full_outside_roi']}")
    if res["neg_max"] >= 0 or args.neg_dir:
        print(f"NEG  max    = {res['neg_max']:.3f} ({res['neg_where'] or 'n/a'})  "
              f"{'PASS' if res['neg_ok'] else 'FAIL'}")
    if 0 <= res["margin"] < MARGIN_WARN:
        print(f"[WARN] margin only {res['margin']:.3f} (<{MARGIN_WARN}): "
              f"score too close to threshold, UI change will break it")
    if res["full_outside_roi"] and res["full_score"] > res["pos_score"]:
        print("[WARN] better match outside the ROI - the box may be on the wrong control")

    if not res["pass"] and not args.force:
        print("RESULT: NOT REGISTERED (failed gate; use --force to write anyway)")
        return 1

    register_templates_json(args.name, rel_path, roi_norm, threshold, kind,
                            args.purpose, args.group)
    print("templates.json: UPDATED")

    if not args.no_coords:
        if args.click_norm:
            cx, cy = [float(v) for v in args.click_norm.split(",")]
        else:
            cx, cy = (roi_norm[0] + roi_norm[2]) / 2, (roi_norm[1] + roi_norm[3]) / 2
        register_coords_json(args.control or args.name, args.name, (cx, cy), args.desc)
        print(f"coords.json   : UPDATED control '{args.control or args.name}' "
              f"norm=[{cx:.4f},{cy:.4f}]")

    print("RESULT: OK" if res["pass"] else "RESULT: FORCED (gate not passed)")
    return 0


def cmd_check(args) -> int:
    store = ConfigStore()
    spec = store.template(args.name)
    frame = _grab_frame(args.shot)
    roi_norm = tuple(float(v) for v in args.box.split(",")) if args.box else spec.roi
    threshold = args.threshold if args.threshold is not None else float(spec.threshold)
    matcher = TemplateMatcher(store)
    res = verify(matcher, frame, spec, roi_norm or (0.0, 0.0, 1.0, 1.0), threshold,
                 args.neg_dir)

    print(f"=== CHECK {args.name} (thr={threshold:.2f}) ===")
    print(f"POS  in-roi= {res['pos_score']:.3f}  {'PASS' if res['pos_ok'] else 'FAIL'}")
    print(f"FULL screen = {res['full_score']:.3f}  outside_roi={res['full_outside_roi']}")
    print(f"NEG  max    = {res['neg_max']:.3f} ({res['neg_where'] or 'n/a'})  "
          f"{'PASS' if res['neg_ok'] else 'FAIL'}")
    if 0 <= res["margin"] < MARGIN_WARN:
        print(f"[WARN] margin only {res['margin']:.3f}")
    print("RESULT: PASS" if res["pass"] else "RESULT: FAIL")
    return 0 if res["pass"] else 1


def cmd_status(args) -> int:
    cfg = _load_json(TPL_JSON)
    items = cfg.get("items", {})
    ready, missing = [], []
    for name, item in items.items():
        p = ROOT / str(item.get("path", "")).lstrip("./")
        exists = p.is_file()
        enabled = bool(item.get("enabled", True))
        row = f"{name:<24} enabled={int(enabled)} file={'Y' if exists else 'N'} " \
              f"kind={item.get('kind', 'icon'):<5} thr={item.get('threshold', 0.88)}"
        print(row)
        (ready if (exists and enabled) else missing).append(name)
    print(f"--- ready={len(ready)}  todo={len(missing)}  total={len(items)} ---")
    if args.todo_only:
        print("TODO: " + ", ".join(sorted(missing)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="template capture / verify / register helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rg = sub.add_parser("register", help="crop from frame, verify, then write json files")
    rg.add_argument("--name", required=True)
    rg.add_argument("--box", default="", help="normalized roi l,t,r,b")
    rg.add_argument("--xywh", default="", help="logical-point x,y,w,h (375x812 space)")
    rg.add_argument("--interactive", action="store_true",
                    help="drag-select ROI in a window (no need to type coordinates)")
    rg.add_argument("--shot", default="", help="use this screenshot instead of live capture")
    rg.add_argument("--group", default="_new", help="subdir under data/templates")
    rg.add_argument("--kind", default=None, choices=["icon", "text"],
                    help="default: keep the existing entry's kind")
    rg.add_argument("--match-mode", default=None, choices=["gray", "shape"],
                    help="default: keep the existing entry's match_mode")
    rg.add_argument("--extra-shots", default="",
                    help="dir of same-page frames NOT used for cropping, "
                         "used as independent positive samples")
    rg.add_argument("--neg-dir", default="",
                    help="negative frames dir: pages where this control must NOT appear "
                         "(default data/neg_frames)")
    rg.add_argument("--threshold", type=float, default=None)
    rg.add_argument("--purpose", default="", help="ASCII only; keeps existing if empty")
    rg.add_argument("--desc", default="", help="coords.json desc (ASCII only)")
    rg.add_argument("--control", default="", help="coords control name (default = --name)")
    rg.add_argument("--click-norm", default="", help="override click point x,y (normalized)")
    rg.add_argument("--no-coords", action="store_true")
    rg.add_argument("--force", action="store_true", help="write even if gate fails")
    rg.set_defaults(func=cmd_register)

    ck = sub.add_parser("check", help="verify an existing template on a frame")
    ck.add_argument("--name", required=True)
    ck.add_argument("--shot", default="")
    ck.add_argument("--box", default="", help="override normalized roi l,t,r,b")
    ck.add_argument("--threshold", type=float, default=None)
    ck.add_argument("--neg-dir", default="", help="negative frames dir")
    ck.set_defaults(func=cmd_check)

    st = sub.add_parser("status", help="list template registry and capture progress")
    st.add_argument("--todo-only", action="store_true")
    st.set_defaults(func=cmd_status)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        sys.exit(2)

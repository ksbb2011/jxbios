"""模板得分基线采集工具。

投屏后图像无透视/无光照变化/无镜头畸变，模板匹配分应非常集中——但每个模板
仍然有自己独立的得分分布。本工具采集每个模板在 N 帧上的正/负样本得分分布，
作为阈值定制的基线，同时识别"卡线模板"（正样本最低分 − 阈值 < 0.04）。

用法：
    # 默认：取 30 帧，每帧对所有启用模板跑一遍
    py -3.11 tools/template_baseline.py

    # 指定帧数 / 阈值 / 输出文件
    py -3.11 tools/template_baseline.py --frames 30 --output data/_probe/template_baseline.json

    # 仅采某几个模板
    py -3.11 tools/template_baseline.py --names 02_source_red,04_detail_share

实现说明（v1 简化版）：
    * 用 iMouse 取 N 帧，对每帧用 cv2.matchTemplate(TM_CCOEFF_NORMED) 跑每个
      模板全帧匹配，取最高分。
    * 这里只拿"帧间最高分分布"作为基线——粗略但能识别"普遍高分/普遍低分"的
      模板。完整的正/负样本区分需要测试集（v2 再说）。
    * 阈值默认用 templates.json 里写的；如缺省按 kind 推断（icon=0.88）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from core.devices.imouse_source import ImouseFrameSource


def load_templates(templates_json: Path) -> dict[str, dict[str, Any]]:
    cfg = json.loads(templates_json.read_text(encoding="utf-8"))
    return {k: v for k, v in cfg.get("items", {}).items()
            if v.get("enabled", True) and v.get("path")}


def threshold_for(tpl: dict[str, Any]) -> float:
    if "threshold" in tpl:
        return float(tpl["threshold"])
    return 0.88 if tpl.get("kind", "icon") == "icon" else 0.60


def match_score(frame: np.ndarray, tpl_img: np.ndarray) -> float:
    """单模板全帧匹配最高分（TM_CCOEFF_NORMED ∈ [-1, 1]）。"""
    if tpl_img.shape[0] > frame.shape[0] or tpl_img.shape[1] > frame.shape[1]:
        return float("nan")
    res = cv2.matchTemplate(frame, tpl_img, cv2.TM_CCOEFF_NORMED)
    return float(res.max())


def load_template_image(base_dir: Path, rel_path: str) -> np.ndarray | None:
    p = base_dir / rel_path
    if not p.exists():
        return None
    img = cv2.imread(str(p), cv2.IMREAD_COLOR)
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description="模板得分基线采集")
    ap.add_argument("--templates", default="config/templates.json")
    ap.add_argument("--template-dir", default="data")
    ap.add_argument("--frames", type=int, default=30, help="采样帧数（默认 30）")
    ap.add_argument("--names", default="", help="只采这几个，逗号分隔；默认全部")
    ap.add_argument("--output", default="docs/_probe/template_baseline.json")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    tpl_json = Path(args.templates)
    tpl_dir = Path(args.template_dir)
    templates = load_templates(tpl_json)
    if args.names:
        wanted = {n.strip() for n in args.names.split(",") if n.strip()}
        templates = {k: v for k, v in templates.items() if k in wanted}
    if not templates:
        print("没有可用模板", file=sys.stderr)
        return 1

    src = ImouseFrameSource(host=args.host)
    src.open()
    print(f"设备：{src.device.name} {src.device.width}x{src.device.height}")
    print(f"模板数：{len(templates)}")
    print(f"采样帧数：{args.frames}")

    # 预加载模板图片
    tpl_imgs: dict[str, np.ndarray] = {}
    for name, meta in templates.items():
        img = load_template_image(tpl_dir, meta["path"])
        if img is None:
            print(f"[跳过] {name}: 找不到图片 {meta['path']}", file=sys.stderr)
            continue
        tpl_imgs[name] = img
    if not tpl_imgs:
        print("所有模板图片都缺失", file=sys.stderr)
        return 1

    # 取 N 帧
    print("取帧中...", end="", flush=True)
    frames: list[np.ndarray] = []
    for i in range(args.frames):
        fs = src.read()
        if fs is None:
            print(f"\n[警告] 第 {i+1} 帧取不到", file=sys.stderr)
            break
        frames.append(fs.raw)
        print(".", end="", flush=True)
        time.sleep(0.05)
    print(f" 拿到 {len(frames)} 帧")

    if not frames:
        print("没拿到帧", file=sys.stderr)
        return 1

    # 跑每帧 × 每模板
    scores: dict[str, list[float]] = {n: [] for n in tpl_imgs}
    for fi, frame in enumerate(frames):
        for name, img in tpl_imgs.items():
            scores[name].append(match_score(frame, img))
        if (fi + 1) % 5 == 0:
            print(f"  跑完 {fi+1}/{len(frames)} 帧", flush=True)

    # 出报告
    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "device": f"{src.device.name} {src.device.width}x{src.device.height}",
        "frames": len(frames),
        "templates": {},
    }
    risky: list[str] = []
    print()
    print(f"{'模板':30s} {'p50':>7s} {'p05':>7s} {'min':>7s} {'阈值':>7s} {'余量':>7s} {'状态'}")
    for name, sc_list in scores.items():
        sc = np.array(sc_list, dtype=np.float64)
        sc = sc[~np.isnan(sc)]
        if len(sc) == 0:
            continue
        thr = threshold_for(templates[name])
        min_v = float(np.min(sc))
        p05 = float(np.percentile(sc, 5))
        p50 = float(np.percentile(sc, 50))
        margin = round(min_v - thr, 3)
        status = "✅" if margin >= 0.04 else ("⚠️" if margin >= 0 else "❌")
        if margin < 0.04:
            risky.append(name)
        report["templates"][name] = {
            "n": int(len(sc)),
            "p50": round(p50, 4),
            "p05": round(p05, 4),
            "min": round(min_v, 4),
            "max": round(float(np.max(sc)), 4),
            "mean": round(float(np.mean(sc)), 4),
            "threshold": thr,
            "margin": margin,
            "status": status,
            "image": templates[name]["path"],
            "kind": templates[name].get("kind", "icon"),
        }
        print(f"{name:30s} {p50:7.3f} {p05:7.3f} {min_v:7.3f} {thr:7.3f} {margin:+7.3f} {status}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n基线已写入：{out}")
    print(f"卡线模板（margin < 0.04）：{len(risy)}")
    for n in risky:
        print(f"  - {n}: margin={report['templates'][n]['margin']}")

    src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
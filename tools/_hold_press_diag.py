"""临时诊断工具：**按住不抬**，用来区分「落点不准」还是「压不实」。

用法：
    py -3.11 tools/_hold_press_diag.py 清空筛选 确认 --hold 12
    py -3.11 tools/_hold_press_diag.py 确认 --z 6.1 --hold 15

为什么这么测：
    电容屏的「点击」和「滑动」都依赖**压下去的那一下被识别到**。若下压不够实，
    两个都会失效 —— 这正是 2026-09-13 15:43 那轮的现象：连续上滑 5 次后省列表
    纹丝不动、找不到「浙江」，最后 `dest 设置失败`。

本工具做三件事，全部留证（截图存 data/shots/hold_*.png）：
  1. 用**与 App 完全相同**的定位逻辑算出目标中心：
     同一 OCR 引擎 + 同一归一化 ROI（见 core/flow/city_picker.py）+ 取最靠上的命中框；
  2. 移动 → 下压 → **保持按住 hold 秒（不抬起）** → 抬起；按下中/抬起后各截一张；
  3. 用「按下前 vs 抬起后」整屏差异**客观**判断触点有没有被 App 收到：
     变化明显 = 触点生效（落点与下压都够）；几乎不变 = 这次按压 App 没收到。

安全：Z 由 calib_common.ArmClient 硬钳制在 6.2 以内；open() 时已注册 atexit/信号
处理器，任何异常退出（含 Ctrl+C）都会 reset + close，不会把机械臂停在压屏状态。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.config_store import ConfigStore  # noqa: E402
from core.devices.imouse_source import ImouseFrameSource  # noqa: E402
from core.vision.imgio import imwrite_unicode  # noqa: E402
from core.vision.ocr import OcrEngine  # noqa: E402
from tools.calib_common import ArmClient  # noqa: E402

# 与 App 完全相同的归一化 ROI（core/flow/city_picker.py）
ROIS = {
    "确认": (0.0, 0.62, 1.0, 0.99),
    "清空筛选": (0.0, 0.84, 0.5, 1.0),
}


def norm_to_roi(w: int, h: int, norm) -> tuple:
    left, top, right, bottom = [float(v) for v in norm]
    x0, y0 = int(round(left * w)), int(round(top * h))
    x1, y1 = int(round(right * w)), int(round(bottom * h))
    return (x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))


def _diff_ratio(a, b) -> float:
    """两帧「明显变化」的像素占比（阈值 25 灰阶）。"""
    if a is None or b is None or a.shape != b.shape:
        return -1.0
    d = cv2.absdiff(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), cv2.cvtColor(b, cv2.COLOR_BGR2GRAY))
    return float((d > 25).mean())


def main() -> int:
    ap = argparse.ArgumentParser(description="按住不抬诊断：落点不准 vs 压不实")
    ap.add_argument("targets", nargs="+", help=f"要按的目标文字，可选 {list(ROIS)}")
    ap.add_argument("--z", type=float, default=None, help="下压深度（默认取 config 的 z_press）")
    ap.add_argument("--hold", type=float, default=12.0, help="按住不抬的秒数（默认 12）")
    ap.add_argument("--arm-url", default="http://127.0.0.1:8082/MyWcfService/getstring")
    ap.add_argument("--arm-com", default="COM4")
    args = ap.parse_args()

    store = ConfigStore()
    z = float(args.z if args.z is not None else (store.get("hardware", "calibration.z_press", 5.9) or 5.9))
    settle = float(store.get("hardware", "delays.tap_settle", 0.15) or 0.15)
    dwell = float(store.get("hardware", "delays.tap_dwell", 0.15) or 0.15)
    cal = store.get("hardware", "calibration", {}) or {}
    act = cal.get("actuation") or {}
    ax_c = [float(v) for v in act.get("ax", [0.0, 1.0, 0.0, 0.0])]
    ay_c = [float(v) for v in act.get("ay", [0.0, 0.0, 1.0, 0.0])]

    src = ImouseFrameSource(store)
    if not src.open():
        print("★ iMouse 取帧源打不开（服务/投屏是否正常？）")
        return 1
    eng = OcrEngine(store, log=lambda lv, m: print(f"  [ocr:{lv}] {m}"))
    out_dir = Path("data/shots")

    arm = ArmClient(args.arm_url, com=args.arm_com)
    try:
        arm.open()
    except Exception as exc:  # noqa: BLE001 串口被占时给一次机会（会弹 UAC）
        print(f"★ 打开串口失败：{exc}")
        print("  串口可能被占用，重试一次（会自动重启 JxbService，可能弹 UAC）…")
        arm.open(auto_restart=True)
    arm.reset()
    time.sleep(1.0)
    print(f"机械臂已就绪（{args.arm_com}），下压 z={z}，按住 {args.hold:.0f}s/个\n")

    results = []
    for target in args.targets:
        direct = None
        if target not in ROIS:
            # 支持 "340,342" 形式的直接按点（不经 OCR）：
            # 用于在「标定画布覆盖区」内做对照实验，判断是不是底部外推区的问题。
            try:
                direct = tuple(float(v) for v in target.replace("，", ",").split(","))
                if len(direct) != 2:
                    raise ValueError
            except Exception:
                print(f'跳过未知目标 {target}（支持 {list(ROIS)} 或 "x,y" 坐标）')
                continue
        fs = src.read()
        if fs is None:
            print(f"[{target}] 取帧失败，跳过")
            continue
        raw = fs.raw
        h, w = raw.shape[:2]
        print(f"=== {target} ===")
        if direct is not None:
            cx, cy = direct
            print(f"  直接按点（不经 OCR）：({cx:.0f},{cy:.0f})")
        else:
            boxes = eng.ocr(raw)
            x0, y0, rw, rh = norm_to_roi(w, h, ROIS[target])
            cands = [
                b
                for b in boxes
                if target in (b.text or "")
                and x0 <= b.center[0] <= x0 + rw
                and y0 <= b.center[1] <= y0 + rh
            ]
            print(f"  ROI 像素 = {(x0, y0, rw, rh)}（归一化 {ROIS[target]}）")
            if not cands:
                hits = [b for b in boxes if target in (b.text or "")]
                print(f"  ★ ROI 内没找到「{target}」；全屏命中 {[(b.text, b.rect) for b in hits]}")
                print("     先确认手机页面正停在该面板上（清空筛选/确认 都在底部操作条）")
                results.append((target, None, None, None))
                continue
            cands.sort(key=lambda b: b.rect[1])  # 与 App 的 find_text 一致：取最靠上的
            box = cands[0]
            cx, cy = box.center
            print(f"  命中文字 {box.text!r} rect={box.rect} → 点击中心 ({cx},{cy})")
        u, v = cx / w, cy / h
        ax = ax_c[0] + ax_c[1] * u + ax_c[2] * v + ax_c[3] * u * v
        ay = ay_c[0] + ay_c[1] * u + ay_c[2] * v + ay_c[3] * u * v
        print(f"  映射机械臂 ({ax:.2f},{ay:.2f})，下压 z={z}")

        stamp = time.strftime("%H%M%S")
        p1 = out_dir / f"hold_{target}_{stamp}_1_before.png"
        p2 = out_dir / f"hold_{target}_{stamp}_2_held.png"
        p3 = out_dir / f"hold_{target}_{stamp}_3_after.png"
        imwrite_unicode(p1, raw)

        arm.move(ax, ay)
        time.sleep(settle)
        arm.press(z)
        time.sleep(dwell)
        print(f"  ▶ 已下压并**保持按住** {args.hold:.0f}s —— 请看笔尖是否正按在「{target}」上")
        held = src.read()
        if held is not None:
            imwrite_unicode(p2, held.raw)
        time.sleep(max(0.0, args.hold - 0.3))
        arm.release()
        time.sleep(0.8)
        after = src.read()
        if after is not None:
            imwrite_unicode(p3, after.raw)

        ratio = _diff_ratio(raw, after.raw if after is not None else None)
        verdict = "触点生效（App 有反应）" if ratio > 0.002 else "★ 几乎无变化：这次按压 App 没收到"
        print(f"  ◀ 已抬起。按下前 vs 抬起后 变化像素 = {ratio * 100:.2f}% → {verdict}")
        print(f"  截图：{p1.name} / {p2.name} / {p3.name}\n")
        results.append((target, (cx, cy), (ax, ay), ratio))

    print("=== 汇总 ===")
    for target, pt, ap_, ratio in results:
        if pt is None:
            print(f"  {target}: 未定位到")
        else:
            print(f"  {target}: 逻辑点{pt} → 机械臂({ap_[0]:.2f},{ap_[1]:.2f}) 画面变化 {ratio * 100:.2f}%")

    try:
        arm.reset()
        arm.close()
    except Exception:  # noqa: BLE001
        pass
    src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""城市面板网格扫描诊断（只读工具，**不改任何配置**）。

为什么需要：
    2026-09-13 现场连续踩了两个坑，只看运行日志很难定位是哪一个：
      ① 幅度不对：小幅度却沿用翻页时长 → 指尖速度只有正常上滑的 1/7，iOS 当拖拽，
         列表基本不动；反过来幅度太大又会一次掠过整个省列表；
      ② 层级不对：面板停在「湖南 > 益阳」子级时，"省级 ROI"里当然找不到「江苏」，
         然后 8 次上滑还会误报"已到列表尽头"。
    本工具把这两件事**当着你面拆开**：先报当前层级（根级/子级），再逐步上滑并打印
    每一步的"位移量 Δ"与是否升级幅度，最后报"第几步找到 / 到尽头时本屏可见哪些文字"。

和运行时是同一套参数：默认读 `config/runtime.json` 的 `swipe.grid_*`
（grid_distance_ratio / grid_duration_ms_range / grid_move_epsilon / grid_escalate /
grid_max_step_ratio），可用命令行覆盖，便于现场试出更好的值再回填配置。

用法（页面由人工摆好，和 calibrate_tap_ui.py 一样）：
    py -3.11 tools\\city_grid_scan.py 江苏                 # 省级找「江苏」
    py -3.11 tools\\city_grid_scan.py 南京 --level 1        # 市级找「南京」
    py -3.11 tools\\city_grid_scan.py 江苏 --dry            # 只看层级+当前可见文字，不滑
    py -3.11 tools\\city_grid_scan.py 江苏 --ratio 0.1 --duration 150 220 --steps 15
    py -3.11 tools\\city_grid_scan.py 江苏 --no-top         # 不先归顶（默认会先归顶）

安全：
    * 只用 ArmClient 做"下滑/上滑"手势，**从不下压戳点**；Z 由 calib_common 硬钳制在 6.2 内；
    * 任何路径都不写配置（要写补偿/标定请用 calibrate_*.py 的显式开关）。
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.config_store import ConfigStore  # noqa: E402
from core.devices.imouse_source import ImouseFrameSource  # noqa: E402
from tools.calib_common import ArmClient  # noqa: E402

DEFAULT_ARM_URL = "http://127.0.0.1:8082/MyWcfService/getstring"
DEFAULT_ARM_COM = "COM4"

# 网格各层级的扫描 ROI（归一化，与 core/flow/city_picker.py 的 GRID_ROIS 保持一致）
GRID_ROIS = (
    (0.02, 0.44, 0.98, 0.86),   # 0 = 省列表
    (0.02, 0.36, 0.98, 0.86),   # 1 = 市列表
    (0.02, 0.42, 0.98, 0.86),   # 2 = 区列表
)


def _delta(a, b) -> float:
    """两帧位移量（32x32 灰度平均绝对差，0~255），与 actions.py::_frame_delta 同口径。"""
    try:
        ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
        if ga.shape != gb.shape:
            return 255.0
        sa = cv2.resize(ga, (32, 32), interpolation=cv2.INTER_AREA)
        sb = cv2.resize(gb, (32, 32), interpolation=cv2.INTER_AREA)
        return float(cv2.mean(cv2.absdiff(sa, sb))[0])
    except Exception:  # noqa: BLE001
        return 255.0


def _roi_px(frame, norm):
    h, w = frame.shape[:2]
    l, t, r, b = norm
    return int(l * w), int(t * h), int(r * w), int(b * h)


class GridScan:
    def __init__(self, store: ConfigStore, arm: ArmClient, cfg: dict, log=print) -> None:
        self.store = store
        self.arm = arm
        self.cfg = cfg
        self.log = log

    # ---------------------------------------------------------------- 取帧 / OCR
    def frame(self):
        fs = self.src.read()
        if fs is None:
            raise RuntimeError("取帧失败（iMouse 掉线？）")
        return fs.raw

    def texts(self, raw, roi_norm) -> list:
        """ROI 内的文字（按 y 排序，便于肉眼看行序）。"""
        x0, y0, x1, y1 = _roi_px(raw, roi_norm)
        panel = raw[y0:y1, x0:x1]
        out = []
        try:
            for box in self._ocr.ocr(panel):
                t = (box.text or "").strip()
                if len(t) >= 2:
                    out.append((int(box.top), t))
        except Exception:  # noqa: BLE001
            return []
        out.sort()
        return [t for _, t in out]

    def level(self, raw) -> str:
        """根级 / 子级 / 未知：只看正向证据（与 city_picker::_panel_level 同口径）。"""
        for t in ("返回上一级", "请选择市", "请选择区"):
            if self._find(t, raw) is not None:
                return "sub"
        for t in ("请选择出发地", "请选择目的地"):
            if self._find(t, raw) is not None:
                return "root"
        return "unknown"

    def _find(self, text, raw):
        try:
            for box in self._ocr.ocr(raw):
                if text in (box.text or ""):
                    return box
        except Exception:  # noqa: BLE001
            return None
        return None

    # ---------------------------------------------------------------- 手势
    def _drag(self, y_from: float, y_to: float, duration_ms: float) -> None:
        """按下 → 分点移动 → 抬起（不下压到屏幕之外，只做滑动）。"""
        w = self.cfg["width"]
        x = w * 0.5
        z = self.cfg["z"]
        self.arm.move(*self._to_arm(x, y_from))
        time.sleep(0.05)
        self.arm.press(z)
        n = 10
        dt = max(0.008, duration_ms / 1000.0 / n)
        for i in range(1, n + 1):
            y = y_from + (y_to - y_from) * i / n
            self.arm.move(*self._to_arm(x, y))
            time.sleep(dt)
        self.arm.release()
        time.sleep(0.35)   # 等惯性滚完再取帧

    def _to_arm(self, px: float, py: float):
        """像素 → 机械臂坐标（与 ArmClient 同一套双线性模型）。"""
        cal = self.store.get("hardware", "calibration", {}) or {}
        act = cal.get("actuation") or {}
        ax = [float(v) for v in act.get("ax", [0.0, 1.0, 0.0, 0.0])]
        ay = [float(v) for v in act.get("ay", [0.0, 0.0, 1.0, 0.0])]
        u = max(0.0, min(1.0, px / self.cfg["width"]))
        v = max(0.0, min(1.0, py / self.cfg["height"]))
        return (ax[0] + ax[1] * u + ax[2] * v + ax[3] * u * v,
                ay[0] + ay[1] * u + ay[2] * v + ay[3] * u * v)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="城市面板网格扫描诊断（只读，不改配置）")
    ap.add_argument("target", help="要找的文字，如 江苏 / 南京 / 江宁")
    ap.add_argument("--level", type=int, default=0, choices=(0, 1, 2), help="0=省 1=市 2=区")
    ap.add_argument("--steps", type=int, default=12, help="最多上滑几次（默认取 grid_max_swipes）")
    ap.add_argument("--ratio", type=float, default=None, help="单步幅度（屏高比例，默认取 grid_distance_ratio）")
    ap.add_argument("--duration", type=float, nargs=2, default=None,
                    metavar=("MIN", "MAX"), help="单步时长毫秒（默认取 grid_duration_ms_range）")
    ap.add_argument("--epsilon", type=float, default=None, help="位移阈值（默认取 grid_move_epsilon）")
    ap.add_argument("--escalate", type=float, default=None, help="无位移时的放大倍率（默认取 grid_escalate）")
    ap.add_argument("--max-step", type=float, default=None, help="单步幅度上限（默认取 grid_max_step_ratio）")
    ap.add_argument("--no-top", action="store_true", help="不先归顶（默认先小幅下滑到顶部）")
    ap.add_argument("--dry", action="store_true", help="只看层级与当前可见文字，不做任何滑动")
    ap.add_argument("--arm-url", default=DEFAULT_ARM_URL)
    ap.add_argument("--arm-com", default=DEFAULT_ARM_COM)
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    store = ConfigStore()
    sw = store.section("runtime").get("swipe", {}) or {}

    def _cfg(key, default):
        try:
            v = sw.get(f"grid_{key}", default)
            return float(v if v is not None else default)
        except Exception:  # noqa: BLE001
            return float(default)

    # 命令行覆盖优先，其次 runtime.json 的 swipe.grid_*，最后代码默认值
    ratio = float(args.ratio if args.ratio is not None else _cfg("distance_ratio", 0.12))
    eps = float(args.epsilon if args.epsilon is not None else _cfg("move_epsilon", 0.35))
    esc = float(args.escalate if args.escalate is not None else _cfg("escalate", 1.6))
    cap = float(args.max_step if args.max_step is not None else _cfg("max_step_ratio", 0.5))
    steps = int(args.steps if args.steps else (sw.get("grid_max_swipes", 12) or 12))
    dur = list(args.duration) if args.duration else [float(x) for x in (sw.get("grid_duration_ms_range") or [180, 260])]
    lw, lh = store.logical_size
    roi = GRID_ROIS[min(args.level, 2)]

    print("=" * 68)
    print(f"  网格扫描诊断：找「{args.target}」  层级={('省', '市', '区')[min(args.level, 2)]}")
    print(f"  参数：幅度 {ratio:.2f} 屏（上限 {cap:.2f}）｜时长 {dur[0]:.0f}~{dur[1]:.0f}ms｜"
          f"最多 {steps} 步｜位移阈值 {eps}｜升幅 ×{esc}")
    print(f"  ROI（归一化）={roi}   屏幕={lw}x{lh}")
    print("=" * 68)

    scan = GridScan(store, None, {"width": lw, "height": lh, "z": float(store.get("hardware", "calibration.z_press", 5.9) or 5.9)})
    src = ImouseFrameSource(store, log=lambda lv, m: None)
    if not src.open():
        print("[失败] iMouse 取帧源打不开（投屏在线吗？）")
        return 1
    scan.src = src
    from core.vision.ocr import OcrEngine  # noqa: PLC0415
    scan._ocr = OcrEngine(store, log=lambda lv, m: None)

    arm = None
    try:
        raw = scan.frame()
        lvl = scan.level(raw)
        print(f"  面板层级 = {lvl}" + ("   ← ⚠️ 不在省级：先手动点「返回上一级」退到省级再跑" if lvl == "sub" else ""))
        print(f"  当前 ROI 内可见文字：{' / '.join(scan.texts(raw, roi)[:30]) or '（没识别到）'}")
        if args.dry:
            print("（--dry：到此为止，不做任何滑动）")
            return 0

        arm = ArmClient(args.arm_url, com=args.arm_com)
        arm.open(auto_restart=True)
        scan.arm = arm

        # 归顶：小幅下滑到画面不再变化（最多 3 次）
        if not args.no_top:
            prev = None
            for i in range(3):
                raw = scan.frame()
                if prev is not None and _delta(prev, raw) < eps:
                    print(f"  归顶完成（第 {i} 次下滑后画面不再变化）")
                    break
                prev = raw
                scan._drag(lh * 0.6, lh * 0.6 + lh * 0.12, dur[1])
            else:
                print("  归顶：下滑 3 次仍未静止（可能已在顶部，继续）")

        # 逐步上滑扫描
        ratio_i = ratio
        no_move = 0
        for i in range(steps + 1):
            raw = scan.frame()
            hit = scan._find(args.target, raw)
            if hit is not None:
                print(f"  ✅ 第 {i} 步找到「{args.target}」@ ({int(hit.cx)},{int(hit.cy)})"
                      f"（当前单步幅度 {ratio_i:.2f} 屏）")
                return 0
            if i == steps:
                print(f"  ⚠️ 滑了 {steps} 步仍未找到；本屏可见文字："
                      f"{' / '.join(scan.texts(raw, roi)[:30])}")
                return 2
            prev = raw
            scan._drag(lh * 0.667, lh * 0.667 - lh * ratio_i, sum(dur) / 2)
            raw2 = scan.frame()
            d = _delta(prev, raw2)
            flag = ""
            if d < eps:
                no_move += 1
                if no_move >= 2:
                    print(f"  [{i + 1}/{steps}] Δ={d:.2f}  连续 2 次几乎未动 → 判定已到列表尽头")
                    print(f"  本屏可见文字：{' / '.join(scan.texts(raw2, roi)[:30])}")
                    return 3
                ratio_i = min(ratio_i * esc, cap)
                flag = f"  ← 未移动，幅度升到 {ratio_i:.2f} 屏重试"
            else:
                no_move = 0
                ratio_i = ratio
            print(f"  [{i + 1}/{steps}] 幅度 {ratio_i:.2f} 屏  Δ={d:.2f}{flag}")
        return 0
    except KeyboardInterrupt:
        print("\n[中断] 已捕获 Ctrl+C，交由 ArmClient 的退出保护收尾")
        return 130
    finally:
        try:
            src.close()
        except Exception:  # noqa: BLE001
            pass
        if arm is not None:
            try:
                arm.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())

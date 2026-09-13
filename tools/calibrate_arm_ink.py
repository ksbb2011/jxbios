"""机械臂映射标定（**墨点版**，推荐）—— 用 iMouse 截图找墨点，无需网页。

================================================================================
🔒 **已固化模块 —— 测试通过，禁止擅自修改**  （用户于 2026-09-09 确认"固化"）

固化依据（真机实测，iPhone X + iMouse 专业版）：
    * 校准 36 点（31 有效）→ 验收 **16/16 全中**
    * 精度：mean **0.95pt** / max **1.57pt**
    * 独立复检（16 个全新随机点）：mean **0.93pt** / max **1.74pt**
    * 阈值 3.0pt，两次结果一致 → 模型稳定，非偶然拟合

固化范围（本文件）：
    InkCalibrator（settle/dwell 时序）· find_ink_point（两级搜索 + 加权重心）
    _targets_in_canvas（canvas 限制 + margin 内缩 + jitter 抖动）· _scan_z
    _report · main 流程（scan-z / probe-one / 校准 / verify-only）

🔑 **关键参数（改动任何一个都必须重新验证）**：
    settle_ms=1500   dwell_ms=2000    ← 小于此值电容笔来不及留痕（曾误判为硬件故障）
    ink_thresh=15    min_area=2       ← 粗笔（第 3 个）适用；换细笔需降到 8
    canvas="20 170 345 660"           ← iPhone X 备忘录可点红框，戳出会退出界面
    margin=0.10                       ← 画布四角极值戳不到，必须内缩
    jitter=25.0                       ← 避开旧墨迹，否则无差分
    z_press=6.0（min_z 5.6 + 加深 0.4mm）
    ⛔ Z 硬上限 6.2 —— 越大越深，超过会**点碎屏幕**（用户 2026-09-09 明确）
    canvas 范围校验=±6pt  ← 2026-09-13 新增：墨点落在 canvas 外一律视为"未检测到"并告警
                             （映射失效时笔会戳到画布外，界面变化曾被当成墨点，算出过偏浅的 z_press）
    arm_range 自动重算     ← 2026-09-13 新增：标定成功后按四角映射写回 calibration.arm_range
                             （此前没有任何工具写它，漏同步会让屏幕边角被错误 clamp）

⚠️ 修改前**必须先征得用户同意**，改完必须重跑 `--verify-only` 复检。
================================================================================

## 它解决什么

投屏解决了"看得准"（截图就是手机屏幕本身），但**没解决"点得准"**：
机械臂不知道"屏幕 (243, 533)"该把笔移到哪个物理坐标。

## 和网页版的区别（网页版已搁置，保留做对照）

| | 网页版 calibrate_screen_arm.py | **墨点版（本文件）** |
|---|---|---|
| 怎么知道戳到哪 | 手机浏览器 touch 事件上报 | **iMouse 截图找墨点** |
| 要不要加到主屏 | ⚠️ 要（最麻烦，必须现场） | ✅ **不要** |
| 手机停在哪 | 标定网页 | **备忘录 / 任何画图 App 的白纸** |
| 可视区一致性 | 靠 standalone 保证（易翻车） | ✅ 原生 App 天生真全屏 |
| 精度 | 触摸坐标 <1pt | 截图像素 **1px**（375×812） |

墨点版从根上绕开了"standalone 全屏"这个最大的坑——**任何原生 App 本身就是
真全屏**，不需要为了模拟全屏去折腾网页。

## 原理

```
手机打开备忘录 → 新建 → 画笔（纯白画布）
    ↓
截图 A → arm.move(ax,ay) → press(z_press) → release
    ↓
截图 B
    ↓
diff = A_gray - B_gray（墨点 = 变暗的地方）
    ↓
在目标点附近找最大连通域的重心 → 实际落点（逻辑点，ratio=1.0 直通）
    ↓
64 个点跑完 → 拟合双线性 ax = c0 + c1*u + c2*v + c3*u*v
    ↓
随机 10 点验收，≤3px 才写入 config/hardware.json
```

用**差分**而不是"绝对找黑点"：这样画布上已有的旧墨点不干扰，只看新增的。

## 用法

```powershell
# 0) 先确认手机停在备忘录白纸（本机实测就是这个）
# 1) 先测下压深度（只用做一次；扫描本身也被 6.2 硬顶限制）
py -3.11 tools\calibrate_arm_ink.py --scan-z

# 2) 跑墨点标定
py -3.11 tools\calibrate_arm_ink.py --z-press 6.0

# 3) 只验收（用已有模型复检）
py -3.11 tools\calibrate_arm_ink.py --verify-only

# 调试：先单独戳一个点，看看墨点能不能检测到
py -3.11 tools\calibrate_arm_ink.py --probe-one --z-press 6.0
```

> ⛔ **Z 轴方向 = 越大越深**，硬上限 **6.2**。任何 `--z-press` / `--z-max`
> 超过 6.2 都会被拒绝或钳制。旧文档里的 7.1 / 7.8 / 8.5 均已废弃。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--z-press` | 6.0 | **必须改成 --scan-z 实测值**；⛔ 硬上限 6.2，超了碎屏 |
| `--grid` | 8 | 校准网格 N×N（8 = 64 点） |
| `--rounds` | 2 | 校准轮数。第 1 轮粗估、第 2 轮用第 1 轮模型铺满全屏 |
| `--canvas` | 15 110 360 700 | 画布可用区（逻辑点 x0 y0 x1 y1），避开顶部工具栏与底部画笔栏 |
| `--max-error-pt` | 3.0 | 验收阈值，超了**不写入** |
| `--ink-thresh` | 30 | 差分灰度阈值，检测不到墨点就调低 |
| `--min-area` | 4 | 墨点最小像素面积，滤噪点 |
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from core.devices.imouse_source import ImouseFrameSource
from tools.calib_common import (
    ArmClient,
    Sample,
    Target,
    Z_HARD_MAX,
    Z_PRESS_SAFE,
    coarse_predict,
    fit_bilinear,
    grid_targets,
    load_arm_range,
    predict,
    random_targets,
    write_actuation,
    write_arm_range_from_model,
    write_z_press,
)

_LOG = logging.getLogger("calib.ink")


# ================================================================ 墨点检测
def _weighted_centroid(diff: np.ndarray, mask: np.ndarray,
                       labels: np.ndarray, comp_id: int,
                       off_x: int, off_y: int) -> Optional[tuple[float, float]]:
    """在指定连通域内按 diff 灰度做加权重心（亚像素）。"""
    comp = (labels == comp_id)
    weights = diff.astype(np.float64) * comp
    total = weights.sum()
    if total <= 0:
        return None
    ys, xs = np.nonzero(comp)
    gx = float((xs * weights[ys, xs]).sum() / total)
    gy = float((ys * weights[ys, xs]).sum() / total)
    return (off_x + gx, off_y + gy)


def find_ink_point(before: np.ndarray, after: np.ndarray, center: tuple[float, float],
                   radius: int = 90, ink_thresh: int = 20,
                   min_area: int = 2, max_area: int = 3000) -> Optional[tuple[float, float]]:
    """在 (before, after) 的差分图上找墨点重心。**两级搜索**。

    第 1 级：只查以 center 为中心、边长 2*radius 的窗口（抗干扰，优先）。
    第 2 级：窗口内找不到就**全屏找**（应对粗估偏差大的情况）。

    ⚠️ **两条实测教训（2026-09-09）**：

    1. 电容笔在备忘录上留下的墨点可能**只有 3×2 像素**（diff 峰值 100、变化像素 5 个）。
       早期版本加了 3×3 形态学开运算"去噪"，结果**把真实墨点整个滤掉**，
       26 档 z 扫描全部误报"未触发"。→ 所以这里**不做形态学滤波**。

    2. 首轮用粗估 arm_range 时，**实际落点与目标可能差 20~44px**（实测）。
       早期窗口半径只有 55，墨点落在窗口外 → 64 点里只检测到 10 个。
       → 所以加第 2 级全屏兜底，并用 max_area 排除"整屏 UI 跳转"这类大面积变化。
    """
    ga = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY).astype(np.int16)
    gb = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY).astype(np.int16)
    # 墨点 = 变暗 → before - after 为正
    diff = np.clip(ga - gb, 0, 255).astype(np.uint8)

    h, w = diff.shape[:2]
    cx, cy = int(round(center[0])), int(round(center[1]))

    # ---- 第 1 级：目标附近窗口
    x0, y0 = max(0, cx - radius), max(0, cy - radius)
    x1, y1 = min(w, cx + radius), min(h, cy + radius)
    if x1 > x0 and y1 > y0:
        sub = diff[y0:y1, x0:x1]
        _, mask = cv2.threshold(sub, ink_thresh, 255, cv2.THRESH_BINARY)
        num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        if num > 1:
            areas = stats[1:, cv2.CC_STAT_AREA]
            best = 1 + int(np.argmax(areas))
            if min_area <= areas[best - 1] <= max_area:
                pt = _weighted_centroid(sub, mask, labels, best, x0, y0)
                if pt is not None:
                    return pt

    # ---- 第 2 级：全屏兜底（粗估偏差大时墨点会跑到窗口外）
    _, mask = cv2.threshold(diff, ink_thresh, 255, cv2.THRESH_BINARY)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if num <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    # 只在合理面积范围内找最大的（排除整屏变化 = UI 跳转）
    valid = [i for i in range(len(areas)) if min_area <= areas[i] <= max_area]
    if not valid:
        return None
    best = 1 + int(max(valid, key=lambda i: areas[i]))
    return _weighted_centroid(diff, mask, labels, best, 0, 0)


def canvas_ink_ratio(img: np.ndarray, canvas: tuple[int, int, int, int],
                     gray_thresh: int = 160) -> float:
    """统计画布区域内"暗像素"占比，用来判断画布干不干净。"""
    x0, y0, x1, y1 = canvas
    roi = img[y0:y1, x0:x1]
    if roi.size == 0:
        return 0.0
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return float((gray < gray_thresh).mean())


def parse_canvas(spec: str) -> tuple[int, int, int, int]:
    parts = [int(v) for v in spec.replace(",", " ").split()]
    if len(parts) != 4:
        raise ValueError(f"--canvas 需要 4 个数：x0 y0 x1 y1，收到 {spec!r}")
    return (parts[0], parts[1], parts[2], parts[3])


def _targets_in_canvas(n: int, width: int, height: int,
                       canvas: tuple[int, int, int, int],
                       margin: float, seed: int,
                       jitter: float = 0.0) -> list[Target]:
    """在 --canvas 矩形内生成网格靶点（--margin 内缩、jitter 抖动）。

    jitter 的作用：画布上若已有旧墨点，新点若与原网格点重合则**不产生差分**
    （那里已经是黑的）。抖动让新点错开旧墨迹。
    标定记录的是「实际墨点位置 ↔ 当时下达的 arm 坐标」，
    所以目标点的轻微偏移**不影响标定结果**——可以放心抖。
    """
    x0, y0, x1, y1 = canvas
    if margin > 0:
        dx, dy = (x1 - x0) * margin, (y1 - y0) * margin
        x0, y0, x1, y1 = x0 + dx, y0 + dy, x1 - dx, y1 - dy
    # 用 ceil 保证点数 >= n（例如 n=36 -> g=6 正好 36 点；n=10 -> g=4 得 16 点）
    # ⚠️ u,v **必须是全屏归一化**（tx/width, ty/height），不能是 canvas 内的 0~1。
    # 曾在这里把 canvas 内的 0~1 当成 u,v 传给 coarse_predict/predict，
    # 导致 Y 方向整体偏移一个 canvas 上边距（约 170px），标定误差飙到 200pt。
    g = max(1, int(math.ceil(math.sqrt(max(1, n)))))
    rng = np.random.default_rng(seed)
    out: list[Target] = []
    coords = np.linspace(0.0, 1.0, g)
    for cu in coords:
        for cv in coords:
            tx = float(x0 + cu * (x1 - x0))
            ty = float(y0 + cv * (y1 - y0))
            if jitter > 0:
                tx += float(rng.uniform(-jitter, jitter))
                ty += float(rng.uniform(-jitter, jitter))
                # 抖动后夹回可点区，避免戳出红框
                tx = min(max(tx, canvas[0]), canvas[2])
                ty = min(max(ty, canvas[1]), canvas[3])
            out.append(Target(tx=tx, ty=ty, u=tx / width, v=ty / height))
    return out


# ================================================================ 标定流程
class InkCalibrator:
    def __init__(self, source: ImouseFrameSource, arm: ArmClient,
                 z_press: float, canvas: tuple[int, int, int, int],
                 width: int, height: int,
                 ink_thresh: int = 20, min_area: int = 2, max_area: int = 3000,
                 radius: int = 90,
                 # ⚠️ 实测（2026-09-09）：这两个值直接决定"能不能画出墨点"，是最关键的参数。
                 #   逐步放大实测：150/150 → 64 点只中 10 个；500/1200 → 仍 0 命中；
                 #   **1500/2000 → 中心点稳定出 263px 墨迹，偏差仅 (+6,+5)**。
                 #   根因是**电容笔需要足够接触时长**才能在备忘录上留痕，
                 #   不是移动没到位，也不是屏幕不平（曾误判过）。不要再调小。
                 #   注意：探针与 z 扫描也必须用这两个值，不能硬编码。
                 settle_ms: int = 1500, dwell_ms: int = 2000) -> None:
        self.src = source
        self.arm = arm
        self.z_press = z_press
        self.canvas = canvas
        self.width = width
        self.height = height
        self.ink_thresh = ink_thresh
        self.min_area = min_area
        self.max_area = max_area
        self.radius = radius
        self.settle_ms = settle_ms
        self.dwell_ms = dwell_ms

    def _shoot(self) -> np.ndarray:
        fs = self.src.read()
        if fs is None:
            raise RuntimeError("取帧失败（iMouse 掉线？）")
        return fs.raw

    # ------------------------------------------------ 墨点 + 画布范围校验（2026-09-13）
    _CANVAS_TOL_PT = 6.0   # 允许超出画布边缘的容差（1 个步长量级）

    def _canvas_ok(self, pt: tuple[float, float]) -> bool:
        """墨点是否落在画布内（含 _CANVAS_TOL_PT 容差）。

        为什么必须校验：XY 映射失效时笔会戳到**画布外**（实测：墨点检测在 x≈10 报"触发"，
        而画布从 x=20 起）——那根本不是墨点，而是戳中工具栏/菜单产生的**界面变化**。
        不校验就会算出偏浅的 z_press（当时算成 5.1/5.8），而且日志上看不出任何异常。
        """
        x0, y0, x1, y1 = self.canvas
        t = self._CANVAS_TOL_PT
        return (x0 - t) <= pt[0] <= (x1 + t) and (y0 - t) <= pt[1] <= (y1 + t)

    def ink_point(self, before: np.ndarray, after: np.ndarray,
                  center: tuple[float, float]) -> Optional[tuple[float, float]]:
        """find_ink_point + 画布范围校验；超出画布一律视为"未检测到"并告警。

        三条调用路径（collect / _scan_z / probe）都走这里，保证判据一致。
        """
        pt = find_ink_point(before, after, center, radius=self.radius,
                            ink_thresh=self.ink_thresh, min_area=self.min_area,
                            max_area=self.max_area)
        if pt is None:
            return None
        if not self._canvas_ok(pt):
            _LOG.warning("墨点 (%.1f,%.1f) 落在画布 %s 之外（容差 %.0fpt）→ 视为未检测到；"
                         "多半是映射已失效、笔戳到了画布外的界面元素",
                         pt[0], pt[1], self.canvas, self._CANVAS_TOL_PT)
            return None
        return pt

    def collect(self, targets: list[Target],
                model: dict | None,
                rng_x: tuple[float, float], rng_y: tuple[float, float],
                label: str) -> list[Sample]:
        """走一轮：每个靶点戳一下，用墨点检测读回实际落点。"""
        samples: list[Sample] = []
        for i, t in enumerate(targets):
            if model is not None:
                ax, ay = predict(model, t.u, t.v)
            else:
                ax, ay = coarse_predict(rng_x, rng_y, t.u, t.v)

            before = self._shoot()
            self.arm.move(ax, ay)
            time.sleep(self.settle_ms / 1000.0)
            self.arm.press(self.z_press)
            time.sleep(self.dwell_ms / 1000.0)
            self.arm.release()
            time.sleep(0.25)          # 等墨点稳定 + 防电容残留
            after = self._shoot()

            pt = self.ink_point(before, after, (t.tx, t.ty))
            if pt is None:
                _LOG.warning("[%s %d/%d] 目标(%.0f,%.0f) arm(%.2f,%.2f) → 未检测到墨点",
                             label, i + 1, len(targets), t.tx, t.ty, ax, ay)
                samples.append(Sample(target=t, touch_x=-1, touch_y=-1, ax=ax, ay=ay))
                continue

            sx, sy = pt
            samples.append(Sample(target=t, touch_x=sx, touch_y=sy, ax=ax, ay=ay))
            _LOG.info("[%s %d/%d] 目标(%.0f,%.0f) arm(%.2f,%.2f) → 墨点(%.1f,%.1f) "
                      "偏差(%.1f,%.1f)",
                      label, i + 1, len(targets), t.tx, t.ty, ax, ay, sx, sy,
                      sx - t.tx, sy - t.ty)
        return samples


def _scan_z(cal: InkCalibrator, arm: ArmClient, hardware_path: Path,
            canvas: tuple[int, int, int, int],
            z_start: float, z_max: float, z_step: float, up_float: float,
            width: int, height: int) -> int:
    """扫描最轻触发深度（墨点版，不依赖网页 touch）。

    每档 z 换一个位置（沿画布对角线均匀分布）—— 因为墨点一旦落下就变黑，
    若每档都戳同一处，后续档位的差分就检测不到新增墨点。
    """
    # ---- 安全校验（碎屏不可逆，两道防线都不可省）
    if z_max > Z_HARD_MAX:
        _LOG.error("⛔ --z-max=%.2f 超过 Z 硬上限 %.2f（会点碎屏幕），拒绝执行。"
                   "请设 ≤ %.2f", z_max, Z_HARD_MAX, Z_HARD_MAX)
        return 2
    if z_start > Z_HARD_MAX:
        _LOG.error("⛔ --z-start=%.2f 已超过 Z 硬上限 %.2f", z_start, Z_HARD_MAX)
        return 2
    if not (0.0 <= up_float <= 0.6):
        _LOG.error("⛔ --up-float=%.2f 超出安全区间 [0, 0.6]。"
                   "旧规范「上浮 1~3mm」在 6.2 硬顶下放不下（5.6+1.5=7.1 会碎屏）",
                   up_float)
        return 2

    n = int(round((z_max - z_start) / z_step)) + 1
    zs = [round(z_start + i * z_step, 3) for i in range(n)]
    x0, y0, x1, y1 = canvas
    # 沿对角线布点，避开边缘 8%
    pad_x, pad_y = (x1 - x0) * 0.12, (y1 - y0) * 0.12
    pts = [
        (x0 + pad_x + (x1 - x0 - 2 * pad_x) * i / max(1, n - 1),
         y0 + pad_y + (y1 - y0 - 2 * pad_y) * i / max(1, n - 1))
        for i in range(n)
    ]
    rng_x, rng_y = load_arm_range(hardware_path)

    _LOG.info("==== z_press 扫描（墨点版）：z ∈ [%.2f, %.2f] 步长 %.2f，共 %d 档 ====",
              z_start, z_max, z_step, n)
    min_z: Optional[float] = None
    for i, z in enumerate(zs):
        tx, ty = pts[i]
        u, v = tx / width, ty / height
        ax, ay = coarse_predict(rng_x, rng_y, u, v)
        before = cal._shoot()
        arm.move(ax, ay)
        time.sleep(cal.settle_ms / 1000.0)
        arm.press(z)
        time.sleep(cal.dwell_ms / 1000.0)
        arm.release()
        time.sleep(0.6)
        after = cal._shoot()
        pt = cal.ink_point(before, after, (tx, ty))
        if pt is not None:
            _LOG.info("z=%.3f → 触发 ✓（墨点 %.1f,%.1f）", z, pt[0], pt[1])
            min_z = z
            break
        _LOG.info("z=%.3f → 未触发", z)

    if min_z is None:
        _LOG.error("扫描到 z=%.2f 仍未触发。请检查：① 画笔是黑色、画布是白纸 "
                   "② --canvas 区域对不对 ③ z 起点是否太浅", z_max)
        return 1

    # ⚠️ 方向：Z 越大 = 压得越深。min_z 是「刚能触发」的深度，
    #    为稳定接触需再加深 up_float，但绝不能越过 6.2 硬顶（碎屏不可逆）。
    z_press = round(min_z + up_float, 3)
    if z_press > Z_HARD_MAX:
        _LOG.warning("⛔ min_z %.3f + %.2f = %.3f 超过硬上限 %.2f，已钳制为 %.2f",
                     min_z, up_float, z_press, Z_HARD_MAX, Z_HARD_MAX)
        z_press = Z_HARD_MAX
    _LOG.info("✅ min_z=%.3f + 加深 %.2f → **z_press=%.3f**（硬顶 %.2f）",
              min_z, up_float, z_press, Z_HARD_MAX)
    write_z_press(hardware_path, z_press, min_z, up_float)
    _LOG.info("✅ 已写入 %s", hardware_path)
    return 0


# ================================================================ main
def main() -> int:
    ap = argparse.ArgumentParser(description="机械臂映射标定（墨点版）")
    ap.add_argument("--z-press", type=float, default=Z_PRESS_SAFE,
                    help=f"下压深度（**必须用 --scan-z 实测值**）。"
                         f"⛔ Z 越大越深，硬上限 {Z_HARD_MAX}，超过会碎屏。默认 {Z_PRESS_SAFE}")
    ap.add_argument("--grid", type=int, default=8, help="校准网格 NxN（默认 8 = 64 点）")
    ap.add_argument("--rounds", type=int, default=2,
                    help="校准轮数（默认 2：首轮粗估 + 次轮用首轮模型铺满全屏）")
    ap.add_argument("--canvas", default="20 170 345 660",
                    help="画布可点区 x0 y0 x1 y1（逻辑点）。**默认按 iPhone X 备忘录实测的红框**："
                         "顶部 y<150 是「返回/撤销/重做/完成」按钮栏，底部 y>680 是画笔栏，"
                         "戳到红框外要么没反应要么**退出标记界面**——后者会让后续所有"
                         "戳都失效，红框就是可点击区域")
    ap.add_argument("--margin", type=float, default=0.10,
                    help="网格在 --canvas 内再向内缩的比例（默认 0.10）。"
                         "**必须留**：实测画布四角极值（arm 9.93/73.20/16.62/154.62）"
                         "全部戳不到，边缘点会全部漏检")
    ap.add_argument("--verify-only", action="store_true", help="只跑验收轮")
    ap.add_argument("--probe-one", action="store_true",
                    help="只在画布中心戳一个点，验证墨点检测是否可用")
    ap.add_argument("--scan-z", action="store_true",
                    help="**先跑这个**：扫描最轻触发深度，写回 z_press（墨点版，无需网页）")
    ap.add_argument("--z-start", type=float, default=5.0)
    ap.add_argument("--z-max", type=float, default=Z_HARD_MAX,
                    help=f"扫描最深 z（硬顶 {Z_HARD_MAX}，绝不可超过）")
    ap.add_argument("--z-step", type=float, default=0.1)
    ap.add_argument("--up-float", type=float, default=0.4,
                    help="在 min_z 基础上**再加深**多少以保证稳定触发。"
                         "⛔ Z 越大越深！旧规范「上浮 1~3mm」在 6.2 硬顶下放不下"
                         "（min_z 5.6 + 1.5 = 7.1 会碎屏），故收紧到 [0, 0.6]")
    ap.add_argument("--max-error-pt", type=float, default=3.0)
    ap.add_argument("--ink-thresh", type=int, default=15,
                    help="差分灰度阈值（默认 15）。用**第 3 个笔（粗马克笔）**时墨迹约 30px 宽、"
                         "颜色深，阈值可以稍高以抗噪；若换细笔（第 1/2 个）需降到 8~10")
    ap.add_argument("--min-area", type=int, default=2,
                    help="墨点最小像素面积（默认 2）。第 3 个笔的粗墨迹面积远大于此，"
                         "保持 2 即可滤掉噪点又不漏检")
    ap.add_argument("--max-area", type=int, default=3000,
                    help="墨点最大像素面积（默认 3000；超过视为整屏 UI 变化，丢弃）")
    ap.add_argument("--jitter", type=float, default=25.0,
                    help="网格点随机抖动幅度 px（默认 25）。画布上有旧墨点时用来错开，"
                         "避免新点落进旧墨迹导致无差分。设为 0 可关闭")
    ap.add_argument("--radius", type=int, default=90,
                    help="第 1 级搜索窗口半径（默认 90；实测粗估偏差可达 44px）")
    ap.add_argument("--arm-url", default="http://127.0.0.1:8082/MyWcfService/getstring")
    ap.add_argument("--arm-com", default="COM4")
    ap.add_argument("--auto-restart", action="store_true",
                    help="串口被占时自动重启 JxbService（**会弹 UAC**）。"
                         "被 Ctrl+C 中断后再跑时很有用，否则需人工执行 "
                         "Restart-Service JxbService -Force")
    ap.add_argument("--hardware", default="config/hardware.json")
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    canvas = parse_canvas(args.canvas)
    hardware_path = Path(args.hardware)

    src = ImouseFrameSource(host=args.host)
    src.open()
    dev = src.device
    _LOG.info("设备：%s %dx%d", dev.name, dev.width, dev.height)

    base = src.read()
    if base is None:
        _LOG.error("取帧失败")
        return 1
    ink = canvas_ink_ratio(base.raw, canvas)
    _LOG.info("画布区域已有墨点占比：%.4f", ink)
    if ink > 0.0005:
        # 实测教训：画布上已有的墨迹会让"戳在同一位置"的差分检测失效
        # （那里已经是黑的，再戳也不变）。必须新建一页才能标定。
        _LOG.warning("⚠️ 画布已有墨迹（暗像素 %.3f%%）。**戳在旧墨迹上不会产生差分**，"
                     "对应点会误报'未检测到'。", ink * 100)
        _LOG.warning("   解决：备忘录 → 返回 → 新建备忘录 → 标记 → 选第 3 个笔（粗的）→ 回到空白画布")

    arm = ArmClient(args.arm_url, com=args.arm_com)
    # open 成功后自动注册 atexit + 信号处理器：
    # 任何异常退出（含 Ctrl+C）都会 reset + close，不再需要人工重启服务。
    arm.open(auto_restart=args.auto_restart)
    arm.reset()

    try:
        cal = InkCalibrator(src, arm, args.z_press, canvas,
                            args.width, args.height,
                            ink_thresh=args.ink_thresh, min_area=args.min_area,
                            max_area=args.max_area, radius=args.radius)

        if args.probe_one:
            rng_x, rng_y = load_arm_range(hardware_path)
            t = Target(tx=(canvas[0] + canvas[2]) / 2, ty=(canvas[1] + canvas[3]) / 2)
            t.u, t.v = t.tx / args.width, t.ty / args.height
            ax, ay = coarse_predict(rng_x, rng_y, t.u, t.v)
            _LOG.info("探针：目标(%.0f,%.0f) → arm(%.2f,%.2f) z=%.2f", t.tx, t.ty, ax, ay,
                      args.z_press)
            # 必须用 InkCalibrator 的 settle/dwell，不能再硬编码 150ms——
            # 实测 150ms 完全来不及留痕，探针会误报"没检测到墨点"。
            before = cal._shoot()
            arm.move(ax, ay)
            time.sleep(cal.settle_ms / 1000.0)
            arm.press(args.z_press)
            time.sleep(cal.dwell_ms / 1000.0)
            arm.release()
            time.sleep(0.6)
            after = cal._shoot()
            pt = cal.ink_point(before, after, (t.tx, t.ty))
            if pt is None:
                _LOG.error("❌ 没检测到墨点（已含画布范围校验）。请依次排查：")
                _LOG.error("   ① z_press=%.2f 够不够（跑 calibrate_z_press.py 测）", args.z_press)
                _LOG.error("   ② 画笔是不是选了黑色、画布是不是白纸")
                _LOG.error("   ③ 调低 --ink-thresh（当前 %d）", args.ink_thresh)
                _LOG.error("   ④ 调低 --min-area（当前 %d）", args.min_area)
                return 1
            _LOG.info("✅ 检测到墨点 (%.1f, %.1f)，与目标偏差 (%.1f, %.1f)",
                      pt[0], pt[1], pt[0] - t.tx, pt[1] - t.ty)
            return 0

        rng_x, rng_y = load_arm_range(hardware_path)
        _LOG.info("粗估机械臂范围：x[%.2f, %.2f] y[%.2f, %.2f]",
                  rng_x[0], rng_x[1], rng_y[0], rng_y[1])

        if args.scan_z:
            return _scan_z(cal, arm, hardware_path, canvas,
                           args.z_start, args.z_max, args.z_step, args.up_float,
                           args.width, args.height)

        if args.verify_only:
            cfg = json.loads(hardware_path.read_text(encoding="utf-8"))
            model = cfg.get("calibration", {}).get("actuation")
            if not model:
                _LOG.error("找不到已有 actuation，请先跑一次完整标定")
                return 1
            # 必须限制在 --canvas 内：random_targets 是**全屏**范围，
            # 会生成 y=93 / y=728 这类落在工具栏、画布外的点——
            # 戳不动或触发按钮，把验收误差从 1pt 级污染成 7pt 级。
            targets = _targets_in_canvas(10, args.width, args.height, canvas,
                                         margin=args.margin,
                                         seed=int(time.time()) % 1000000,
                                         jitter=args.jitter)
            samples = cal.collect(targets, model, rng_x, rng_y, "verify")
            _report(samples, args.max_error_pt, write_path=None)
            return 0

        # ---- 校准轮
        model = None
        all_valid: list[Sample] = []
        # 网格只在 --canvas 范围内生成，并按 --margin 再向内缩。
        # 此前默认 margin=0.06 + 默认 canvas 偏大（110,700）→ 部分点戳到工具栏/画笔栏
        # 把屏幕退出了标记界面，后续点全失效。现在收紧到红框内（20,170,345,660）。
        # 另外曾把 args.grid（网格边长）误当总点数传入，导致 6 只生成 4 个点——
        # 这里统一传「总点数 = grid*grid」。
        for rd in range(1, args.rounds + 1):
            if rd == 1:
                targets = _targets_in_canvas(args.grid * args.grid, args.width, args.height,
                                             canvas, margin=args.margin, seed=1,
                                             jitter=args.jitter)
            else:
                targets = _targets_in_canvas(args.grid * args.grid, args.width, args.height,
                                             canvas, margin=args.margin + 0.02, seed=rd * 97)
            _LOG.info("==== 第 %d 轮校准（%d 点，模型=%s）====",
                      rd, len(targets), "粗估" if model is None else "上轮拟合")
            samples = cal.collect(targets, model, rng_x, rng_y, f"R{rd}")
            valid = [s for s in samples if s.touch_x >= 0]
            if len(valid) < 16:
                _LOG.error("有效样本不足（%d/%d）。检查 z_press 与画布。",
                           len(valid), len(targets))
                return 1
            # 以实测落点归一化后作为特征
            fit_samples = []
            for s in valid:
                u, v = s.touch_x / args.width, s.touch_y / args.height
                if 0 <= u <= 1 and 0 <= v <= 1:
                    fit_samples.append(Sample(
                        target=Target(tx=s.touch_x, ty=s.touch_y, u=u, v=v),
                        touch_x=s.touch_x, touch_y=s.touch_y, ax=s.ax, ay=s.ay))
            model = fit_bilinear(fit_samples)
            all_valid = fit_samples
            _LOG.info("第 %d 轮拟合：ax=%s ay=%s", rd, model["ax"], model["ay"])
            _LOG.info("内部残差 ax 均/最 %.3f/%.3f mm，ay 均/最 %.3f/%.3f mm",
                      model["fit_residual_ax_mm"]["mean"], model["fit_residual_ax_mm"]["max"],
                      model["fit_residual_ay_mm"]["mean"], model["fit_residual_ay_mm"]["max"])

        # ---- 验收轮
        _LOG.info("==== 验收轮（10 个点，只测不纠偏）====")
        # 验收点用**随机 seed + jitter**：画布上会累积大量旧墨点，
        # 固定点会落进旧墨迹 → 无差分 → 重跑复检时全部"未检测到"。
        v_targets = _targets_in_canvas(10, args.width, args.height,
                                       canvas, margin=args.margin,
                                       seed=int(time.time()) % 1000000,
                                       jitter=args.jitter)
        v_samples = cal.collect(v_targets, model, rng_x, rng_y, "V")
        return _report(v_samples, args.max_error_pt, hardware_path,
                       model=model, fit_n=len(all_valid))

    finally:
        try:
            arm.reset()
            arm.close()
        except Exception:  # noqa: BLE001
            pass
        src.close()


def _report(samples: list[Sample], max_allowed: float,
            write_path: Path | None, model: dict | None = None,
            fit_n: int = 0) -> int:
    valid = [s for s in samples if s.touch_x >= 0]
    if not valid:
        _LOG.error("验收轮全部未检测到墨点")
        return 1
    errs = [(s, s.touch_x - s.target.tx, s.touch_y - s.target.ty,
             ((s.touch_x - s.target.tx) ** 2 + (s.touch_y - s.target.ty) ** 2) ** 0.5)
            for s in valid]
    max_err = max(e[3] for e in errs)
    mean_err = sum(e[3] for e in errs) / len(errs)
    worst = max(errs, key=lambda e: e[3])

    _LOG.info("验收：样本 %d/%d，mean=%.2fpt，max=%.2fpt（阈值 %.1fpt）",
              len(valid), len(samples), mean_err, max_err, max_allowed)
    _LOG.info("最差点：目标(%.1f,%.1f) 墨点(%.1f,%.1f) dx=%.1f dy=%.1f",
              worst[0].target.tx, worst[0].target.ty,
              worst[0].touch_x, worst[0].touch_y, worst[1], worst[2])

    if write_path is None:
        return 0

    if max_err > max_allowed:
        _LOG.error("❌ 超阈值（%.2f > %.1f），**不写入配置**。常见原因：", max_err, max_allowed)
        _LOG.error("   ① z_press 不够（手机没真正被触发）")
        _LOG.error("   ② 机械臂间隙大 / 支架松动")
        _LOG.error("   ③ 画布区域设置有误（--canvas）")
        return 2

    report = {
        "fit_samples": fit_n,
        "verify_samples": len(valid),
        "mean_err_pt": round(mean_err, 3),
        "max_err_pt": round(max_err, 3),
        "worst": {"tx": worst[0].target.tx, "ty": worst[0].target.ty,
                  "touch_x": worst[0].touch_x, "touch_y": worst[0].touch_y,
                  "dx": worst[1], "dy": worst[2]},
        "method": "ink_dot_screenshot",
    }
    write_actuation(write_path, model or {}, max_err, report)
    _LOG.info("✅ 已写入 %s", write_path)
    # 重标后必须同步 arm_range：否则屏幕边角按旧范围 clamp —— 就是 2026-09-10
    # "返回箭头点不到"那次的根因（此前只有读它的 load_arm_range，没有任何工具写它）。
    write_arm_range_from_model(write_path, model, width, height)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # ArmClient 的信号处理器已先执行 safe_shutdown（reset + close），
        # 这里只做友好提示；不放心可再跑 tools/arm_home.py 确认归位。
        print("\n[中断] 已捕获 Ctrl+C，机械臂应已自动归位并释放串口。")
        print("       如需确认，执行：py -3.11 tools\\arm_home.py")
        raise SystemExit(130)
"""动作工具箱：取帧、判定、点模板、点文字、滑动、等待状态。

为什么要有这一层：
    旧工程每个模块各自写「取帧→匹配→点击→sleep」，导致等待时长、抖动、坐标换算
    到处不一致。这里把动作收敛成一套，流程层只说「点什么」，不说「怎么点」。

三条硬纪律：
    1. 点击坐标一律 fast 帧坐标系（OCR 在 raw 帧上跑，结果必须换算回来）；
    2. 点完必须重新取帧复判 —— 机械臂点击有失败率，点了不等于成功；
    3. 越界点击直接拒绝并记录，绝不猜一个「差不多」的位置。
"""

from __future__ import annotations

import random
import time
from typing import Callable, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from core.config_store import ConfigStore
from core.devices.arm import ArmError, RobotArm
from core.devices.frame_source import FrameSet, FrameSource
from core.vision.matcher import MatchResult, TemplateMatcher
from core.vision.ocr import OcrEngine, TextBox
from core.vision.page_state import PageDetector, PageResult, ScreenState
from core.vision.stable_state import StableDetector

LogFn = Optional[Callable[[str, str], None]]


def _cubic_bezier(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    """三次贝塞尔曲线点（旧工程已固化的人体轨迹，照抄）。"""
    u = 1.0 - t
    return u ** 3 * p0 + 3 * u ** 2 * t * p1 + 3 * u * t ** 2 * p2 + t ** 3 * p3


class ActionKit:
    """把设备与视觉能力打包成一组语义动作。所有 handler 通过它操作真机。"""

    def __init__(
        self,
        store: ConfigStore,
        source: FrameSource,
        arm: RobotArm,
        matcher: TemplateMatcher,
        detector: PageDetector,
        ocr: Optional[OcrEngine] = None,
        log: LogFn = None,
    ) -> None:
        self.store = store
        self.source = source
        self.arm = arm
        self.matcher = matcher
        self.detector = detector
        self.ocr = ocr
        self._log = log
        # 双帧一致包装（默认关闭，回归验证有效后再开）：过渡帧不动手
        self._stable = StableDetector(detector, store)
        self.last_origin: Optional[ScreenState] = None
        self.stats: dict = {"clicks": 0, "swipes": 0, "frames": 0, "rejected": 0}
        # 帧级 OCR 缓存（只留最近一帧）：详情页一趟要 3 次全屏 OCR
        # （弹窗判据 → 读字段 → 找关闭按钮），整屏 OCR 是全流程最贵的一步，
        # 同一帧反复识别纯属浪费。缓存后详情页 OCR 从 3 次降到 1 次。
        self._ocr_cache: Optional[tuple] = None
        self.stats["ocr_cache_hits"] = 0

    def reload_config(self) -> None:
        """配置热重载后调用（阈值的 stable 段可能变了）。"""
        self._stable.reload_config()

    # ---------------------------------------------------------------- 日志

    def _emit(self, level: str, msg: str) -> None:
        if self._log:
            self._log(level, msg)

    def sleep(self, seconds: float) -> bool:
        """sleep 兼容实现（防御）。

        detail.py 曾写 kit.sleep(0.6) 而本类没有该方法 → AttributeError 被
        except 吞掉 → 详情第二屏每次读不到，价格被 OCR 取成车长等错值 → 漏单。
        现 detail.py 已改回 ctx.sleep，但该文件多次出现「改动未落盘」的情况，
        故保留本兜底：即使再遇到旧版调用 kit.sleep，也只普通休眠，不会再抛错。
        """
        time.sleep(max(0.0, float(seconds)))
        return True

    def click_delay(self) -> float:
        """动作后等待时长 = 物理点击延时 + 取帧链路延时。

        取帧链路延时（vision.capture.video_latency_ms）：摄像头约 0；
        HDMI 采集卡约 100~200ms。

        为什么必须加：不加的话，下一帧取到的是「动作发生之前」的画面，
        判定就滞后一轮 —— 表现为「点了没反应 → 又点一次」的重复点击，
        既慢又容易误触。这个坑在接采集卡时一定会踩，现在就把口子留好。
        """
        base = float(self.store.get("hardware", "delays.click_after", 0.5) or 0.5)
        latency_ms = float(self.store.get("vision", "capture.video_latency_ms", 0) or 0)
        return base + latency_ms / 1000.0

    # ---------------------------------------------------------------- 取帧

    def frame(self, retries: int = 3) -> Optional[FrameSet]:
        """取一帧；偶发模糊帧直接重取。"""
        for _ in range(max(1, retries)):
            try:
                fs = self.source.read()
            except Exception as exc:  # 摄像头掉线 / 采集卡无信号
                self._emit("warning", f"取帧异常: {exc}")
                time.sleep(0.2)
                continue
            if fs is not None and fs.fast is not None and fs.fast.size:
                self.stats["frames"] += 1
                return fs
            time.sleep(0.1)
        self._emit("warning", "连续取帧失败")
        return None

    def detect(self, fs: Optional[FrameSet] = None, origin: Optional[ScreenState] = None) -> PageResult:
        """判定当前页面。origin 用于区分列表弹窗/详情弹窗。

        结果可能是 pending=True（双帧一致未达成，处于过渡帧）。
        调用方必须先看 pending 再决定动作——pending 时只等下一帧。
        """
        if fs is None:
            fs = self.frame()
        if fs is None:
            return PageResult(state=ScreenState.UNKNOWN, note="取帧失败")
        res = self._stable.detect(fs.fast, origin=origin or self.last_origin)
        # pending 时的 state 是占位 UNKNOWN，不能污染 origin 记忆
        if not res.pending:
            self.last_origin = res.state
        return res

    def reset_stable(self) -> None:
        """清空双帧一致历史（页面被外部改变、或流程重启后调用）。"""
        self._stable.reset()

    def wait_state(
        self,
        want: Sequence[ScreenState],
        timeout: float = 6.0,
        # 0.25s：旧值 0.4~0.5 让每次「等页面到位」白白多睡一两百毫秒，
        # 一轮三十来单、每单三四次等待，累计就是十几秒的纯空等。
        interval: float = 0.25,
        origin: Optional[ScreenState] = None,
    ) -> Tuple[Optional[FrameSet], PageResult]:
        """轮询等待页面变成期望状态之一。返回 (最后一帧, 判定结果)。"""
        deadline = time.time() + max(0.0, timeout)
        fs = self.frame()
        res = self.detect(fs, origin=origin)
        while time.time() < deadline:
            if fs is not None:
                res = self.detect(fs, origin=origin)
                if res.state in want:
                    return fs, res
            time.sleep(interval)
            fs = self.frame()
        if fs is not None:
            res = self.detect(fs, origin=origin)
        return fs, res

    # ---------------------------------------------------------------- 点击

    def click_point(
        self,
        x: float,
        y: float,
        z: Optional[float] = None,
        jitter: Optional[int] = None,
        reason: str = "",
    ) -> bool:
        """按 fast 帧坐标点击。越界会被 arm 拒绝并计数。"""
        ok = self.arm.tap(x, y, z=z, jitter=jitter)
        if ok:
            self.stats["clicks"] += 1
            self._emit("info", f"点击 ({x:.0f},{y:.0f}) {reason}".rstrip())
        else:
            self.stats["rejected"] += 1
        time.sleep(self.click_delay())
        return ok

    def click_template(
        self,
        name: str,
        fs: Optional[FrameSet] = None,
        roi: Optional[Sequence[float]] = None,
        threshold: Optional[float] = None,
        dy: int = 0,
        z: Optional[float] = None,
        reason: str = "",
    ) -> Optional[MatchResult]:
        """在当前帧找模板并点其中心。返回命中结果（未命中返回 None）。"""
        if fs is None:
            fs = self.frame()
        if fs is None:
            return None
        hit = self.matcher.find(fs.fast, self.store.template(name), roi=roi, threshold=threshold)
        if hit is None:
            self._emit("debug", f"模板未命中: {name}")
            return None
        self.click_point(hit.cx, hit.cy + dy, z=z, reason=f"{reason or name} score={hit.score:.2f}")
        return hit

    def click_close_candidate(self, fs: Optional[FrameSet] = None) -> Optional[MatchResult]:
        """未知页/弹窗关闭：在关闭按钮候选里取**最高分**，绝不取第一个命中。"""
        if fs is None:
            fs = self.frame()
        if fs is None:
            return None
        best: Optional[MatchResult] = None
        best_name = ""
        for spec in self.store.close_candidates():
            hit = self.matcher.find(fs.fast, spec)
            if hit is not None and (best is None or hit.score > best.score):
                best, best_name = hit, spec.name
        if best is None:
            return None
        self.click_point(best.cx, best.cy, reason=f"关闭[{best_name}] score={best.score:.2f}")
        return best

    # ---------------------------------------------------------------- OCR

    @property
    def ocr_scale(self) -> Tuple[float, float]:
        """raw 帧坐标 → fast 帧坐标的比例。"""
        raw = tuple(self.store.get("vision", "capture.capture_resolution", (480, 640)))
        fast = tuple(self.store.get("vision", "capture.fast_size", (480, 640)))
        return (float(fast[0]) / float(raw[0]), float(fast[1]) / float(raw[1]))

    def ocr_frame(
        self, fs: FrameSet, roi: Optional[Sequence[int]] = None
    ) -> List[TextBox]:
        """在 raw 帧（或 ROI 裁剪）上 OCR，坐标已换算到 fast 帧。

        整屏（roi 为空）结果按帧缓存：同一帧只识别一次。带 ROI 的小图不缓存
        （ROI 各不相同，命中率极低，缓存只会占内存）。
        """
        if self.ocr is None:
            return []
        scale = self.ocr_scale
        full_roi = (0, 0, fs.raw.shape[1], fs.raw.shape[0])
        if roi is None:
            cache = self._ocr_cache
            # 同时比对对象 id 与时间戳：只比 id 有被 GC 后复用的风险
            if cache is not None and cache[0] is fs and cache[1] == getattr(fs, "ts", None):
                self.stats["ocr_cache_hits"] = int(self.stats.get("ocr_cache_hits", 0)) + 1
                return list(cache[2])
            boxes = self.ocr.ocr_roi(fs.raw, full_roi, coord_scale=scale)
            # 存 fs 引用：保证 id 在这段时间内不会被别的帧复用
            self._ocr_cache = (fs, getattr(fs, "ts", None), list(boxes))
            return boxes
        return self.ocr.ocr_roi(fs.raw, roi, coord_scale=scale)

    def find_text(
        self,
        text: str,
        fs: Optional[FrameSet] = None,
        roi: Optional[Sequence[int]] = None,
    ) -> Optional[TextBox]:
        """OCR 找包含 text 的框（roi 为 raw 帧像素）。命中多个时取最靠上的。"""
        if fs is None:
            fs = self.frame()
        if fs is None:
            return None
        best: Optional[TextBox] = None
        for box in self.ocr_frame(fs, roi):
            if text in box.text and (best is None or box.top < best.top):
                best = box
        return best

    def norm_to_raw_roi(self, fs: FrameSet, norm: Sequence[float]) -> Tuple[int, int, int, int]:
        """归一化 [l,t,r,b] → raw 帧像素 (x,y,w,h)。"""
        h, w = fs.raw.shape[:2]
        left, top, right, bottom = [float(v) for v in norm]
        x0, y0 = int(round(left * w)), int(round(top * h))
        x1, y1 = int(round(right * w)), int(round(bottom * h))
        return (x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))

    def click_text_norm(
        self,
        text: str,
        norm_roi: Sequence[float],
        fs: Optional[FrameSet] = None,
        z: Optional[float] = None,
        reason: str = "",
    ) -> Optional[TextBox]:
        """按归一化 ROI 在 raw 帧上找文字并点击（省得调用方到处算像素）。"""
        if fs is None:
            fs = self.frame()
        if fs is None:
            return None
        return self.click_text(text, fs=fs, roi=self.norm_to_raw_roi(fs, norm_roi), z=z, reason=reason)

    def ensure_text_visible(
        self,
        text: str,
        norm_roi: Sequence[float] = (0.0, 0.0, 1.0, 1.0),
        max_swipes: int = 3,
        distance_ratio: Optional[float] = None,
        detect_end: bool = False,
        duration_range: Optional[Sequence[float]] = None,
        move_epsilon: Optional[float] = None,
        escalate: Optional[float] = None,
        roi_height_ratio: Optional[float] = None,
    ) -> bool:
        """目标文字不可见时才上滑展开（列表默认只显示一部分，不做盲滑）。

        distance_ratio：单次幅度基准，默认取 `runtime.swipe.grid_distance_ratio`（0.12 屏）、
        再退回 0.12。⚠️ **城市网格必须传小值**（见 city_picker）：默认翻页幅度是给订单列表
        用的（一次 0.32~0.5 屏高），而省列表总共才约 0.5 屏高 —— 一次就整屏掠过，目标被甩出
        扫描区再也找不到（2026-09-13 实测根因：目的地选不上 → 确认按钮浅红不可点）。
        duration_range：显式时长区间，**优先于** `runtime.swipe.duration_ms_range`。
        为什么必须能覆盖：小幅度还得配短时长 —— 幅度小、时长仍按翻页值（490~700ms）时
        指尖速度只有正常上滑的 1/7，iOS 会把它当"拖拽"（见 runtime.json 的
        `_note_back_gesture`），表现就是"滑了但列表没动"。
        detect_end：用**带阈值的画面差分**判断"滑了没动"。旧实现是 16x16 灰度求和"完全相等"，
        太脆 —— 一次没滚动就立刻判"已到列表尽头"，正是"几个省滚不出来"的出口。
        move_epsilon：差分阈值（32x32 灰度平均绝对差），低于它算"没动"。
        escalate：判定"没动"时先把幅度乘它（受 roi_height_ratio 上限约束）再试一次，
        **连续 2 次确实没动**才认定到底 —— 不把"某次没滚动"误判成"到底"而漏掉后面的行。
        roi_height_ratio：单步幅度上限（相对屏高，默认 0.5）。锁死"不超过扫描区高度的一半"，
        保证任意一行都至少完整经过一次、不会被跳过（旧问题"一次滑过头"的机制性防线）。
        """
        cfg = self.store.section("runtime").get("swipe", {}) or {}
        base = float(distance_ratio if distance_ratio is not None
                     else (cfg.get("grid_distance_ratio", 0.12) or 0.12))
        eps = float(move_epsilon if move_epsilon is not None
                    else (cfg.get("grid_move_epsilon", 0.35) or 0.35))
        esc = float(escalate if escalate is not None
                    else (cfg.get("grid_escalate", 1.6) or 1.6))
        cap = float(roi_height_ratio if roi_height_ratio is not None
                    else (cfg.get("grid_max_step_ratio", 0.5) or 0.5))
        ratio = base
        prev = None
        no_move = 0
        shown: List[str] = []
        for i in range(max_swipes + 1):
            fs = self.frame()
            if fs is None:
                return False
            roi = self.norm_to_raw_roi(fs, norm_roi)
            if self.find_text(text, fs, roi) is not None:
                if i:
                    self._emit("info", f"上滑 {i} 次后找到「{text}」")
                return True
            if i == max_swipes:
                shown = self._visible_texts(fs, roi)
                break
            if detect_end and prev is not None:
                delta = self._frame_delta(prev, fs)
                if delta < eps:
                    no_move += 1
                    if no_move >= 2:
                        self._emit("info", f"连续 {no_move} 次上滑画面几乎未变（Δ={delta:.2f}），"
                                          f"判定已到列表尽头")
                        break
                    # 先加大幅度重试，而不是立刻放弃（很可能只是这一次没滚动）
                    ratio = min(ratio * esc, cap)
                    self._emit("info", f"上滑未移动（Δ={delta:.2f}），幅度升到 {ratio:.2f} 屏重试")
                else:
                    no_move = 0
                    ratio = base
            prev = fs
            self.swipe_up(distance_ratio=ratio, duration_range=duration_range)
            time.sleep(0.4)
        if shown:
            self._emit("warning", f"上滑 {max_swipes} 次后仍未找到「{text}」；"
                                  f"本屏可见文字：{' / '.join(shown[:40])}")
        else:
            self._emit("warning", f"上滑 {max_swipes} 次后仍未找到「{text}」")
        return False

    @staticmethod
    def _frame_delta(a, b) -> float:
        """两帧"动了多少"（32x32 灰度平均绝对差，0~255）。

        算不出来时**返回最大值**（= 认为"动了"）：宁可多滑几次，也不能误判成"到底"而漏行。
        """
        try:
            import cv2

            ga = cv2.cvtColor(a.fast, cv2.COLOR_BGR2GRAY)
            gb = cv2.cvtColor(b.fast, cv2.COLOR_BGR2GRAY)
            if ga.shape != gb.shape:
                return 255.0
            sa = cv2.resize(ga, (32, 32), interpolation=cv2.INTER_AREA)
            sb = cv2.resize(gb, (32, 32), interpolation=cv2.INTER_AREA)
            return float(cv2.mean(cv2.absdiff(sa, sb))[0])
        except Exception:  # noqa: BLE001
            return 255.0

    def _visible_texts(self, fs, roi) -> List[str]:
        """当前帧（限定 roi）内识别到的文字，去重后返回。

        用途：扫描失败时打印"本屏到底看到了什么"，一眼能看出是层级不对、还是行没滚出来。
        """
        out: List[str] = []
        try:
            for box in self.ocr_frame(fs, roi):
                t = (box.text or "").strip()
                if len(t) >= 2 and t not in out:
                    out.append(t)
        except Exception:  # noqa: BLE001 - 诊断信息拿不到不能影响主流程
            return out
        return out

    def click_text(
        self,
        text: str,
        fs: Optional[FrameSet] = None,
        roi: Optional[Sequence[int]] = None,
        z: Optional[float] = None,
        reason: str = "",
    ) -> Optional[TextBox]:
        """OCR 找文字并点其中心（坐标已换算到 fast 帧）。"""
        box = self.find_text(text, fs, roi)
        if box is None:
            return None
        cx, cy = box.center
        self.click_point(cx, cy, z=z, reason=f"{reason or 'OCR点'} '{box.text}'")
        return box

    # ---------------------------------------------------------------- 滑动

    def swipe(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        duration_ms: int = 350,
        steps: int = 0,
        z: Optional[float] = None,
        duration_range: Optional[Sequence[float]] = None,
    ) -> bool:
        """从 (x0,y0) 拖到 (x1,y1)（fast 帧坐标）。按下→连续轨迹 move_continuous→抬起。

        轨迹照抄旧工程「已固化」的 SwipeController：三次贝塞尔弯曲 + 每步极小
        机械臂坐标抖动(±0.15) + 步间 3ms 连续下发。逐点用 move()+sleep 会被识别成
        「搓屏」，必须走 move_continuous（delay≈0），详见 core/devices/arm.py。

        **不要用像素级抖动**：旧版每步加 6px 抖动，换算到机械臂坐标约 0.8 单位，
        远大于人体自然抖动，是「一卡一卡」的根因——已改为 ±0.15 机械臂单位。
        """
        cfg = self.store.section("runtime").get("swipe", {}) or {}
        # 受控慢拖：真正使用时长配置（默认 350ms，配置 duration_ms_range=[600,1000]），
        # 消除 iOS 惯性翻页。旧实现 steps×0.003≈30ms 极快甩动，惯性滚动把 0.8 屏
        # 放大成翻好几页、扫描漏卡。时长从配置区间随机取，再叠轻微抖动避免机械式匀速。
        # duration_range 显式给出时**优先于配置**：网格扫描要"小幅度 + 短时长"，
        # 必须能压过全局翻页时长（否则慢速短拖会被 iOS 当成拖拽，滑不动）。
        dur_cfg = duration_range or cfg.get("duration_ms_range") or cfg.get("duration_ms") or duration_ms
        if isinstance(dur_cfg, (list, tuple)) and len(dur_cfg) == 2:
            duration_ms = int(random.uniform(float(dur_cfg[0]), float(dur_cfg[1])))
        else:
            duration_ms = int(dur_cfg)
        duration_ms = int(duration_ms * random.uniform(0.85, 1.15))
        # 步间隔（不是步数）才是手感的关键：旧版 duration/8 ≈ 每 8ms 一个点，
        # 而每条轨迹点都是一次 HTTP 下发，RTT 抖动 5~30ms 与固定 sleep 叠加后
        # 点位间隔忽长忽短 → 屏幕上的列表就「一卡一卡」。放粗到 16ms 一个点、
        # 并按绝对时间轴补偿（见下），下发次数减半、节奏还更稳。
        step_ms = float(cfg.get("step_interval_ms", 16) or 16)
        steps = steps or max(12, int(round(duration_ms / step_ms)))
        jit = float(cfg.get("point_jitter_arm", 0.15) or 0.15)
        press_settle = float(cfg.get("press_settle_ms", 60) or 0) / 1000.0
        release_settle = float(cfg.get("release_settle_ms", 120) or 0) / 1000.0
        try:
            ax0, ay0 = self.arm.pixel_to_arm(x0, y0)
            ax1, ay1 = self.arm.pixel_to_arm(x1, y1)
            self.arm.move(ax0, ay0)        # 先离散定位到起点
            self.arm.press(z)
            if press_settle > 0:
                # 压下去先稳一下：笔尖刚接触屏幕就开滑，前几个点常常打滑不跟手，
                # 表现就是「先卡一下再动」。
                time.sleep(press_settle)
            # 随机控制点：轨迹弯曲程度随机，避免每次一条直线（机械式特征）
            c1x = ax0 + random.uniform(-0.3, 0.3)
            c1y = ay0 + random.uniform(-0.4, 0.1)
            c2x = ax1 + random.uniform(-0.3, 0.3)
            c2y = ay1 + random.uniform(-0.1, 0.4)
            # 绝对时间轴 pacing：每步按「应当到达的时刻」补 sleep，而不是每步固定睡。
            # 某一步 HTTP 慢了，下一步自动少睡，总时长与点位间隔都稳，
            # 不会被 RTT 抖动越拖越长（旧实现总时长能翻好几倍）。
            t0 = time.perf_counter()
            span = duration_ms / 1000.0
            for i in range(1, steps + 1):
                deadline = t0 + span * (i / steps)
                t = i / steps
                cx = _cubic_bezier(ax0, c1x, c2x, ax1, t) + random.uniform(-jit, jit)
                cy = _cubic_bezier(ay0, c1y, c2y, ay1, t) + random.uniform(-jit, jit)
                self.arm.move_continuous(cx, cy)   # 连续轨迹：delay≈0，机械臂平滑跟随
                rest = deadline - time.perf_counter()
                if rest > 0:
                    time.sleep(rest)
            self.arm.release()
            if release_settle > 0:
                # 抬手后等惯性滚完再归位/取帧，否则下一帧拍到的是滚动中途的糊画面
                time.sleep(release_settle)
            self.stats["swipes"] += 1
            if cfg.get("back_to_home_after", True):
                self.arm.back_to_home()
        except ArmError as exc:
            self._emit("warning", f"滑动失败: {exc}")
            return False
        self._emit("info", f"滑动 ({x0:.0f},{y0:.0f}) -> ({x1:.0f},{y1:.0f})")
        return True

    def swipe_up(self, distance_ratio: Optional[float] = None,
                 start_y_ratio: Optional[float] = None,
                 duration_range: Optional[Sequence[float]] = None) -> bool:
        """上滑（内容下移，看后面的订单）。

        幅度系数照抄旧工程「已固化」值：基准 × random.uniform(1.7, 2.3)，
        均值约 2 倍屏高——这是用户实测认可的翻页幅度。旧版直接把 0.3~0.5 当距离，
        幅度差几倍，翻页几乎看不出效果（被误认为「方向反了」）。
        """
        cfg = self.store.section("runtime").get("swipe", {}) or {}
        rng = cfg.get("up_distance_ratio_range", [0.3, 0.5]) or [0.3, 0.5]
        base = distance_ratio if distance_ratio else random.uniform(float(rng[0]), float(rng[1]))
        mult = cfg.get("amplitude_multiplier", [1.7, 2.3]) or [1.7, 2.3]
        ratio = base * random.uniform(float(mult[0]), float(mult[1]))
        w, h = self._fast_size()
        x = w * random.uniform(0.42, 0.58)
        if start_y_ratio is not None:
            y0_ratio = float(start_y_ratio)
        else:
            y0_ratio = float(cfg.get("up_start_ratio", 0.667) or 0.667)
        y0 = max(1, min(h - 1, int(h * y0_ratio)))
        y1 = max(h * 0.08, y0 - h * ratio)
        if bool(cfg.get("invert_y", False)):
            y0, y1 = y1, y0
        return self.swipe(x, y0, x, y1, duration_range=duration_range)

    def swipe_down(self, distance_ratio: Optional[float] = None,
                   duration_range: Optional[Sequence[float]] = None,
                   start_y_ratio: Optional[float] = None) -> bool:
        """下滑（内容上移，展开顶部筛选条 / 回列表顶 / 网格归顶）。"""
        cfg = self.store.section("runtime").get("swipe", {}) or {}
        ratio = distance_ratio or float(cfg.get("down_distance_ratio", 0.25) or 0.25)
        w, h = self._fast_size()
        x = w * random.uniform(0.42, 0.58)
        y0 = h * (float(start_y_ratio) if start_y_ratio is not None else 0.25)
        y1 = min(h * 0.9, y0 + h * ratio)
        if bool(cfg.get("invert_y", False)):
            y0, y1 = y1, y0
        return self.swipe(x, y0, x, y1, duration_range=duration_range)

    def swipe_back_gesture(self) -> bool:
        """iOS 返回手势：手指从屏幕左边缘出发，向右滑 2/3 屏宽（不是向左滑）。

        时长必须快（back_gesture_duration_ms_range，默认 250~350ms）：
        iOS 把慢速滑动识别为拖拽——沿用全局 duration_ms_range=[600,1000] 时
        页面被拖出一半又弹回（用户描述「卡顿、搓屏幕」），在「已被抢」弹窗页
        上还会横切进相邻订单详情。普通上下滑仍走慢速 duration_ms_range（防风控）。
        """
        cfg = self.store.section("runtime").get("swipe", {}) or {}
        ratio = float(cfg.get("left_distance_ratio", 0.667) or 0.667)
        dur = cfg.get("back_gesture_duration_ms_range") or (250, 350)
        if isinstance(dur, (list, tuple)) and len(dur) == 2:
            duration_ms = int(random.uniform(float(dur[0]), float(dur[1])))
        else:
            duration_ms = int(dur)
        w, h = self._fast_size()
        y = h * 0.5
        x0, x1 = (w * 0.04, w * ratio)
        if bool(cfg.get("invert_x", False)):
            x0, x1 = x1, x0
        return self.swipe(x0, y, x1, y, duration_ms=duration_ms)

    # ---------------------------------------------------------------- 物理键

    def press_back(self, z: Optional[float] = None) -> None:
        """返回上一页。按实际页面选择方式：
        - 详情页「锚点+偏移」模式（默认）：
          用右侧复杂区域（举报/分享）做锚点模板匹配 → 取中心 →
          X向左偏移固定像素得返回箭头坐标 → 点击。
          原因：返回箭头 `<` 太单薄、模板匹配不稳定；右侧区域特征多、命中可靠，
          且详情页 UI 布局固定，左右偏差恒定。
        - 无锚点配置时退化：直接找 detail_back 模板中心点击；
        - 列表页等无箭头页面 → iOS 左边缘右滑 2/3 屏宽（系统级返回）；
        - 上述都不可用 → 回退物理返回键（旧机型）。

        z：加强下压深度（详情页退出失败时逐级加大，应对 z_press 漂移）。
        """
        fs = self.frame()
        if fs is not None and fs.fast is not None and fs.fast.size:
            try:
                cfg = self.store.section("runtime").get("detail_back", {}) or {}
                anchor_name = str(cfg.get("anchor_template", "detail_back") or "detail_back")
                offset = cfg.get("offset", [-250, 0])
                if isinstance(offset, (list, tuple)) and len(offset) >= 2:
                    off_x, off_y = int(offset[0]), int(offset[1])
                else:
                    off_x, off_y = -250, 0

                hit = self.matcher.find(
                    fs.fast,
                    self.store.template(anchor_name),
                    threshold=float(self.store.get("thresholds", "detail.back", 0.86)),
                )
                if hit is not None:
                    bx = hit.cx + off_x
                    by = hit.cy + off_y
                    reason = f"返回箭头(锚点{anchor_name}+偏移{off_x},{off_y}) score={hit.score:.2f}"
                    if off_x != 0 or off_y != 0:
                        self.click_point(bx, by, z=z, reason=reason)
                    else:
                        self.click_point(hit.cx, hit.cy, z=z, reason=f"返回箭头 score={hit.score:.2f}")
                    return
                # 模板未命中（变异详情页背景色变化导致）→ 固定坐标兜底：
                # 详情页返回箭头位置固定，已判定为详情页后直接点固定坐标即可，
                # 不必依赖模板定位（也不退化到不可靠的右滑手势）。
                fixed = cfg.get("fixed_point")
                if isinstance(fixed, (list, tuple)) and len(fixed) >= 2:
                    lw, lh = self.store.logical_size
                    self.click_point(
                        float(fixed[0]) * lw, float(fixed[1]) * lh,
                        z=z, reason="返回箭头固定坐标(fixed_point)",
                    )
                    return
            except Exception as exc:  # 模板缺失/匹配异常 → 退化手势返回，绝不崩
                self._emit("warning", f"返回箭头检测失败，改手势: {exc}")
        # 无箭头 → 左边缘往右滑手势返回（复用未知页退出手势）
        if self.swipe_back_gesture():
            return
        # 兜底：物理返回键
        try:
            self.arm.press_back_key()
            self._emit("info", "按返回键")
        except ArmError as exc:
            self._emit("warning", f"返回键失败: {exc}")
        time.sleep(self.click_delay())

    def press_home(self) -> None:
        """回桌面。iPhone X 无 Home 键 → 走 home_gesture 底部上滑；
        其余机型（有 Home 键）保留物理点击 home_key。"""
        cfg = (self.store.section("hardware").get("calibration", {}) or {}).get("home_gesture") or {}
        if cfg.get("type") == "swipe_up":
            try:
                fs = self.frame()
                if fs is not None and fs.fast is not None and fs.fast.size:
                    h, w = fs.fast.shape[:2]
                    fr = cfg.get("from_ratio", [0.5, 0.9])
                    tr = cfg.get("to_ratio", [0.5, 0.1])
                    self.swipe(fr[0] * w, fr[1] * h, tr[0] * w, tr[1] * h)
                    self._emit("info", "回桌面（底部上滑手势）")
                    return
            except ArmError as exc:
                self._emit("warning", f"回桌面手势失败: {exc}")
        try:
            self.arm.press_home_key()
            self._emit("info", "按主页键")
        except ArmError as exc:
            self._emit("warning", f"主页键失败: {exc}")
        time.sleep(float(self.store.get("hardware", "delays.press", 0.5) or 0.5))

    # ---------------------------------------------------------------- 辅助

    def _fast_size(self) -> Tuple[int, int]:
        fast = tuple(self.store.get("vision", "capture.fast_size", (480, 640)))
        return int(fast[0]), int(fast[1])

    def save_debug(self, fs: FrameSet, tag: str) -> str:
        """关键步骤留证截图（按 runtime.trace.shot_sample_rate 抽样）。"""
        trace_cfg = self.store.section("runtime").get("trace", {}) or {}
        if not trace_cfg.get("enabled", True):
            return ""
        from core.vision.imgio import imwrite_unicode
        from pathlib import Path

        rate = float(trace_cfg.get("shot_sample_rate", 0.2) or 0.2)
        if rate < 1.0 and random.random() > rate:
            return ""
        max_side = int(trace_cfg.get("max_side_px", 800) or 800)
        img = fs.raw
        scale = min(1.0, max_side / max(img.shape[0], img.shape[1]))
        if scale < 1.0:
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        # 相对路径锚定项目根（打包后 = exe 同级），否则提权启动会写进 CWD\data\traces
        out_dir = Path(str(trace_cfg.get("dir", "data/traces")))
        if not out_dir.is_absolute():
            out_dir = Path(self.store.root) / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}.jpg"
        path = out_dir / name
        imwrite_unicode(path, img, [int(cv2.IMWRITE_JPEG_QUALITY), int(trace_cfg.get("jpeg_quality", 60))])
        return str(path)

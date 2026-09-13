"""摄像头：一次采集、双流输出（raw 高分辨率 + fast 480x640）。

为什么是双流：
    * 模板匹配与页面判定耗时 ∝ 像素数。若直接在高分辨率帧上匹配，
      分辨率翻倍即耗时翻四倍，而现有全部模板都是按 480x640 采集的，
      高分辨率下还会失配 —— 旧方案「提高分辨率必须重采全部模板」的坑就在这。
    * OCR 恰恰相反：检测阶段会先把图缩到 det_limit_side_len，几乎不吃分辨率，
      但识别阶段在原始像素的裁剪行上工作，分辨率越高小字越清晰。

    所以：判定类任务走 fast 帧（模板零重采、速度快），
          识别类任务走 raw 帧的 ROI 裁剪（精度高、不吃全图开销）。

分辨率硬约束：capture_resolution 必须是 roi_calib_size 的整数倍且同比例（3:4），
否则 screen_roi 会被非等比拉伸，导致点击整体偏移。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import cv2
import numpy as np

from core.config_store import ConfigStore
from core.devices.frame_source import (  # noqa: F401  FrameSet 在此 re-export，旧导入路径保持可用
    FrameSet,
    FrameSourceError,
    crop_letterbox,
)

LogFn = Optional[Callable[[str, str], None]]


class CameraError(FrameSourceError):
    """摄像头不可用。

    继承 FrameSourceError：业务层只需捕获取帧源异常，
    将来换成 HDMI 采集卡时不用改异常处理代码（采集卡复用本类）。
    """


@dataclass
class FrameSet:
    """一次采集的两路产物。

    raw  —— 采集分辨率（用于 OCR 与截图留证）
    fast —— 480x640 快帧（用于模板匹配、页面判定、快筛）
    """

    raw: np.ndarray
    fast: np.ndarray
    ts: float

    @property
    def width(self) -> int:
        return int(self.fast.shape[1])

    @property
    def height(self) -> int:
        return int(self.fast.shape[0])


def _rotate(frame: np.ndarray, rotation: int) -> np.ndarray:
    if rotation == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if rotation == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if rotation == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return frame


def frame_quality(frame: np.ndarray) -> Tuple[bool, str]:
    """粗判一帧是不是「有效物理画面」。返回 (是否有效, 原因文本)。

    为什么需要（2026-09-13 真实事故）：机械臂摄像头打不开时，回退逻辑会继续扫其它
    index，其中 index 1 是个**能读到帧、却不是真实画面**的假源（噪点）。只判「读到了帧」
    就会把它当摄像头用 → GUI 预览一片雪花，用户以为摄像头坏了。

    两条判据：
      ① 亮度均值 < 1 → 全黑（镜头被挡 / 环境全黑 / 选错设备）；
      ② 3x3 中值滤波残差占比 > 5% → 椒盐噪点 / 垃圾帧。真场景相邻像素高度相关
         （文字、图标都是连通的线条块面），而噪声是孤立像素，一滤就露馅。

    也供 GUI 预览做「画面异常」提示复用（同一判据，避免两处不一致）。
    """
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    except Exception:  # noqa: BLE001 花屏/通道异常的帧直接判无效
        return False, "转灰度失败"
    if gray.size == 0:
        return False, "空帧"
    mean = float(gray.mean())
    if mean < 1.0:
        return False, f"全黑(均值{mean:.1f})"
    residual = cv2.absdiff(gray, cv2.medianBlur(gray, 3))
    ratio = float((residual > 40).mean())
    if ratio > 0.05:
        return False, f"噪点(残差占比{ratio:.1%})"
    return True, f"正常(均值{mean:.0f},残差{ratio:.1%})"


class Camera:
    """摄像头封装（单帧采集 + 双流输出）。"""

    def __init__(self, store: ConfigStore, log: LogFn = None, *, native: bool = False) -> None:
        self._store = store
        self._log = log
        self._cap: Optional[cv2.VideoCapture] = None
        # native=True：不做「缩放/裁剪到 capture_resolution + fast_size」的归一化，
        # 原样保留摄像头输出（raw=fast=原始帧）。给 GUI 预览与截图用——手机屏是竖屏
        # 375x812，而机械臂摄像头是横屏 640x480，套用手机分辨率会被强行拉成细长条
        # （2026-09-13 实测预览/截图严重变形）。业务判定路径仍走归一化（模板匹配依赖）。
        self._native = bool(native)
        self._rotation = int(store.get("vision", "capture.camera_rotation", 0) or 0)
        self._capture_size: Tuple[int, int] = tuple(
            store.get("vision", "capture.capture_resolution", (480, 640))
        )
        self._fast_size: Tuple[int, int] = tuple(
            store.get("vision", "capture.fast_size", (480, 640))
        )
        self._interp = getattr(cv2, str(store.get("vision", "capture.fast_interpolation", "INTER_AREA")))
        # 黑边处理：camera 模式无黑边（none）；接 HDMI 采集卡时竖屏画面两侧有约
        # 710px 纯黑边，必须设 auto 裁掉，否则归一化 ROI 全错、模板匹配在黑边上刷噪声。
        self._letterbox = str(store.get("vision", "capture.letterbox", "none") or "none").lower()
        self._actual: Tuple[int, int] = (0, 0)
        # ---- 后台采集线程 + 最新帧缓存（实时预览 / detect 共用）----
        # 只有采集线程碰 cv2.VideoCapture.read()，GUI 预览与业务 detect 都从同一缓存取帧，
        # 避免两线程并发抢设备导致丢帧/错位（这也是之前"实时画面卡顿"的隐患之一）。
        self._latest: Optional[FrameSet] = None
        self._cv = threading.Condition()
        self._stop_cap = threading.Event()
        self._cap_thread: Optional[threading.Thread] = None
        self._capture_running = False

    # ---------------------------------------------------------------- 生命周期

    def _emit(self, level: str, msg: str) -> None:
        if self._log:
            self._log(level, msg)

    def open(self) -> bool:
        index = int(self._store.get("hardware", "robot.camera_index", 0) or 0)
        # capture_resolution 是「旋转后」的目标尺寸，设置摄像头属性需反算
        w, h = self._capture_size
        cap_w, cap_h = (h, w) if self._rotation in (90, 270) else (w, h)

        candidates = [index] + [i for i in range(0, 5) if i != index]
        # 后端顺序：DSHOW → 默认（Windows 上即 MSMF）。
        # 为什么 DSHOW 优先：同为 USB 摄像头，MSMF 常出现「open 成功但 read 拿不到帧」或
        # 打不开，而 DSHOW 稳定（2026-09-13 实测机械臂 USB Camera2：MSMF idx=0 读不到帧；
        # DSHOW idx=0 正常出 640x480 真实画面）。
        backends: list = [(cv2.CAP_DSHOW, "DSHOW"), (None, "默认")]
        attempts: list = []  # 每次尝试的失败原因（以前只打成功那条，出问题无法定位）

        def _try(strict: bool):
            """扫一遍 后端×index，返回 (cap, 后端名, index, 旋转后首帧, 校验说明) 或 None。

            strict=True：画面必须通过 frame_quality（非全黑、非噪点）。
            strict=False：只要「能读到帧」就接受（最后兜底，外层会打 warning）。
            """
            for api, api_name in backends:
                for idx in candidates:
                    cap = cv2.VideoCapture(idx) if api is None else cv2.VideoCapture(idx, api)
                    if not cap.isOpened():
                        attempts.append(f"{api_name}#{idx}:打不开")
                        cap.release()
                        continue
                    if not self._native:  # 预览模式不设尺寸，保留摄像头原生输出
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(cap_w))
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(cap_h))
                    fps = self._store.get("vision", "capture.camera_fps")
                    if fps:
                        cap.set(cv2.CAP_PROP_FPS, float(fps))
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        attempts.append(f"{api_name}#{idx}:读不到帧")
                        cap.release()
                        continue
                    rotated = _rotate(frame, self._rotation)
                    good, why = frame_quality(rotated)
                    if strict and not good:
                        attempts.append(f"{api_name}#{idx}:画面无效({why})")
                        cap.release()
                        continue
                    return cap, api_name, idx, rotated, why
            return None

        found = _try(True)
        if found is None:
            # 真实设备可能就是全黑/噪点（镜头被挡、环境全黑、USB 带宽不足）：
            # 兜底接受，但必须打 warning 讲清「画面可能不可用」，别让用户以为摄像头坏了。
            self._emit(
                "warning",
                "未找到画面有效的摄像头，退而接受「能读到帧」的设备（画面可能全黑/噪点）："
                + "；".join(attempts[-8:]),
            )
            found = _try(False)
        if found is None:
            self._emit(
                "error",
                "未找到可用摄像头（DSHOW/默认 后端均已尝试 index 0~4）：" + "；".join(attempts[-8:]),
            )
            return False

        cap, api_name, idx, rotated, why = found
        self._cap = cap
        self._actual = (int(rotated.shape[1]), int(rotated.shape[0]))
        if idx != index:
            self._emit(
                "warning",
                f"配置的 camera_index={index} 不可用，已改用 index={idx}（backend={api_name}）；"
                f"若不是预期设备，请核对 hardware.robot.camera_index 与 camera_name",
            )
        target_text = "原生" if self._native else f"{w}x{h}"
        self._emit(
            "info",
            f"摄像头已打开: backend={api_name} index={idx} 采集={cap_w}x{cap_h} "
            f"旋转后={self._actual[0]}x{self._actual[1]} "
            f"目标={target_text} 快帧={self._fast_size[0]}x{self._fast_size[1]} "
            f"｜画面校验={why}",
        )
        if (not self._native) and self._actual != (int(w), int(h)):
            self._emit(
                "warning",
                f"实际画面 {self._actual} 与配置 capture_resolution {(int(w), int(h))} 不一致，"
                f"将按配置尺寸缩放；若偏差过大请用 tools/query_camera_res.py 核对摄像头能力",
            )
        self.start_capture()  # 摄像头开好后立即启动实时采集（预览 / detect 共用缓存）
        return True

    def close(self) -> None:
        self.stop_capture()
        if self._cap is not None:
            self._cap.release()
            self._cap = None
            self._emit("info", "摄像头已关闭")

    @property
    def is_opened(self) -> bool:
        return self._cap is not None and bool(self._cap.isOpened())

    @property
    def actual_size(self) -> Tuple[int, int]:
        """旋转后的实际画面尺寸，是 ROI 归一化的基准。"""
        return self._actual

    # ---------------------------------------------------------------- 取帧

    def _build_frame(self, frame: np.ndarray) -> FrameSet:
        """把原始帧做旋转/裁黑边/双流缩放，产出 FrameSet。采集线程与回退读共用。"""
        frame = _rotate(frame, self._rotation)
        # 先裁黑边再缩放：这样 raw 始终是「有效画面」的标称尺寸，
        # screen_roi_ratio 的语义（相对有效区）在两种取帧源下保持一致。
        if self._letterbox == "auto":
            frame = crop_letterbox(frame)
        if self._native:
            # 预览/截图模式：不缩放（也不复制，帧本身是每次 read 的新数组），保持原始比例
            return FrameSet(raw=frame, fast=frame, ts=time.time())
        target = (int(self._capture_size[0]), int(self._capture_size[1]))
        if (frame.shape[1], frame.shape[0]) != target:
            raw = cv2.resize(frame, target, interpolation=cv2.INTER_AREA)
        else:
            raw = frame

        fast_size = (int(self._fast_size[0]), int(self._fast_size[1]))
        if (raw.shape[1], raw.shape[0]) == fast_size:
            fast = raw
        else:
            fast = cv2.resize(raw, fast_size, interpolation=self._interp)

        return FrameSet(raw=raw, fast=fast, ts=time.time())

    def read(self, timeout: float = 0.3) -> Optional[FrameSet]:
        """读取最新一帧并生成双流。失败返回 None。

        采集线程已启动时（业务运行 / 实时预览态）：直接返回缓存的最新帧——
        非阻塞，仅在首帧未就绪时最多等待 timeout；线程安全，不碰设备读取。
        否则（工具单帧读取等）：走旧的直接 read 行为。
        """
        if self._capture_running and self._cap_thread is not None:
            with self._cv:
                if not self._cv.wait_for(lambda: self._latest is not None, timeout=timeout):
                    return None
                return self._latest
        if not self.is_opened:
            raise CameraError("摄像头未打开（请先调用 open()）")
        ok, frame = self._cap.read()  # type: ignore[union-attr]
        if not ok or frame is None:
            return None
        return self._build_frame(frame)

    # ---------------------------------------------------------------- 后台采集

    def start_capture(self) -> None:
        """启动后台采集线程：按摄像头帧率持续取帧并缓存最新一帧（实时预览 / detect 共用）。"""
        if self._capture_running:
            return
        if not self.is_opened:
            return
        self._latest = None
        self._stop_cap.clear()
        self._capture_running = True
        self._cap_thread = threading.Thread(target=self._capture_loop, daemon=True, name="cam-capture")
        self._cap_thread.start()

    def _capture_loop(self) -> None:
        while not self._stop_cap.is_set():
            try:
                ok, frame = self._cap.read()  # type: ignore[union-attr]
            except Exception as exc:  # 摄像头掉线
                self._emit("warning", f"采集线程取帧异常: {exc}")
                time.sleep(0.2)
                continue
            if not ok or frame is None:
                time.sleep(0.1)
                continue
            fs = self._build_frame(frame)
            with self._cv:
                self._latest = fs
                self._cv.notify_all()

    def stop_capture(self) -> None:
        """停止后台采集线程。"""
        self._capture_running = False
        self._stop_cap.set()
        t = self._cap_thread
        self._cap_thread = None
        if t is not None and t.is_alive():
            t.join(timeout=1.0)

    def reload_config(self) -> None:
        """配置热重载后调用。分辨率变化需要重新 open()。"""
        self._rotation = int(self._store.get("vision", "capture.camera_rotation", 0) or 0)
        self._capture_size = tuple(
            self._store.get("vision", "capture.capture_resolution", (480, 640))
        )
        self._fast_size = tuple(self._store.get("vision", "capture.fast_size", (480, 640)))
        self._interp = getattr(
            cv2, str(self._store.get("vision", "capture.fast_interpolation", "INTER_AREA"))
        )
        self._letterbox = str(
            self._store.get("vision", "capture.letterbox", "none") or "none"
        ).lower()

    def resolution_changed(self) -> bool:
        """配置里的分辨率是否已变化（需要重建采集流）。"""
        return tuple(self._store.get("vision", "capture.capture_resolution", (480, 640))) != tuple(
            self._capture_size
        )

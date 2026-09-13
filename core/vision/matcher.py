"""多尺度模板匹配引擎 —— 页面判定的底层。

设计要点：
    * 在传入帧（手机逻辑点分辨率，iPhone X = 375x812）上匹配，模板与帧同分辨率，
      模板零重采；
    * 每个模板含多张变体（alt），匹配取所有变体最高分；
    * ROI 限定搜索：模板注册表里带归一化 roi 时只搜 ROI，命中即返回；
    * 模板与金字塔缓存：同一模板多次缩放只算一次；
    * find_all 做多目标峰值迭代 + NMS，供「结算行 + 头像」组合锚点切卡使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from core.config_store import ConfigStore, TemplateSpec
from core.vision import imgio

LogFn = Optional[Callable[[str, str], None]]


@dataclass(frozen=True)
class MatchResult:
    """一次模板命中的完整描述（坐标在传入的 frame 坐标系内）。"""

    name: str
    score: float
    center: Tuple[int, int]  # (cx, cy)
    rect: Tuple[int, int, int, int]  # (x, y, w, h) 帧像素
    scale: float
    path: str = ""

    @property
    def cx(self) -> int:
        return self.center[0]

    @property
    def cy(self) -> int:
        return self.center[1]

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "score": round(float(self.score), 3),
            "cx": self.cx,
            "cy": self.cy,
            "rect": list(self.rect),
            "scale": round(float(self.scale), 3),
        }


class TemplateMatcher:
    """多尺度模板匹配器（线程安全：内部操作均为局部变量，模板缓存由锁保护）。

    支持 match_mode:
      - "gray"  : 默认，灰度 NCC（对亮度偏移鲁棒，但对极性反转敏感）
      - "shape" : Canny 边缘形状匹配（抗颜色/背景/极性，适合图标类模板）
    """

    # shape 模式的 Canny 参数（对所有 shape 模板统一）
    SHAPE_GAUSSIAN_KSIZE = 3
    SHAPE_CANNY_LOW = 50
    SHAPE_CANNY_HIGH = 150

    def __init__(self, store: ConfigStore, log: LogFn = None) -> None:
        self._store = store
        self._log = log
        self._method = getattr(
            cv2, str(store.get("vision", "matcher.method", "TM_CCOEFF_NORMED"))
        )
        self._scales: Tuple[float, ...] = tuple(
            float(s) for s in store.get("vision", "matcher.scales", (1.0,))
        )
        self._cache_pyramid = bool(store.get("vision", "matcher.cache_pyramid", True))
        self._gray_cache: Dict[str, np.ndarray] = {}
        self._shape_cache: Dict[str, np.ndarray] = {}   # shape 模板的边缘缓存
        self._pyr_cache: Dict[Tuple[str, float], np.ndarray] = {}

    # ---------------------------------------------------------------- 模板加载

    def _emit(self, level: str, msg: str) -> None:
        if self._log:
            self._log(level, msg)

    def clear_cache(self) -> None:
        self._gray_cache.clear()
        self._shape_cache.clear()
        self._pyr_cache.clear()

    @staticmethod
    def _to_shape(gray: np.ndarray) -> np.ndarray:
        """灰度图 → Canny 边缘图（形状匹配用，抗颜色/极性/背景）。"""
        blurred = cv2.GaussianBlur(gray, (TemplateMatcher.SHAPE_GAUSSIAN_KSIZE,) * 2, 0)
        edges = cv2.Canny(
            blurred,
            TemplateMatcher.SHAPE_CANNY_LOW,
            TemplateMatcher.SHAPE_CANNY_HIGH,
        )
        return edges

    def _gray(self, frame: np.ndarray) -> np.ndarray:
        if frame.ndim == 3:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return frame

    def _load_gray(self, spec: TemplateSpec) -> Optional[np.ndarray]:
        key = str(spec.path)
        if key in self._gray_cache:
            return self._gray_cache[key]
        img = imgio.imread_unicode(spec.path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            # 变体缺失不算致命（注册校验已报错），主文件缺失时报警
            self._emit("warning", f"模板读取失败: {spec.name} -> {spec.path}")
            return None
        if img.size == 0:
            return None
        self._gray_cache[key] = img
        return img

    def _load_scaled(self, spec: TemplateSpec, scale: float) -> Optional[np.ndarray]:
        """取模板某一尺度的灰度/形状图（带缓存）。"""
        is_shape = spec.match_mode == "shape"

        # shape 模式：从 shape 缓存取
        if is_shape:
            key = str(spec.path)
            if key in self._shape_cache:
                base = self._shape_cache[key]
            else:
                gray = self._load_gray(spec)
                if gray is None:
                    return None
                base = self._to_shape(gray)
                self._shape_cache[key] = base
        else:
            base = self._load_gray(spec)
            if base is None:
                return None

        if abs(scale - 1.0) < 1e-9:
            return base
        key = (str(spec.path), round(scale, 4))
        if self._cache_pyramid and key in self._pyr_cache:
            return self._pyr_cache[key]
        new_w = max(1, int(round(base.shape[1] * scale)))
        new_h = max(1, int(round(base.shape[0] * scale)))
        scaled = cv2.resize(base, (new_w, new_h), interpolation=cv2.INTER_AREA)
        if self._cache_pyramid:
            self._pyr_cache[key] = scaled
        return scaled

    # ---------------------------------------------------------------- 核心匹配

    def _match_frame(
        self,
        frame_gray: np.ndarray,
        search_rect: Tuple[int, int, int, int],
        spec: TemplateSpec,
        threshold: float,
        scales: Optional[Sequence[float]] = None,
    ) -> Optional[MatchResult]:
        """在搜索区内对模板做多尺度匹配，返回最高分命中（未达阈值返回 None）。"""
        sx, sy, sw, sh = search_rect
        if sw < 4 or sh < 4:
            return None
        region = frame_gray[sy : sy + sh, sx : sx + sw]
        # shape 模式：搜索区也转边缘，实现"只比线条形状"
        if spec.match_mode == "shape":
            region = self._to_shape(region)
        best: Optional[MatchResult] = None
        for scale in (scales if scales else self._scales):
            tpl = self._load_scaled(spec, scale)
            if tpl is None:
                continue
            th, tw = tpl.shape[:2]
            if tw > sw or th > sh:
                continue  # 该尺度模板大于搜索区，跳过
            res = cv2.matchTemplate(region, tpl, self._method)
            # 抗反色（仅 gray 模式）：TM_CCOEFF_NORMED 对「亮度反色」会得到负相关。
            # 详情页导航栏背景色会变（白/蓝/黄/粉红），图标随之反色（深色图标↔白色图标），
            # 取绝对值后两种极性都能命中，不必为每种背景色单独采变体。
            # shape 模式（Canny 边缘）语义不同，不取绝对值。
            if spec.match_mode != "shape":
                res = np.abs(res)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if best is None or max_val > best.score:
                best = MatchResult(
                    name=spec.name,
                    score=float(max_val),
                    center=(sx + max_loc[0] + tw // 2, sy + max_loc[1] + th // 2),
                    rect=(sx + max_loc[0], sy + max_loc[1], tw, th),
                    scale=scale,
                    path=str(spec.path),
                )
        if best is not None and best.score >= threshold:
            return best
        return None

    def _search_rect(
        self,
        frame_shape: Tuple[int, int],
        roi: Optional[Sequence[float]],
        override: Optional[Tuple[int, int, int, int]] = None,
    ) -> Tuple[int, int, int, int]:
        if override is not None:
            return override
        fh, fw = frame_shape[:2]
        if not roi:
            return (0, 0, fw, fh)
        left, top, right, bottom = roi
        x0 = max(0, int(round(left * fw)))
        y0 = max(0, int(round(top * fh)))
        x1 = min(fw, int(round(right * fw)))
        y1 = min(fh, int(round(bottom * fh)))
        return (x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))

    def find(
        self,
        frame: np.ndarray,
        spec: TemplateSpec,
        roi: Optional[Sequence[float]] = None,
        threshold: Optional[float] = None,
        search_rect: Optional[Tuple[int, int, int, int]] = None,
    ) -> Optional[MatchResult]:
        """单个模板（含全部变体）匹配。

        搜索区优先级：search_rect > roi 参数 > 模板注册 spec.roi > 全屏。
        阈值优先取参数，其次取注册阈值。
        """
        if not spec.enabled:
            return None
        thr = spec.threshold if threshold is None else float(threshold)
        effective_roi = spec.roi if roi is None else roi
        rect = self._search_rect(frame.shape, effective_roi, search_rect)
        gray = self._gray(frame)
        # 模板级多尺度优先（如桌面图标抗尺寸浮动），否则用全局 scales
        scales = spec.scales if spec.scales else self._scales

        # 主文件 + 变体全部跑一遍，取最高分
        candidates: List[TemplateSpec] = [spec]
        for alt in spec.alts:
            candidates.append(_replace_spec_path(spec, alt))
        best: Optional[MatchResult] = None
        for cand in candidates:
            hit = self._match_frame(
                gray, rect, cand, thr if best is None else 0.0, scales
            )
            if hit is not None and (best is None or hit.score > best.score):
                best = hit
        return best if best is not None and best.score >= thr else None

    def find_any(
        self,
        frame: np.ndarray,
        specs: Iterable[TemplateSpec],
        roi: Optional[Sequence[float]] = None,
        threshold: Optional[float] = None,
    ) -> Optional[MatchResult]:
        """在多个模板（如红色 Tab 的两种形态）中找最高分命中。"""
        best: Optional[MatchResult] = None
        for spec in specs:
            thr = spec.threshold if threshold is None else float(threshold)
            hit = self.find(frame, spec, roi=roi, threshold=thr if best is None else 0.0)
            if hit is not None and (best is None or hit.score > best.score):
                best = hit
        return best

    def find_all(
        self,
        frame: np.ndarray,
        spec: TemplateSpec,
        roi: Optional[Sequence[float]] = None,
        threshold: Optional[float] = None,
        min_gap: int = 10,
        top_k: int = 60,
    ) -> List[MatchResult]:
        """多目标匹配：跨尺度峰值迭代 + NMS（供组合锚点切卡）。"""
        thr = spec.threshold if threshold is None else float(threshold)
        rect = self._search_rect(frame.shape, roi)
        sx, sy, sw, sh = rect
        if sw < 4 or sh < 4:
            return []
        gray = self._gray(frame)
        region = gray[sy : sy + sh, sx : sx + sw]

        peaks: List[Tuple[float, int, int, int, int, float]] = []  # (score, cx, cy, tw, th, scale)
        for scale in self._scales:
            cand = self._load_scaled(spec, scale)
            if cand is None or cand.shape[1] > sw or cand.shape[0] > sh:
                continue
            th, tw = cand.shape[:2]
            res = cv2.matchTemplate(region, cand, self._method)
            res_h, res_w = res.shape[:2]
            work = res.copy()
            for _ in range(top_k):
                _, max_val, _, max_loc = cv2.minMaxLoc(work)
                if max_val < thr:
                    break
                x, y = max_loc
                cx = sx + x + tw // 2
                cy = sy + y + th // 2
                peaks.append((float(max_val), cx, cy, tw, th, scale))
                # 尺度内 NMS：抑制当前峰值附近区域
                x0 = max(0, x - min_gap)
                y0 = max(0, y - min_gap)
                x1 = min(res_w, x + min_gap + 1)
                y1 = min(res_h, y + min_gap + 1)
                work[y0:y1, x0:x1] = -1.0

        if not peaks:
            return []

        # 跨尺度 NMS：分数降序，抑制帧坐标上靠近的重复峰（同位置不同尺度的副本）
        peaks.sort(key=lambda p: p[0], reverse=True)
        kept: List[Tuple[float, int, int, int, int, float]] = []
        for p in peaks:
            val, cx, cy, tw, th, scale = p
            if any(abs(k[1] - cx) < min_gap and abs(k[2] - cy) < min_gap for k in kept):
                continue
            kept.append(p)

        return [
            MatchResult(
                name=spec.name,
                score=float(val),
                center=(cx, cy),
                rect=(cx - tw // 2, cy - th // 2, tw, th),
                scale=scale,
                path=str(spec.path),
            )
            for (val, cx, cy, tw, th, scale) in kept
        ]

    # ---------------------------------------------------------------- 便捷工具

    def score(self, frame: np.ndarray, spec: TemplateSpec) -> float:
        """只回分数（调试用，不设阈值）。"""
        hit = self.find(frame, spec, threshold=-2.0)
        return hit.score if hit else -1.0


def _replace_spec_path(spec: TemplateSpec, path: Path) -> TemplateSpec:
    """构造一个指向变体文件的同参模板规格（内部用）。"""
    return TemplateSpec(
        name=spec.name,
        path=str(path),
        alts=(),
        threshold=spec.threshold,
        roi=spec.roi,
        scales=spec.scales,
        kind=spec.kind,
        match_mode=spec.match_mode,
        enabled=spec.enabled,
        purpose=spec.purpose,
    )

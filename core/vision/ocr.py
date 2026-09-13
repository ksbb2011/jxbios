"""OCR 封装：RapidOCR（ONNX CPU），线程串行 + 帧差去重。

设计要点：
    * 后端可配置（vision.ocr.backend），首次调用时才加载模型（懒加载）；
    * RapidOCR 检测阶段先把图缩到 det_limit_side_len=640，识别在原始裁剪行上，
      所以「判定用 fast 帧、识别用 raw 帧 ROI」能得到最优性价比；
    * OCR 非线程安全，全库一把锁串行；
    * 帧差去重：内容不变的帧直接复用上次结果，省掉最贵的一次完整 OCR；
    * 红字判定供城市面板层级铁律使用：文本框与红色像素区域重合才算红字。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from core.config_store import ConfigStore

LogFn = Optional[Callable[[str, str], None]]

_OCR_LOCK = threading.Lock()


@dataclass
class TextBox:
    """一次 OCR 识别出的文本行。"""

    text: str
    score: float
    box: np.ndarray  # (4, 2) 四边形顶点，帧像素

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        x = int(self.box[:, 0].min())
        y = int(self.box[:, 1].min())
        w = int(self.box[:, 0].max()) - x
        h = int(self.box[:, 1].max()) - y
        return (x, y, w, h)

    @property
    def center(self) -> Tuple[int, int]:
        x, y, w, h = self.rect
        return (x + w // 2, y + h // 2)

    @property
    def left(self) -> int:
        return int(self.box[:, 0].min())

    @property
    def right(self) -> int:
        return int(self.box[:, 0].max())

    @property
    def top(self) -> int:
        return int(self.box[:, 1].min())

    @property
    def bottom(self) -> int:
        return int(self.box[:, 1].max())

    def contains(self, x: float, y: float) -> bool:
        x0, y0, w, h = self.rect
        return x0 <= x <= x0 + w and y0 <= y <= y0 + h

    def overlaps(self, x0: int, y0: int, x1: int, y1: int) -> bool:
        return not (self.right < x0 or self.left > x1 or self.bottom < y0 or self.top > y1)

    def to_dict(self) -> dict:
        x, y, w, h = self.rect
        return {
            "text": self.text,
            "score": round(float(self.score), 3),
            "x": x,
            "y": y,
            "w": w,
            "h": h,
        }


def _hash_image(image: np.ndarray, size: int = 32) -> np.ndarray:
    """均值哈希（对亮度变化稳健），用于帧差去重。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    small = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)
    return small.astype(np.int16)


class OcrEngine:
    """OCR 引擎封装（懒加载 + 线程串行 + 帧差去重）。

    缺少 rapidocr_onnxruntime 时优雅降级：所有 OCR 返回空列表，主循环不崩
    （模板匹配/页面判定仍正常工作，仅 OCR 字段提取和弹窗文字判据跳过）。
    """

    def __init__(self, store: ConfigStore, log: LogFn = None) -> None:
        self._store = store
        self._log = log
        self._engine = None
        self._available: Optional[bool] = None  # None=未尝试, True=可用, False=缺包降级
        self._lock = threading.Lock()
        self._conf_thr = float(store.get("vision", "ocr.conf_threshold", 0.5) or 0.5)
        self._dedup_enabled = bool(store.get("vision", "ocr.dedup_frame", True))
        self._dedup_thresh = float(store.get("vision", "ocr.dedup_thresh", 4.0) or 4.0)
        self._dedup_hash: Optional[np.ndarray] = None
        self._dedup_result: List[TextBox] = []

    @property
    def available(self) -> bool:
        """OCR 引擎是否可用（缺包时返回 False，不崩溃）。"""
        if self._available is None:
            try:
                self._ensure_engine()
                self._available = True
            except Exception:
                self._available = False
        return self._available

    def _emit(self, level: str, msg: str) -> None:
        if self._log:
            self._log(level, msg)

    # ---------------------------------------------------------------- 引擎

    def _ensure_engine(self):
        if self._engine is not None:
            return self._engine
        if self._available is False:
            # 首轮已打过降级 warning，后续直接失败，避免每条调用重复刷屏
            raise RuntimeError("缺少 rapidocr_onnxruntime（OCR 已降级，跳过）")
        backend = str(self._store.get("vision", "ocr.backend", "rapid"))
        with self._lock:
            if self._engine is not None:
                return self._engine
            if backend == "rapid":
                try:
                    from rapidocr_onnxruntime import RapidOCR
                except Exception as exc:  # pragma: no cover
                    # 打包环境曾因漏打 six（rapidocr 依赖链）导致整个 rapidocr 导入失败，
                    # 但旧代码只捕获 ImportError 且不打详情，日志只说「缺少」无从排障。
                    # 现捕获所有异常并把真实原因写进日志（如 ModuleNotFoundError: six）。
                    self._available = False
                    self._emit("warning",
                        f"rapidocr_onnxruntime 导入失败，OCR 功能已降级（跳过，不崩溃）。"
                        f"原因: {type(exc).__name__}: {exc}"
                    )
                    raise RuntimeError(
                        f"rapidocr_onnxruntime 导入失败: {exc}"
                    ) from exc
                det_limit = int(self._store.get("vision", "ocr.det_limit_side_len", 640))
                rec_batch = bool(self._store.get("vision", "ocr.rec_batch_mode", True))
                self._engine = RapidOCR(det_limit_side_len=det_limit, rec_batch_mode=rec_batch)
                self._available = True
                self._emit("info", f"RapidOCR 已加载（det_limit_side_len={det_limit}）")
            else:  # pragma: no cover
                raise RuntimeError(f"暂不支持 OCR 后端: {backend}")
        return self._engine

    # ---------------------------------------------------------------- 识别

    def ocr(self, image: np.ndarray, roi: Optional[Sequence[int]] = None) -> List[TextBox]:
        """对 image（或其 ROI 裁剪）做完整 OCR。结果坐标相对 image 原点。

        OCR 引擎不可用时（缺包等）返回空列表，不抛异常。
        """
        crop = image
        if roi is not None:
            x, y, w, h = [int(v) for v in roi]
            crop = image[y : y + h, x : x + w]
        if crop.size == 0:
            return []

        try:
            engine = self._ensure_engine()
        except Exception:
            # 引擎不可用：优雅降级，返回空列表
            # 首次失败已由 _ensure_engine 打过 warning 日志，后续不再刷屏
            return []
        with _OCR_LOCK:
            result, elapse = engine(crop)
        boxes: List[TextBox] = []
        if not result:
            return boxes
        for entry in result:
            quad, text, score = entry[0], str(entry[1]), float(entry[2])
            if score < self._conf_thr:
                continue
            poly = np.asarray(quad, dtype=np.float64).reshape(-1, 2)
            if roi is not None:
                poly = poly + np.array([roi[0], roi[1]], dtype=np.float64)
            boxes.append(TextBox(text=text, score=score, box=poly))
        return boxes

    def ocr_roi(
        self,
        raw_image: np.ndarray,
        roi: Sequence[int],
        coord_scale: Tuple[float, float] = (1.0, 1.0),
    ) -> List[TextBox]:
        """raw 帧上按像素 ROI 识别，坐标再换算到目标坐标系（如 fast 帧）。

        coord_scale = (scale_x, scale_y)，由调用方按 fast/raw 尺寸换算。
        """
        boxes = self.ocr(raw_image, roi=roi)
        if coord_scale == (1.0, 1.0):
            return boxes
        sx, sy = coord_scale
        for tb in boxes:
            tb.box = tb.box * np.array([[sx, sy]], dtype=np.float64)
        return boxes

    def ocr_dedup(self, image: np.ndarray, roi: Optional[Sequence[int]] = None) -> List[TextBox]:
        """帧差去重的识别：与上一帧内容一致时直接复用上次结果。"""
        if not self._dedup_enabled:
            return self.ocr(image, roi=roi)
        key = _hash_image(image)
        with self._lock:
            same = (
                self._dedup_hash is not None
                and int(np.abs(key - self._dedup_hash).mean()) <= self._dedup_thresh
            )
            if same:
                return list(self._dedup_result)
        boxes = self.ocr(image, roi=roi)
        with self._lock:
            self._dedup_hash = key
            self._dedup_result = list(boxes)
        return boxes

    def reset_dedup(self) -> None:
        with self._lock:
            self._dedup_hash = None
            self._dedup_result = []

    # ---------------------------------------------------------------- 红字

    @staticmethod
    def red_pixels(
        image: np.ndarray,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        r_min: int = 140,
        rg: int = 50,
        rb: int = 50,
    ) -> Tuple[int, int]:
        """区域内红色像素 (命中数, 总数)。判定：R 显著高于 G/B 且 R 够亮。"""
        x0 = max(0, int(x0))
        y0 = max(0, int(y0))
        x1 = min(image.shape[1], int(x1))
        y1 = min(image.shape[0], int(y1))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return 0, 0
        patch = image[y0:y1, x0:x1]
        if patch.ndim == 2:
            return 0, 0
        b = patch[:, :, 0].astype(np.int16)
        g = patch[:, :, 1].astype(np.int16)
        r = patch[:, :, 2].astype(np.int16)
        mask = (r > r_min) & (r - g > rg) & (r - b > rb)
        return int(mask.sum()), int(mask.size)

    @classmethod
    def red_ratio(cls, image: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> float:
        """区域中「红色像素」占比。判定：R 显著高于 G/B 且 R 够亮。"""
        hit, total = cls.red_pixels(image, x0, y0, x1, y1)
        return 0.0 if total == 0 else float(hit) / float(total)

    def red_text_boxes(self, image: np.ndarray, boxes: List[TextBox]) -> List[TextBox]:
        """从识别结果里筛出红色文本行（严格档，通用）。"""
        out: List[TextBox] = []
        for tb in boxes:
            x, y, w, h = tb.rect
            ratio = self.red_ratio(image, x, y, x + w, y + h)
            if ratio >= 0.25:
                out.append(tb)
        return out

    def city_red_text_boxes(self, image: np.ndarray, boxes: List[TextBox]) -> List[TextBox]:
        """城市面板红字（铁律 §2.3 / 犯错记录 [0831-H]）——放宽档，仅供城市面板。

        教训 [0831-H]：细红字落在 OCR 给的大框里（多为白底），红色像素占比天然很低，
        用严格档（r>140 & R-G>50 & R-B>50 且占比≥0.25）**永远返回 False**，顶部红字
        "看不见" → 只能走确认兜底 → 面板展开时确认不退出 → 死循环。
        故放宽为：色带 R>120 & R>G+25 & R>B+25，且「红像素数≥6 且占比>0.01」。
        """
        out: List[TextBox] = []
        for tb in boxes:
            x, y, w, h = tb.rect
            hit, total = self.red_pixels(
                image, x, y, x + w, y + h, r_min=120, rg=25, rb=25
            )
            if total and hit >= 6 and (float(hit) / float(total)) > 0.01:
                out.append(tb)
        return out

    @staticmethod
    def filter_red_text(
        boxes: List[TextBox], image: Optional[np.ndarray] = None
    ) -> List[TextBox]:
        """按文本特征粗筛红色系关键词（无图像时的兜底）。"""
        red_hints = ("出发", "目的地", "请选择")
        return [tb for tb in boxes if any(h in tb.text for h in red_hints)]

"""双帧一致判定：连续 N 帧判定一致才动作，过渡帧不动手。

背景（离线回归实测数据）：226 张详情页里有 47 张判成 UNKNOWN，
绝大多数是「整屏压暗的过渡帧」——页面加载中、转场动画中、弹窗淡入淡出中。

两种修法，选后者：

    A. 调低阈值去拟合这些过渡帧
       → 连累正常帧：阈值一降，详情页/列表页的误判率同步上升，得不偿失。

    B. 要求「连续 N 帧判定一致」才动作
       → 过渡帧**天然无法连续两帧一致**（它在变），用约 150ms 的等待换准确率，
         对抢单节奏几乎无影响。

pending 语义（事故教训，务必理解后再改）：
    未稳定时返回 pending=True，**绝不返回 UNKNOWN**。
    若返回 UNKNOWN，主循环会走未知页分支（静置 → 关闭按钮 → 左滑 → 返回键 → 主页键），
    把正常页面当成未知页反复折腾——这比判不出来更糟。
    pending=True 时主循环只等下一帧，不做任何动作。

取帧间隔下限：
    摄像头/采集卡有内部缓冲，连续 read() 可能拿到同一帧。若不设间隔下限，
    「重复读取同一帧」会被误判成「连续两帧一致」，本检测器就失效了。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque, List, Optional

from core.config_store import ConfigStore
from core.vision.page_state import PageDetector, PageResult, ScreenState


class StableDetector:
    """包装 PageDetector，加一层「连续 N 帧一致」的时间一致性约束。

    纯判定逻辑，不碰设备、不碰机械臂 —— 可以用离线帧序列直接单测。
    """

    def __init__(self, detector: PageDetector, store: Optional[ConfigStore] = None) -> None:
        self._detector = detector
        self._store = store
        self._history: Deque[ScreenState] = deque(maxlen=2)
        self._last_ts = 0.0
        self._load()

    # ---------------------------------------------------------------- 配置

    def _load(self) -> None:
        cfg = (self._store.section("thresholds").get("stable", {}) or {}) if self._store else {}
        # 默认关闭：先跑回归集量化验证有效再开，绝不能凭手感开
        self.enabled = bool(cfg.get("enabled", False))
        self.consensus = max(1, int(cfg.get("consensus_frames", 2) or 2))
        self.interval = float(cfg.get("interval_sec", 0.15) or 0.15)
        self._history = deque(maxlen=self.consensus)

    def reload_config(self) -> None:
        self._load()

    # ---------------------------------------------------------------- 判定

    def detect(self, frame, origin: Optional[ScreenState] = None) -> PageResult:
        res = self._detector.detect(frame, origin=origin)
        if not self.enabled:
            return res

        now = time.time()
        if self._last_ts and (now - self._last_ts) < self.interval:
            # 取帧太快，可能还是缓冲区里的同一帧，不纳入一致性统计
            return PageResult(
                state=ScreenState.UNKNOWN,
                pending=True,
                note="取帧间隔过短，等待新帧",
            )
        self._last_ts = now

        self._history.append(res.state)
        stable = len(self._history) >= self.consensus and len(set(self._history)) == 1
        if not stable:
            return PageResult(
                state=ScreenState.UNKNOWN,
                pending=True,
                note=f"判定未稳定 {len(self._history)}/{self.consensus}，"
                     f"当前={res.state.value}",
            )
        return res

    # ---------------------------------------------------------------- 控制

    def reset(self) -> None:
        """清空历史。页面已被外部改变（如手动干预、重启流程）后调用。"""
        self._history.clear()
        self._last_ts = 0.0

    @property
    def votes(self) -> List[ScreenState]:
        """当前累积的判定票（供调试与日志）。"""
        return list(self._history)


def has_content(frame, min_edge_density: float = 0.01, min_std: float = 8.0) -> bool:
    """快速判断这一屏「有没有内容」，用于跳过空屏的整屏 OCR。

    为什么不用纯亮度判断（vision.fast_scan.dark_brightness）：
        深色主题页面、夜间模式、压暗的过渡帧亮度都很低，但**有内容**；
        而白屏加载页亮度很高却**没内容**。亮度既会漏也会误。

    这里用两个更贴近「有内容」的信号：
        ① 边缘密度（Canny 非零点占比）—— 文字/图标会产生大量边缘
        ② 灰度标准差 —— 纯色画面（白屏/黑屏/纯色加载页）std 极低

    成本：Canny 在 480x640 上约 1~2ms，相比整屏 OCR（数百毫秒）可以忽略。
    """
    if frame is None or frame.size == 0:
        # 没有帧：交给调用方处理（scan_once 已单独判空），这里不给「有内容」的假信号
        return False
    try:
        import cv2
        import numpy as np

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        std = float(gray.std())
        if std < float(min_std):
            return False  # 纯色画面（黑屏/白屏/纯色加载页）：没内容

        # Canny 阈值必须自适应：固定 50/150 对低对比度暗色画面会检测不到任何边缘，
        # 把「压暗但有内容」的帧误判成空屏——这正是我们批评纯亮度判断的理由，
        # 自己不能再犯同样的错。用图像中位数按 Canny 推荐的 0.66/1.33 倍取阈值。
        median = float(np.median(gray))
        lower = max(1, int(0.66 * median))
        upper = min(255, max(lower + 1, int(1.33 * median)))
        edges = cv2.Canny(gray, lower, upper)
        density = float((edges > 0).sum()) / float(edges.size)
        return density >= float(min_edge_density)
    except Exception:
        # 处理异常时按「有内容」处理：宁可多跑一次 OCR，也不要漏掉一屏订单
        return True

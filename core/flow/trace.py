"""异步轨迹日志：主线程只 put_nowait，写盘与存图都在后台线程。

为什么要异步：
    整屏 OCR 与模板匹配已经很吃 CPU，若每次动作都同步写日志+存 JPEG，
    一屏能多出几百毫秒，直接拖慢抢单节奏。队列满时按配置丢弃并计数 ——
    宁可丢日志，也不能让日志把主线程堵住。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.config_store import ConfigStore
from core.vision.imgio import imwrite_unicode

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]


class TraceWriter:
    """JSONL 事件流 + 截图，后台单线程消费。"""

    def __init__(self, store: Optional[ConfigStore] = None) -> None:
        self.store = store or ConfigStore()
        cfg = self.store.section("runtime").get("trace", {}) or {}
        self.enabled = bool(cfg.get("enabled", True))
        # 相对路径锚定「项目根」（打包后 = exe 所在目录）：配置里写的是可读的
        # "data/traces"，但 frozen + UAC 提权启动时 CWD 可能是 System32，
        # 直接 Path(...) 会把轨迹与截图写丢到用户看不见的地方（2026-09-13 打包适配）。
        _dir = Path(str(cfg.get("dir", "data/traces")))
        self.dir = _dir if _dir.is_absolute() else (Path(self.store.root) / _dir)
        self.queue_size = int(cfg.get("queue_size", 500) or 500)
        self.drop_when_full = bool(cfg.get("drop_when_full", True))
        self.max_side = int(cfg.get("max_side_px", 800) or 800)
        self.quality = int(cfg.get("jpeg_quality", 60) or 60)
        self.shot_rate = float(cfg.get("shot_sample_rate", 0.2) or 0.2)
        # 每帧事件默认只记 1/4：主循环 0.2s 一帧，全记会把队列塞满、把真正有用的
        # 业务日志一起挤掉（实测 622s 只落盘 64/3109 帧，业务日志也被丢弃）。
        self.frame_rate = float(cfg.get("frame_sample_rate", 0.25) or 0.25)
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=self.queue_size)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.dropped = 0
        self._seq = 0

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path = self.dir / f"trace_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        self._stop.clear()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=3.0)
        self._thread = None

    # ---------------------------------------------------------------- 写入

    def put(self, level: str, msg: str, **extra: Any) -> None:
        if not self.enabled:
            return
        if level == "frame" and self.frame_rate < 1.0:
            import random

            if random.random() > self.frame_rate:
                return
        self._seq += 1
        event = {
            "seq": self._seq,
            "ts": round(time.time(), 3),
            "wall": time.strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        }
        if extra:
            event.update(extra)
        try:
            self._q.put_nowait(event)
        except queue.Full:
            self.dropped += 1
            if not self.drop_when_full:
                self._q.get_nowait()
                self._q.put_nowait(event)

    def shot(self, frame, tag: str, always: bool = False) -> None:
        """异步存证截图。always=False 时按 shot_sample_rate 抽样。"""
        if not self.enabled or frame is None or cv2 is None:
            return
        if not always and self.shot_rate < 1.0:
            import random

            if random.random() > self.shot_rate:
                return
        try:
            self._q.put_nowait({"__shot__": True, "frame": frame.copy(), "tag": tag})
        except queue.Full:
            self.dropped += 1

    # ---------------------------------------------------------------- 后台

    def _drain(self) -> None:
        while not self._stop.is_set() or not self._q.empty():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if item.get("__shot__"):
                    self._write_shot(item)
                else:
                    with open(self._path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def _write_shot(self, item: Dict[str, Any]) -> None:
        img = item["frame"]
        scale = min(1.0, self.max_side / max(img.shape[0], img.shape[1]))
        if scale < 1.0:
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        name = f"{time.strftime('%Y%m%d_%H%M%S')}_{item['tag']}.jpg"
        imwrite_unicode(self.dir / name, img, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])

    @property
    def pending(self) -> int:
        return self._q.qsize()

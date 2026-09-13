"""标定工具共用：取帧（真机或截图）+ 组装只读的 ActionKit。

只给 tools/ 下的标定脚本用，业务流程不依赖本模块。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

import cv2

from core.config_store import ConfigStore
from core.devices.frame_source import FrameSet, create_frame_source
from core.flow.actions import ActionKit
from core.vision.imgio import imread_unicode
from core.vision.matcher import TemplateMatcher
from core.vision.ocr import OcrEngine
from core.vision.page_state import PageDetector


def grab_frame(store: ConfigStore, image: str = "") -> FrameSet:
    """取一帧：给了 --image 就离线分析截图，否则开真机摄像头抓一帧。"""
    if image:
        img = imread_unicode(image)
        if img is None:
            raise SystemExit(f"图片读取失败: {image}")
        fast = tuple(store.get("vision", "capture.fast_size", (480, 640)))
        target = (int(fast[0]), int(fast[1]))
        if (img.shape[1], img.shape[0]) != target:
            img = cv2.resize(img, target, interpolation=cv2.INTER_AREA)
        return FrameSet(raw=img, fast=img.copy(), ts=0.0)

    source = create_frame_source(store)
    if not source.open():
        raise SystemExit(
            "取帧源打开失败（检查 vision.capture.source 与 hardware.robot.camera_index）"
        )
    try:
        fs = source.read()
    finally:
        source.close()
    if fs is None:
        raise SystemExit("取帧失败")
    return fs


def build_kit(store: ConfigStore, image: str = "") -> Tuple[ActionKit, FrameSet]:
    """组装一个只读 ActionKit（不连机械臂，不会有任何点击动作）。"""
    fs = grab_frame(store, image)
    matcher = TemplateMatcher(store)
    detector = PageDetector(store, matcher)
    # 只读用途：不连机械臂（None），不会有任何点击动作
    kit = ActionKit(
        store, None, None, matcher, detector, OcrEngine(store)  # type: ignore[arg-type]
    )
    return kit, fs

"""中文路径安全的图像读写（Windows 下 cv2.imread/imwrite 遇中文路径必失败）。

cv2.imread 读中文路径会静默返回 None，cv2.imwrite 写中文路径会直接失败，
统一走 np.fromfile + imdecode / imencode + tofile 规避。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence, Union

import cv2
import numpy as np


def imread_unicode(
    path: Union[str, Path], flags: int = cv2.IMREAD_COLOR
) -> Optional[np.ndarray]:
    """读取图像，支持中文/空格路径。失败返回 None（不抛异常）。"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite_unicode(
    path: Union[str, Path], img: np.ndarray, params: Optional[Sequence[Any]] = None
) -> bool:
    """写出图像，支持中文/空格路径（自动建父目录）。"""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        ext = target.suffix or ".png"
        ok, buf = cv2.imencode(ext, img, params or [])
        if not ok:
            return False
        buf.tofile(str(target))
    except (OSError, cv2.error):
        return False
    return True


__all__ = ["imread_unicode", "imwrite_unicode"]

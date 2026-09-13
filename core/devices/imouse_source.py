"""iMouse 取帧源 FrameSource 实现。

复用 `core.devices.frame_source` 已有的 FrameSet / FrameSource / crop_letterbox /
resize_keep_aspect，业务层（RobotTask / ActionKit / FlowContext）完全无感。

设计要点：
    * 取一帧（jpg，~60ms）→ raw = fast = 同图：实测 375x812 这个分辨率下没有
      双流必要；fast_size 配置被尊重（如果配置不等于原始分辨率则缩放）。
    * **绝不**给 IMouseFrameSource 加任何旋转/缩放/CLAHE：投屏截图本身
      已经是手机原生像素，没有畸变/反光/拉伸。
    * close() 是 no-op（HTTP 无状态连接）；is_opened 由 _opened 标志控制。
    * reload_config / resolution_changed：投屏后手机分辨率固定，恒返回 False。
      真要换手机，由用户重启程序（这是显式生命周期，比"悄无声息重连"安全）。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import numpy as np

from core.devices.frame_source import (
    FrameSet,
    FrameSourceError,
    resize_keep_aspect,
)
from core.devices.imouse_client import DeviceInfo, ImouseClient, ImouseError

_LOG = logging.getLogger("imouse.source")


class ImouseFrameSourceError(FrameSourceError):
    """iMouse 取帧源不可用（服务掉线 / 设备离线 / 取图超时）。"""

    @property
    def hint(self) -> str:
        return "检查：① iMouse 内核服务是否在跑 ② 手机是否投屏在线 ③ 网络是否通"


class ImouseFrameSource:
    """iMouse 取帧源，实现 FrameSource 协议。"""

    def __init__(self, store=None, log: Optional[Callable[[str, str], None]] = None,
                 *, client: Optional[ImouseClient] = None,
                 fast_size: tuple[int, int] = (375, 812),
                 host: str = "127.0.0.1") -> None:
        """两种构造方式：
          1) 真实环境：store=ConfigStore（暂未移植），client=None；
             open() 会探测 + 建客户端。
          2) 测试/标定工具：client=外部构造好的 ImouseClient（或 mock）；
             open() 直接复用。允许完全脱离服务跑单测。

        fast_size：模板匹配/页面判定使用的快帧尺寸。
          专业版 iPhone X 截图为 375x812，因此默认就是它；
          若用户在 vision.fast_size 配置里写了不同值，会缩放。
        """
        self._store = store
        self._log = log or (lambda lvl, msg: _LOG.log(
            {"info": logging.INFO, "warn": logging.WARNING,
             "error": logging.ERROR, "debug": logging.DEBUG}.get(lvl, logging.INFO),
            msg))
        self._client = client
        self._fast_size: tuple[int, int] = tuple(int(v) for v in fast_size)
        self._host = host

        self._device: Optional[DeviceInfo] = None
        self._opened = False

    # ---- 协议实现
    def open(self) -> bool:
        """探测服务、选设备、建立客户端。失败抛 ImouseFrameSourceError。"""
        try:
            if self._client is None:
                self._client = ImouseClient(host=self._host, log=self._log)
            if not self._client.edition:
                self._client.detect()
            self._device = self._client.pick_online()
            # 烟雾测试一次截图，确认链路通
            self._client.screenshot(self._device.id, jpg=True)
            self._opened = True
            self._log("info",
                      f"iMouse 取帧源就绪：{self._device.name or self._device.id} "
                      f"{self._device.width}x{self._device.height} "
                      f"@ {self._client.host}:{self._client.port}")
            return True
        except ImouseError as exc:
            self._opened = False
            raise ImouseFrameSourceError(f"iMouse 取帧源打开失败：{exc}") from exc

    def close(self) -> None:
        """HTTP 是无状态连接，没有真"设备"要关。"""
        self._opened = False

    @property
    def is_opened(self) -> bool:
        return self._opened and self._client is not None

    def read(self) -> Optional[FrameSet]:
        if not self.is_opened or self._device is None:
            return None
        try:
            img = self._client.screenshot(self._device.id, jpg=True)
        except ImouseError as exc:
            self._log("warn", f"iMouse 截图失败：{exc}")
            return None
        # 投屏截图无畸变/无黑边，letterbox=none（投屏模式不需要 crop）。
        fast = resize_keep_aspect(img, self._fast_size) \
            if (img.shape[1], img.shape[0]) != self._fast_size else img
        return FrameSet(raw=img, fast=fast, ts=time.time())

    def reload_config(self) -> None:
        """iMouse 取帧源在 open 后分辨率固定，热重载无需重建。"""
        return None

    def resolution_changed(self) -> bool:
        """投屏后手机分辨率由设备本身决定、程序改不了，恒 False。"""
        return False

    # ---- 暴露给上层（如标定工具、调试）
    @property
    def client(self) -> ImouseClient:
        assert self._client is not None, "请先调用 open()"
        return self._client

    @property
    def device(self) -> DeviceInfo:
        assert self._device is not None, "请先调用 open()"
        return self._device

    @property
    def logical_size(self) -> tuple[int, int]:
        """屏幕逻辑点尺寸（iPhone X = 375x812）。坐标空间唯一基准。"""
        return (self._device.width, self._device.height) if self._device else (0, 0)

    @property
    def scale_to_logical(self) -> tuple[float, float]:
        """实测截图（iMouse 默认）相对逻辑点的缩放因子。

        本机实测 ratio=1.0；若将来用 capture_card 之类上游加了不同比例，
        上层点坐标应统一经过 `core.vision.screen_space` 换算。
        """
        if not self._device or self._device.width == 0 or self._device.height == 0:
            return (1.0, 1.0)
        # 取最近一帧的 raw 实际尺寸
        try:
            img = self._client.screenshot(self._device.id, jpg=True)  # 仅用于探测比例
            h, w = img.shape[:2]
            return (w / self._device.width, h / self._device.height)
        except ImouseError:
            return (1.0, 1.0)
"""ImouseFrameSource 单元测试 —— 用 mock client 隔离 iMouse 服务。

覆盖：
    * open() 必须做烟雾测试截图（确认链路通）
    * read() 必须返回 FrameSet(raw, fast, ts)
    * ratio=1.0 时 fast = raw（无 resize）
    * fast_size 与原图不一致时正确 resize
    * 截图失败时 read() 返回 None 且不抛
    * close()/is_opened 状态正确
    * reload_config/resolution_changed 是无害 no-op
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

from core.devices.frame_source import FrameSet
from core.devices.imouse_client import ImouseError
from core.devices.imouse_source import ImouseFrameSource, ImouseFrameSourceError


def _bmp_bytes(w: int, h: int) -> bytes:
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[..., 0] = 200  # BGR: 蓝色
    ok, buf = cv2.imencode(".bmp", img)
    assert ok
    return buf.tobytes()


def _fake_device(id_: str = "DE:AD:BE:EF:00:01") -> SimpleNamespace:
    return SimpleNamespace(
        id=id_, name="iPhone X",
        width=375, height=812,
        imgw=376, imgh=812,
        rotate=0, state=1,
        model="iPhone10,3", version="16.7.10",
        raw={"deviceid": id_},
    )


@pytest.fixture
def mock_client() -> MagicMock:
    cli = MagicMock()
    cli.edition = "pro"
    cli.port = 9912
    cli.list_devices.return_value = [_fake_device()]
    cli.pick_online.return_value = _fake_device()
    cli.screenshot.return_value = cv2.imdecode(
        np.frombuffer(_bmp_bytes(375, 812), np.uint8), cv2.IMREAD_COLOR)
    return cli


class TestImouseFrameSource:
    def test_open_runs_smoke_screenshot(self, mock_client: MagicMock) -> None:
        src = ImouseFrameSource(client=mock_client)
        assert src.open() is True
        assert src.is_opened
        # 至少调过 1 次截图作为烟雾测试
        assert mock_client.screenshot.call_count >= 1
        assert src.device.id == "DE:AD:BE:EF:00:01"
        assert src.logical_size == (375, 812)

    def test_open_failure_raises_typed_error(self, mock_client: MagicMock) -> None:
        mock_client.screenshot.side_effect = ImouseError("boom")
        src = ImouseFrameSource(client=mock_client)
        with pytest.raises(ImouseFrameSourceError):
            src.open()
        assert src.is_opened is False

    def test_read_returns_frame_set_with_ratio_one(self, mock_client: MagicMock) -> None:
        """fast_size == 截图实际尺寸时，fast 应等于 raw（无多余拷贝）。"""
        src = ImouseFrameSource(client=mock_client, fast_size=(375, 812))
        src.open()
        before = time.time()
        fs = src.read()
        assert isinstance(fs, FrameSet)
        assert fs.raw.shape == (812, 375, 3)
        assert fs.fast.shape == (812, 375, 3)
        # ratio=1 时 np 应保持同一对象（resize_keep_aspect 检测尺寸一致就直通）
        assert fs.fast is fs.raw
        assert fs.ts >= before

    def test_read_resizes_when_fast_size_differs(self, mock_client: MagicMock) -> None:
        """fast_size != 实际截图时，必须 resize。"""
        src = ImouseFrameSource(client=mock_client, fast_size=(187, 406))
        src.open()
        fs = src.read()
        assert fs.fast.shape == (406, 187, 3)
        assert fs.raw.shape == (812, 375, 3)

    def test_read_returns_none_on_transient_failure(self, mock_client: MagicMock) -> None:
        """截图失败时 read() 必须返回 None 而不是抛——上层按"未取到帧"重试。"""
        # open 烟雾测试要成功
        src = ImouseFrameSource(client=mock_client)
        src.open()
        # 之后让截图失败
        mock_client.screenshot.side_effect = ImouseError("timeout")
        assert src.read() is None

    def test_close_clears_opened(self, mock_client: MagicMock) -> None:
        src = ImouseFrameSource(client=mock_client)
        src.open()
        src.close()
        assert src.is_opened is False

    def test_reload_config_and_resolution_changed_are_noop(self, mock_client: MagicMock) -> None:
        src = ImouseFrameSource(client=mock_client)
        # 这两个方法必须存在且不抛、不改状态——业务层主循环每帧会调
        assert src.reload_config() is None
        assert src.resolution_changed() is False
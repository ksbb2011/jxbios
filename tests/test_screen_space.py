"""坐标空间单元测试 —— 不依赖 iMouse 服务，可直接跑。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.vision.screen_space import ScreenSpace


def _dev(w: int, h: int, imgw: int = 0, imgh: int = 0) -> SimpleNamespace:
    return SimpleNamespace(width=w, height=h, imgw=imgw, imgh=imgh)


class TestScreenSpace:
    """专业版 iPhone X 实测截图 375x812 == 逻辑点 → ratio=1.0，无换算。"""

    def test_ratio_one_when_pixel_equals_logical(self) -> None:
        # 注：本机实测 iMouse Pro /device/get 报 imgw=376 而实际截图=375，
        # ±1px 偏差对业务无可见影响，但要"理想 ratio=1.0" 测试得用 imgw=375。
        sp = ScreenSpace.from_device(_dev(375, 812, 375, 812))
        assert sp.pixel_equals_logical is True
        assert sp.scale_x == pytest.approx(1.0)
        assert sp.scale_y == pytest.approx(1.0)
        assert sp.img_to_logical(100, 200) == (100.0, 200.0)
        u, v = sp.logical_to_norm(375, 812)
        assert u == pytest.approx(1.0)
        assert v == pytest.approx(1.0)
        lx, ly = sp.norm_to_logical(0.5, 0.25)
        assert (lx, ly) == (187.5, 203.0)

    def test_imouse_reported_one_px_offset(self) -> None:
        """记录 iMouse Pro /device/get 自报 imgw=376 与实际截图 375 的 ±1px 偏差。

        实测：截图实际像素 375x812，设备自报 376x812，差 1 像素不影响业务。
        上层若需"理论精确"，应在拿到首帧实际尺寸后重建 ScreenSpace
        （见 ImouseFrameSource.scale_to_logical）。
        """
        sp = ScreenSpace.from_device(_dev(375, 812, 376, 812))
        assert sp.pixel_equals_logical is False  # 设备自报与逻辑点差 1
        assert abs(sp.scale_x - 1.0) < 0.01  # 偏差 < 1%
        assert sp.scale_y == pytest.approx(1.0)
        # 1 像素的偏差：如果按自报 imgw=376 算，100px 映射到 99.468 逻辑点
        lx, _ = sp.img_to_logical(100, 0)
        assert abs(lx - 100) < 1

    def test_xp_style_double_pixels(self) -> None:
        """XP 版截图是 2 倍图（375x667 逻辑 → 750x1334 像素）→ 应换算。"""
        sp = ScreenSpace.from_device(_dev(375, 667, 750, 1334))
        assert sp.pixel_equals_logical is False
        assert sp.scale_x == pytest.approx(2.0)
        assert sp.scale_y == pytest.approx(2.0)
        # 截图像素 (600, 200) → 逻辑点 (300, 100)
        assert sp.img_to_logical(600, 200) == (300.0, 100.0)

    def test_from_device_with_zero_declared_img(self) -> None:
        """若 imgw/imgh 为 0（旧接口/未报），降级用 width/height。"""
        sp = ScreenSpace.from_device(_dev(375, 812, 0, 0))
        assert sp.scale_x == 1.0
        assert sp.scale_y == 1.0
        assert sp.pixel_equals_logical is True

    def test_zero_logical_falls_back_safely(self) -> None:
        """极端：width/height 都没拿到 → scale=1.0 不崩。"""
        sp = ScreenSpace.from_device(_dev(0, 0, 0, 0))
        assert sp.scale_x == 1.0
        assert sp.scale_y == 1.0
        # 此时 img_to_logical 等于恒等映射（不报错），上层应自行判断
        assert sp.img_to_logical(100, 200) == (100.0, 200.0)

    def test_describe_contains_key_facts(self) -> None:
        sp = ScreenSpace.from_device(_dev(375, 812, 375, 812))
        d = sp.describe()
        assert "375x812" in d
        assert "ratio=(1.000,1.000)" in d
        assert "equal=True" in d
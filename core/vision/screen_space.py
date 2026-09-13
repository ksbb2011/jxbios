"""坐标空间统一层。

**全系统唯一对外坐标空间** = 手机逻辑点（来自 `/device/get` 的 `width/height`）。

为什么选它：
    * 与设备绑定，不随投屏分辨率设置（air_ratio）、截图是否 original、jpg 压缩变化
    * `imgw/imgh` 会随投屏配置浮动（专业版 ≈ 逻辑宽，XP 版 ≈ 2 倍），不能作基准
    * 屏幕点与 iOS 触摸坐标 / UI 自动化坐标体系天然一致

实测（本机 2026-09-09 专业版 iPhone X）：
    截图实际像素 375x812 == width x height，**ratio = 1.0**。
    → 现在不需要换算。但保留此模块作为未来接口（换设备/换采集卡时仍有意义）。

使用：
    sp = ScreenSpace.from_device(device)
    # 截图像素 → 逻辑点
    lx, ly = sp.img_to_logical(px, py)
    # 逻辑点 → 归一化 u,v（用于模板 ROI / 双线性标定）
    u, v = sp.logical_to_norm(lx, ly)
"""

from __future__ import annotations

from dataclasses import dataclass

from core.devices.imouse_client import DeviceInfo


@dataclass(frozen=True)
class ScreenSpace:
    """唯一坐标空间换算器。"""

    logical_w: int
    logical_h: int
    img_w: int
    img_h: int

    @property
    def scale_x(self) -> float:
        """截图像素 → 逻辑点的 X 缩放（应=1.0）。"""
        return self.img_w / self.logical_w if self.logical_w else 1.0

    @property
    def scale_y(self) -> float:
        """截图像素 → 逻辑点的 Y 缩放（应=1.0）。"""
        return self.img_h / self.logical_h if self.logical_h else 1.0

    @property
    def pixel_equals_logical(self) -> bool:
        return self.img_w == self.logical_w and self.img_h == self.logical_h

    def img_to_logical(self, x: float, y: float) -> tuple[float, float]:
        """截图像素坐标 → 逻辑点坐标。ratio=1 时直通。"""
        return (x / self.scale_x, y / self.scale_y)

    def logical_to_norm(self, x: float, y: float) -> tuple[float, float]:
        """逻辑点 → 归一化 u,v ∈ [0, 1]（用于模板 ROI 与双线性标定）。"""
        return (x / self.logical_w, y / self.logical_h)

    def norm_to_logical(self, u: float, v: float) -> tuple[float, float]:
        return (u * self.logical_w, v * self.logical_h)

    @classmethod
    def from_device(cls, dev: DeviceInfo) -> "ScreenSpace":
        """从 iMouse /device/get 构造。

        img_w/img_h 优先使用设备自报；若设备未报（width/height=0）则假设 1:1。
        """
        return cls(
            logical_w=int(dev.width or 0),
            logical_h=int(dev.height or 0),
            img_w=int(dev.imgw or dev.width or 0),
            img_h=int(dev.imgh or dev.height or 0),
        )

    def describe(self) -> str:
        return (f"ScreenSpace(logical={self.logical_w}x{self.logical_h}, "
                f"img={self.img_w}x{self.img_h}, "
                f"ratio=({self.scale_x:.3f},{self.scale_y:.3f}), "
                f"equal={self.pixel_equals_logical})")
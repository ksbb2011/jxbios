"""取帧源抽象：把「画面从哪来」和「画面怎么用」彻底解耦。

为什么要这一层（2026-09-04 定）：
    当前只有摄像头一种取帧方式，但已确定将来要加 **HDMI 采集卡**
    （iPhone X --Lightning--> Apple Digital AV Adapter --HDMI--> 采集卡 --USB--> PC）。
    采集卡在 Windows 上就是个 UVC 设备，画面质量是量级提升（零反光、零畸变、
    分辨率约 499x1080 vs 现在 480x640），但会带来两个新需求：

        1. 画面是 1080p 横屏信号 + 两侧大片黑边 → 需要**黑边裁剪**
        2. 采集链路有 100~200ms 延迟 → 点击后要**多等一会**再判定

    如果现在就把 camera 写死在 RobotTask / ActionKit / FlowContext 里，
    将来接采集卡要改十几个调用点；抽出这一层后只需：
        新增一个实现类 + 改一行配置（vision.capture.source）
    ——这就是"现在做改动量最小"的全部理由。

设计选择：
    用 Protocol（结构化类型）而不是 ABC（名义类型），Camera 无需改继承关系，
    只要方法签名匹配就自动满足接口 → 对现有 35 个单测零破坏。

取帧源矩阵（供决策时对照）：
    | 方式           | 画质         | 痕迹 | 状态     |
    |----------------|--------------|------|----------|
    | 摄像头         | 有反光/畸变  | 零   | 降级分支 |
    | HDMI 采集卡    | 像素级清晰   | 极低 | 未启用   |
    | iMouse 投屏    | 像素级清晰   | 高   | **当前在用** |
    | ADB / WDA      | -            | 高   | **禁用** |
    | 投屏 / 镜像    | -            | 高   | **禁用** |

    新项目（2026-09 起）的取帧源是 **iMouse 投屏**：
    用 AirPlay 镜像取手机原生分辨率帧（专业版实测 375x812 == 逻辑点，
    ratio=1.0，无需换算层）。**反风控风险留痕**：iMouse 与源项目曾标注的
    「投屏/镜像=禁用」属同一族手段，App 侧可能识别——本项目接受该风险，
    摄像头保留为降级分支（详见 docs/方案_标定体系重设计.md 第②层）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

import cv2
import numpy as np


class FrameSourceError(Exception):
    """取帧源不可用（摄像头掉线 / 采集卡无信号）。"""

    @property
    def hint(self) -> str:
        return ""


@dataclass
class FrameSet:
    """一次采集的两路产物。

    raw  —— 采集分辨率（用于 OCR 与截图留证）
    fast —— 480x640 快帧（用于模板匹配、页面判定、快筛）

    双流的原因见 camera.py 文件头：判定类任务吃速度、识别类任务吃分辨率，
    一次采集两次使用，避免"提高分辨率就得重采全部模板"的坑。
    """

    raw: np.ndarray
    fast: np.ndarray
    ts: float

    @property
    def width(self) -> int:
        return int(self.fast.shape[1])

    @property
    def height(self) -> int:
        return int(self.fast.shape[0])


@runtime_checkable
class FrameSource(Protocol):
    """取帧源统一接口。camera 与将来的 capture_card 必须完全等价。

    业务层（RobotTask / ActionKit / FlowContext）只依赖这个协议，
    不关心画面来自摄像头还是采集卡。
    """

    def open(self) -> bool:
        """打开设备。返回是否成功。"""
        ...

    def close(self) -> None:
        """关闭设备。"""
        ...

    @property
    def is_opened(self) -> bool:
        """设备是否已打开。"""
        ...

    def read(self) -> Optional[FrameSet]:
        """取一帧（raw + fast 双流）。失败返回 None。"""
        ...

    def reload_config(self) -> None:
        """配置热重载后调用。"""
        ...

    def resolution_changed(self) -> bool:
        """配置里的分辨率是否已变化（需要重建采集流）。

        摄像头换分辨率必须重新 open；采集卡由手机输出决定、程序改不了，恒 False。
        """
        ...


# ---------------------------------------------------------------- 画面后处理

def crop_letterbox(img: np.ndarray, threshold: int = 12) -> np.ndarray:
    """裁掉四周纯黑边。

    为什么需要：iPhone X 是 1125x2436（9:19.5），经 AV Adapter 输出的是
    1080p 横屏信号，竖屏画面居中显示、两侧各留约 710px 纯黑边。
    不裁掉的话：① 归一化 ROI 全错（screen_roi 是按有效区标的）；
               ② 模板匹配在黑边上会产生大量低分噪声。

    用亮度阈值找非黑区域的外接矩形，比写死裁剪比例可靠（换机型/换采集卡都不用改）。
    全黑帧（设备无信号）原样返回，交给上层判定为取帧失败。
    """
    if img is None or img.size == 0:
        return img
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = gray > int(threshold)
    if not bool(mask.any()):
        return img
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return img
    return img[int(rows[0]): int(rows[-1]) + 1, int(cols[0]): int(cols[-1]) + 1]


def resize_keep_aspect(img: np.ndarray, size: tuple, interp: int = cv2.INTER_AREA) -> np.ndarray:
    """缩放到目标尺寸。尺寸已一致时直接返回（省一次拷贝）。"""
    if img is None or img.size == 0:
        return img
    w, h = int(size[0]), int(size[1])
    if (img.shape[1], img.shape[0]) == (w, h):
        return img
    return cv2.resize(img, (w, h), interpolation=interp)


def create_frame_source(store=None, log=None) -> FrameSource:
    """按配置创建取帧源。新增取帧方式时只改这里。

    vision.capture.source:
        camera        摄像头（**降级分支**，旧项目沿用）
        capture_card  HDMI 采集卡（未启用，需要 letterbox=auto）
        imouse        **当前默认** —— iMouse 投屏，需 imouse.host
        其它值        显式报错（绝不静默回退到 camera——会导致标定错配）

    store=None 仅在 imouse 路径下允许：标定工具 / 单测可不依赖 ConfigStore。
    """
    # ---- 读配置（store 可能为 None，仅 imouse 路径允许）
    source = "imouse"  # 新项目默认走投屏（见矩阵）
    if store is not None:
        try:
            source = str(store.get("vision", "capture.source", "imouse") or "imouse").lower()
        except Exception:
            source = "imouse"

    #---- 投屏（默认）
    if source == "imouse":
        from core.devices.imouse_source import ImouseFrameSource
        host = "127.0.0.1"
        fast_size = (375, 812)
        if store is not None:
            try:
                host = str(store.get("imouse", "host", "127.0.0.1") or "127.0.0.1")
                fs = store.get("vision", "capture.fast_size", (375, 812))
                fast_size = (int(fs[0]), int(fs[1]))
            except Exception:
                pass
        return ImouseFrameSource(store=store, log=log, fast_size=fast_size, host=host)

    #---- 摄像头 / 采集卡（降级分支）—— 需要 ConfigStore，未知 source 必须先抛错
    if source not in ("camera", "capture_card"):
        raise FrameSourceError(
            f"未知取帧源 vision.capture.source={source!r}"
            f"（可选 camera / capture_card / imouse）"
        )
    if store is None:
        raise FrameSourceError(f"{source} 模式需要 ConfigStore 配置")
    from core.devices.camera import Camera

    if source == "capture_card":
        # 采集卡与摄像头同为 UVC 设备，仅分辨率、黑边、延迟三项不同，
        # 因此复用 Camera 实现、由配置区分行为即可，不必单独写一个类。
        return Camera(store, log=log)
    return Camera(store, log=log)

"""固化坐标点击框架（ActionKit）。

设计目标（用户 2026-09-09 需求）：
    * 列表页等固定 UI 元素的坐标固化为「手机逻辑点」(iPhone X 375x812 归一化中心)；
    * 点击叠加小幅随机偏移（arm.tap 的 jitter，默认 hardware.click.jitter_px=4），
      避免每次命中同一像素、增加拟人度；
    * 点击后做**反馈检测**（verify 回调）：若预期效果未出现，重试若干次；
    * 重试仍失败 → 拉起图色（TemplateMatcher）**重新定位**该控件 → 更新固化坐标（自学习）；
    * 仍失败 → 返回 False，由上层决定降级（如走通用返回键）。

⚠️ 不固化的部分（关键，避免纰漏）：
    * 列表内动态卡片：随滚动变化，必须逐卡片动态定位（用对应模板 find），
      不写入固化表；ActionKit 提供 click_norm() 直接点归一化坐标供动态场景。
    * 坐标体系永远是「手机逻辑点」，绝不参与机械臂 arm_range 运算（见 arm.py 纪律）。

与现有层的关系：
    * 执行侧复用 core/devices/arm.RobotArm（已含双线性标定映射、jitter、越界保护）；
    * 图色兜底复用 core/vision/matcher.TemplateMatcher；
    * 坐标表存 config/coords.json（与 hardware.json 解耦，独立读写）。
"""
from __future__ import annotations

import json
import threading
from typing import Callable, Optional, Tuple

import numpy as np

from core.config_store import ConfigStore
from core.devices.arm import RobotArm
from core.vision.matcher import TemplateMatcher

LogFn = Optional[Callable[[str, str], None]]
FrameFn = Callable[[], Optional[np.ndarray]]          # 取当前帧
VerifyFn = Callable[[Optional[np.ndarray]], bool]     # 反馈检测：返回点击是否生效


class ActionKit:
    """固化坐标点击器：固化坐标 + 随机偏移 + 反馈检测 + 图色兜底 + 自学习。"""

    def __init__(self, store: ConfigStore, arm: RobotArm, matcher: TemplateMatcher,
                 log: LogFn = None) -> None:
        self._store = store
        self._arm = arm
        self._matcher = matcher
        self._log = log
        self._lock = threading.RLock()
        self._coords = self._load()

    # ---------------------------------------------------------------- 配置
    def _coords_path(self):
        return self._store.root / "config" / "coords.json"

    def _load(self) -> dict:
        p = self._coords_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {"controls": {}}
        return {"controls": {}}

    def _save(self) -> None:
        try:
            self._coords_path().write_text(
                json.dumps(self._coords, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self._emit("warning", f"固化坐标表保存失败: {exc}")

    def _emit(self, level: str, msg: str) -> None:
        if self._log:
            self._log(level, msg)

    # ---------------------------------------------------------------- 登记
    def register(self, name: str, norm: Tuple[float, float], template: str = "",
                 desc: str = "") -> None:
        """注册/更新一个固化控件（norm 为归一化中心坐标 [x,y]）。"""
        with self._lock:
            self._coords.setdefault("controls", {})[name] = {
                "template": template,
                "norm": [float(norm[0]), float(norm[1])],
                "desc": desc,
            }
            self._save()

    def control(self, name: str) -> Optional[dict]:
        return self._coords.get("controls", {}).get(name)

    def controls(self) -> dict:
        return dict(self._coords.get("controls", {}))

    # ---------------------------------------------------------------- 点击
    def click(self, name: str, *, get_frame: Optional[FrameFn] = None,
              verify: Optional[VerifyFn] = None, max_retry: int = 3,
              jitter: Optional[int] = None, z: Optional[float] = None) -> bool:
        """点固化控件，带反馈检测与图色兜底重定位。

        verify 为 None 时退化为「直接点固化坐标」（不检测生效，最快路径）；
        提供 verify 时：点击 → 检测 → 未生效重试 → 图色兜底重定位 → 再检测。
        """
        ctrl = self.control(name)
        if ctrl is None:
            self._emit("error", f"固化控件不存在: {name}（先用 register 登记）")
            return False
        norm = ctrl.get("norm")
        if not norm:
            self._emit("error", f"固化控件 {name} 无坐标")
            return False
        lw, lh = self._store.logical_size

        # 第 1 级：图色（模板）定位优先 —— 用户铁律：先用图色正常流程判断
        reloc = self._relocate(name, ctrl, get_frame)
        if reloc is not None:
            if self._tap(reloc[0], reloc[1], jitter, z) and \
               (verify is None or self._check(verify, get_frame)):
                return True
            self._emit("warning", f"{name} 图色定位点击失效，改用固定坐标")

        # 第 2 级：固定坐标点击（用户给的准确坐标兜底）
        base_px, base_py = norm[0] * lw, norm[1] * lh
        if self._tap(base_px, base_py, jitter, z) and \
           (verify is None or self._check(verify, get_frame)):
            return True

        # 第 3 级：都失效 → 暂停提醒重新采集模板（不无限重试）
        self._emit("error", f"{name} 图色定位与固定坐标点击均失效，暂停提醒重新采集模板")
        return False

    def click_norm(self, norm: Tuple[float, float], *, jitter: Optional[int] = None,
                   z: Optional[float] = None) -> bool:
        """直接点归一化坐标（动态场景：列表卡片等不固化元素）。"""
        lw, lh = self._store.logical_size
        return self._tap(norm[0] * lw, norm[1] * lh, jitter, z)

    # ---------------------------------------------------------------- 内部
    def _tap(self, px: float, py: float, jitter, z) -> bool:
        try:
            return bool(self._arm.tap(px, py, z=z, jitter=jitter))
        except Exception as exc:  # noqa: BLE001
            self._emit("warning", f"点击异常 ({px:.0f},{py:.0f}): {exc}")
            return False

    def _check(self, verify: VerifyFn, get_frame: Optional[FrameFn]) -> bool:
        try:
            frame = get_frame() if get_frame else None
            return bool(verify(frame))
        except Exception as exc:  # noqa: BLE001
            self._emit("warning", f"反馈检测异常: {exc}")
            return False

    def _relocate(self, name: str, ctrl: dict,
                  get_frame: Optional[FrameFn]) -> Optional[Tuple[float, float]]:
        """图色兜底：用模板在当前帧重新定位控件中心，成功则自学习更新固化坐标。"""
        tpl_name = ctrl.get("template")
        if not tpl_name or get_frame is None:
            return None
        frame = get_frame()
        if frame is None:
            return None
        spec = self._store.template(tpl_name)
        if spec is None or not spec.enabled:
            return None
        try:
            hit = self._matcher.find(frame, spec)
        except Exception:  # noqa: BLE001
            return None
        if hit is None:
            return None
        lw, lh = self._store.logical_size
        new_norm = [hit.cx / lw, hit.cy / lh]
        with self._lock:
            self._coords.setdefault("controls", {})[name] = {**ctrl, "norm": new_norm}
            self._save()
        self._emit("info", f"{name} 图色重定位 -> 像素({hit.cx:.0f},{hit.cy:.0f})，已更新固化坐标")
        return float(hit.cx), float(hit.cy)

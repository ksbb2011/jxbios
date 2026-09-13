"""机械臂 HTTP WCF 控制 —— 逻辑点坐标↔机械臂坐标换算的唯一出口。

协议（已逆向验证）：
    GET {url}?duankou={COM}&hco=0&daima=0        打开端口，返回资源号（>1 成功，0 失败）
    GET {url}?duankou=0&hco={id}&daima=X{x}Y{y}  移动
    GET {url}?duankou=0&hco={id}&daima=Z{z}      下压
    GET {url}?duankou=0&hco={id}&daima=Z0        抬起
    GET {url}?duankou=0&hco={id}&daima=X0Y0Z0    复位
    GET {url}?duankou=0&hco={id}&daima=0         关闭端口

纪律（历史上为此栽过两次）：
    * 业务层一律使用「手机逻辑点」坐标（iPhone X = 375x812），禁止拿 arm_range
      去钳制或参与任何像素运算；
    * 像素→机械臂的换算只在本文件实现（actuation 双线性模型），其他模块不得重复实现；
    * 每个动作结束后必须 back_to_home()，否则笔尖会遮挡摄像头。
    * ⛔ Z 安全红线：Z 越大 = 压得越深，硬上限 6.2，超过会点碎屏幕（用户 2026-09-09）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import requests

from core.config_store import ConfigStore
from core.devices import jxb_service

LogFn = Optional[Callable[[str, str], None]]

# 落点允许超出屏幕逻辑点的像素余量，超出即拒绝点击（防误点到屏幕外）
REACHABLE_MARGIN_PX = 30

# ⛔ Z 硬上限（碎屏红线）
Z_HARD_MAX = 6.2


class ArmError(Exception):
    """机械臂不可用或指令失败。"""


@dataclass
class ArmState:
    opened: bool = False
    resource_id: str = ""
    last_error: str = ""
    frame_size: Tuple[int, int] = (0, 0)


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


class RobotArm:
    """机械臂控制器（线程安全，所有指令串行下发）。"""

    def __init__(self, store: ConfigStore, log: LogFn = None) -> None:
        self._store = store
        self._log = log
        self._lock = threading.RLock()
        self._state = ArmState()
        # HTTP keep-alive：滑动时每秒要下发几十条轨迹点，逐条新建 TCP 连接
        # 的握手 + 延迟抖动（几 ms~几十 ms 不等）会直接变成「一顿一顿」的手感。
        # 复用一条连接后 RTT 稳定，轨迹才跟得上（连接坏了自动重建）。
        self._session: Optional[requests.Session] = requests.Session()
        self._load_calibration()

    # ---------------------------------------------------------------- 配置

    def _load_calibration(self) -> None:
        # calibration 在 hardware.json 的 hardware.calibration 下，不是顶层 section。
        # 曾误写成 section("calibration")（读的是不存在的 config/calibration.json），
        # 结果是 actuation 模型静默退化成默认系数、所有点击整体偏移——最难排查的事故。
        cal = self._store.get("hardware", "calibration", {}) or {}
        act = cal.get("actuation") or {}
        self._ax = [float(x) for x in act.get("ax", [0.0, 1.0, 0.0, 0.0])]
        self._ay = [float(x) for x in act.get("ay", [0.0, 0.0, 1.0, 0.0])]
        self._z_press = float(cal.get("z_press", 5.9))
        self._z_release = float(cal.get("z_release", 0.0))
        self._rest_pos = cal.get("rest_pos")
        arm = cal.get("arm_range") or {}
        self._arm_x = tuple(arm.get("x", (0.0, 1.0)))
        self._arm_y = tuple(arm.get("y", (0.0, 1.0)))
        lw, lh = self._store.logical_size
        self._lw, self._lh = int(lw), int(lh)
        self._tap_corr = self._load_tap_correction()

    # ------------------------------------------- 点击残差补偿（2026-09-13 新增）
    # 为什么需要：墨点标定的画布只覆盖 y≈170~660（备忘录上下被按钮栏遮挡，留不下墨点），
    # 屏幕底部那排按钮（清空筛选/确认/返回顶部/全国货源）只能靠模型外推，实测**统一偏左
    # 6~7pt** —— 于是"期望点一次点击"点不动，而校准工具因为会扫点所以看着"能点到"。
    # 数据来源：tools/calibrate_tap_ui.py 的实测（期望点不生效、偏移后即生效），
    # 由 `校准工具.exe tap --apply` 写进 hardware.json::calibration.tap_correction。
    # 设计取舍：只认"实测锚点 + 半径内最近者"，未测区域一律不补（宁可不补也不猜）。
    _TAP_CORR_BASE_SIZE = (375, 812)   # 锚点坐标是绝对逻辑点，换机型/改分辨率会整体错位

    def _load_tap_correction(self) -> dict:
        """读 calibration.tap_correction。**防御式解析**：任何异常都退化为"不补偿"。

        为什么必须防：这里是每一次点击的必经之路，解析一旦抛异常 = 全流程点击全挂；
        而 ConfigStore 的严格校验只覆盖 actuation / z_press，管不到这一段。
        """
        off = {"enabled": False, "radius": 40.0, "points": [], "note": ""}
        try:
            raw = self._store.get("hardware", "calibration.tap_correction", {}) or {}
            if not isinstance(raw, dict) or not bool(raw.get("enabled", False)):
                return off
            if (self._lw, self._lh) != self._TAP_CORR_BASE_SIZE:
                # fail-safe：机型/分辨率变了就停用，绝不"静默点歪"
                return {**off, "note": f"logical_size={self._lw}x{self._lh} 与锚点基准不符，已停用"}
            radius = float(raw.get("radius_px", 40) or 40)
            pts = []
            for item in (raw.get("points") or []):
                try:
                    if not isinstance(item, dict):
                        continue
                    pts.append({
                        "name": str(item.get("name") or "?"),
                        "x": float(item["x"]),
                        "y": float(item["y"]),
                        "dx": float(item.get("dx", 0) or 0),
                        "dy": float(item.get("dy", 0) or 0),
                    })
                except Exception:  # noqa: BLE001 - 单条坏项跳过，不影响其余
                    continue
            if not pts:
                return off
            self._emit("info", f"点击残差补偿已启用：{len(pts)} 个锚点，半径 {max(0.0, radius):.0f}pt")
            return {"enabled": True, "radius": max(0.0, radius), "points": pts, "note": ""}
        except Exception as exc:  # noqa: BLE001 - 兜底：坏配置不允许影响点击
            return {**off, "note": f"解析失败({type(exc).__name__})，已停用"}

    def _tap_correction(self, px: float, py: float) -> Tuple[float, float, str]:
        """按"半径内最近锚点"返回 (dx, dy, 锚点名)；未命中返回 (0,0,"")。"""
        corr = getattr(self, "_tap_corr", None) or {}
        pts = corr.get("points") or []
        if not pts:
            return 0.0, 0.0, ""
        r = float(corr.get("radius") or 0.0)
        best = None
        best_d2 = None
        for p in pts:
            d2 = (float(px) - p["x"]) ** 2 + (float(py) - p["y"]) ** 2
            if d2 <= r * r and (best_d2 is None or d2 < best_d2):
                best, best_d2 = p, d2
        if best is None:
            return 0.0, 0.0, ""
        return best["dx"], best["dy"], best["name"]

    def reload_config(self) -> None:
        with self._lock:
            self._load_calibration()

    @property
    def screen_roi(self) -> Tuple[int, int, int, int]:
        # imouse 全屏即逻辑点整屏，ROI 即 (0,0,lw,lh)
        return (0, 0, self._lw, self._lh)

    def set_frame_size(self, width: int, height: int) -> None:
        """登记实际帧尺寸（兼容 flow 的初始化调用）。

        源项目用它把 screen_roi_ratio 按帧尺寸换算成像素 ROI（摄像头有黑边和
        缩放，必须换算）。投屏版画面即手机逻辑点整屏，坐标换算走 actuation
        模型、不依赖帧尺寸，所以这里只登记并做一致性校验：

        帧尺寸与逻辑点分辨率不一致，说明 vision.capture.fast_size 或取帧源配错，
        会让所有点击整体偏移（模板 ROI 也随之错位），必须显式告警而不是静默。
        """
        with self._lock:
            self._state.frame_size = (int(width), int(height))
        if (int(width), int(height)) != (self._lw, self._lh):
            self._emit(
                "warning",
                f"帧尺寸 ({width}x{height}) 与逻辑点 ({self._lw}x{self._lh}) 不一致："
                f"投屏版坐标换算以逻辑点为唯一基准，请核对 vision.capture.fast_size "
                f"与取帧源输出的帧尺寸，否则点击会整体偏移",
            )

    # ---------------------------------------------------------------- 连接

    def _emit(self, level: str, msg: str) -> None:
        if self._log:
            self._log(level, msg)

    def _url(self) -> str:
        return str(self._store.get("hardware", "robot.url"))

    def _com(self) -> str:
        return str(self._store.get("hardware", "robot.com", "COM4"))

    def _delay(self, key: str) -> float:
        return float(self._store.get("hardware", f"delays.{key}", 0.0) or 0.0)

    def _http_get(self, params: dict, timeout: float):
        if self._session is None:
            self._session = requests.Session()
        try:
            return self._session.get(self._url(), params=params, timeout=timeout)
        except requests.RequestException:
            # 连接多半已被服务端断开（长连接闲置超时）。换一条新连接重试一次，
            # 仍失败才抛给上层——临时网络抖动不该让整轮跑批中断。
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = requests.Session()
            return requests.get(self._url(), params=params, timeout=timeout)

    def _send(self, duankou: str, hco: str, daima: str, timeout: float = 5.0) -> str:
        params = {"duankou": duankou, "hco": hco, "daima": daima}
        resp = self._http_get(params, timeout)
        resp.raise_for_status()
        return (resp.text or "").strip()

    def open(self, restart_service: bool = True) -> bool:
        """打开串口。返回 True 表示拿到有效资源号。"""
        with self._lock:
            if self._state.opened:
                return True

            if restart_service and self._store.get("hardware", "service.restart_before_open", True):
                name = str(self._store.get("hardware", "service.process_name", "JxbService"))
                jxb_service.restart_service(name, log=self._log)

            retry = int(self._store.get("hardware", "robot.open_retry", 3) or 3)
            wait = float(self._store.get("hardware", "robot.open_wait_sec", 2.0) or 2.0)
            last_err = ""
            for i in range(1, retry + 1):
                try:
                    # WCF 服务返回的资源号带双引号，必须剥掉，否则 hco 会变成
                    # %221568%22，服务端一律 400，表现为「连上了但所有动作失败」。
                    rid = self._send(self._com(), "0", "0").strip().strip('"').strip("'")
                except requests.RequestException as exc:
                    last_err = f"HTTP 请求失败: {exc}"
                    self._emit("warning", f"打开串口失败({i}/{retry}): {last_err}")
                    time.sleep(wait)
                    continue
                if rid and rid != "0":
                    # 拿到资源号不等于能通信：发一条无害的复位指令验证。
                    try:
                        self._send("0", rid, "X0Y0Z0")
                    except requests.RequestException as exc:
                        last_err = f"资源号 {rid} 通信验证失败: {exc}"
                        self._emit("warning", f"机械臂通信验证失败({i}/{retry}): {last_err}")
                        time.sleep(wait)
                        continue
                    self._state.opened = True
                    self._state.resource_id = rid
                    self._state.last_error = ""
                    time.sleep(wait)  # 打开后必须等机械臂就绪
                    self._emit("info", f"机械臂已连接并验证通信正常: {self._com()} 资源号={rid}")
                    return True
                last_err = f"资源号为 0（串口可能被占用，需重启 {self._com()} 所属服务）"
                self._emit("warning", f"打开串口失败({i}/{retry}): {last_err}")
                time.sleep(wait)

            self._state.last_error = last_err
            self._emit("error", f"机械臂连接失败: {last_err}")
            return False

    def close(self) -> None:
        with self._lock:
            if not self._state.opened:
                return
            try:
                self._send("0", self._state.resource_id, "X0Y0Z0")
                time.sleep(self._delay("reset"))
                self._send("0", self._state.resource_id, "0")
            except requests.RequestException as exc:
                self._emit("warning", f"关闭串口异常: {exc}")
            finally:
                if self._session is not None:
                    try:
                        self._session.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._session = None
                self._state.opened = False
                self._state.resource_id = ""
                self._emit("info", "机械臂已断开")

    def recover(self) -> bool:
        """运行中掉线后的自动恢复：重连 → 重启服务 → 复位。"""
        with self._lock:
            self._state.opened = False
            self._state.resource_id = ""
            if self.open(restart_service=True):
                self.reset()
                return True
            return False

    @property
    def is_open(self) -> bool:
        return self._state.opened

    @property
    def state(self) -> ArmState:
        return self._state

    # ---------------------------------------------------------------- 动作

    def _require_open(self) -> None:
        if not self._state.opened:
            raise ArmError("机械臂未连接（请先调用 open()）")

    def move(self, arm_x: float, arm_y: float) -> None:
        with self._lock:
            self._require_open()
            x0, x1 = self._arm_x
            y0, y1 = self._arm_y
            ax = _clamp(float(arm_x), min(x0, x1), max(x0, x1))
            ay = _clamp(float(arm_y), min(y0, y1), max(y0, y1))
            self._send("0", self._state.resource_id, f"X{ax:.2f}Y{ay:.2f}")
            time.sleep(self._delay("move"))

    def move_continuous(self, arm_x: float, arm_y: float) -> None:
        """滑动专用：下发 move 指令且几乎不等待（delay≈0），让机械臂连续跟随
        轨迹点平滑划过，避免逐点停顿造成的「搓屏」感（与旧工程 robot_arm.py
        的 move_continuous 一致）。连续密集下发依赖串行锁保证不丢指令；单次
        离散定位仍用 move()。"""
        with self._lock:
            self._require_open()
            x0, x1 = self._arm_x
            y0, y1 = self._arm_y
            ax = _clamp(float(arm_x), min(x0, x1), max(x0, x1))
            ay = _clamp(float(arm_y), min(y0, y1), max(y0, y1))
            self._send("0", self._state.resource_id, f"X{ax:.2f}Y{ay:.2f}")

    def press(self, z: Optional[float] = None) -> None:
        with self._lock:
            self._require_open()
            depth = self._z_press if z is None else float(z)
            # ⛔ Z 安全红线：Z 越大压得越深，硬上限 6.2，超过点碎屏幕
            depth = min(float(depth), Z_HARD_MAX)
            self._send("0", self._state.resource_id, f"Z{depth:.2f}")
            time.sleep(self._delay("press"))

    def release(self) -> None:
        with self._lock:
            self._require_open()
            self._send("0", self._state.resource_id, f"Z{self._z_release:.2f}")
            time.sleep(self._delay("release"))

    def reset(self) -> None:
        with self._lock:
            self._require_open()
            self._send("0", self._state.resource_id, "X0Y0Z0")
            time.sleep(self._delay("reset"))

    def back_to_home(self) -> None:
        """归位，避免笔尖遮挡摄像头。"""
        with self._lock:
            if not self._state.opened:
                return
            rest = self._rest_pos
            try:
                if isinstance(rest, list) and len(rest) == 2:
                    self.move(float(rest[0]), float(rest[1]))
                else:
                    self.reset()
            except requests.RequestException as exc:
                self._emit("warning", f"归位失败: {exc}")

    def ensure_homed(self) -> bool:
        """确保机械臂已归位（移到 rest_pos）。返回是否成功下发归位指令。"""
        try:
            self.back_to_home()
            return True
        except Exception as exc:  # noqa: BLE001
            self._emit("warning", f"归位失败: {exc}")
            return False

    def press_back_key(self) -> None:
        """点击手机物理返回键（机械臂坐标系中的固定位置）。"""
        key = self._store.get("hardware", "calibration.back_key")
        if isinstance(key, list) and len(key) == 2:
            self.click_arm(float(key[0]), float(key[1]))

    def press_home_key(self) -> None:
        key = self._store.get("hardware", "calibration.home_key")
        if isinstance(key, list) and len(key) == 2:
            self.click_arm(float(key[0]), float(key[1]))

    def click_arm(self, arm_x: float, arm_y: float, z: Optional[float] = None) -> None:
        """直接用机械臂坐标点击（仅限返回键/主页键等固定物理位置）。"""
        with self._lock:
            self.move(arm_x, arm_y)
            time.sleep(self._delay("tap_settle"))   # 等机械臂物理到位稳定（旧工程 click 关键停留）
            self.press(z)
            time.sleep(self._delay("tap_dwell"))    # 触屏驻留，确保电容屏识别为一次点击
            self.release()
            self.back_to_home()

    # ---------------------------------------------------------------- 坐标

    def pixel_to_arm(self, px: float, py: float) -> Tuple[float, float]:
        """手机逻辑点 → 机械臂坐标（actuation 双线性模型，含 u*v 交叉项）。

        u = px / logical_w,  v = py / logical_h
        ax = a0 + a1*u + a2*v + a3*u*v
        ay = b0 + b1*u + b2*v + b3*u*v
        模型来自墨水点标定（calibrate_screen_arm.py），验收误差 ≤3pt。
        """
        u = _clamp(float(px) / self._lw, 0.0, 1.0)
        v = _clamp(float(py) / self._lh, 0.0, 1.0)
        ax = self._ax[0] + self._ax[1] * u + self._ax[2] * v + self._ax[3] * u * v
        ay = self._ay[0] + self._ay[1] * u + self._ay[2] * v + self._ay[3] * u * v
        return ax, ay

    def arm_to_pixel(self, arm_x: float, arm_y: float) -> Tuple[int, int]:
        """机械臂坐标 → 逻辑点（数值逆解，调试可视化用）。"""
        a0, a1, a2, a3 = self._ax
        b0, b1, b2, b3 = self._ay
        u = _clamp((arm_x - a0) / (a1 or 1.0), 0.0, 1.0)
        v = _clamp((arm_y - b0) / (b2 or 1.0), 0.0, 1.0)
        for _ in range(6):  # 牛顿迭代消去 u*v 交叉项
            eu = arm_x - (a0 + a1 * u + a2 * v + a3 * u * v)
            ev = arm_y - (b0 + b1 * u + b2 * v + b3 * u * v)
            du = a1 + a3 * v
            dv = b2 + b3 * u
            if abs(du) > 1e-6:
                u = _clamp(u + eu / du, 0.0, 1.0)
            if abs(dv) > 1e-6:
                v = _clamp(v + ev / dv, 0.0, 1.0)
        return int(round(u * self._lw)), int(round(v * self._lh))

    def point_reachable(self, px: float, py: float, margin: int = REACHABLE_MARGIN_PX) -> bool:
        """点击点是否落在屏幕逻辑点范围内，超出即为误点风险。"""
        return (-margin <= px <= self._lw + margin) and (-margin <= py <= self._lh + margin)

    def click_pixel(self, px: float, py: float, z: Optional[float] = None, jitter: int = 0) -> bool:
        """按逻辑点点击。越界直接拒绝，绝不猜测。

        z 用于「加强点击」场景（如底部按钮点不动时 z_press + 0.2），
        不传则用配置值，调用方无需也不应修改全局 z_press。

        2026-09-13 起还会套用 `calibration.tap_correction` 的落点残差补偿（见 _tap_correction）：
        命中的锚点会把点击点平移几 pt，日志同时打印"原始点 → 补偿后点"。
        """
        import random

        # 残差补偿：按**原始意图点**解析锚点（不受 jitter 影响，便于复现与排查）
        ox, oy, anchor = self._tap_correction(float(px), float(py))
        tx = float(px) + ox + (random.randint(-jitter, jitter) if jitter else 0)
        ty = float(py) + oy + (random.randint(-jitter, jitter) if jitter else 0)
        if not self.point_reachable(tx, ty) and (ox or oy):
            # 补偿把合法点推到了护栏外 → 退回未补偿点重判（补偿不该让"能点的点"变成拒绝）
            self._emit("warning",
                       f"补偿[{anchor} {ox:+.0f},{oy:+.0f}]后越界，已回退未补偿点 ({px:.0f},{py:.0f})")
            tx, ty = tx - ox, ty - oy
            anchor = ""
        if not self.point_reachable(tx, ty):
            self._emit("warning", f"点击点越界，已拒绝: ({tx:.0f},{ty:.0f})")
            return False
        with self._lock:
            self._require_open()
            ax, ay = self.pixel_to_arm(tx, ty)
            self.move(ax, ay)
            time.sleep(self._delay("tap_settle"))   # 等机械臂物理到位稳定（旧工程 click 关键停留）
            self.press(z)
            time.sleep(self._delay("tap_dwell"))    # 触屏驻留，确保电容屏识别为一次点击
            self.release()
            self.back_to_home()
        if anchor:
            # 同时打出“原始点 → 补偿后点”：否则会与调用方 actions.py 的原始点日志互相打架
            self._emit("info",
                       f"点击像素 ({px:.0f},{py:.0f}) 补偿[{anchor} {ox:+.0f},{oy:+.0f}]"
                       f" -> ({tx:.0f},{ty:.0f}) 机械臂 ({ax:.2f},{ay:.2f})")
        else:
            self._emit("info", f"点击像素 ({tx:.0f},{ty:.0f}) -> 机械臂 ({ax:.2f},{ay:.2f})")
        return True

    def tap(self, px: float, py: float, z: Optional[float] = None, jitter: Optional[int] = None) -> bool:
        """带随机抖动的点击（固定 UI 坐标用，避免每次命中同一像素）。"""
        j = int(self._store.get("hardware", "click.jitter_px", 4) or 0) if jitter is None else jitter
        return self.click_pixel(px, py, z=z, jitter=j)

    # ---------------------------------------------------------------- 流畅滑动

    def swipe_smooth(self, from_px, to_px, z: Optional[float] = None,
                     steps: int = 14, jitter: int = 0) -> bool:
        """流畅滑动（非卡顿式）：按下 → 连续 move_continuous 轨迹 → 抬起。

        from_px / to_px 为手机逻辑点坐标 (x, y)。
        steps 越大轨迹点越密、越平滑；move_continuous 用 delay≈0 连续下发，
        机械臂连续跟随划过，避免逐点停顿造成的「搓屏」感。

        用途（2026-09-09 用户需求）：列表页顶部隐藏时，在屏幕中部短滑一下
        展开顶部区，露出「找货记录 / 司机课堂 / 听单」。
        """
        import random

        fx, fy = float(from_px[0]), float(from_px[1])
        tx, ty = float(to_px[0]), float(to_px[1])
        if jitter:
            fx += random.randint(-jitter, jitter); fy += random.randint(-jitter, jitter)
            tx += random.randint(-jitter, jitter); ty += random.randint(-jitter, jitter)
        if not (self.point_reachable(fx, fy) and self.point_reachable(tx, ty)):
            self._emit("warning", f"滑动端点越界，已拒绝: ({fx:.0f},{fy:.0f})->({tx:.0f},{ty:.0f})")
            return False
        with self._lock:
            self._require_open()
            ax0, ay0 = self.pixel_to_arm(fx, fy)
            ax1, ay1 = self.pixel_to_arm(tx, ty)
            self.move(ax0, ay0)                        # 先到起点（带 tap_settle 等到位）
            time.sleep(self._delay("tap_settle"))
            self.press(z)                              # 下压（触屏接触）
            time.sleep(self._delay("press"))
            for i in range(1, steps + 1):              # 流畅滑动核心：连续 move_continuous
                t = i / steps
                self.move_continuous(
                    ax0 + (ax1 - ax0) * t,
                    ay0 + (ay1 - ay0) * t,
                )
            self.release()                             # 抬起
            self.back_to_home()                        # 归位，避免笔尖遮挡
        self._emit("info", f"流畅滑动 ({fx:.0f},{fy:.0f})->({tx:.0f},{ty:.0f}) steps={steps}")
        return True

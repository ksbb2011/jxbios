"""机械臂标定共用组件（网页版与墨点版共用，避免两份重复代码）。

================================================================================
🔒 **已固化模块 —— 测试通过，禁止擅自修改**  （用户于 2026-09-09 确认"固化"）

固化依据（真机实测）：
    * 墨点版标定：校准 31 样本 → 验收 **16/16 全中**
    * 精度：mean **0.95pt** / max **1.57pt**
    * 独立复检（随机 seed + 25px 抖动，点与标定完全不重合）：
      16/16 全中，mean **0.93pt** / max **1.74pt**（阈值 3.0pt）
    * 对比源项目旧标定：±4px、边角 9px → 提升一个量级

固化范围（本文件）：
    ArmClient（含 atexit/信号中断保护）· restart_jxb_service · is_arm_port_busy
    Sample/Target · grid_targets · random_targets · fit_bilinear · predict
    coarse_predict · write_actuation · write_z_press
    arm_range_from_model · write_arm_range_from_model（2026-09-13 新增，用户同意）

⚠️ 修改本文件前**必须先征得用户同意**。确需改动时：
    ① 说明改动理由与影响面
    ② 改完必须重跑 `tools\calibrate_arm_ink.py --verify-only`
    ③ 复检 mean/max 仍 ≤3pt 才算通过，否则回滚
    （2026-09-13 的改动属**纯搬移**：把 arm_range 重算从 calibrate_arm_ink.py 迁入本文件，
      让墨点版与整屏网格工具共用；墨点判据/画布校验/参数一律未动，仍需复检。）
================================================================================

包含：
    * ArmClient       机械臂 HTTP:8082 协议封装
    * Target / Sample 靶点与样本数据结构
    * grid_targets    网格靶点生成
    * fit_bilinear    双线性拟合（含 u*v 交叉项）
    * predict         用模型预测机械臂坐标
    * write_actuation 把标定结果写回 hardware.json
    * load_arm_range  从 hardware.json 读粗略的机械臂工作范围（首轮粗估用）
    * arm_range_from_model       按模型算屏幕四角 → 机械臂范围（纯计算，可离线单测）
    * write_arm_range_from_model 把上面算出的范围写回 hardware.json（防"漏同步"事故）

两套标定工具的差异只在"怎么知道机械臂实际戳到哪儿了"：
    网页版 calibrate_screen_arm.py —— 手机浏览器 touch 事件上报
    墨点版 calibrate_arm_ink.py    —— iMouse 截图找墨点（推荐，无需网页/无需加主屏）
其余流程完全一致。
"""

from __future__ import annotations

import atexit
import json
import logging
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import requests

_LOG = logging.getLogger("calib.common")


# ================================================================ 服务重启
def restart_jxb_service(service: str = "JxbService", wait_sec: float = 4.0) -> bool:
    """重启 JxbService 以释放被占用的 COM4 串口。

    为什么需要：进程被强杀（Ctrl+C / 超时 / 崩溃）时不会执行 close_port，
    WCF 服务会一直占着 COM4，下次 open 返回资源号 0，必须重启服务才能恢复。

    重启**需要管理员权限**，所以用 `Start-Process -Verb RunAs` 提权，
    运行时会弹 UAC 让用户确认。失败返回 False（通常就是用户取消了 UAC）。
    """
    ps_cmd = f"Restart-Service -Name {service} -Force"
    try:
        # 内层用 -Verb RunAs 提权；外层 -Wait 等它跑完
        outer = (
            f"Start-Process powershell -Verb RunAs -Wait "
            f"-ArgumentList '-NoProfile','-Command',\"{ps_cmd}\""
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", outer],
                       check=True, timeout=90)
        time.sleep(wait_sec)
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:  # noqa: BLE001  UAC 被取消 / 服务不存在等
        return False


def is_arm_port_busy(arm_url: str, com: str = "COM4", timeout: float = 4.0) -> bool:
    """探测机械臂串口是否被占（open 返回 0 即被占）。"""
    try:
        r = requests.get(arm_url,
                         params={"duankou": com, "hco": 0, "daima": 0},
                         timeout=timeout)
        return int(r.text.strip().strip('"')) <= 0
    except Exception:  # noqa: BLE001
        return True


# ================================================================ 机械臂
# ================================================================ Z 轴安全红线
# ⛔ 硬上限：Z 值越大 = 压得越深，超过此值会把屏幕点碎。
#    用户于 2026-09-09 明确：「z最高6.2，不能超过这个，超出这个，你会把屏幕点碎的」
#
#    历史教训：min_z=5.6（最轻触发深度）时曾按「+上浮1.5mm」算出 z_press=7.1，
#    方向理解反了——z 越大越深，加号等于继续往下压。后又为排查问题试到 8.5，
#    那是直接怼屏。故此处做**代码级钳制**，任何调用路径都不可能突破。
#
#    注意：「上浮 1~3mm」这条旧规范在本机上**放不下**（5.6+1.5=7.1 > 6.2）。
#    可用窗口只有 5.6 ~ 6.2 共 0.6mm，因此实际取 6.0（上下各留余量）。
Z_HARD_MAX: float = 6.2

# 建议工作值：比 min_z 深一点保证稳定触发，同时离硬顶留 0.2mm 余量
Z_PRESS_SAFE: float = 6.0


class ArmClient:
    """机械臂 HTTP:8082 协议封装。

    URL: http://127.0.0.1:8082/MyWcfService/getstring
    命令: ?duankou={COM}&hco={id}&daima={code}
        X{x}Y{y}   移动      Z{z}  下压      Z0   抬起
        X0Y0Z0     复位      0     释放资源

    ⛔ Z 轴方向：**Z 越大 = 压得越深**（远离 = 小 / 贴近 = 大）。
       press() 会把 z 钳制到 Z_HARD_MAX(6.2) 以内，超限时打警告并截断，
       绝不静默放行——碎屏是不可逆损失。
    """

    def __init__(self, url: str, com: str = "COM4", timeout: float = 5.0,
                 z_hard_max: float = Z_HARD_MAX) -> None:
        self.url = url
        self.com = com
        self.timeout = timeout
        self.z_hard_max = float(z_hard_max)
        self._session = requests.Session()
        self._id: int = 0
        self._clamp_warned = False
        self._cleanup_registered = False

    # ---- 退出保护：任何异常退出都要归位 + 释放串口
    def _register_cleanup(self) -> None:
        """注册 atexit 与信号处理器。

        没有这个保护时，标定跑到一半被 Ctrl+C，机械臂会停在原地（可能压着屏幕），
        且串口不释放 → 下次 open 返回 0 → 只能人工重启 JxbService。
        """
        if self._cleanup_registered:
            return
        atexit.register(self.safe_shutdown)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                pass  # 非主线程注册不了信号，靠 atexit 兜底
        self._cleanup_registered = True

    def _on_signal(self, signum, frame) -> None:  # noqa: ANN001
        self.safe_shutdown()
        # 交回默认行为（SIGINT 默认抛 KeyboardInterrupt）
        if signum == signal.SIGINT:
            raise KeyboardInterrupt

    def safe_shutdown(self) -> None:
        """归位 + 释放串口，永不抛异常。"""
        try:
            if self._id:
                self.reset()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    def open(self, auto_restart: bool = False) -> int:
        """打开串口。返回资源号。

        auto_restart=True 时，若发现串口被占（返回 0），会尝试自动重启
        JxbService（**会弹 UAC**，用户取消则失败）。默认 False——避免在
        无人值守时弹窗。
        """
        for attempt in range(2):
            r = self._session.get(self.url,
                                  params={"duankou": self.com, "hco": 0, "daima": 0},
                                  timeout=self.timeout)
            r.raise_for_status()
            # 服务返回的是带引号的 JSON 字符串（实测形如 "1240" 或 "0"），
            # 必须先去引号再转 int，否则 int('"1240"') 直接 ValueError。
            raw = r.text.strip().strip('"').strip()
            try:
                self._id = int(raw)
            except ValueError as exc:
                raise RuntimeError(f"机械臂 open 返回无法解析：{r.text!r}") from exc

            if self._id > 0:
                self._register_cleanup()
                return self._id

            # 返回 0 = 串口被占。最常见原因：上次进程被强杀没执行 close_port，
            # WCF 服务一直占着 COM4。
            if attempt == 0 and auto_restart:
                print("[机械臂] 串口被占，尝试重启 JxbService（会弹 UAC，请确认）...",
                      flush=True)
                if restart_jxb_service():
                    continue
            raise RuntimeError(
                f"机械臂 open 返回 {raw}（资源号非正）——**COM4 串口被占用**。\n"
                f"  解决：管理员 PowerShell 执行 → Restart-Service JxbService -Force\n"
                f"  或让本工具自动重启：ArmClient.open(auto_restart=True)"
            )
        raise RuntimeError("机械臂 open 失败（重启服务后仍返回非正资源号）")

    def _cmd(self, daima: str) -> None:
        r = self._session.get(self.url,
                              params={"duankou": 0, "hco": self._id, "daima": daima},
                              timeout=self.timeout)
        r.raise_for_status()

    def move(self, ax: float, ay: float) -> None:
        self._cmd(f"X{ax}Y{ay}")

    def press(self, z: float) -> None:
        """下压到 z。**z 越大越深**，故硬钳制在 z_hard_max(默认 6.2) 以内。

        超限时不抛异常（抛异常可能导致机械臂停在压屏状态），而是截断到
        上限并打醒目警告——宁可"点不动"，也绝不允许"压碎屏"。
        """
        z = float(z)
        if z > self.z_hard_max:
            if not self._clamp_warned:  # 同一进程只刷一次，避免刷屏
                self._clamp_warned = True
                print(
                    f"\n⛔ [安全钳制] 请求下压 Z={z}，超过硬上限 {self.z_hard_max}！\n"
                    f"   Z 越大 = 压得越深，超限会**点碎屏幕**。已自动截断为 "
                    f"{self.z_hard_max}。\n"
                    f"   请检查调用方：z_press 配置是否为安全值（建议 {Z_PRESS_SAFE}）。\n",
                    flush=True)
            z = self.z_hard_max
        self._cmd(f"Z{z}")

    def release(self) -> None:
        self._cmd("Z0")

    def reset(self) -> None:
        self._cmd("X0Y0Z0")

    def close(self) -> None:
        if not self._id:
            return
        try:
            self._cmd("0")
        except Exception:  # noqa: BLE001
            pass
        self._id = 0


def load_arm_range(hardware_path: Path) -> tuple[tuple[float, float], tuple[float, float]]:
    """从 hardware.json 读 arm_range，作为首轮标定的粗估范围。

    没有配置时退回 iPhone X 的实测经验值（来自源项目 robot_order_picker_new
    的 `hardware.json`）：x[9.93, 73.2]、y[16.62, 154.62]。
    """
    default_x = (9.93, 73.2)
    default_y = (16.62, 154.62)
    if not hardware_path.exists():
        return default_x, default_y
    try:
        cfg = json.loads(hardware_path.read_text(encoding="utf-8"))
        rng = cfg.get("calibration", {}).get("arm_range") or {}
        rx, ry = rng.get("x"), rng.get("y")
        if isinstance(rx, (list, tuple)) and len(rx) == 2:
            default_x = (float(rx[0]), float(rx[1]))
        if isinstance(ry, (list, tuple)) and len(ry) == 2:
            default_y = (float(ry[0]), float(ry[1]))
    except Exception:  # noqa: BLE001
        pass
    return default_x, default_y


# ================================================================ 数据结构
@dataclass
class Target:
    """屏幕逻辑点靶点。u,v ∈ [0,1] 按 width/height 归一化。"""
    tx: float
    ty: float
    u: float = 0.0
    v: float = 0.0


@dataclass
class Sample:
    """一个标定样本。

    target   我们想让它落在屏幕哪里（逻辑点）
    touch_x/y 手机实测落点（逻辑点）；墨点版由截图算出，网页版由 touch 事件上报
    ax/ay    当时下达给机械臂的坐标
    """
    target: Target
    touch_x: float
    touch_y: float
    ax: float
    ay: float


def grid_targets(grid_n: int, width: int, height: int, margin: float = 0.06) -> list[Target]:
    """生成 grid_n × grid_n 的屏幕网格靶点（避开边缘 margin）。"""
    out: list[Target] = []
    coords = np.linspace(margin, 1.0 - margin, grid_n)
    for u in coords:
        for v in coords:
            out.append(Target(tx=float(u * width), ty=float(v * height),
                              u=float(u), v=float(v)))
    return out


def random_targets(n: int, width: int, height: int, margin: float = 0.08,
                   seed: int | None = None, min_dist: float = 0.12) -> list[Target]:
    """生成 n 个随机靶点（带最小间距，避免与网格点重合导致墨点重叠）。"""
    rng = np.random.default_rng(seed)
    out: list[Target] = []
    pts: list[tuple[float, float]] = []
    guard = 0
    while len(out) < n and guard < n * 200:
        guard += 1
        u = float(rng.uniform(margin, 1.0 - margin))
        v = float(rng.uniform(margin, 1.0 - margin))
        if any((u - pu) ** 2 + (v - pv) ** 2 < min_dist ** 2 for pu, pv in pts):
            continue
        pts.append((u, v))
        out.append(Target(tx=u * width, ty=v * height, u=u, v=v))
    return out


def fit_bilinear(samples: list[Sample]) -> dict[str, Any]:
    """拟合双线性 ax = c0 + c1*u + c2*v + c3*u*v（含 u*v 交叉项）。

    输入特征用**实测落点**归一化后的 (u,v)，输出是当时下达的机械臂坐标。
    这样拟合出的模型直接回答："想戳到屏幕 (u,v)，机械臂该给什么 ax,ay"。
    """
    A = np.array([[1.0, s.target.u, s.target.v, s.target.u * s.target.v]
                  for s in samples])
    bx = np.array([s.ax for s in samples])
    by = np.array([s.ay for s in samples])
    cx, *_ = np.linalg.lstsq(A, bx, rcond=None)
    cy, *_ = np.linalg.lstsq(A, by, rcond=None)
    rx = np.abs(A @ cx - bx)
    ry = np.abs(A @ cy - by)
    return {
        "ax": [float(v) for v in cx],
        "ay": [float(v) for v in cy],
        "fit_residual_ax_mm": {"mean": float(rx.mean()), "max": float(rx.max())},
        "fit_residual_ay_mm": {"mean": float(ry.mean()), "max": float(ry.max())},
    }


def predict(model: dict[str, list[float]], u: float, v: float) -> tuple[float, float]:
    cx, cy = model["ax"], model["ay"]
    return (cx[0] + cx[1] * u + cx[2] * v + cx[3] * u * v,
            cy[0] + cy[1] * u + cy[2] * v + cy[3] * u * v)


def coarse_predict(rng_x: tuple[float, float], rng_y: tuple[float, float],
                   u: float, v: float) -> tuple[float, float]:
    """首轮粗估：把屏幕归一化坐标线性映射到机械臂工作范围。"""
    return (rng_x[0] + u * (rng_x[1] - rng_x[0]),
            rng_y[0] + v * (rng_y[1] - rng_y[0]))


def write_actuation(hardware_path: Path, model: dict[str, Any], max_err_pt: float,
                    report: dict[str, Any]) -> None:
    """把标定结果写回 hardware.json 的 calibration.actuation。"""
    if not hardware_path.exists():
        hardware_path.parent.mkdir(parents=True, exist_ok=True)
        hardware_path.write_text(json.dumps({"calibration": {}},
                                            ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    cfg = json.loads(hardware_path.read_text(encoding="utf-8"))
    calib = cfg.setdefault("calibration", {})
    calib["actuation"] = {
        "model": "bilinear",
        "ax": model["ax"],
        "ay": model["ay"],
        "max_error_pt": round(float(max_err_pt), 3),
        "calibrated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "report": report,
    }
    hardware_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def write_z_press(hardware_path: Path, z_press: float, min_z: float,
                  up_float: float) -> None:
    """写回 z_press（最轻触发深度 + 上浮 1~3mm）。"""
    if not hardware_path.exists():
        hardware_path.parent.mkdir(parents=True, exist_ok=True)
        hardware_path.write_text(json.dumps({"calibration": {}},
                                            ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    cfg = json.loads(hardware_path.read_text(encoding="utf-8"))
    calib = cfg.setdefault("calibration", {})
    calib["z_press"] = round(float(z_press), 3)
    calib["z_release"] = float(calib.get("z_release", 15.0))
    calib["z_calibration"] = {
        "min_z": round(float(min_z), 3),
        "up_float_mm": float(up_float),
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "method": "scan_dwell_then_lift",
    }
    hardware_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                             encoding="utf-8")


# =========================================== 屏幕四角 → 机械臂范围（2026-09-13 新增）
def arm_range_from_model(model: dict[str, Any], width: int,
                         height: int) -> tuple[dict[str, list[float]], list[str]]:
    """按模型算屏幕四角对应的机械臂坐标。

    返回 `({"x": [lo, hi], "y": [lo, hi]}, 四角明细字符串)`。口径与
    `config/hardware.json` 的 `_note_arm_range_fixed` 一致：屏幕四角
    (0,0)/(W,0)/(0,H)/(W,H) 经模型映射后取 x/y 的最小最大。

    单独抽出来是为了能**离线单测**（不需要真机、不需要串口）。
    """
    xs: list[float] = []
    ys: list[float] = []
    rows: list[str] = []
    for px, py in ((0, 0), (width, 0), (0, height), (width, height)):
        ax, ay = predict(model, px / width, py / height)
        xs.append(ax)
        ys.append(ay)
        rows.append(f"({px},{py})→arm({ax:.2f},{ay:.2f})")
    return ({"x": [round(min(xs), 2), round(max(xs), 2)],
             "y": [round(min(ys), 2), round(max(ys), 2)]}, rows)


def write_arm_range_from_model(hardware_path: Path, model: Optional[dict],
                               width: int, height: int) -> None:
    """按新模型重算屏幕四角映射，把 min/max 写回 `calibration.arm_range`。

    为什么要自动写：`arm_range` 是**机械臂坐标的钳制范围**
    （`core/devices/arm.py::move` 会 clamp 到它）。重标后 `actuation` 变了、它没同步，
    屏幕边角就会被**错误 clamp** —— 2026-09-10 就吃过这个亏（机械臂重新就位后 actuation
    更新、arm_range 没同步 → 左上角被钳住 → **返回箭头点不到**，排查很久）。
    此前全仓只有 `load_arm_range()` **读**它，没有任何工具**写**它。

    2026-09-13 由 `tools/calibrate_arm_ink.py` 迁入本文件（用户同意），供墨点版与
    整屏网格标定工具共用一份实现。
    """
    if not model or not model.get("ax") or not model.get("ay"):
        _LOG.warning("没有可用的模型，跳过 arm_range 自动写入")
        return
    try:
        cfg = json.loads(hardware_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 同步失败不影响标定本身
        _LOG.warning("读 %s 失败（%s），跳过 arm_range 自动写入", hardware_path, exc)
        return

    calib = cfg.setdefault("calibration", {})
    old = calib.get("arm_range") or {}
    new, rows = arm_range_from_model(model, width, height)
    calib["arm_range"] = new
    try:
        hardware_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("写 %s 失败（%s），arm_range 未更新", hardware_path, exc)
        return

    _LOG.info("✅ arm_range 已按新模型重算：x%s y%s  →  x%s y%s",
              old.get("x"), old.get("y"), new["x"], new["y"])
    _LOG.info("   四角映射：%s", "  ".join(rows))
    if old and old != new:
        _LOG.info("   （旧值已过期，若屏幕边角按钮点不到，多半就是它没同步造成的）")
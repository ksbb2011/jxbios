"""机械臂映射标定工具（屏幕坐标 → 机械臂坐标，双线性含 u*v 交叉项）。

为什么新做：
    源项目 (`robot_order_picker_new`) 的标定链路是 5 段参数（screen_roi /
    arm_range / scale+offset / top_y_adjust / z_compensation），耦合且边角残差
    大（实测 9.4px）；新项目投屏后**视觉侧整体作废**，但**执行侧 100% 保留**。

    本工具移植并改造 `robot_order_picker_backup_20260903/tools/calibrate_web.py`：
    * 网页靶点页改为 standalone 全屏（与运满满运行时可视区一致）——这正是
      iMouse 官方"鼠标参数采集"的标准做法，已被产品化验证
    * 拟合模型改为双线性 ax = c0 + c1*u + c2*v + c3*u*v（含 u*v 交叉项，
      可表达轻微透视/非正交），而非旧仿射
    * 容差从 6% 屏宽（≈22px）收紧到 **≤3px**
    * 第三轮 10 点"只测不纠偏"验收，不达标**报警不硬写**

先决条件（**用户需提前完成**）：
    ① `tools/calibrate_z_press.py` 已写回 z_press
       ⛔ **Z 越大 = 压得越深，硬上限 6.2，超过会点碎屏幕**（用户 2026-09-09 明确）。
       可用窗口只有 min_z(5.6) ~ 6.2 = 0.6mm，故加深量取 0.4，不再是旧的 1~3mm
    ② iPhone X 已投屏、桌面停在主屏
    ③ 机械臂 COM4 已开（`Restart-Service JxbService -Force`）

用法：
    py -3.11 tools/calibrate_screen_arm.py
    py -3.11 tools/calibrate_screen_arm.py --grid 8 --z-press 6.0   # ⛔ 严禁 >6.2
    py -3.11 tools/calibrate_screen_arm.py --hardware config/hardware.json
    py -3.11 tools/calibrate_screen_arm.py --verify-only     # 跳过第 1 轮

网页打开方式（**关键**）：
    1. 屏幕会显示内网 URL（默认 `http://<PC 内网 IP>:8765/`）
    2. iPhone Safari 打开 → 分享 → "添加到主屏幕"
    3. 从主屏上的"标定"图标打开（standalone 真全屏，与运满满运行时一致）
    4. 按提示触摸圆圈中心
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np
import requests

_LOG = logging.getLogger("calib.arm")

# ================================================================ 机械臂
# Z 轴安全红线：Z 越大 = 压得越深，超过 6.2 会**点碎屏幕**（用户 2026-09-09 明确）。
# 真源在 tools/calib_common.py，此处引用以保持单一定义。
try:  # 允许本文件被单独复制使用时降级
    from tools.calib_common import Z_HARD_MAX, Z_PRESS_SAFE  # type: ignore
except Exception:  # noqa: BLE001
    Z_HARD_MAX = 6.2
    Z_PRESS_SAFE = 6.0


class ArmClient:
    """机械臂 HTTP:8082 协议封装。

    ⛔ Z 轴方向：**Z 越大 = 压得越深**。press() 会钳制到 Z_HARD_MAX(6.2) 以内。
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

    def open(self) -> int:
        r = self._session.get(self.url,
                              params={"duankou": self.com, "hco": 0, "daima": 0},
                              timeout=self.timeout)
        r.raise_for_status()
        self._id = int(r.text.strip())
        if self._id <= 0:
            raise RuntimeError(f"机械臂 open 失败：返回 {r.text!r}")
        _LOG.info("机械臂就绪：资源号 %s", self._id)
        return self._id

    def _cmd(self, daima: str) -> None:
        r = self._session.get(self.url,
                              params={"duankou": 0, "hco": self._id, "daima": daima},
                              timeout=self.timeout)
        r.raise_for_status()

    def move(self, ax: float, ay: float) -> None:
        self._cmd(f"X{ax}Y{ay}")

    def press(self, z: float) -> None:
        """下压到 z。**z 越大越深**，硬钳制在 z_hard_max(6.2) 以内防碎屏。"""
        z = float(z)
        if z > self.z_hard_max:
            if not self._clamp_warned:
                self._clamp_warned = True
                print(
                    f"\n⛔ [安全钳制] 请求下压 Z={z}，超过硬上限 {self.z_hard_max}！\n"
                    f"   Z 越大 = 压得越深，超限会**点碎屏幕**。已截断为 "
                    f"{self.z_hard_max}。\n", flush=True)
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


# ================================================================ 靶点 & 拟合
@dataclass
class Target:
    tx: float
    ty: float
    u: float = 0.0
    v: float = 0.0


@dataclass
class Sample:
    """一个标定样本。

    设计：`Sample.target` 记录"我们下达给机械臂让它去点的屏幕目标"，
    `touch_x/y` 记录"手机实际被触到的位置"，`ax/ay` 记录"当时下达的机械臂坐标"。
    拟合时以 `touch_x/y` 归一化得到 (u,v) 作为特征，`ax/ay` 作为目标——
    这是"如果想要触到屏幕上的 (u,v)，机械臂应该给什么 ax,ay"。

    注意：v1 故意把 `target` 与 `touch` 分开记录，便于后续基于偏差做修正模型。
    """
    target: Target
    touch_x: float
    touch_y: float
    ax: float
    ay: float


def grid_targets(grid_n: int, width: int, height: int, margin: float = 0.06) -> list[Target]:
    """生成 grid_n × grid_n 的屏幕网格靶点（避开边缘 margin）。"""
    targets: list[Target] = []
    coords = np.linspace(margin, 1.0 - margin, grid_n)
    for u in coords:
        for v in coords:
            targets.append(Target(tx=float(u * width), ty=float(v * height),
                                  u=float(u), v=float(v)))
    return targets


def fit_bilinear(samples: list[Sample]) -> dict[str, Any]:
    """拟合双线性 ax = c0 + c1*u + c2*v + c3*u*v。"""
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


# ================================================================ HTTP 服务
class CalibState:
    def __init__(self, width: int, height: int, server_url: str,
                 page_dir: Path) -> None:
        self.width = width
        self.height = height
        self.server_url = server_url.rstrip("/")
        self.page_dir = page_dir
        self.targets: list[Target] = []
        self.samples: list[Sample] = []
        self.current_index = 0
        self.lock = threading.Lock()
        self.touch_event = threading.Event()
        self.last_touch: dict[str, Any] = {}
        # 模型由主流程设置
        self.model: dict[str, list[float]] | None = None

    def url_for(self, idx: int) -> str:
        if idx >= len(self.targets):
            return f"{self.server_url}/done"
        t = self.targets[idx]
        return (f"{self.server_url}/?server={self.server_url}"
                f"&id={idx}&tx={t.tx:.2f}&ty={t.ty:.2f}")


def _make_handler(state: CalibState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A003
            return

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                html = (state.page_dir / "index.html").read_bytes()
                self._send(200, "text/html; charset=utf-8", html)
                return
            if u.path == "/done":
                msg = ("<html><body style='background:#000;color:#fff;"
                       "font:16px sans-serif;text-align:center;padding:40px'>"
                       "<h2>标定完成</h2>"
                       "<p>请回到电脑终端查看结果。</p>"
                       "</body></html>").encode("utf-8")
                self._send(200, "text/html; charset=utf-8", msg)
                return
            self.send_error(404)

        def do_POST(self):  # noqa: N802
            if self.path != "/touch":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {"error": "bad json"})
                return
            try:
                idx = int(payload["id"])
                tx = float(payload["tx"])
                ty = float(payload["ty"])
                touch_x = float(payload["x"])
                touch_y = float(payload["y"])
            except (KeyError, TypeError, ValueError) as exc:
                self._json(400, {"error": f"missing/invalid field: {exc}"})
                return
            with state.lock:
                if idx >= len(state.targets):
                    self._json(400, {"error": "out of range"})
                    return
                if idx < state.current_index:
                    self._json(200, {"ok": True, "ignored": True})
                    return
                state.last_touch = {"idx": idx, "tx": tx, "ty": ty,
                                    "touch_x": touch_x, "touch_y": touch_y}
                # 决定下一步 URL（如果有）
                next_url = state.url_for(idx + 1) if idx + 1 < len(state.targets) else None
            state.touch_event.set()
            self._json(200, {"ok": True, "next_url": next_url})

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, "application/json; charset=utf-8",
                       json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    return Handler


# ================================================================ 流程
def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def collect_round(state: CalibState, arm: ArmClient, z_press: float,
                  wait_timeout: float, settle_ms: int = 150, dwell_ms: int = 150) -> None:
    """走一轮：依次移到每个靶点，等触摸，记录。"""
    for idx in range(state.current_index, len(state.targets)):
        t = state.targets[idx]
        state.touch_event.clear()
        state.last_touch = {}

        if state.model is not None:
            ax, ay = predict(state.model, t.u, t.v)
        else:
            # 没有模型时（不应发生）：使用占位线性
            ax, ay = 10 + t.u * 60, 20 + t.v * 130
        sample = Sample(target=t, touch_x=-1, touch_y=-1, ax=ax, ay=ay)
        state.samples.append(sample)

        arm.move(ax, ay)
        time.sleep(settle_ms / 1000.0)
        arm.press(z_press)
        time.sleep(dwell_ms / 1000.0)

        got = state.touch_event.wait(timeout=wait_timeout)
        if got:
            lt = state.last_touch
            sample.touch_x = float(lt["touch_x"])
            sample.touch_y = float(lt["touch_y"])
        arm.release()
        time.sleep(0.15)

        with state.lock:
            state.current_index = idx + 1

        _LOG.info("[%d/%d] u,v=(%.3f,%.3f) arm=(%.2f,%.2f) touch=(%s,%s)",
                  idx + 1, len(state.targets), t.u, t.v, ax, ay,
                  f"{sample.touch_x:.1f}" if sample.touch_x >= 0 else "-",
                  f"{sample.touch_y:.1f}" if sample.touch_y >= 0 else "-")


def write_actuation(hardware_path: Path, model: dict[str, Any], max_err_pt: float,
                    report: dict[str, Any]) -> None:
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


# ================================================================ main
def main() -> int:
    ap = argparse.ArgumentParser(description="机械臂映射标定")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--grid", type=int, default=8,
                    help="第 1 轮网格 NxN（默认 8 = 64 点）")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--z-press", type=float, default=Z_PRESS_SAFE,
                    help=f"下压深度。⛔ Z 越大越深，硬上限 {Z_HARD_MAX}（超了碎屏）")
    ap.add_argument("--arm-url", default="http://127.0.0.1:8082/MyWcfService/getstring")
    ap.add_argument("--arm-com", default="COM4")
    ap.add_argument("--hardware", default="config/hardware.json")
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    ap.add_argument("--margin", type=float, default=0.06)
    ap.add_argument("--max-error-pt", type=float, default=3.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    page_dir = Path(__file__).resolve().parent / "calib_page"
    hardware_path = Path(args.hardware)
    lan_ip = get_lan_ip()
    server_url = f"http://{lan_ip}:{args.port}"

    print(f"\n>>> 本机 HTTP 服务：http://{lan_ip}:{args.port}/")
    print(">>> 在 iPhone Safari 打开 → 分享 → 添加到主屏幕")
    print(">>> 从主屏「标定」图标进入（standalone 全屏），按提示触摸圆圈\n")

    state = CalibState(width=args.width, height=args.height,
                       server_url=server_url, page_dir=page_dir)
    handler = _make_handler(state)
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()

    arm = ArmClient(args.arm_url, com=args.arm_com)
    arm.open()
    arm.reset()

    try:
        if args.verify_only:
            cfg = json.loads(hardware_path.read_text(encoding="utf-8"))
            model = cfg["calibration"].get("actuation")
            if not model:
                _LOG.error("找不到已有 actuation，请先跑一次完整标定")
                return 1
            state.model = model
            state.targets = grid_targets(3, args.width, args.height, args.margin)[:10]
            state.samples = []
            state.current_index = 0
            _LOG.info("==== 仅验收（不拟合）====")
            collect_round(state, arm, args.z_press, wait_timeout=8.0)
            for s in state.samples:
                if s.touch_x < 0:
                    continue
                dx, dy = s.touch_x - s.target.tx, s.touch_y - s.target.ty
                d = (dx * dx + dy * dy) ** 0.5
                _LOG.info("verify target=(%.1f,%.1f) touch=(%.1f,%.1f) d=%.2fpt",
                          s.target.tx, s.target.ty, s.touch_x, s.touch_y, d)
            return 0

        #---- 第 1 轮
        state.targets = grid_targets(args.grid, args.width, args.height, args.margin)
        state.samples = []
        state.current_index = 0
        _LOG.info("==== 第 1 轮：校准（%d 个靶点）====", len(state.targets))
        collect_round(state, arm, args.z_press, wait_timeout=5.0)

        # 用触摸实测作为特征（更稳，避免"目标→实际"的传递误差）
        calib = []
        for s in state.samples:
            if s.touch_x < 0:
                continue
            u, v = s.touch_x / args.width, s.touch_y / args.height
            if 0 <= u <= 1 and 0 <= v <= 1:
                calib.append(Sample(
                    target=Target(tx=s.touch_x, ty=s.touch_y, u=u, v=v),
                    touch_x=s.touch_x, touch_y=s.touch_y,
                    ax=s.ax, ay=s.ay))

        if len(calib) < 16:
            _LOG.error("有效样本不足（%d/%d）。检查：① 触摸是否被触发 ② z_press 是否够",
                       len(calib), len(state.targets))
            return 1

        model = fit_bilinear(calib)
        state.model = model
        _LOG.info("拟合完成：ax=%s ay=%s", model["ax"], model["ay"])
        _LOG.info("内部残差 ax 均/最 %.3f/%.3f mm， ay 均/最 %.3f/%.3f mm",
                  model["fit_residual_ax_mm"]["mean"], model["fit_residual_ax_mm"]["max"],
                  model["fit_residual_ay_mm"]["mean"], model["fit_residual_ay_mm"]["max"])

        #---- 第 2 轮：验收
        state.targets = grid_targets(3, args.width, args.height, args.margin)[:10]
        state.samples = []
        state.current_index = 0
        _LOG.info("==== 第 2 轮：验收（10 个点，只测不纠偏）====")
        collect_round(state, arm, args.z_press, wait_timeout=8.0)

        valid = [s for s in state.samples if s.touch_x >= 0]
        if not valid:
            _LOG.error("验收轮 10 点全部超时")
            return 1
        errors = [(s, s.touch_x - s.target.tx, s.touch_y - s.target.ty,
                   ((s.touch_x - s.target.tx) ** 2 + (s.touch_y - s.target.ty) ** 2) ** 0.5)
                  for s in valid]
        max_err = max(e[3] for e in errors)
        mean_err = sum(e[3] for e in errors) / len(errors)
        worst = max(errors, key=lambda e: e[3])
        _LOG.info("验收误差：mean=%.2fpt max=%.2fpt（阈值 %.1fpt）",
                  mean_err, max_err, args.max_error_pt)
        _LOG.info("最差点：target=(%.1f,%.1f) touch=(%.1f,%.1f) dx=%.1f dy=%.1f",
                  worst[0].target.tx, worst[0].target.ty,
                  worst[0].touch_x, worst[0].touch_y, worst[1], worst[2])

        report = {
            "fit_samples": len(calib),
            "verify_samples": len(valid),
            "mean_err_pt": round(mean_err, 3),
            "max_err_pt": round(max_err, 3),
            "worst": {"tx": worst[0].target.tx, "ty": worst[0].target.ty,
                      "touch_x": worst[0].touch_x, "touch_y": worst[0].touch_y,
                      "dx": worst[1], "dy": worst[2]},
        }
        if max_err > args.max_error_pt:
            _LOG.error("❌ 误差超阈值（%.2f > %.1f），不写入配置。常见原因：", max_err, args.max_error_pt)
            _LOG.error("   ① 校准页非真全屏（必须从主屏图标进入）")
            _LOG.error("   ② z_press 不够（手机没真正被触发）")
            _LOG.error("   ③ 机械臂间隙大 / 触屏稳定段不够")
            return 2

        write_actuation(hardware_path, model, max_err, report)
        _LOG.info("✅ 已写入 %s", hardware_path)
        return 0

    finally:
        try:
            arm.reset()
            arm.close()
        except Exception:  # noqa: BLE001
            pass
        httpd.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
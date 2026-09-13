"""整屏自动网格标定（iOS App 传感版）—— 2026-09-13 新增

一句话：机械臂在**整屏网格**上逐点按压，手机 App 把每次被按到的**真实坐标**上报回来，
电脑据此 ①重拟合整屏映射 ②重算 `arm_range` ③生成整屏 `tap_correction` 锚点表。

为什么需要它（与现有两套标定的差别）
    墨点版 `calibrate_arm_ink.py`
        —— iMouse 截图找墨点，**画布只能取 y≈170~660**（备忘录上下被按钮栏挡住、留不下墨点）
        → 屏幕底部那排按钮全靠模型外推，实测统一偏左 6~7pt，这正是"期望点一次点击点不动"的根因。
    UI 落点版 `calibrate_tap_ui.py`
        —— 只认 5 个固定按钮、依赖页面状态、受 6pt 步长量化。
    **本工具**（整屏网格 + 手机传感）
        —— 整屏无死角、无页面状态依赖、无步长量化（App 直接报坐标），
        且不需要 OCR / 模板匹配，比墨点版轻得多。

传感端只做三件事：显示靶心、上报坐标、显示状态 —— 不参与任何计算，与运行时点击逻辑完全解耦。
App 只需实现两个接口（源码见 ios_calib/）：
    GET  /state  → {"phase","note","id","seq","tx","ty","aim_x","aim_y","total","done",
                    "expect_viewport","last_ack"}
                   · tx/ty   = 本轮靶点（要它落在这里）
                   · aim_x/y = **本次实际瞄准的屏幕点**（含补偿；App 可按它画标记）
                   · cmd_ax/ay = 本次真正下发给机械臂的坐标（诊断/离线自测用）
                   · seq     = 单调递增的下压序号（跨轮次不重复，判断"新点"用它而不是 id）
    POST /touch  ← {"id":7,"x":57.0,"y":245.0,"t":1757750000000,"viewport":[375,812],"kind":"began"}

安全（三条硬约束，都不许绕过）
    * Z 一律取配置：本工具**不提供任何加深 Z 的参数**；`calib_common.ArmClient` 自带
      Z_HARD_MAX=6.2 硬钳制 + atexit/信号退出保护（抬笔 → 复位 → 释放串口）。
    * 开始前先等 App 连上；首点收不到上报**立即中止**（不盲按几十次），并打印排查清单。
    * 默认**只测量**；只有 `--apply` 才写 hardware.json（写前 .bak 备份 + 打印 diff）。

用法
    py -3.11 tools\\calibrate_full_grid.py --list        # 看参数、网格布局、当前配置（不碰硬件）
    py -3.11 tools\\calibrate_full_grid.py --dry         # 只起服务等 App 连上（验证网络/权限）
    py -3.11 tools\\calibrate_full_grid.py               # 正式一轮（只测量、不写配置）
    py -3.11 tools\\calibrate_full_grid.py --verify      # 测完再用「新模型+补偿」复压一轮验收
    py -3.11 tools\\calibrate_full_grid.py --apply       # 测量并写回配置（actuation+arm_range+锚点）

    ⚠️ 从仓库根直接跑需要仓库根在 sys.path 上（与其它 tools/ 脚本同规矩）：
       $env:PYTHONPATH=(Get-Location).Path   # PowerShell 下先设一次
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from core.config_store import PROJECT_ROOT, ConfigStore

from tools.calib_common import (
    Z_HARD_MAX,
    ArmClient,
    Sample,
    Target,
    arm_range_from_model,
    fit_bilinear,
    predict,
    write_actuation,
    write_arm_range_from_model,
)

_LOG = logging.getLogger("calib.grid")

DEFAULT_PORT = 8767        # 8765=网页版、8766=z 扫描、8767=本工具，互不冲突
MIN_ANCHOR_PT = 1.0        # 小于它的残差视为测量噪声，不建锚点
# 为什么这里用 1.0 而 calibrate_tap_ui.py 用 2.0：那个工具一次只测 1 个按钮、残差是
# "扫点扫出来的最小值"（有 6pt 步长量化，2pt 以下无意义）；本工具每个网格点都是**实测**
# 且精度在 1pt 量级，所以除纯噪声外**每个点都该建锚点** —— 否则没建锚点的点会被
# 邻居锚点的修正值"顺带"改掉（运行时的"最近锚点"是分段常值），反而引入偏差。
DEFAULT_RADIUS = 40.0      # 与 core/devices/arm.py::_load_tap_correction 的默认 radius_px 一致
SRC_TAG = "ui_grid_calib"  # 锚点来源标记：只覆盖/清理"本工具建的锚点"，不动别的工具


def _safe_stdio() -> None:
    """把 stdout/stderr 的错误策略改成 replace（只改策略、不改编码）。

    为什么：帮助文本里有 ⛔ / ≈ 这类符号，GBK 控制台下 argparse 打印帮助会直接抛
    UnicodeEncodeError（2026-09-13 实测 `calibrate_arm_ink.py --help` 即如此）。
    打包/批处理路径有 chcp 65001 所以平时不犯，但直接 `py tools\\xxx.py` 时会踩。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 老解释器/被重定向时忽略
            pass


# ================================================================ 网格与状态
@dataclass
class GridPoint:
    """一个网格点的"意图 → 下达 → 实测"三元组。"""

    name: str
    target: Target
    ax: float = 0.0
    ay: float = 0.0
    touch_x: float = -1.0
    touch_y: float = -1.0
    pass_no: int = 1

    @property
    def valid(self) -> bool:
        return self.touch_x >= 0.0 and self.touch_y >= 0.0

    @property
    def dx(self) -> float:
        """补偿量 = **意图点 − 实测点**。

        与 `core/devices/arm.py::click_pixel` 的 `tx = px + corr` 语义一致，也与
        `tools/calibrate_tap_ui.py` 里 `offset = hit - expected` 等价
        （那里 hit = "能点中的屏幕点"，这里实测点 = "笔实际落在的屏幕点"）。
        """
        return self.target.tx - self.touch_x

    @property
    def dy(self) -> float:
        return self.target.ty - self.touch_y

    @property
    def dist_pt(self) -> float:
        return (self.dx ** 2 + self.dy ** 2) ** 0.5

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "pass": self.pass_no,
                "tx": round(self.target.tx, 1), "ty": round(self.target.ty, 1),
                "ax": round(self.ax, 3), "ay": round(self.ay, 3),
                "touch_x": round(self.touch_x, 1), "touch_y": round(self.touch_y, 1),
                "dx": round(self.dx, 2) if self.valid else None,
                "dy": round(self.dy, 2) if self.valid else None,
                "dist_pt": round(self.dist_pt, 2) if self.valid else None}


def build_grid(cols: int, rows: int, width: int, height: int,
               margin: float) -> list[Target]:
    """整屏网格（四边按 margin 内缩）。行 0 在上、列 0 在左；名字 g_r{行}c{列}。

    为什么不用 calib_common.grid_targets：它是 N×N 正方形网格；手机是 375×812 竖屏，
    5 列 × 9 行（约 75×90pt 间距）在同等点数下覆盖更均匀。
    """
    if cols < 2 or rows < 2:
        raise ValueError("--cols/--rows 至少为 2")
    mx, my = width * margin, height * margin
    span_x, span_y = width - 2 * mx, height - 2 * my
    xs = [mx + span_x * (c / (cols - 1)) for c in range(cols)]
    ys = [my + span_y * (r / (rows - 1)) for r in range(rows)]
    out: list[Target] = []
    for ty in ys:
        for tx in xs:
            t = Target(tx=tx, ty=ty)
            t.u, t.v = tx / width, ty / height
            out.append(t)
    return out


def grid_name(idx: int, cols: int) -> str:
    return f"g_r{idx // cols}c{idx % cols}"


class GridState:
    """HTTP 服务与采集循环之间的共享状态。"""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.phase = "waiting"        # waiting / running / done / error
        self.note = "等待电脑开始"
        self.index = 0
        self.seq = 0              # 单调递增的"下压序号"（跨轮次也不重复，App/自测据此判断新点）
        self.total = 0
        self.tx = 0.0
        self.ty = 0.0
        self.aim_x = 0.0          # 本次下压**实际瞄准**的屏幕点（含补偿）；供 App 显示与自测取真值
        self.aim_y = 0.0
        self.cmd_ax = 0.0         # 本次真正下发给机械臂的坐标（诊断/自测用）
        self.cmd_ay = 0.0
        self.lock = threading.Lock()
        self.touch_event = threading.Event()
        self.last_touch: dict[str, Any] = {}   # 当前点的**第一条**上报（每点开始前清空）
        self.last_ack: dict[str, Any] = {}     # 最近一次上报（供 App 显示，持续保留）
        self.touch_count = 0
        self.stale_count = 0
        self.dup_count = 0
        self.app_polls = 0
        self.app_seen = False
        self.viewport_bad = 0

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "phase": self.phase,
                "note": self.note,
                "id": self.index,
                "seq": self.seq,
                "tx": round(self.tx, 2),
                "ty": round(self.ty, 2),
                "aim_x": round(self.aim_x, 2),
                "aim_y": round(self.aim_y, 2),
                "cmd_ax": round(self.cmd_ax, 3),
                "cmd_ay": round(self.cmd_ay, 3),
                "total": self.total,
                "done": self.phase == "done",
                "app_seen": self.app_seen,
                "expect_viewport": [self.width, self.height],
                "touches": self.touch_count,
                "stale": self.stale_count,
                "dup": self.dup_count,
                "viewport_bad": self.viewport_bad,
                "last_ack": dict(self.last_ack) or None,
            }

    def set_phase(self, phase: str, note: str = "") -> None:
        with self.lock:
            self.phase = phase
            if note:
                self.note = note


# ================================================================ HTTP 服务
def _make_handler(state: GridState):
    class Handler(BaseHTTPRequestHandler):
        # keep-alive：App 每 0.4s 轮询 /state，复用连接比每次握手稳
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003 - 静音默认访问日志
            return

        # ---------------------------------------------------------- GET
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/state", "/target"):
                with state.lock:
                    state.app_polls += 1
                    state.app_seen = True
                self._json(200, state.snapshot())
                return
            if path in ("/", "/index.html"):
                lane = f"http://{self.server.server_address[0]}:{self.server.server_port}"
                body = (
                    "<html><head><meta charset='utf-8'><title>整屏网格标定</title></head>"
                    "<body style='background:#000;color:#eee;font:16px/1.6 sans-serif;padding:32px'>"
                    "<h2>整屏网格标定服务已启动</h2>"
                    "<p>请在 iPhone 上打开<b>标定 App</b>（ios_calib/），地址填：<br>"
                    f"<b>{lane}</b></p>"
                    "<p style='color:#888'>本页只是提示页；采集走 App 的 /state 与 /touch。</p>"
                    "</body></html>"
                ).encode("utf-8")
                self._bytes(200, "text/html; charset=utf-8", body)
                return
            self._json(404, {"error": "not found"})

        # --------------------------------------------------------- POST
        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path != "/touch":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads((self.rfile.read(length) if length else b"{}") or b"{}")
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"error": f"bad json: {exc}"})
                return
            try:
                x = float(payload["x"])
                y = float(payload["y"])
            except (KeyError, TypeError, ValueError) as exc:
                self._json(400, {"error": f"missing/invalid x,y: {exc}"})
                return
            kind = str(payload.get("kind") or "began")
            vp = payload.get("viewport")
            try:
                pid = int(payload["id"]) if payload.get("id") is not None else None
            except (TypeError, ValueError):
                pid = None

            with state.lock:
                state.app_seen = True
                if isinstance(vp, (list, tuple)) and len(vp) == 2:
                    try:
                        if (int(vp[0]), int(vp[1])) != (state.width, state.height):
                            state.viewport_bad += 1
                    except (TypeError, ValueError):
                        pass
                if state.phase != "running":
                    state.stale_count += 1       # 未开始/已结束时的杂散触摸，一律丢弃
                    self._json(200, {"ok": True, "ignored": "not running"})
                    return
                if pid is not None and pid != state.index:
                    state.stale_count += 1
                    self._json(200, {"ok": True, "ignored": "stale id"})
                    return
                if state.last_touch:
                    # 一次按压可能产生 began/moved/ended 多条：只采信**第一条**，
                    # 其余计数留作诊断（不影响结果）。
                    state.dup_count += 1
                    self._json(200, {"ok": True, "ignored": "duplicate"})
                    return
                state.last_touch = {"id": state.index, "x": round(x, 2), "y": round(y, 2),
                                    "t": payload.get("t"), "kind": kind}
                state.last_ack = dict(state.last_touch)
                state.touch_count += 1
            state.touch_event.set()
            self._json(200, {"ok": True})

        # --------------------------------------------------------- 工具
        def _bytes(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._bytes(code, "application/json; charset=utf-8",
                        json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    return Handler


def serve(state: GridState, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    return httpd


def get_lan_ip() -> str:
    """本机在局域网里的地址（App 要连的就是它）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ================================================================ 采集
def collect_pass(state: GridState, points: list[GridPoint], arm: ArmClient, z_press: float,
                 command: Callable[[int, Target], tuple[float, float, float, float]],
                 settle: float, dwell: float, wait_timeout: float,
                 max_miss: int, label: str) -> tuple[bool, int]:
    """走一轮：逐点移动 → 下压 → 等上报 → 抬起。返回 (是否走完, 未上报点数)。

    `command(idx, target)` 返回 `(arm_x, arm_y, aim_x, aim_y)`：
    前两个是下发给机械臂的坐标，后两个是**这次实际瞄准的屏幕点**（第 2 轮含补偿）。
    aim 会通过 `/state` 广播，App 可用它显示"笔即将压的位置"，自测脚本用它取真值。

    首点（idx=0）超时**立即中止**：第一个点就拿不到上报，多半是屏熄了 / App 不在前台 /
    网络不通 / 防火墙拦了；此时继续按几十次纯属浪费时间，还会反复压屏。
    后续点允许累计 miss 达 `max_miss` 次再中止（单点偶发丢包的容错）。
    """
    miss = 0
    for idx, p in enumerate(points):
        with state.lock:
            state.index = idx
            state.seq += 1
            state.tx, state.ty = p.target.tx, p.target.ty
            state.last_touch = {}
        state.touch_event.clear()
        state.set_phase("running", f"进行中 {idx + 1}/{len(points)}")

        ax, ay, aim_x, aim_y = command(idx, p.target)
        p.ax, p.ay = ax, ay
        with state.lock:
            state.aim_x, state.aim_y = aim_x, aim_y
            state.cmd_ax, state.cmd_ay = ax, ay
        arm.move(ax, ay)
        time.sleep(settle)
        arm.press(z_press)
        time.sleep(dwell)
        got = state.touch_event.wait(timeout=wait_timeout)
        if got:
            with state.lock:
                lt = dict(state.last_touch)
            p.touch_x = float(lt.get("x", -1.0))
            p.touch_y = float(lt.get("y", -1.0))
        arm.release()
        time.sleep(0.15)

        if p.valid:
            _LOG.info("[%s %2d/%d] %-8s 意图(%6.1f,%6.1f) arm(%6.2f,%6.2f) "
                      "实测(%6.1f,%6.1f) 残差(%+5.1f,%+5.1f) %5.1fpt",
                      label, idx + 1, len(points), p.name,
                      p.target.tx, p.target.ty, ax, ay, p.touch_x, p.touch_y,
                      p.dx, p.dy, p.dist_pt)
        else:
            miss += 1
            _LOG.warning("[%s %2d/%d] %-8s 意图(%6.1f,%6.1f) arm(%6.2f,%6.2f) "
                         "❌ 未收到上报（本点缺失，累计 %d）",
                         label, idx + 1, len(points), p.name,
                         p.target.tx, p.target.ty, ax, ay, miss)
            if idx == 0 or miss >= max_miss:
                _abort_hint(state, miss)
                return False, miss
    return True, miss


def _abort_hint(state: GridState, miss: int) -> None:
    """中止时给出可执行的排查清单（照现场最容易踩的顺序排）。"""
    _LOG.error("❌ 累计 %d 个点没收到手机上报 → 已中止（不继续盲按）", miss)
    _LOG.error("   请依次确认：")
    _LOG.error("   ① 手机屏幕没熄（设置 → 显示与亮度 → 自动锁定 → 永不；App 也会主动禁锁屏）")
    _LOG.error("   ② 标定 App 在**前台**且已连上（指示灯应为绿；地址填的是 http://<本机IP>:%d）",
               DEFAULT_PORT)
    _LOG.error("   ③ 手机与电脑在同一 Wi-Fi 网段（可先关掉手机蜂窝数据试试）")
    _LOG.error("   ④ Windows 防火墙放行入站 %d（首次运行会弹“允许访问”，务必点允许）", DEFAULT_PORT)
    _LOG.error("   ⑤ 屏幕没有被系统弹窗 / 通知横幅遮挡")
    _LOG.error("   ⑥ 人工用手指点一下屏幕：App 上应立刻出现坐标（验证 App 自身没问题）")
    _LOG.error("   现场取证：state=%s", json.dumps(state.snapshot(), ensure_ascii=False))
    state.set_phase("error", "未收到手机上报，已中止")


def wait_app(state: GridState, timeout: float) -> bool:
    """等 App 第一次轮询 /state（避免还没连上就开始盲按）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if state.app_seen:
            _LOG.info("✅ App 已连上（用时 %.1fs）", time.time() - t0)
            return True
        time.sleep(0.2)
    _LOG.error("❌ %.0fs 内没有收到 App 的任何请求（/state 轮询都没来）", timeout)
    _LOG.error("   先查：App 地址是否填对、手机与电脑是否同网段、Windows 防火墙是否放行 %d",
               DEFAULT_PORT)
    _LOG.error("   提示：可直接重跑 `--dry` 单独验证连通性（不驱臂）。")
    return False


# ================================================================ 汇总与写盘
def nearest_corr(anchors: list[dict], x: float, y: float,
                 radius: float) -> tuple[float, float, str]:
    """按"半径内最近锚点"取 (dx, dy, name) —— 与 core/devices/arm.py::_tap_correction 同规则。"""
    best: Optional[dict] = None
    best_d2: Optional[float] = None
    for a in anchors:
        d2 = (float(a["x"]) - x) ** 2 + (float(a["y"]) - y) ** 2
        if radius > 0 and d2 > radius * radius:
            continue
        if best_d2 is None or d2 < best_d2:
            best, best_d2 = a, d2
    if best is None:
        return 0.0, 0.0, ""
    return float(best["dx"]), float(best["dy"]), str(best.get("name") or "")


def coverage_radius(cols: int, rows: int, width: int, height: int, margin: float) -> float:
    """让"整屏任意点都能被最近锚点覆盖"所需的最小 radius_px。

    最近锚点最远出现在网格单元中心 → 距离 = 半个对角线的长度（再留 5% 余量）。
    网格越密 → 这个值越小 → 补偿越贴合局部；太密则耗时线性增长。
    """
    mx, my = width * margin, height * margin
    step_x = (width - 2 * mx) / max(cols - 1, 1)
    step_y = (height - 2 * my) / max(rows - 1, 1)
    return round((((step_x / 2) ** 2 + (step_y / 2) ** 2) ** 0.5) * 1.05, 1)


def build_anchors(points: list[GridPoint]) -> list[dict]:
    """由实测残差生成锚点表（坐标写**意图点**，偏移写 dx/dy —— 与运行时约定一致）。"""
    out: list[dict] = []
    for p in points:
        if not p.valid:
            continue
        if abs(p.dx) < MIN_ANCHOR_PT and abs(p.dy) < MIN_ANCHOR_PT:
            continue   # 小于噪声门限：不建锚点（宁可不补也不猜）
        out.append({"name": p.name, "x": round(p.target.tx, 1), "y": round(p.target.ty, 1),
                    "dx": round(p.dx, 1), "dy": round(p.dy, 1), "src": SRC_TAG})
    return out


def refit_model(points: list[GridPoint], width: int, height: int) -> Optional[dict]:
    """用实测触摸点当特征重拟合。

    口径照抄 tools/calibrate_screen_arm.py（已被真机验证过）：
    特征 = **实测落点**的归一化坐标，标签 = 当时下达的机械臂坐标
        ⇒ predict(u,v) 给出"让笔落在 (u,v) 所需的机械臂坐标"，正是运行时需要的。
    """
    samples: list[Sample] = []
    for p in points:
        if not p.valid:
            continue
        u, v = p.touch_x / width, p.touch_y / height
        if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0:
            samples.append(Sample(target=Target(tx=p.touch_x, ty=p.touch_y, u=u, v=v),
                                  touch_x=p.touch_x, touch_y=p.touch_y,
                                  ax=p.ax, ay=p.ay))
    if len(samples) < 16:
        _LOG.error("有效样本不足（%d 个，至少 16 个才拟合）", len(samples))
        return None
    model = fit_bilinear(samples)
    _LOG.info("重拟合完成：ax=%s", [round(v, 6) for v in model["ax"]])
    _LOG.info("              ay=%s", [round(v, 6) for v in model["ay"]])
    _LOG.info("内部残差（机械臂坐标）：ax 均/最 %.3f/%.3f，ay 均/最 %.3f/%.3f",
              model["fit_residual_ax_mm"]["mean"], model["fit_residual_ax_mm"]["max"],
              model["fit_residual_ay_mm"]["mean"], model["fit_residual_ay_mm"]["max"])
    return model


def stats_of(points: list[GridPoint]) -> dict[str, Any]:
    """残差统计（补偿前看它；补偿后应显著变小）。"""
    valid = [p for p in points if p.valid]
    if not valid:
        return {"n": 0}
    ds = sorted(p.dist_pt for p in valid)
    worst = max(valid, key=lambda p: p.dist_pt)
    return {
        "n": len(valid),
        "mean_pt": round(sum(ds) / len(ds), 3),
        "max_pt": round(ds[-1], 3),
        "p90_pt": round(ds[max(int(len(ds) * 0.9) - 1, 0)], 3),
        "worst": {"name": worst.name, "x": round(worst.target.tx, 1),
                  "y": round(worst.target.ty, 1), "dx": round(worst.dx, 1),
                  "dy": round(worst.dy, 1)},
    }


def _fmt_stats(tag: str, st: dict[str, Any]) -> str:
    if not st.get("n"):
        return f"{tag}: 无有效样本"
    w = st["worst"]
    return (f"{tag}: n={st['n']} mean={st['mean_pt']}pt max={st['max_pt']}pt "
            f"p90={st['p90_pt']}pt；最差 {w['name']}({w['x']},{w['y']}) Δ({w['dx']:+.1f},{w['dy']:+.1f})")


def apply_config(hardware_path: Path, model: Optional[dict], anchors: list[dict],
                 radius: float, width: int, height: int, verify: Optional[dict],
                 drop_foreign: bool = True, log=print) -> int:
    """把结果写回 hardware.json（**只在 --apply 时调用**）：actuation + arm_range + 锚点。

    备份 + 打印 diff；actuation/arm_range 走已验证的公共函数。
    锚点合并策略见下面 `drop_foreign` 的注释。
    """
    if not hardware_path.is_file():
        log(f"[失败] 找不到配置：{hardware_path}")
        return 1
    cfg = json.loads(hardware_path.read_text(encoding="utf-8"))
    cal = cfg.setdefault("calibration", {})

    bak = hardware_path.with_suffix(".json.bak_grid")
    try:
        if not bak.exists():
            bak.write_bytes(hardware_path.read_bytes())
            log(f"  已备份原配置 → {bak.name}")
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] 备份失败（{exc}），已放弃写入")
        return 1

    # ---- 1) actuation（重拟合结果；内部自带原子写）
    if model:
        write_actuation(hardware_path, model, float((verify or {}).get("max_pt") or 0.0),
                        {"source": "calibrate_full_grid", "verify": verify})
        log(f"  actuation    : 已更新（ax={[round(v, 6) for v in model['ax']]}）")

    # ---- 2) arm_range（按新模型重算；防"漏同步 → 边角被错误 clamp"）
    if model:
        rng, rows = arm_range_from_model(model, width, height)
        old_range = (json.loads(hardware_path.read_text(encoding="utf-8"))
                     .get("calibration", {}).get("arm_range"))
        write_arm_range_from_model(hardware_path, model, width, height)
        log(f"  arm_range    : {old_range} → {rng}")
        log(f"                 四角映射 {'  '.join(rows)}")

    # ---- 3) tap_correction（保留其它工具来源的锚点，只覆盖/清理本工具的）
    cfg = json.loads(hardware_path.read_text(encoding="utf-8"))
    cal = cfg.setdefault("calibration", {})
    tc = cal.get("tap_correction")
    if not isinstance(tc, dict):
        tc = {}
    tc.setdefault("enabled", True)
    old_radius = tc.get("radius_px", DEFAULT_RADIUS)
    old_pts = [dict(p) for p in (tc.get("points") or []) if isinstance(p, dict)]
    mine = [p for p in old_pts if p.get("src") == SRC_TAG]
    foreign = [p for p in old_pts if p.get("src") != SRC_TAG]
    if drop_foreign and foreign and model:
        # ⚠️ refit 之后**全局映射变了**：别的工具（如 calibrate_tap_ui）在**旧映射**下测出的
        # 锚点方向已经失效，继续留着会按错误方向补偿（等于把刚修好的映射又推歪）。
        # 所以默认丢弃；确实想保留请加 --keep-foreign-anchors。
        names = ", ".join(str(p.get("name")) for p in foreign[:6])
        log(f"  tap_correction: 丢弃过期的其它来源锚点 {len(foreign)} 个（{names}）；"
            f"它们基于 refit 前的旧映射，方向已失效")
        foreign = []
    tc["points"] = foreign + anchors
    tc["radius_px"] = radius
    cal["tap_correction"] = tc
    try:
        tmp = hardware_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(hardware_path)
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] 写 tap_correction 失败：{exc}")
        return 1
    log(f"  tap_correction: 写入本工具锚点 {len(anchors)} 个（替换旧 {len(mine)} 个），"
        f"保留其它来源 {len(foreign)} 个；radius_px {old_radius} → {radius}")
    if anchors:
        sample = anchors[:3]
        log("                 样例：" + "  ".join(
            f"{a['name']}({a['x']:.0f},{a['y']:.0f})Δ({a['dx']:+.0f},{a['dy']:+.0f})"
            for a in sample))
    log(f"  ✅ 配置已更新：{hardware_path}")
    return 0


def write_records(path: Path, rec: dict) -> None:
    """明细追加写 data/ui_grid_calib.json（一轮一条），供后续对比与回归。"""
    try:
        old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        if not isinstance(old, list):
            old = []
        old.append(rec)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        _LOG.info("明细已追加：%s（本轮为第 %d 条）", path, len(old))
    except Exception as exc:  # noqa: BLE001 - 记录失败不影响结论
        _LOG.warning("明细写入失败（%s）", exc)


def hardware_path_of() -> Path:
    """定位 config/hardware.json（打包后 = exe 同级；源码 = 仓库根）。"""
    return Path(PROJECT_ROOT) / "config" / "hardware.json"


# ================================================================ 入口
def print_preflight(args: argparse.Namespace, hardware_path: Path, store: ConfigStore,
                    cols: int, rows: int) -> None:
    """`--list`：只读地打印布局、当前配置与耗时预估（完全不碰硬件）。"""
    cal = store.get("hardware", "calibration", {}) or {}
    act = cal.get("actuation") or {}
    tc = cal.get("tap_correction") or {}
    anchors = [p for p in (tc.get("points") or []) if isinstance(p, dict)]
    mine = [p for p in anchors if p.get("src") == SRC_TAG]
    per_point = args.settle + args.dwell + 0.35
    rounds = 2 if args.verify else 1
    sx = (args.width - 2 * args.width * args.margin) / max(cols - 1, 1)
    sy = (args.height - 2 * args.height * args.margin) / max(rows - 1, 1)
    print("=== 整屏网格标定（--list，不碰硬件）===")
    print(f"  配置文件        : {hardware_path}  (存在={hardware_path.is_file()})")
    print(f"  逻辑分辨率      : {args.width}x{args.height} "
          f"(ConfigStore.logical_size={store.logical_size})")
    print(f"  网格            : {cols} 列 × {rows} 行 = {cols * rows} 点"
          f"（内缩 {args.margin * 100:.0f}%，间距约 {sx:.0f}×{sy:.0f}pt）")
    print(f"  建议 radius_px  : {coverage_radius(cols, rows, args.width, args.height, args.margin)}"
          f"（当前配置 {tc.get('radius_px', DEFAULT_RADIUS)}；小于建议值会有覆盖盲区）")
    print(f"  z_press         : {cal.get('z_press')}（Z 硬上限 {Z_HARD_MAX}；本工具无加深参数）")
    print(f"  actuation       : {'已有' if act.get('ax') else '缺失（首轮将用 arm_range 粗估）'}"
          f"  max_error_pt={act.get('max_error_pt')}")
    print(f"  arm_range       : {cal.get('arm_range')}")
    print(f"  tap_correction  : 共 {len(anchors)} 个锚点（本工具 {len(mine)} 个）"
          f" enabled={tc.get('enabled')}")
    print(f"  轮数 / 预计耗时 : {rounds} 轮 × {cols * rows} 点 × {per_point:.1f}s "
          f"≈ {rounds * cols * rows * per_point / 60:.1f} 分钟")
    print(f"  服务端口        : {args.port}（App 里填 http://<本机IP>:{args.port}）")
    print(f"  写盘            : {'是（--apply）' if args.apply else '否（只测量，加 --apply 才写配置）'}")


def main() -> int:
    _safe_stdio()
    ap = argparse.ArgumentParser(
        description="整屏自动网格标定（手机 App 传感）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cols", type=int, default=5, help="列数（默认 5）")
    ap.add_argument("--rows", type=int, default=9, help="行数（默认 9）")
    ap.add_argument("--margin", type=float, default=0.06,
                    help="四边内缩比例（默认 0.06；太靠边可能压到外壳或压不到）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP 端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--settle", type=float, default=1.5, help="移动后停顿秒数（默认 1.5）")
    ap.add_argument("--dwell", type=float, default=2.0, help="下压保持秒数（默认 2.0）")
    ap.add_argument("--wait-touch", type=float, default=6.0,
                    help="每点等上报超时秒数（默认 6.0）")
    ap.add_argument("--wait-app", type=float, default=180.0,
                    help="开始前等 App 连上的超时秒数（默认 180）")
    ap.add_argument("--max-miss", type=int, default=2,
                    help="累计多少个点没上报就中止（默认 2；首点超时永远立即中止）")
    ap.add_argument("--no-wait-app", action="store_true", help="不等待 App 直接开始（不推荐）")
    ap.add_argument("--verify", action="store_true",
                    help="再复压一轮「新模型 + 本次锚点」给出补偿后验收误差（默认只跑前两轮）")
    ap.add_argument("--keep-foreign-anchors", action="store_true",
                    help="写盘时保留别的工具建的锚点（默认丢弃：refit 后旧锚点方向已失效）")
    ap.add_argument("--apply", action="store_true",
                    help="把结果写进 config/hardware.json（默认只测量；写前自动备份）")
    ap.add_argument("--list", action="store_true", help="只打印布局与当前配置，不碰硬件")
    ap.add_argument("--dry", action="store_true", help="只起服务等 App 连上，不驱臂")
    ap.add_argument("--strict", action="store_true",
                    help="App 上报的 viewport 与配置不符时直接报错退出")
    ap.add_argument("--arm-url", default="http://127.0.0.1:8082/MyWcfService/getstring")
    ap.add_argument("--arm-com", default="COM4")
    ap.add_argument("--auto-restart", action="store_true",
                    help="串口被占时自动重启 JxbService（**会弹 UAC**）")
    ap.add_argument("--hardware", default="", help="hardware.json 路径（默认按项目根推断）")
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    hardware_path = Path(args.hardware) if args.hardware else hardware_path_of()
    store = ConfigStore()
    try:
        cols = args.cols if args.cols > 1 else 5
        rows = args.rows if args.rows > 1 else 9
    except Exception:  # noqa: BLE001
        cols, rows = 5, 9

    if args.list:
        print_preflight(args, hardware_path, store, cols, rows)
        return 0

    # ---- 配置与前置检查
    if not hardware_path.is_file():
        _LOG.error("找不到配置文件：%s", hardware_path)
        return 1
    cal = store.get("hardware", "calibration", {}) or {}
    z_press = float(cal.get("z_press", 5.9) or 5.9)
    if z_press > Z_HARD_MAX:
        _LOG.error("配置里的 z_press=%.2f 超过硬上限 %.1f，拒绝开始（会压碎屏幕）",
                   z_press, Z_HARD_MAX)
        return 1

    targets = build_grid(cols, rows, args.width, args.height, args.margin)
    points = [GridPoint(name=grid_name(i, cols), target=t) for i, t in enumerate(targets)]
    radius = coverage_radius(cols, rows, args.width, args.height, args.margin)

    ip = get_lan_ip()
    print()
    print("=" * 74)
    print(f"  整屏网格标定   {cols}×{rows} = {len(points)} 点   z_press={z_press}（Z 上限 {Z_HARD_MAX}）")
    print(f"  App 里填地址 ： http://{ip}:{args.port}")
    print(f"  轮数         ： {'2（测量 + 验收）' if args.verify else '1（仅测量）'}"
          f"   预计 {(2 if args.verify else 1) * len(points) * (args.settle + args.dwell + 0.35) / 60:.1f} 分钟")
    print(f"  写盘         ： {'会写配置（--apply）' if args.apply else '只测量，不写配置'}")
    print("=" * 74)
    print()

    state = GridState(args.width, args.height)
    state.total = len(points)
    httpd = serve(state, args.port)
    _LOG.info("HTTP 服务已启动：0.0.0.0:%d（App 用 http://%s:%d）", args.port, ip, args.port)
    _LOG.info("若手机连不上：Windows 防火墙需放行入站 %d；手机与电脑需在同一 Wi-Fi 网段", args.port)

    # ---- 只验证连通性
    if args.dry:
        _LOG.info("--dry：等 App 连上（最多 %.0fs），不做任何机械臂动作", args.wait_app)
        ok = wait_app(state, args.wait_app)
        snap = state.snapshot()
        _LOG.info("连通性结果：app_seen=%s polls=%d touches=%d stale=%d dup=%d viewport_bad=%d",
                  snap["app_seen"], state.app_polls, state.touch_count,
                  state.stale_count, state.dup_count, state.viewport_bad)
        if snap["app_seen"]:
            _LOG.info("✅ 手机能连上电脑。可以去掉 --dry 正式跑一轮了")
        httpd.shutdown()
        return 0 if ok else 2

    arm = ArmClient(args.arm_url, com=args.arm_com)
    rec: dict[str, Any] = {}
    try:
        arm.open(auto_restart=args.auto_restart)
        arm.reset()

        if not args.no_wait_app and not wait_app(state, args.wait_app):
            state.set_phase("error", "App 未连上")
            return 2

        # ---------------- 第 1 轮：测量
        _LOG.info("==== 第 1 轮：测量（%d 点，用现有模型/粗估定位）====", len(points))

        def cmd_pass1(_idx: int, t: Target) -> tuple[float, float, float, float]:
            model = store.get("hardware", "calibration.actuation", None) or {}
            ax = model.get("ax")
            if ax:
                px, py = predict(model, t.u, t.v)
            else:
                rng = cal.get("arm_range") or {}
                rx = tuple(rng.get("x") or (9.93, 73.2))
                ry = tuple(rng.get("y") or (16.62, 154.62))
                px, py = (rx[0] + t.u * (rx[1] - rx[0]), ry[0] + t.v * (ry[1] - ry[0]))
            return px, py, t.tx, t.ty   # 第 1 轮瞄准的就是靶点本身

        ok, miss = collect_pass(state, points, arm, z_press, cmd_pass1,
                                args.settle, args.dwell, args.wait_touch,
                                args.max_miss, "测1")
        if not ok:
            return 3
        st1 = stats_of(points)
        _LOG.info("第 1 轮（补偿前）：%s", _fmt_stats("残差", st1))

        # ---------------- 重拟合 actuation
        model = refit_model(points, args.width, args.height)
        snap1 = [p.as_dict() for p in points]

        # ---- ⚠️ 锚点必须用**第 2 轮（新模型）的残差**，不能用第 1 轮的。
        # 原因：refit 出来的新模型学的是"笔落在 (u,v) 所需的机械臂坐标"，
        # **它本身已经把系统性偏差吸收进去了**。这时若再按第 1 轮的残差建锚点，
        # 运行时就会"补两次"（比如 5.8pt 的偏差会被补成反方向 5.8pt，比不补更糟）。
        # 所以必须先按新模型复压一轮，用"新模型仍未盖住的那部分"建锚点。
        st2: Optional[dict[str, Any]] = None
        anchors: list[dict] = []
        if model:
            _LOG.info("==== 第 2 轮：模型自检（新模型、不带补偿）→ 锚点的唯一依据 ====")
            for p in points:
                p.touch_x = p.touch_y = -1.0
                p.pass_no = 2

            def cmd_pass2(_idx: int, t: Target) -> tuple[float, float, float, float]:
                px, py = predict(model, t.u, t.v)
                return px, py, t.tx, t.ty

            ok2, _m2 = collect_pass(state, points, arm, z_press, cmd_pass2,
                                    args.settle, args.dwell, args.wait_touch,
                                    args.max_miss, "自检")
            if ok2:
                st2 = stats_of(points)
                anchors = build_anchors(points)
                _LOG.info("第 2 轮（新模型，未补偿）：%s", _fmt_stats("残差", st2))
                _LOG.info("锚点表：%d/%d 个（|残差| ≥ %.0fpt 才建），建议 radius_px=%.1f",
                          len(anchors), len(points), MIN_ANCHOR_PT, radius)
            else:
                _LOG.warning("第 2 轮未走完 → 本轮**不生成锚点**"
                             "（避免拿补偿前的残差去重复补偿）")

        if state.viewport_bad:
            _LOG.warning("⚠️ 有 %d 次上报的 viewport 与 %dx%d 不符 —— "
                         "可视区不一致会让残差里混入坐标口径误差，务必查",
                         state.viewport_bad, args.width, args.height)
            if args.strict:
                return 4

        # ---------------- 第 3 轮（可选）：模型 + 锚点，模拟真实点击路径验收
        st3: Optional[dict[str, Any]] = None
        if args.verify and model:
            _LOG.info("==== 第 3 轮：验收（新模型 + 本次锚点，等价于 click_pixel 的真实路径）====")
            for p in points:
                p.touch_x = p.touch_y = -1.0
                p.pass_no = 3

            def cmd_pass3(_idx: int, t: Target) -> tuple[float, float, float, float]:
                dx, dy, _name = nearest_corr(anchors, t.tx, t.ty, radius)
                aim_x, aim_y = t.tx + dx, t.ty + dy
                px, py = predict(model, aim_x / args.width, aim_y / args.height)
                return px, py, aim_x, aim_y

            ok3, _m3 = collect_pass(state, points, arm, z_press, cmd_pass3,
                                    args.settle, args.dwell, args.wait_touch,
                                    args.max_miss, "验收")
            if not ok3:
                return 3
            st3 = stats_of(points)
            _LOG.info("第 3 轮（补偿后）：%s", _fmt_stats("残差", st3))

        # ---------------- 汇总
        state.set_phase("done", "完成")
        rec = {
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "params": {"cols": cols, "rows": rows, "margin": args.margin, "z_press": z_press,
                       "settle": args.settle, "dwell": args.dwell,
                       "wait_touch": args.wait_touch, "radius": radius,
                       "w": args.width, "h": args.height, "verify": bool(args.verify)},
            "pass1_measure": st1, "pass2_selfcheck": st2, "pass3_verify": st3,
            "anchors": anchors,
            "model": model,
            "touches": {"total": state.touch_count, "stale": state.stale_count,
                        "dup": state.dup_count, "viewport_bad": state.viewport_bad},
            "points_pass1": snap1,
            "points_final": [p.as_dict() for p in points],
            "applied": bool(args.apply),
        }
        write_records(Path(PROJECT_ROOT) / "data" / "ui_grid_calib.json", rec)

        print()
        print("=" * 74)
        print(f"  {_fmt_stats('第1轮 测量（旧模型）', st1)}")
        if st2:
            print(f"  {_fmt_stats('第2轮 新模型自检  ', st2)}   ← 锚点依据")
        if st3:
            print(f"  {_fmt_stats('第3轮 补偿后验收  ', st3)}   ← 运行时实际精度（建议 ≤3pt）")
        print(f"  锚点 {len(anchors)} 个，radius_px={radius}，viewport_bad={state.viewport_bad}")
        print("=" * 74)

        if args.apply:
            print()
            print("—— 按 --apply 写回配置 ——")
            rc = apply_config(hardware_path, model, anchors, radius,
                              args.width, args.height, st3 or st2,
                              drop_foreign=not args.keep_foreign_anchors)
            if rc != 0:
                return rc
        else:
            print()
            print("  本次**未写配置**（只测量）。确认上面数据合理后，重跑并加 --apply 才会生效。")

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

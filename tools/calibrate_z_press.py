"""机械臂下压深度 z_press 标定工具。

⚠️ Z 轴方向：**Z 越大 = 压得越深**。硬上限 **6.2**（用户 2026-09-09 明确：
"z最高6.2，不能超过这个，超出这个，你会把屏幕点碎的"），代码已做双重防线：
① 本文件与 calibrate_arm_ink.py 在参数层拒绝超限输入；② ArmClient.press()
在命令层把 z 钳制到 Z_HARD_MAX(6.2)。碎屏不可逆，宁可点不动也不能压过头。

扫描方式：从 Z_START（浅）到 Z_MAX（深）逐档加深，每档按下去后等手机触摸
事件。**第一个被触发的 z** = 最轻触发深度，记为 `min_z`。

z_press 取值：`z_press = min_z + 加深量`。实测 min_z=5.6，距硬顶 6.2 只剩
0.6mm 窗口，故加深量收紧到 [0, 0.6]，默认 0.4。
【历史教训】旧规范「上浮 1~3mm」曾把方向理解反：z 越大越深，"加号"等于继续
下压，算出 z_press=7.100 超限 0.9mm，后来又试到 8.5 —— 那是在直接怼屏。
若实测 min_z ≥ 6.2，说明笔尖/手机位置偏高，**必须先调整物理位置**，
绝不能靠加大 Z 去够（够到就是碎屏）。

复用了 `calibrate_screen_arm.py` 的 HTTP 服务 / 机械臂客户端。

先决条件：
    ① iPhone X 已投屏、桌面停在主屏（中央有 App 图标位置最佳）
    ② 机械臂 COM4 已开
    ③ 已在主屏打开过 `calib_page/index.html` 的 standalone 全屏图（首次
       跑标定需要建图，否则 z_press 触发后页面没响应）
       —— 本工具第一次跑时如果不放心，可先用 `tools/calibrate_screen_arm.py`
       跑一遍建图，再用本工具。

用法：
    py -3.11 tools/calibrate_z_press.py
    py -3.11 tools/calibrate_z_press.py --up-float 0.4 --start 5.0 --max 6.2
    py -3.11 tools/calibrate_z_press.py --hardware config/hardware.json
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from tools.calibrate_screen_arm import (  # type: ignore
    ArmClient,
    CalibState,
    Z_HARD_MAX,
    Z_PRESS_SAFE,
    _make_handler,
    get_lan_ip,
)

_LOG = logging.getLogger("calib.z_press")


def read_actuation_anchor(hardware_path: Path, width: int, height: int):
    """找一个安全的屏幕点作为 z 扫描的触点。

    优先用已标定的 actuation 模型预测屏幕中心对应的机械臂坐标；
    若没有模型（首次标定），用保守的线性占位。
    """
    target_u = 0.5
    target_v = 0.5
    if hardware_path.exists():
        try:
            cfg = json.loads(hardware_path.read_text(encoding="utf-8"))
            act = cfg.get("calibration", {}).get("actuation")
            if act:
                ax = act["ax"]
                ay = act["ay"]
                pred_ax = ax[0] + ax[1] * target_u + ax[2] * target_v + ax[3] * target_u * target_v
                pred_ay = ay[0] + ay[1] * target_u + ay[2] * target_v + ay[3] * target_u * target_v
                _LOG.info("使用 actuation 模型预测中心点 ax=%.2f ay=%.2f", pred_ax, pred_ay)
                return pred_ax, pred_ay, width * target_u, height * target_v
        except Exception:  # noqa: BLE001
            pass
    # 占位：屏幕中心对应 arm 大概位置（实际会偏，但首次 z 扫描不需要精确位置）
    _LOG.warning("没有现成 actuation 模型，使用线性占位（点不准不影响 z 扫描本身）")
    return (10 + 0.5 * 60, 20 + 0.5 * 130, width * 0.5, height * 0.5)


def scan_min_trigger(state: CalibState, arm: ArmClient, ax: float, ay: float,
                     start: float, max_z: float, step: float,
                     dwell_ms: int = 150, timeout: float = 2.0) -> float | None:
    """从 start 加深到 max_z，第一个触发的 z 即为 min_trigger。"""
    z = start
    while z <= max_z + 1e-9:
        state.touch_event.clear()
        state.last_touch = {}
        # 移到目标（用同一坐标，每次都一样）
        arm.move(ax, ay)
        time.sleep(0.15)
        arm.press(round(z, 3))
        time.sleep(dwell_ms / 1000.0)
        got = state.touch_event.wait(timeout=timeout)
        arm.release()
        time.sleep(0.20)  # 防止电容屏残留
        if got:
            _LOG.info("z=%.3f 触发触摸 ✓", z)
            return z
        _LOG.info("z=%.3f 未触发", z)
        z = round(z + step, 3)
    return None


def write_z_press(hardware_path: Path, z_press: float, min_z: float, up_float: float) -> None:
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


def main() -> int:
    ap = argparse.ArgumentParser(description="机械臂 z_press 标定")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=8766,
                    help="HTTP 服务端口（与 calibrate_screen_arm 默认 8765 区分开）")
    ap.add_argument("--start", type=float, default=5.0, help="扫描起始 z（浅）")
    ap.add_argument("--max", dest="max_z", type=float, default=Z_HARD_MAX,
                    help=f"扫描最深 z（硬顶，绝不允许超过 {Z_HARD_MAX}）")
    ap.add_argument("--step", type=float, default=0.1, help="扫描步长")
    ap.add_argument("--up-float", type=float, default=0.4,
                    help="在 min_z 基础上再加深多少以保证稳定触发（会被 6.2 硬顶截断）")
    ap.add_argument("--arm-url", default="http://127.0.0.1:8082/MyWcfService/getstring")
    ap.add_argument("--arm-com", default="COM4")
    ap.add_argument("--hardware", default="config/hardware.json")
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    ap.add_argument("--no-write", action="store_true", help="只扫描不写回硬件配置")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    page_dir = Path(__file__).resolve().parent / "calib_page"
    hardware_path = Path(args.hardware)

    # ---- 安全校验（碎屏不可逆，两道防线都不可省）
    if args.max_z > Z_HARD_MAX:
        _LOG.error("⛔ --max=%.2f 超过 Z 硬上限 %.2f（会点碎屏幕），已拒绝执行。"
                   "如需扫描请设 ≤ %.2f", args.max_z, Z_HARD_MAX, Z_HARD_MAX)
        return 2
    if args.start > Z_HARD_MAX:
        _LOG.error("⛔ --start=%.2f 已超过 Z 硬上限 %.2f", args.start, Z_HARD_MAX)
        return 2
    # min_z 实测 5.6，到硬顶 6.2 只剩 0.6mm 窗口，加深量必须远小于旧规范的 1~3mm
    if not (0.0 <= args.up_float <= 0.6):
        _LOG.error("⛔ --up-float=%.2f 超出安全区间 [0, 0.6]。"
                   "旧规范「上浮 1~3mm」在 6.2 硬顶下放不下（5.6+1.5=7.1 会碎屏）",
                   args.up_float)
        return 2

    lan_ip = get_lan_ip()
    server_url = f"http://{lan_ip}:{args.port}"
    print(f"\n>>> 本机 HTTP 服务：http://{lan_ip}:{args.port}/")
    print(">>> 在 iPhone Safari 打开 → 分享 → 添加到主屏幕")
    print(">>> 从主屏「标定」图标进入（standalone 全屏）\n")

    # 用单个固定靶点（屏幕中央）作为 z 扫描的触发位置
    from tools.calibrate_screen_arm import Target  # noqa: WPS433
    state = CalibState(width=args.width, height=args.height,
                       server_url=server_url, page_dir=page_dir)
    cx_px, cy_px = args.width / 2, args.height / 2
    state.targets = [Target(tx=cx_px, ty=cy_px, u=0.5, v=0.5)]

    handler = _make_handler(state)
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()

    arm = ArmClient(args.arm_url, com=args.arm_com)
    arm.open()
    arm.reset()
    try:
        ax, ay, tx, ty = read_actuation_anchor(hardware_path, args.width, args.height)
        _LOG.info("触点：屏幕 (%.1f, %.1f) → arm (%.2f, %.2f)", tx, ty, ax, ay)
        _LOG.info("扫描：z ∈ [%.2f, %.2f] 步长 %.2f", args.start, args.max_z, args.step)

        min_z = scan_min_trigger(state, arm, ax, ay,
                                 start=args.start, max_z=args.max_z, step=args.step)
        if min_z is None:
            _LOG.error("扫描到 z=%.2f 仍未触发触摸。可能：① 手机没真全屏打开标定页"
                       "② 触点没落在可点击元素上 ③ 机械臂 z 与手机距离过大",
                       args.max_z)
            return 1

        # ⚠️ 方向：Z 越大 = 压得越深。min_z 是「刚能触发」的深度，
        #    为保证稳定接触需**再加深** up_float，但绝不能越过 6.2 硬顶。
        z_press = round(min_z + args.up_float, 3)
        if z_press > Z_HARD_MAX:
            _LOG.warning("⛔ min_z %.3f + %.2f = %.3f 超过硬上限 %.2f，"
                         "已钳制为 %.2f（宁可偶尔点不动，也不能碎屏）",
                         min_z, args.up_float, z_press, Z_HARD_MAX, Z_HARD_MAX)
            z_press = Z_HARD_MAX
        _LOG.info("✅ min_z=%.3f + 加深 %.2f = **z_press=%.3f**（硬顶 %.2f）",
                  min_z, args.up_float, z_press, Z_HARD_MAX)

        if args.no_write:
            _LOG.info("--no-write：未写入硬件配置")
            return 0

        if not hardware_path.exists():
            _LOG.warning("硬件配置不存在：%s，写入默认结构", hardware_path)
            hardware_path.parent.mkdir(parents=True, exist_ok=True)
            hardware_path.write_text(json.dumps(
                {"calibration": {}}, ensure_ascii=False, indent=2), encoding="utf-8")

        write_z_press(hardware_path, z_press, min_z, args.up_float)
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
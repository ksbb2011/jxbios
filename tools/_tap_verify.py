"""一次性验证：arm 归位 → 轻点全国货源 Tab (244,535)@z(可配) → 归位。

坐标转换链路（与运行时一致）：
    480x640 标定帧 click (244,535)
      → screen_roi[85,63,406,575] 内归一化 (u,v)
      → predict(actuation, u, v) → 机械臂 (ax, ay) mm
下压 z 硬钳制在 6.2（Z 越大越深，超了碎屏）。

用途：验证不同 z 值能否稳定触发触摸（不碎屏）。--z 可调。
"""
import argparse
import io
import json
import sys
import time
from pathlib import Path

# Windows 默认 gbk，脚本含中文打印时避免编码崩溃
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ap = argparse.ArgumentParser(description="一次性轻点验证（z 可配）")
ap.add_argument("--z", type=float, default=None,
                help="下压深度。不传则读 config z_press（默认 5.9）。Z越大越深，硬上限 6.2（超了碎屏）")
args = ap.parse_args()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from calib_common import ArmClient, predict  # noqa: E402

HW = Path("config/hardware.json")
cfg = json.loads(HW.read_text(encoding="utf-8"))
calib = cfg["calibration"]
model = calib["actuation"]
roi = calib["screen_roi"]            # [l, t, r, b] @480x640
lx, ty, rx, by = roi
z_hard_max = calib.get("z_calibration", {}).get("z_hard_max", 6.2)

# 全国货源 Tab 中心（480x640 标定帧，旧项目 click_point 经 source_white ROI 验证）
CLICK = (244, 535)
u = (CLICK[0] - lx) / (rx - lx)
v = (CLICK[1] - ty) / (by - ty)
ax, ay = predict(model, u, v)
Z_PRESS = args.z if args.z is not None else cfg["calibration"]["z_press"]

print(f"[坐标] click={CLICK}  归一化 u,v=({u:.4f},{v:.4f})  →  机械臂 (ax,ay)=({ax:.2f},{ay:.2f})")
print(f"[安全] 请求下压 Z={Z_PRESS}，硬上限 Z_HARD_MAX={z_hard_max}（Z越大越深，超了碎屏）")

arm = ArmClient(url=cfg["robot"]["url"], com=cfg["robot"]["com"], z_hard_max=z_hard_max)
try:
    arm.open(auto_restart=True)      # COM 被占时自动重启 JxbService（会弹 UAC）
    arm.reset()                      # 归位
    print("[1/4] 已归位，2 秒后移动到靶点...")
    time.sleep(2.0)
    arm.move(ax, ay)
    time.sleep(0.15)                 # tap_settle：物理到位稳定
    print(f"[2/4] 已移动到 (ax,ay)=({ax:.2f},{ay:.2f})，下压 Z={Z_PRESS}")
    arm.press(Z_PRESS)
    time.sleep(0.15)                 # tap_dwell：触屏驻留
    print("[3/4] 已抬起")
    arm.release()
    time.sleep(0.5)
    arm.reset()                      # 归位
    print("[4/4] [OK] 轻点完成并已归位。")
    print(">>> 请查看手机：白色全国货源是否切到红色列表页？屏幕有无异样？")
except Exception as exc:  # noqa: BLE001
    print(f"\n[ERR] 执行异常：{exc}")
    raise
finally:
    try:
        arm.reset()
    except Exception:  # noqa: BLE001
        pass
    try:
        arm.close()
    except Exception:  # noqa: BLE001
        pass

"""机械臂归位工具：抬笔 → 回原点 → 释放串口。

什么时候用：
    * 标定/调试被中断后，机械臂停在半路（可能压着屏幕）
    * 遇到 `机械臂 open 返回 0 —— COM4 串口被占用` 时
    * 任何想让机械臂安全回到原位的场景

它做了三件事：
    1. 打开串口；若被占（返回 0），**自动弹 UAC 重启 JxbService** 后重试
    2. 抬笔（Z0）→ 回原点（X0Y0Z0）
    3. 释放串口（close），避免下次再出现"串口被占"

用法：
    py -3.11 tools\\arm_home.py                 # 归位（串口被占会弹 UAC 自动重启服务）
    py -3.11 tools\\arm_home.py --no-restart    # 不自动重启，被占就直接报错
"""

from __future__ import annotations

import argparse
import sys
import time

from tools.calib_common import ArmClient, restart_jxb_service

DEFAULT_ARM_URL = "http://127.0.0.1:8082/MyWcfService/getstring"


def main() -> int:
    ap = argparse.ArgumentParser(description="机械臂归位")
    ap.add_argument("--arm-url", default=DEFAULT_ARM_URL)
    ap.add_argument("--arm-com", default="COM4")
    ap.add_argument("--no-restart", action="store_true",
                    help="串口被占时不自动重启服务，直接报错退出")
    args = ap.parse_args()

    arm = ArmClient(args.arm_url, com=args.arm_com)

    try:
        arm.open(auto_restart=not args.no_restart)
    except RuntimeError as exc:
        print(f"[失败] {exc}")
        print("\n可以手动执行（管理员 PowerShell）：")
        print("    Restart-Service JxbService -Force")
        return 1

    print(f"串口已打开，资源号 {arm._id}")
    try:
        # 先抬笔，防止归位途中笔尖拖过屏幕
        arm.release()
        time.sleep(0.3)
        arm.reset()
        time.sleep(0.5)
        print("归位完成：X0Y0Z0")
    finally:
        arm.close()
        print("串口已释放")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[中断] 已捕获 Ctrl+C，机械臂状态未知，建议重跑本工具确认归位")
        sys.exit(130)
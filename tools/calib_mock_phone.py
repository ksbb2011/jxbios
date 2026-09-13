"""免手机的整屏标定自测（假手机 + 假机械臂，**真跑 HTTP 协议**）—— 2026-09-13 新增

为什么需要它：真机一轮要几分钟，还得有人盯着屏幕；但"采样 → 拟合 → 生成锚点 → 回压验收
→ 写配置/备份"这条链路的**协议与数学**完全可以离线验证。本脚本干的就是这个：

    * **假手机**：真的去轮询 `GET /state`、真的 `POST /touch`（走真实 HTTP，不是直接调函数）
    * **假机械臂**：只记录指令，不做任何 IO（不占串口、不动电机）
    * **物理模型（关键）**：笔的真实落点按**真值模型**反解出来的屏幕点算，再叠加两类误差
        ‑ `--bias`  **线性系统偏差**（全局一致，例如机械臂标定整体偏了 5pt）
                    → 应该被 refit 出的新 `actuation` **吸收**，此时锚点表**本就该是空的**
        ‑ `--noise` **逐点局部误差**（同一点每次都一样、不同点不同，模拟机械非线性/触屏差异）
                    → bilinear 模型吃不掉，应该由 `tap_correction` 锚点**吃掉**
                    （由于运行时是"最近锚点、分段常值"，只有"每个网格点都建锚点"才能
                      把这层误差压到最小 —— 自测会验证这一点）
      这两层分工正是本工具的设计意图，自测就是来证明它没搞反（早期版本就踩过"两层重复补偿"的坑：
      拿第 1 轮残差建锚点 → 运行时补两次 → 5.8pt 的偏差被补成反方向 5.8pt）。

    * **断言**（任一不过 → 退出码 1）：
        ① 每点都收到上报（`miss=0`），无重复/过期上报干扰
        ② 第 1 轮测出的①号偏差 ≈ `--bias`
        ③ refit 后第 2 轮（不带补偿）残差 ≈ 只剩 `--noise` 的量级 ⇒ 证明"线性偏差已被模型吸收"
        ④ 锚点数 == 有效网格点数（每个点都实测，除噪声外都该建锚点）
        ⑤ 第 3 轮（模型 + 锚点）残差 ≤1.5pt 且**明显小于**第 2 轮 ⇒ 补偿方向与幅度都对
        ⑥ 写盘正确（actuation / arm_range / 锚点 / radius_px / 备份）
        ⑦ **真实 `config/hardware.json` 一个字节都没动**（写盘只作用在临时副本）

用法
    py -3.11 tools\\calib_mock_phone.py                          # 默认：bias=+5,-3  noise=3
    py -3.11 tools\\calib_mock_phone.py --bias 0,0 --noise 0       # 无误差：残差应≈0
    py -3.11 tools\\calib_mock_phone.py --client http://127.0.0.1:8767
        # 只当"假手机"连真服务：配合 `calibrate_full_grid.py --dry` 验连通性/协议（不驱臂不写盘）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sys
import threading
import time
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Optional

import tools.calibrate_full_grid as g


def _safe_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _json_req(url: str, payload: Optional[dict] = None, timeout: float = 3.0) -> dict:
    """极简 HTTP JSON 客户端（GET 不带 body / POST 带 body）。"""
    if payload is None:
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def inverse_true(model: dict, ax: float, ay: float, width: int, height: int,
                 u0: float, v0: float, iters: int = 6) -> tuple[float, float]:
    """真值模型的**逆**：给定下达的机械臂坐标，求"笔真正被瞄准的屏幕点"（逻辑点）。

    这是自测物理忠实的关键：偏差要作用在**机械臂实际行为**上，而不是"PC 以为的瞄准点"。
    双线性模型没有简洁解析逆，用牛顿迭代（对 6 参数平滑模型 6 步足以收敛到 1e-6）。
    """
    cx, cy = model["ax"], model["ay"]
    u, v = u0, v0
    for _ in range(iters):
        px = cx[0] + cx[1] * u + cx[2] * v + cx[3] * u * v
        py = cy[0] + cy[1] * u + cy[2] * v + cy[3] * u * v
        j11, j12 = cx[1] + cx[3] * v, cx[2] + cx[3] * u
        j21, j22 = cy[1] + cy[3] * v, cy[2] + cy[3] * u
        det = j11 * j22 - j12 * j21
        if abs(det) < 1e-12:
            break
        du = ((ax - px) * j22 - (ay - py) * j12) / det
        dv = (-(ax - px) * j21 + (ay - py) * j11) / det
        u += du
        v += dv
    return u * width, v * height


def noise_at(tx: float, ty: float, amp: float) -> tuple[float, float]:
    """确定性"逐点局部误差"：同一个点每次跑都一样（物理误差不随时间变）。

    用点坐标做 CRC32 种子 → 可复现、可对照。amp=0 时返回 0。
    """
    if amp <= 0:
        return 0.0, 0.0
    rnd = random.Random(zlib.crc32(f"{tx:.1f},{ty:.1f}".encode("utf-8")))
    return rnd.uniform(-amp, amp), rnd.uniform(-amp, amp)


class StubArm:
    """假机械臂：只记录指令，不做任何 IO。"""

    def __init__(self) -> None:
        self.moves: list[tuple[float, float]] = []
        self.presses = 0

    def open(self, *a, **k) -> None:
        pass

    def reset(self) -> None:
        pass

    def move(self, x: float, y: float) -> None:
        self.moves.append((round(x, 3), round(y, 3)))

    def press(self, z: Optional[float] = None) -> None:
        self.presses += 1

    def release(self) -> None:
        pass

    def close(self) -> None:
        pass


def phone_loop(state: "g.GridState", base: str, true_model: dict, bias: tuple[float, float],
               noise: float, viewport: tuple[int, int], stop: threading.Event,
               seen: list[int], errs: list[str]) -> None:
    """假手机主循环：看到新的 seq，就按"真值模型反解 + 偏差 + 局部误差"上报。"""
    wp, hp = viewport
    last_seq = -1
    while not stop.is_set():
        try:
            st = _json_req(base + "/state", timeout=2.0)
        except Exception:  # noqa: BLE001 - 服务未起或短暂抖动
            time.sleep(0.02)
            continue
        seq = int(st.get("seq", 0))
        if st.get("phase") == "running" and seq != last_seq:
            last_seq = seq
            tx, ty = float(st.get("tx", 0.0)), float(st.get("ty", 0.0))
            cmd_ax, cmd_ay = float(st.get("cmd_ax", 0.0)), float(st.get("cmd_ay", 0.0))
            sx, sy = inverse_true(true_model, cmd_ax, cmd_ay, wp, hp, tx / wp, ty / hp)
            nx, ny = noise_at(tx, ty, noise)
            body = {"id": int(st.get("id", 0)),
                    "x": sx + bias[0] + nx, "y": sy + bias[1] + ny,
                    "t": int(time.time() * 1000),
                    "viewport": [wp, hp], "kind": "began"}
            try:
                _json_req(base + "/touch", body, timeout=2.0)
                seen.append(seq)
            except Exception as exc:  # noqa: BLE001
                errs.append(f"上报失败 seq={seq}: {exc}")
        time.sleep(0.02)


# ================================================================ 模式：只当假手机
def client_mode(base: str, viewport: tuple[int, int], seconds: float) -> int:
    """连真服务：只轮询 /state 并在 running 时原地（偏差 0）上报，用于连通性验证。"""
    print(f"假手机连：{base}（{seconds:.0f}s；只验证 /state 与 /touch 是否通）")
    t0, polls, reports, last_seq = time.time(), 0, 0, -1
    while time.time() - t0 < seconds:
        try:
            st = _json_req(base + "/state", timeout=2.0)
            polls += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  /state 失败：{exc}")
            time.sleep(0.5)
            continue
        print(f"  /state phase={st.get('phase')} id={st.get('id')} seq={st.get('seq')} "
              f"aim=({st.get('aim_x')},{st.get('aim_y')})")
        if st.get("phase") == "running" and int(st.get("seq", 0)) != last_seq:
            last_seq = int(st.get("seq", 0))
            try:
                _json_req(base + "/touch", {"id": int(st.get("id", 0)),
                                            "x": float(st.get("aim_x", 0.0)),
                                            "y": float(st.get("aim_y", 0.0)),
                                            "t": int(time.time() * 1000),
                                            "viewport": [viewport[0], viewport[1]],
                                            "kind": "began"}, timeout=2.0)
                reports += 1
                print(f"    → 已上报 seq={last_seq}")
            except Exception as exc:  # noqa: BLE001
                print(f"    → 上报失败：{exc}")
        time.sleep(0.3)
    print(f"结束：/state 轮询 {polls} 次，上报 {reports} 次")
    return 0 if polls else 2


# ================================================================ 模式：端到端自测
def pipeline(args: argparse.Namespace) -> int:
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("  ✅ " if ok else "  ❌ ") + name + ("   " + detail if detail else ""))
        if not ok:
            fails.append(name)

    real = g.hardware_path_of()
    if not real.is_file():
        print(f"[失败] 找不到真实配置：{real}")
        return 1
    real_md5 = hashlib.md5(real.read_bytes()).hexdigest()

    tmp = Path("data/_mock_hw.json")
    shutil.copy2(real, tmp)
    cfg = json.loads(tmp.read_text(encoding="utf-8"))
    cal = cfg.get("calibration", {})
    true_model = cal.get("actuation")
    if not true_model or not true_model.get("ax"):
        print("[失败] 真实配置里没有 actuation，无法自测（先跑一次映射标定）")
        return 1
    z = float(cal.get("z_press", 5.9) or 5.9)
    bias, noise = args.bias, args.noise

    print("=" * 76)
    print(f"  整屏标定离线自测   网格 {args.cols}×{args.rows}   "
          f"线性偏差({bias[0]:+.0f},{bias[1]:+.0f})pt   逐点噪声±{noise:.1f}pt   z={z}")
    print("=" * 76)

    targets = g.build_grid(args.cols, args.rows, args.width, args.height, args.margin)
    points = [g.GridPoint(name=g.grid_name(i, args.cols), target=t)
              for i, t in enumerate(targets)]
    state = g.GridState(args.width, args.height)
    state.total = len(points)
    httpd = g.serve(state, args.port)
    base = f"http://127.0.0.1:{args.port}"
    stop = threading.Event()
    seen: list[int] = []
    errs: list[str] = []
    th = threading.Thread(target=phone_loop, name="fake-phone",
                          args=(state, base, true_model, bias, noise,
                                (args.width, args.height), stop, seen, errs), daemon=True)
    th.start()
    time.sleep(0.2)

    stub = StubArm()

    def cmd1(_idx: int, t: g.Target) -> tuple[float, float, float, float]:
        px, py = g.predict(true_model, t.u, t.v)
        return px, py, t.tx, t.ty

    print(f"\n[第 1 轮] 测量（{len(points)} 点；假机械臂只记录指令，不动真机）")
    ok1, miss1 = g.collect_pass(state, points, stub, z, cmd1, 0.0, 0.0,
                                args.wait_touch, 2, "测1")
    st1 = g.stats_of(points)
    check("第 1 轮：每点都收到上报", ok1 and miss1 == 0,
          f"miss={miss1}，服务端收到 {state.touch_count} 条")
    check("假机械臂确实被驱动", stub.presses == len(points),
          f"下压 {stub.presses} 次 / {len(points)} 点")
    check("无重复/过期上报干扰", state.dup_count == 0 and state.stale_count == 0,
          f"dup={state.dup_count} stale={state.stale_count}")
    print(f"        残差（第1轮，未补偿）：{g._fmt_stats('', st1).lstrip(': ')}")

    valid = [p for p in points if p.valid]
    mdx = sum(-p.dx for p in valid) / max(len(valid), 1)
    mdy = sum(-p.dy for p in valid) / max(len(valid), 1)
    check("测出的系统偏差 ≈ 注入的 --bias",
          abs(mdx - bias[0]) <= 1.0 and abs(mdy - bias[1]) <= 1.0,
          f"测得({mdx:+.2f},{mdy:+.2f}) vs 注入({bias[0]:+.0f},{bias[1]:+.0f})")

    print("\n[拟合]")
    model2 = g.refit_model(points, args.width, args.height)
    radius = g.coverage_radius(args.cols, args.rows, args.width, args.height, args.margin)
    check("重拟合成功", model2 is not None)

    st2: Optional[dict[str, Any]] = None
    anchors: list[dict] = []
    st3: Optional[dict[str, Any]] = None
    if model2 is not None:
        for p in points:
            p.touch_x = p.touch_y = -1.0
            p.pass_no = 2

        def cmd2(_idx: int, t: g.Target) -> tuple[float, float, float, float]:
            px, py = g.predict(model2, t.u, t.v)
            return px, py, t.tx, t.ty

        print("\n[第 2 轮] 新模型自检（不带补偿）→ 锚点的依据")
        ok2, _m2 = g.collect_pass(state, points, stub, z, cmd2, 0.0, 0.0,
                                  args.wait_touch, 2, "自检")
        st2 = g.stats_of(points)
        anchors = g.build_anchors(points)
        print(f"        残差（第2轮，新模型）：{g._fmt_stats('', st2).lstrip(': ')}")
        print(f"        锚点：{len(anchors)}/{len(points)} 个（|残差| ≥ {g.MIN_ANCHOR_PT:.0f}pt 才跳过），"
              f"radius_px={radius}")
        check("第 2 轮：走完且全部收到上报", ok2)
        # 线性系统偏差应已被新模型吸收：残差只剩逐点噪声量级（不应再看到 bias 那么大）
        max2 = st2.get("max_pt")
        limit2 = max(2.0, noise * 1.6)
        check(f"线性偏差已被新模型吸收（第2轮 max ≤ {limit2:.1f}pt）",
              max2 is not None and float(max2) <= limit2,
              f"max={max2}pt（若还≈{abs(bias[0]):.0f}pt 说明 refit 没生效）")
        exp_anchors = [p for p in points if p.valid
                       and (abs(p.dx) >= g.MIN_ANCHOR_PT or abs(p.dy) >= g.MIN_ANCHOR_PT)]
        check("锚点数 == 超噪声门限的有效点数", len(anchors) == len(exp_anchors),
              f"锚点 {len(anchors)} 个 / 应有 {len(exp_anchors)} 个")
        check("锚点值正确（补偿量 = 意图点 − 实测点）",
              all(abs(a["dx"] - round(p.dx, 1)) < 0.2 and abs(a["dy"] - round(p.dy, 1)) < 0.2
                  for a, p in zip(anchors, exp_anchors)))

        for p in points:
            p.touch_x = p.touch_y = -1.0
            p.pass_no = 3

        def cmd3(_idx: int, t: g.Target) -> tuple[float, float, float, float]:
            dx, dy, _n = g.nearest_corr(anchors, t.tx, t.ty, radius)
            aim_x, aim_y = t.tx + dx, t.ty + dy
            px, py = g.predict(model2, aim_x / args.width, aim_y / args.height)
            return px, py, aim_x, aim_y

        print("\n[第 3 轮] 验收（新模型 + 本次锚点，等价于 click_pixel 真实路径）")
        ok3, _m3 = g.collect_pass(state, points, stub, z, cmd3, 0.0, 0.0,
                                  args.wait_touch, 2, "验收")
        st3 = g.stats_of(points)
        print(f"        残差（第3轮，补偿后）：{g._fmt_stats('', st3).lstrip(': ')}")
        check("第 3 轮：走完且全部收到上报", ok3)
        max3 = st3.get("max_pt")
        check("补偿后残差 ≤ 1.5pt（补偿方向与幅度都对的证据）",
              max3 is not None and float(max3) <= 1.5,
              f"max={max3}pt  mean={st3.get('mean_pt')}pt")
        if st2:
            check("锚点确实起作用（第3轮明显优于第2轮）",
                  float(max3 or 99) < float(st2.get("max_pt") or 0)
                  and float(st3.get("mean_pt") or 99) < float(st2.get("mean_pt") or 0),
                  f"max {st2.get('max_pt')}→{max3}pt，mean {st2.get('mean_pt')}→{st3.get('mean_pt')}pt")

    print("\n[写盘]（只写到临时副本 data/_mock_hw.json）")
    rc = g.apply_config(tmp, model2, anchors, radius, args.width, args.height, st3,
                        log=lambda s: print("        " + s))
    check("写盘函数返回 0", rc == 0)
    out = json.loads(tmp.read_text(encoding="utf-8")).get("calibration", {})
    check("actuation 已更新", bool((out.get("actuation") or {}).get("ax")))
    expect_range = g.arm_range_from_model(model2, args.width, args.height)[0] if model2 else None
    check("arm_range 与模型一致", out.get("arm_range") == expect_range, f"{out.get('arm_range')}")
    pts_out = (out.get("tap_correction") or {}).get("points") or []
    check("锚点写入正确（本工具锚点 + 保留其它来源）", len(pts_out) >= len(anchors),
          f"配置里 {len(pts_out)} 个（本次 {len(anchors)} 个）")
    check("radius_px 已写入",
          abs(float((out.get("tap_correction") or {}).get("radius_px") or 0) - radius) < 0.01)
    check("已生成备份文件", tmp.with_suffix(".json.bak_grid").is_file())
    check("真实配置一个字节都没动",
          hashlib.md5(real.read_bytes()).hexdigest() == real_md5)

    stop.set()
    httpd.shutdown()
    for f in (tmp, tmp.with_suffix(".json.bak_grid")):
        try:
            f.unlink()
        except OSError:
            pass
    if errs:
        print("\n  假手机上报异常：" + "；".join(errs[:3]))
    unseen = state.touch_count - len(seen)
    if unseen:
        print(f"  （提示：假手机上报 {len(seen)} 次，服务端记到 {state.touch_count} 次）")

    print()
    print("=" * 76)
    if fails:
        print(f"  ❌ 自测未通过：{len(fails)} 项失败 → {fails}")
        return 1
    print("  ✅ 自测全部通过：协议 / 采样 / 拟合 / 两层补偿分工 / 写盘 / 备份 均正确")
    print("     下一步：真机上跑 `calibrate_full_grid.py`（可先加 --dry 验连通性）")
    return 0


def logging_setup() -> None:
    import logging
    logging.basicConfig(level=logging.WARNING,   # 自测时压掉逐点 INFO，只看断言
                        format="%(levelname)s %(message)s")


def main() -> int:
    _safe_stdio()
    ap = argparse.ArgumentParser(description="整屏标定免手机自测（假手机 + 假机械臂）")
    ap.add_argument("--pipeline", action="store_true", help="端到端自测（默认模式）")
    ap.add_argument("--client", default="", help="只当假手机连真服务，如 http://127.0.0.1:8767")
    ap.add_argument("--seconds", type=float, default=15.0, help="--client 模式持续时间")
    ap.add_argument("--bias", default="5,-3", help="注入的线性系统偏差 dx,dy（默认 5,-3）")
    ap.add_argument("--noise", type=float, default=3.0, help="注入的逐点局部误差幅度（默认 3pt）")
    ap.add_argument("--cols", type=int, default=5)
    ap.add_argument("--rows", type=int, default=9)
    ap.add_argument("--margin", type=float, default=0.06)
    ap.add_argument("--port", type=int, default=8799, help="自测端口（默认 8799，避开 8767）")
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    ap.add_argument("--wait-touch", type=float, default=3.0)
    args = ap.parse_args()

    try:
        bx, by = [float(v) for v in str(args.bias).split(",", 1)]
    except Exception:  # noqa: BLE001
        print("--bias 需要形如 5,-3")
        return 2
    args.bias = (bx, by)

    logging_setup()
    if args.client:
        return client_mode(args.client.rstrip("/"), (args.width, args.height), args.seconds)
    return pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())

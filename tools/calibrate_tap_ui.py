"""界面按钮落点验证（UI 反馈标定）—— 补上墨点画布覆盖不到的区域。

为什么需要：
    墨点标定的画布受 App 遮挡限制（备忘录顶部 y<150 是按钮栏、底部 y>680 是画笔栏），
    屏幕上下边缘**无法用墨点校准**。本工具改用 **App 自身的可见反应**当反馈：
    在当前页面按一下目标按钮，看整屏有没有变化 —— 变了说明触点被 App 收到（落点够）。

实测判据（2026-09-13）：点中 → 整屏变化 6.30%；没点中 → 0.00%。对比度足够大，可靠。

策略：**中心优先 → 逐环外扩 → 首次成功即停**
    大多数按钮点下去会改变页面（详情返回会退出详情页），所以不能在同一页面上反复
    试几十个点。先试 App 本来要点的那个点；没反应再按步长一圈圈往外试，一旦 App
    有反应立刻停下 —— 这个点与期望点的差就是**落点偏差**。

用法（一次一个目标；页面由人工摆好）：
    py -3.11 tools\\calibrate_tap_ui.py --list                 # 看目标清单与准备要求
    py -3.11 tools\\calibrate_tap_ui.py back_to_top --dry      # 只定位+体检，不按
    py -3.11 tools\\calibrate_tap_ui.py back_to_top            # 正式验证（会真的点）
    py -3.11 tools\\calibrate_tap_ui.py city_confirm --wait    # 等你摆好页面按回车

安全：本工具**默认只测量**，结果**追加**写 data/ui_tap_calib.json；
只有显式加 `--apply`（交互菜单里也会问一句"是否应用"）才会把偏差写进
`config/hardware.json::calibration.tap_correction` —— 供运行时在点击前补偿
（`core/devices/arm.py::_tap_correction`，按"半径内最近锚点"生效）。
Z 由 calib_common.ArmClient 硬钳制在 6.2 以内，且 open() 已注册退出保护。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.config_store import ConfigStore  # noqa: E402
from core.devices.imouse_source import ImouseFrameSource  # noqa: E402
from core.vision.imgio import imwrite_unicode  # noqa: E402
from core.vision.matcher import TemplateMatcher  # noqa: E402
from core.vision.ocr import OcrEngine  # noqa: E402
from tools.calib_common import ArmClient  # noqa: E402

# 判定"App 有反应"的阈值：变化像素占比（灰阶差 > 25 记为变化）。
# 实测标定（2026-09-13）：
#   真实"点中" → 1.95% / 2.75% / 4.60% / 6.30% / 6.48%
#   噪声 & "点了但无可见变化"（幂等操作，例如没有已选地区时点清空筛选）→ ≤ 0.13%
# 两组相差 15 倍以上，故阈值取 1.0%；0.15%~1% 之间标为"疑似"，让人工判断。
REACT_RATIO = 0.010   # ≥1.0% 判定"App 有反应"
NOISE_RATIO = 0.0015  # <0.15% 视为噪声；两者之间 = 疑似

DEFAULT_ARM_URL = "http://127.0.0.1:8082/MyWcfService/getstring"
DEFAULT_ARM_COM = "COM4"

# 「已选地区」那一行的归一化 ROI（含红色城市名与删除 ×）。
# 实测位置（375x812）：标题「已选地区（最多选3个）」y≈258、城市名与 × 行 y≈295、
# 下一行「历史地区」y≈342 —— **必须排除**（历史地区里也可能有红字）→ 取 y∈[0.29,0.41]。
# 必须定义在 TARGETS 之前：下面的目标表要引用它。
CITY_SELECTED_ROI = (0.0, 0.29, 1.0, 0.41)

# 五个目标。locate/precheck 里的 template:xxx 走模板匹配，text:词@(l,t,r,b) 走 OCR。
TARGETS = {
    "back_to_top": dict(
        name="列表页「返回顶部」",
        setup="把列表页**滑到较深处**，让右下角红色「返回顶部」按钮出现",
        locate="template:back_to_top",
        precheck="template:back_to_top",
        z_delta=0.0,
        feedback="页面滚回顶部（整屏大幅变化）",
    ),
    "source_red": dict(
        name="列表页「全国货源」Tab（此刻是白色那个）",
        setup="先切到「新货」Tab（此时底部「全国货源」显示为白色）；"
              "**已在列表页时点它不会有任何变化**",
        # ⚠️ 定位必须用 source_white：Tab 位置不动、只是颜色随选中态变化。
        # 在「新货」页时「全国货源」是白的 → 要点的就是它（与 App 一致：
        # core/flow/handlers.py::handle_home_other 点 source_white 回订单列表）。
        # 2026-09-13：一开始错写成 source_red，导致体检通过但定位必然失败。
        locate="template:source_white",
        precheck="template:source_white",
        z_delta=0.0,
        feedback="切回红色「全国货源」列表（Tab 变红 + 列表刷新）",
    ),
    "city_clear": dict(
        name="城市面板「清空筛选」",
        setup="城市面板已展开，且**已有 1~2 个已选地区**（没有选中时清空无变化）",
        locate="text:清空筛选@(0.0,0.84,0.5,1.0)",
        # 体检 + 判据都用**「确认按钮是否正红」**（用户 2026-09-13 提出的"看已选地区状态"
        # 口径的最稳实现）：有已选城市时确认按钮正红、清空后变浅红。
        # 为什么不用"找已选地区那行的红色城市字"：实测那行位置会随面板滚动而变（固定 ROI
        # 抓不准），而且城市字不是"正红"（过不了 R>180&G<120&B<120）→ 复用 App 自己的
        # confirm_button_clickable 判据最可靠（它是被实机验证过的）。
        precheck="confirm_red",
        z_delta=0.0,
        feedback="确认按钮由正红变浅红（= 已选地区被清空）",
        feedback_state="confirm_red",
    ),
    "city_confirm": dict(
        name="城市面板「确认」（正红）",
        setup="城市面板已展开，且**已选中至少一个城市**（此时确认按钮才是正红可点）",
        locate="text:确认@(0.0,0.62,1.0,0.99)",
        precheck="confirm_red",
        z_delta=0.2,
        feedback="面板收起 / 回到列表页",
    ),
    "detail_back": dict(
        name="详情页「返回」箭头",
        setup="手机停在**订单详情页**（左上角有返回箭头）",
        locate="template:detail_back",
        precheck="template:detail_share",
        z_delta=0.0,
        feedback="回到列表页",
    ),
}


def norm_to_roi(w: int, h: int, norm: Sequence[float]) -> Tuple[int, int, int, int]:
    left, top, right, bottom = [float(v) for v in norm]
    x0, y0 = int(round(left * w)), int(round(top * h))
    x1, y1 = int(round(right * w)), int(round(bottom * h))
    return (x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))


def red_ratio(raw, norm_roi: Sequence[float]) -> float:
    """ROI 内「红色像素」占比。

    判据与 core/flow/city_picker.py::confirm_button_clickable 完全一致（避免两套口径）：
    正红 = R>180 且 G<120 且 B<120；浅红（如浅红确认按钮 RGB≈[249,193,202]）不算红。
    帧为 BGR（cv2 约定）。
    """
    h, w = raw.shape[:2]
    x0, y0, rw, rh = norm_to_roi(w, h, norm_roi)
    patch = raw[y0 : y0 + rh, x0 : x0 + rw].astype("int16")
    if patch.size == 0:
        return 0.0
    b, g, r = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
    return float(((r > 180) & (g < 120) & (b < 120)).mean())


def diff_ratio(a, b) -> float:
    """两帧「明显变化」像素占比（灰阶差 > 25 记为变化）。"""
    if a is None or b is None or a.shape != b.shape:
        return -1.0
    d = cv2.absdiff(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), cv2.cvtColor(b, cv2.COLOR_BGR2GRAY))
    return float((d > 25).mean())


class UiTapChecker:
    def __init__(self, store: ConfigStore, log=print) -> None:
        self._store = store
        self._log = log
        self._ocr = OcrEngine(store, log=lambda lv, m: None)
        self._matcher = TemplateMatcher(store, log=lambda lv, m: None)
        cal = store.get("hardware", "calibration", {}) or {}
        act = cal.get("actuation") or {}
        self._ax = [float(v) for v in act.get("ax", [0.0, 1.0, 0.0, 0.0])]
        self._ay = [float(v) for v in act.get("ay", [0.0, 0.0, 1.0, 0.0])]
        self._z = float(cal.get("z_press", 5.9) or 5.9)

    # ---------------------------------------------------------------- 定位 / 体检

    def _locate(self, fs, spec: str) -> Tuple[Optional[Tuple[int, int]], str]:
        """按 spec 定位目标中心。返回 ((x,y), 说明) 或 (None, 失败原因)。"""
        raw = fs.raw
        h, w = raw.shape[:2]
        if spec.startswith("template:"):
            name = spec.split(":", 1)[1]
            tpl = self._store.template(name)
            if tpl is None:
                return None, f"模板 {name} 未注册"
            hit = self._matcher.find(fs.fast, tpl)
            if hit is None:
                return None, f"模板 {name} 未命中"
            return (int(hit.cx), int(hit.cy)), f"模板 {name} score={hit.score:.2f}"
        if spec.startswith("text:"):
            body = spec.split(":", 1)[1]
            word, roi_txt = body.split("@", 1)
            norm = [float(v) for v in roi_txt.strip("()").split(",")]
            roi = norm_to_roi(w, h, norm)
            x0, y0, rw, rh = roi
            cands = [
                b
                for b in self._ocr.ocr(raw)
                if word in (b.text or "")
                and x0 <= b.center[0] <= x0 + rw
                and y0 <= b.center[1] <= y0 + rh
            ]
            if not cands:
                return None, f"OCR 在 ROI 内没找到「{word}」"
            cands.sort(key=lambda b: b.rect[1])  # 与 App 一致：取最靠上的
            b = cands[0]
            return b.center, f"OCR「{b.text}」rect={b.rect}"
        return None, f"未知定位方式 {spec}"

    def _precheck(self, fs, spec: str) -> Tuple[bool, str]:
        if spec == "confirm_red":
            from core.flow.city_picker import confirm_button_clickable

            ok = bool(confirm_button_clickable(self._store, fs))
            return ok, "确认按钮正红可点" if ok else "确认按钮是浅红（没有选中城市，点了也没反应）"
        if spec == "red_chips":
            r = red_ratio(fs.raw, CITY_SELECTED_ROI)
            ok = r > 0.005
            return ok, (
                f"已选地区有红色城市字（红像素占比 {r * 100:.2f}%）"
                if ok
                else f"已选地区**没有**红色城市字（占比 {r * 100:.2f}%）"
                     "→ 先手动点 1~2 个城市，否则清空无任何变化"
            )
        pos, why = self._locate(fs, spec)
        return pos is not None, why

    def _confirm_red(self, fs) -> bool:
        """「确认」按钮是否正红（= 当前有已选城市）。复用项目自己的判据。"""
        from core.flow.city_picker import confirm_button_clickable

        try:
            return bool(confirm_button_clickable(self._store, fs))
        except Exception:  # noqa: BLE001
            return False

    def to_arm(self, x: float, y: float, w: int, h: int) -> Tuple[float, float]:
        u, v = x / w, y / h
        ax = self._ax[0] + self._ax[1] * u + self._ax[2] * v + self._ax[3] * u * v
        ay = self._ay[0] + self._ay[1] * u + self._ay[2] * v + self._ay[3] * u * v
        return ax, ay

    # ---------------------------------------------------------------- 主流程

    def check(self, key: str, args) -> int:
        spec = TARGETS[key]
        src = ImouseFrameSource(self._store)
        if not src.open():
            self._log("★ iMouse 取帧源打不开")
            return 1
        fs = src.read()
        if fs is None:
            self._log("★ 取帧失败")
            return 1
        h, w = fs.raw.shape[:2]

        self._log(f"目标：{spec['name']}")
        self._log(f"准备要求：{spec['setup']}")
        self._log(f"成功判据：{spec['feedback']}（整屏变化 > {REACT_RATIO * 100:.1f}%）")

        ok, why = self._precheck(fs, spec["precheck"])
        self._log(f"体检：{'✓ 通过' if ok else '✗ 不通过'} —— {why}")
        pos, how = self._locate(fs, spec["locate"])
        if pos is None:
            self._log(f"★ 定位失败：{how}")
            src.close()
            return 2
        cx, cy = pos
        ax, ay = self.to_arm(cx, cy, w, h)
        z = self._z + float(spec["z_delta"])
        self._log(f"定位：{how} → 期望落点 ({cx},{cy})，映射机械臂 ({ax:.2f},{ay:.2f})，下压 z={z:.1f}")

        if not ok:
            self._log("提示：先按上面的准备要求摆好页面（体检不通过时点了也不会有反应）")
        if args.dry:
            self._log("--dry：只定位体检，不按。")
            src.close()
            return 0
        if args.wait:
            try:
                input("页面摆好后按回车开始扫点 …")
            except EOFError:
                pass

        # 中心优先 + 逐环外扩
        step, radius = int(args.step), int(args.radius)
        offs = [(dx, dy) for dx in range(-radius, radius + 1, step)
                for dy in range(-radius, radius + 1, step)]
        offs.sort(key=lambda o: (max(abs(o[0]), abs(o[1])), abs(o[0]) + abs(o[1])))

        arm = ArmClient(args.arm_url, com=args.arm_com)
        try:
            arm.open()
        except Exception as exc:  # noqa: BLE001 串口被占时给一次机会（可能弹 UAC）
            self._log(f"★ 打开串口失败：{exc}；重试一次（自动重启 JxbService）…")
            arm.open(auto_restart=True)
        arm.reset()
        time.sleep(0.8)

        settle = float(self._store.get("hardware", "delays.tap_settle", 0.15) or 0.15)
        dwell = float(self._store.get("hardware", "delays.tap_dwell", 0.15) or 0.15)

        trials: List[Tuple[int, int, float]] = []
        hit = None
        hit_off = None
        for dx, dy in offs:
            tx, ty = cx + dx, cy + dy
            before = src.read()
            if before is None:
                continue
            if not args.no_jitter:
                tx += random.randint(-1, 1)
                ty += random.randint(-1, 1)
            sax, say = self.to_arm(tx, ty, w, h)
            arm.move(sax, say)
            time.sleep(settle)
            arm.press(z)
            time.sleep(dwell)
            arm.release()
            time.sleep(float(args.settle_after))
            after = src.read()
            ratio = diff_ratio(before.raw, after.raw if after is not None else None)
            trials.append((dx, dy, ratio))
            region = spec.get("feedback_region")
            if spec.get("feedback_state") == "confirm_red":
                # 语义判据（最可靠）：清空成功的标志 = 确认按钮从正红变浅红。
                s0 = self._confirm_red(before)
                s1 = self._confirm_red(after) if after is not None else s0
                reacted = s0 and not s1
                detail = f"确认按钮正红 {s0} → {s1}"
            elif region is not None:
                # 语义判据（用户 2026-09-13 口径）：清空后「已选地区」那行的红色城市字消失。
                # 比整屏差分准得多 —— 噪声/渲染抖动只有 ~0.1%，而从有红字到没有是明确跳变。
                r0 = red_ratio(before.raw, region)
                r1 = red_ratio(after.raw, region) if after is not None else -1.0
                reacted = r0 > 0.005 and 0.0 <= r1 < max(0.003, r0 * 0.3)
                detail = f"已选地区红字 {r0 * 100:5.2f}% → {r1 * 100:5.2f}%"
            else:
                reacted = ratio > REACT_RATIO
                detail = f"整屏变化 {ratio * 100:5.2f}%"
            if reacted:
                mark = "✓ 有反应"
            elif ratio > REACT_RATIO:
                mark = "? 整屏变了但语义判据未通过（请人工确认）"
            elif ratio > NOISE_RATIO:
                mark = "? 疑似（噪声带）"
            else:
                mark = "·"
            self._log(f"  Δ({dx:+3d},{dy:+3d}) @({tx},{ty}) {detail}（整屏 {ratio * 100:5.2f}%）  {mark}")
            if reacted:
                hit = (tx, ty)
                hit_off = (dx, dy)
                break
            # 页面若已被这次点击改变（非预期的副作用），后面的点就不可信了
            if ratio > 0.05:
                self._log("  （变化较大但未达判定；若页面已切走请重新摆好再跑）")

        src.close()
        try:
            arm.reset()
            arm.close()
        except Exception:  # noqa: BLE001
            pass

        self._log("")
        if hit is None:
            self._log(
                f"结论：±{radius}pt 内没找到可触发点 —— 要么前置条件没满足（看上面的体检），"
                f"要么该按钮当前不可点。"
            )
        elif hit_off == (0, 0):
            self._log(f"结论：**期望点 ({cx},{cy}) 本身即可触发 → 该按钮落点达标，无需补偿** ✓")
        else:
            dx, dy = hit_off
            dist = (dx * dx + dy * dy) ** 0.5
            self._log(
                f"结论：期望点 ({cx},{cy}) **未触发**；最近可触发点 {hit}（偏移 Δ({dx:+d},{dy:+d})，"
                f"约 {dist:.1f}pt，受 {step}pt 步长量化）→ 建议补偿：该区域点击点朝 ({dx:+d},{dy:+d}) 方向挪。"
            )
        self._log(
            "说明：本目标多为**一次性动作**（点成功一次后状态就变了），所以一次运行 = 一次测量；"
            "要重复测量请重新摆页面再跑，或改用可重复探针（如列表页红色「全国货源」= 刷新，可反复点）。"
        )

        rec = dict(target=key, name=spec["name"], when=time.strftime("%Y-%m-%dT%H:%M:%S"),
                   expected=[cx, cy], hit=list(hit) if hit else None,
                   offset=[hit[0] - cx, hit[1] - cy] if hit else None,
                   z=z, step=step, radius=radius, trials=trials)
        out = Path("data/ui_tap_calib.json")
        try:
            old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
            old.append(rec)
            out.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
            self._log(f"记录已追加：{out}")
        except Exception as exc:  # noqa: BLE001 记录失败不影响结论
            self._log(f"（记录写入失败：{exc}）")
        if getattr(ns, "apply", False):
            self._log("")
            self._log("—— 按 --apply 把本次偏差写入配置 ——")
            apply_corrections(only=key, log=self._log)
        return 0


def menu() -> int:
    """交互式菜单：页面由人工摆好，选一个目标就跑一轮验证。

    为什么菜单放在 Python 而不是 bat：bat 里写中文会因编码/全角括号被 cmd 误解析
    （2026-09-13 实测：UTF-8 的 bat 里中文括号把菜单搅乱、选项 2/3/5 整行被吃掉）。
    Python 输出走 chcp 65001 能正确显示中文，bat 只留 ASCII 启动器，两边都稳。
    """
    keys = list(TARGETS)
    store = ConfigStore()
    while True:
        print()
        print("=" * 64)
        print("  界面按钮落点验证      robot_order_picker_imouse")
        print("=" * 64)
        print("  先按下面说明把手机页面摆好，再选数字；选中后按提示回车开始。\n")
        for i, k in enumerate(keys, 1):
            v = TARGETS[k]
            print(f"  {i}. {v['name']}")
            print(f"       准备：{v['setup']}")
            print(f"       判据：{v['feedback']}")
        print("  " + "-" * 60)
        print("  0. 退出")
        try:
            ch = input("  请输入数字后回车: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if ch in ("", "0", "q", "Q"):
            return 0
        if not ch.isdigit() or not 1 <= int(ch) <= len(keys):
            print("  ✗ 无效输入，请重新选")
            continue
        key = keys[int(ch) - 1]
        ns = argparse.Namespace(
            dry=False, wait=True, step=6, radius=18, settle_after=0.7,
            no_jitter=False, arm_url=DEFAULT_ARM_URL, arm_com=DEFAULT_ARM_COM,
        )
        try:
            UiTapChecker(store).check(key, ns)
        except Exception as exc:  # noqa: BLE001 单个目标失败不该把菜单打死
            print(f"  ✗ 本轮异常：{type(exc).__name__}: {exc}")
        # 菜单里问一句是否应用（默认 Y）：这样"走一遍校准"就能一步到位，
        # 又不会在无人值守/自动化场景下擅自改配置（--menu 之外一切照旧只测量）。
        try:
            ans = input("  是否把本次偏差应用到配置(tap_correction)？(Y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if ans in ("", "y", "yes"):
            apply_corrections(only=key)
        try:
            input("\n  按回车回到菜单 …")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0


# ============================================================ 补偿写回（--apply）
# 只有显式 --apply / 菜单确认才会走到这里；默认路径永远只测量。
MIN_APPLY_PT = 2   # |dx|/|dy| 小于它视为噪声（受 6pt 步长量化），不建锚点


def _hardware_path() -> Path:
    """定位 config/hardware.json（打包后 = exe 同级；源码 = 仓库根）。"""
    try:
        from core.config_store import PROJECT_ROOT  # noqa: PLC0415 - 冻结感知，按需导入
        return Path(PROJECT_ROOT) / "config" / "hardware.json"
    except Exception:  # noqa: BLE001
        return Path("config/hardware.json")


def _records_path() -> Path:
    """定位测量记录。优先与 hardware.json 同一个根（PROJECT_ROOT），再退回 CWD。

    为什么要同根：否则"从仓库跑 py tools\\... --apply-all"会读到仓库里的旧记录
    （旧映射时期的遗留），把刚测好的锚点覆盖掉。
    """
    root_p = _hardware_path().parent.parent / "data" / "ui_tap_calib.json"
    cwd_p = Path("data/ui_tap_calib.json")
    for p in (root_p, cwd_p):
        if p.exists():
            return p
    return root_p


def latest_offsets() -> dict:
    """每个目标取最新一条测量 → {target: (exp_x, exp_y, dx, dy, when)}。

    记录是追加写的，同目标后面的覆盖前面的（最后一次测量生效）。
    "期望点本身即可触发"（没有 offset）会记为 (0,0)，调用方据此**删除**历史锚点（自愈）。
    """
    out: dict = {}
    try:
        recs = json.loads(_records_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return out
    for r in recs or []:
        t = r.get("target")
        exp = r.get("expected")
        if not t or not isinstance(exp, (list, tuple)) or len(exp) != 2:
            continue
        off = r.get("offset")
        dx, dy = (float(off[0]), float(off[1])) if isinstance(off, (list, tuple)) and len(off) == 2 else (0.0, 0.0)
        out[str(t)] = (float(exp[0]), float(exp[1]), dx, dy, str(r.get("when") or ""))
    return out


def apply_corrections(only: Optional[str] = None, log=print) -> int:
    """把实测偏差写进 hardware.json::calibration.tap_correction（带备份 + diff 打印）。

    与运行时 `core/devices/arm.py::_tap_correction` 的"最近锚点"约定配套：
      * 只对 |dx| 或 |dy| ≥ MIN_APPLY_PT 的目标建锚点（1pt 级属噪声，不入表）
      * 期望点本身即可触发的目标 → 删除其历史锚点（配置自愈，避免旧补偿残留）
      * 锚点坐标写**期望点**（= 程序实际会点的位置），偏移量写 dx/dy
    这是本工具唯一的写配置入口，必须由 --apply / 菜单确认显式触发。
    """
    cfg_p = _hardware_path()
    if not cfg_p.is_file():
        log(f"[失败] 找不到配置：{cfg_p}")
        return 1
    data = latest_offsets()
    if not data:
        log(f"[失败] 没有可用的测量记录：{_records_path()}")
        return 1
    if only:
        if only not in data:
            log(f"[失败] 记录里没有目标 {only}（先跑一次测量再来应用）")
            return 1
        data = {only: data[only]}

    cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
    cal = cfg.setdefault("calibration", {})
    tc = cal.get("tap_correction")
    if not isinstance(tc, dict):
        tc = {}
    tc.setdefault("enabled", True)
    tc.setdefault("radius_px", 40)
    pts = [dict(p) for p in (tc.get("points") or []) if isinstance(p, dict)]
    n_before = len(pts)

    def _upsert(name: str, x: float, y: float, dx: float, dy: float) -> None:
        for p in pts:
            if p.get("name") == name:
                p.update({"x": x, "y": y, "dx": dx, "dy": dy, "src": "ui_tap_calib"})
                return
        pts.append({"name": name, "x": x, "y": y, "dx": dx, "dy": dy, "src": "ui_tap_calib"})

    def _remove(name: str) -> None:
        for i in range(len(pts) - 1, -1, -1):
            if pts[i].get("name") == name:
                del pts[i]

    log("  目标            期望点         实测偏移     处理")
    log("  " + "-" * 60)
    for name, (x, y, dx, dy, when) in sorted(data.items()):
        if abs(dx) >= MIN_APPLY_PT or abs(dy) >= MIN_APPLY_PT:
            _upsert(name, x, y, dx, dy)
            log(f"  {name:14s} ({x:5.0f},{y:5.0f})  ({dx:+.0f},{dy:+.0f})   写入/更新锚点  [{when}]")
        else:
            _remove(name)
            log(f"  {name:14s} ({x:5.0f},{y:5.0f})  ({dx:+.0f},{dy:+.0f})   "
                f"无需补偿(<{MIN_APPLY_PT}pt) → 清除旧锚点  [{when}]")

    tc["points"] = pts
    cal["tap_correction"] = tc

    bak = cfg_p.with_suffix(".json.bak_tapcorr")
    try:
        if not bak.exists():
            bak.write_bytes(cfg_p.read_bytes())
        tmp = cfg_p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(cfg_p)
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] 写配置失败：{exc}")
        return 1

    log("  " + "-" * 60)
    log(f"  已写入 {cfg_p}（备份 {bak.name}）；enabled={tc.get('enabled')}，锚点 {n_before} → {len(pts)}")
    log("  提示：运行中的程序会热重载该配置；重打包前记得把 dist 的 config 拷回仓库。")
    return 0


def main() -> int:
    """命令行入口。"""
    ap = argparse.ArgumentParser(description="界面按钮落点验证（UI 反馈标定，只读不改配置）")
    ap.add_argument("target", nargs="?", choices=sorted(TARGETS), help="要验证的目标")
    ap.add_argument("--list", action="store_true", help="列出全部目标与准备要求")
    ap.add_argument("--dry", action="store_true", help="只定位+体检，不按任何点")
    ap.add_argument("--wait", action="store_true", help="开始扫点前等你摆好页面按回车")
    ap.add_argument("--step", type=int, default=6, help="扫点步长 px（默认 6）")
    ap.add_argument("--radius", type=int, default=24,
                    help="扫点最大半径 px（默认 24：实测底部区域偏差可达 ~20pt，±18 会刚好扫不到）")
    ap.add_argument("--z", type=float, default=None, help="覆盖基础下压深度（默认取 config）")
    ap.add_argument("--settle-after", type=float, default=0.7, help="抬手后等待并取帧的秒数")
    ap.add_argument("--no-jitter", action="store_true", help="关闭 ±1px 随机抖动")
    ap.add_argument("--arm-url", default=DEFAULT_ARM_URL)
    ap.add_argument("--arm-com", default=DEFAULT_ARM_COM)
    ap.add_argument("--menu", action="store_true", help="交互式菜单（供 界面按钮标定.bat 调用）")
    ap.add_argument("--apply", action="store_true",
                    help="把本次测得的偏差写进 config/hardware.json 的 tap_correction（默认只测量）")
    ap.add_argument("--apply-all", action="store_true",
                    help="不按压：直接把 data/ui_tap_calib.json 里各目标的最新偏差写进配置")
    args = ap.parse_args()

    if args.apply_all:
        print("按 data/ui_tap_calib.json 的最新测量结果更新 tap_correction：\n")
        return apply_corrections(None)

    if args.menu:
        return menu()

    if args.list or not args.target:
        print("可用目标（一次一个；页面由人工摆好）：\n")
        for k, v in TARGETS.items():
            print(f"  {k:14s} {v['name']}")
            print(f"     准备：{v['setup']}")
            print(f"     判据：{v['feedback']}（下压 +{v['z_delta']:.1f}）\n")
        print("例：py -3.11 tools\\calibrate_tap_ui.py back_to_top --wait")
        return 0

    store = ConfigStore()
    if args.z is not None:
        pass  # 由 checker 内部用 --z 覆盖
    checker = UiTapChecker(store)
    if args.z is not None:
        checker._z = float(args.z)  # noqa: SLF001 临时覆盖，仅本工具内生效
    return checker.check(args.target, args)


if __name__ == "__main__":
    sys.exit(main())

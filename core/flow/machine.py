"""状态机主循环：取帧 → 判定 → 分发 → 复判。

设计原则：
    * 主循环**不做具体动作**，只负责「当前是什么态 → 交给谁处理」；
      新增页面分支时只动 handlers，主循环不动；
    * 每一轮都重新取帧判定，绝不跨帧复用判定结果（手机画面随时在变）；
    * 异常一律就地恢复（摄像头/机械臂/未知页），恢复不了才停下并留下证据截图；
    * 停止信号来自 GUI 的 stop_event，循环里所有等待都是可中断的。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from core.devices.arm import ArmError
from core.devices.frame_source import FrameSourceError
from core.domain.route_plan import RoutePlan, selection_steps
from core.flow.city_picker import CityPicker
from core.flow.context import FlowContext
from core.flow import handlers
from core.vision.page_state import PageResult, ScreenState

LogFn = Optional[Callable[[str, str], None]]


class FlowMachine:
    """抢单主流程状态机。"""

    def __init__(self, ctx: FlowContext) -> None:
        self.ctx = ctx
        self.picker = CityPicker(ctx.kit, ctx.regions, ctx.store, stop_event=ctx.stop_event)
        self._last_state: Optional[ScreenState] = None
        self._setup_pending: bool = False

    # ---------------------------------------------------------------- 路线设置

    def setup_routes(self, plan: Optional[RoutePlan] = None) -> bool:
        """给当前路线设置始发地/目的地。失败到上限即停（多半是入口偏移失效）。"""
        plan = plan or self.ctx.plan
        if plan is None or plan.current is None:
            self.ctx.emit("warning", "没有可设置的路线")
            return False
        route = plan.current
        self.ctx.emit("info", f"开始设置路线: {route.describe()}")

        if not self._ensure_list_ready():
            return False
        if not self.picker.select_side("origin", selection_steps(route.origin)):
            self._count_setup_fail("origin")
            return False
        if not self._ensure_list_ready():
            return False
        if not self.picker.select_side("dest", selection_steps(route.dest)):
            self._count_setup_fail("dest")
            return False

        _, res = self.ctx.kit.wait_state(handlers.LIST_STATES, timeout=6.0, interval=0.5)
        ok = res.state in handlers.LIST_STATES
        # 设置后复核：只「回到列表页」不等于筛选真的应用了——城市面板点红字收起
        # 可能只是收面板、没提交选择（假成功）。展开筛选条读一次核对目标城市，
        # 不符就告警 + 存现场 + 计入连续失败（触发既有重试/停止链，不无限空转）。
        if ok and not self._verify_route_applied(route):
            self._count_setup_fail("verify")
            ok = False
        self.ctx.bump("route_setup_ok" if ok else "route_setup_fail")
        if ok:
            self.ctx.reset("setup_fail")
        # 路线设置完，若列表直接就是「推荐货源」（该路线无订单，App 推荐其他货源），
        # 立即切下一条路线——继续在空列表上扫是浪费时间。复用「列表到底→换路线」逻辑。
        if ok and self._route_has_no_order():
            self.ctx.emit("info", f"路线 {route.describe()} 无订单（列表直接推荐货源），切换下一条路线")
            handlers._next_route_or_finish(self.ctx)
            return False  # 已切路线：主循环下一轮用新路线重设（route_setup_pending 已置起）
        return ok

    def _verify_route_applied(self, route) -> bool:
        """设置后复核：读一次顶部筛选条，核对出发地/目的地是否真的变成目标路线。

        2026-09-12 新增。此前只以「回到列表页」判成功，无法发现「确认未真正应用」
        （city_picker._confirm 里点顶部红字收起 = 只收面板不提交）。复核不通过则存
        现场并告警，由调用方计入 setup_fail 走既有重试/停止链。
        取不到帧 / OCR 不可用时按「通过」处理（不误判失败、不中断流程）。
        """
        try:
            fs = self.ctx.kit.frame()
            if fs is None:
                return True
            matched, text = handlers.filter_bar_matches_route(self.ctx, fs, route)
            if matched:
                self.ctx.emit("info", f"路线设置复核通过：筛选条={text!r}")
                return True
            self.ctx.emit(
                "error",
                f"路线设置复核失败：筛选条={text!r} 与目标 {route.describe()} 不符"
                "（疑似城市面板只收起未提交），已存现场并重试",
            )
            self._save_setup_debug(fs, "route_setup_verify_fail")
            return False
        except Exception as exc:  # noqa: BLE001 复核异常不误判失败
            self.ctx.emit("warning", f"路线设置复核异常（按通过处理）：{exc}")
            return True

    def _save_setup_debug(self, fs, tag: str) -> None:
        """存诊断现场（不受 trace 抽样影响；无 trace 时退回 save_debug）。"""
        tr = getattr(self.ctx, "trace", None)
        try:
            if tr is not None and getattr(tr, "enabled", False):
                tr.shot(fs.raw, tag, always=True)
                return
        except Exception:  # noqa: BLE001 截图失败绝不打断流程
            pass
        try:
            kit = self.ctx.kit
            if hasattr(kit, "save_debug"):
                kit.save_debug(fs, tag)
        except Exception:  # noqa: BLE001
            pass

    def _route_has_no_order(self) -> bool:
        """检测路线设置完列表是否「推荐货源」（该路线无订单）。整屏 OCR 一次。"""
        fs = self.ctx.kit.frame()
        if fs is None or getattr(self.ctx.kit, "ocr", None) is None:
            return False
        try:
            boxes = self.ctx.kit.ocr_frame(fs)
            text = "".join(getattr(b, "text", "") or "" for b in boxes)
        except Exception:  # noqa: BLE001 OCR 失败不阻断
            return False
        for kw in ("推荐货源", "暂无货源", "没有货源"):
            if kw in text:
                return True
        return False

    def _count_setup_fail(self, side: str) -> None:
        streak = self.ctx.bump("setup_fail")
        self.ctx.emit("error", f"{side} 设置失败（第 {streak} 次）")
        if streak >= self.ctx.limit("setup_enter_fail_max", 3):
            self.ctx.emit("error", "路线设置连续失败，停止运行（请核对入口锚点与偏移标定）")
            self.ctx.stop("路线设置连续失败（请核对入口锚点与偏移标定）")

    def _ensure_list_ready(self) -> bool:
        """设置路线前必须真的在列表页且顶部筛选条已展开。"""
        _, res = self.ctx.kit.wait_state(handlers.LIST_STATES, timeout=8.0, interval=0.6)
        if res.state not in handlers.LIST_STATES:
            self.ctx.emit("error", f"不在列表页（{res.state.value}），无法设置路线")
            return False
        if not handlers.ensure_top_visible(self.ctx, res):
            self.ctx.emit("error", "顶部筛选条无法展开，路线设置中止（请核对列表页顶部是否可见、entry_anchor 偏移是否仍准确）")
            return False
        # 关听单必须**前置到设路线之前**（2026-09-08 用户指出流程颠倒）：
        # 原流程是先 setup_routes 再走 handle_order_list 里的 ensure_tingdan_off，
        # 于是设置路线的全程听单都是开着的——听单会接管页面并持续刷新列表，
        # 设路线时界面跳动会导致入口/网格点不准。ensure_tingdan_off 内部幂等
        # （已关则直接返回 True），此处与列表页首次进入那次重复调用无副作用。
        # 关不掉只告警不中止：听单开着仍能设路线，中止反而让流程卡死。
        if not handlers.ensure_tingdan_off(self.ctx, res):
            self.ctx.emit("warning", "听单未能关闭，继续设置路线（可能受自动刷新干扰）")
        return True

    # ---------------------------------------------------------------- 主循环

    def run(self, setup_first: bool = True) -> None:
        """主循环。GPU/真机异常一律就地恢复，恢复不了才退出。"""
        ctx = self.ctx
        ctx.start_run()
        if ctx.watchdog is not None:
            ctx.watchdog.reset()   # 新一轮：别拿上一轮的计数判这一轮
        warmup = float(ctx.store.get("runtime", "loop.startup_warmup_sec", 2.0) or 2.0)
        self.ctx.emit("info", f"主循环启动（预热 {warmup:.1f}s）")
        ctx.sleep(warmup)

        interval = float(ctx.store.get("runtime", "loop.frame_interval_sec", 0.35) or 0.35)
        # 路线设置推迟到「真正进入列表页之后」再做：否则一开始还在桌面/未知页时
        # setup_routes 会因 wait_state 超时直接失败且永不重试（用户实测反馈的坑）。
        self._setup_pending = bool(
            setup_first and ctx.plan is not None and ctx.plan.current is not None
        )
        while not ctx.stopped:
            # 优雅停止：外部写 data/stop.flag 即触发（替代强杀进程，避免串口句柄
            # 泄漏导致 COM4 卡死需手动重启 JxbService）。检测到即删标志并收工。
            _stop_flag = Path(ctx.store.root) / "data" / "stop.flag"
            if _stop_flag.exists():
                _stop_flag.unlink(missing_ok=True)
                ctx.stop("外部停止标志")
                break
            try:
                # 详情页候选合格后暂停等人工：机器臂停在原地，画面停在详情页，
                # 不取帧不判定，直到 GUI 点「继续找单」或收到停止信号。
                if ctx.paused:
                    ctx.wait_resume()
                    continue
                t_iter = time.perf_counter()
                self._check_config()
                fs = ctx.kit.frame()
                if fs is None:
                    self._recover_source()
                    continue
                res = ctx.kit.detect(fs)
                self._trace_frame(res)
                self._note_state(res)
                if ctx.watchdog is not None:
                    # 每帧过一遍监护（计数器/滞留/停滞判据）。放这里而不是
                    # dispatch 之后：详情页里会长时间动作，不能等它回来才判。
                    ctx.watchdog.tick(res)
                # 启动阶段：从桌面/未知页导航到列表后，再设置路线。
                # 路线设置是扫描的前置条件——设置失败绝不能进扫描（否则会扫一堆
                # 未限定出发地/目的地的单子）。失败不清 _setup_pending，下一轮重试；
                # 连续失败由 setup_routes 内的 _count_setup_fail 触发停止。加冷却避免
                # 每帧狂点把界面搞乱、反而让顶部筛选条永远渲染不出来。
                # ctx.route_setup_pending：切路线后由 handlers 置起，要求重设城市
                # （否则 App 筛选仍是旧路线，刷出来的还是老货源）。
                if (self._setup_pending or ctx.route_setup_pending) and res.state in handlers.LIST_STATES:
                    if self.setup_routes():
                        self._setup_pending = False
                        ctx.route_setup_pending = False
                    elif not ctx.stopped:
                        ctx.sleep(3.0)
                    continue
                self._dispatch(fs, res)
            except ArmError as exc:
                self.ctx.emit("error", f"机械臂异常: {exc}")
                self._recover_arm()
            except FrameSourceError as exc:
                self.ctx.emit("error", f"取帧源异常: {exc}")
                self._recover_source()
            except Exception as exc:  # 兜底：任何异常都不能让循环静默死掉
                ctx.bump("loop_exception")
                ctx.emit("error", f"主循环异常: {exc}")
                ctx.sleep(1.0)
            ctx.timings["单轮循环"] = (time.perf_counter() - t_iter) * 1000.0
            if self._should_rest():
                self._rest()
            if not ctx.sleep(interval):
                break

        ctx.emit("info", f"主循环结束 | {ctx.summary()}")

    # ---------------------------------------------------------------- 分发

    def _dispatch(self, fs, res: PageResult) -> None:
        ctx = self.ctx
        if res.pending:
            # 判定未稳定（页面正在加载/动画中）：只等下一帧，绝不动手。
            # 这里一旦动起来，就会把正常页面当成未知页折腾——比判不出来更糟。
            ctx.bump("pending_frames")
            return
        state = res.state
        # 单步耗时采集（毫秒）：列表扫描 / 订单详情页，供 GUI「耗时明细」展示。
        # 计时只包住 dispatch 本身（不含 sleep），能直接反映各页面处理开销。
        t0 = time.perf_counter()
        if state is ScreenState.DESKTOP:
            handlers.handle_desktop(ctx, fs, res)
        elif state is ScreenState.HOME_OTHER:
            handlers.handle_home_other(ctx, fs, res)
        elif state in handlers.LIST_STATES:
            handlers.handle_order_list(ctx, fs, res)
        elif state is ScreenState.DETAIL:
            handlers.handle_detail(ctx, fs, res)
        elif state in handlers.CITY_STATES:
            handlers.handle_city_panel(ctx, fs, res)
        elif state in handlers.DIALOG_STATES:
            handlers.handle_dialog(ctx, fs, res)
        elif state is ScreenState.CHAT:
            handlers.handle_chat(ctx, fs, res)
        else:
            handlers.handle_unknown(ctx, fs, res)
        dt = (time.perf_counter() - t0) * 1000.0
        if state is ScreenState.DETAIL:
            ctx.timings["订单详情页"] = dt
        elif state in handlers.CITY_STATES:
            ctx.timings["城市选择"] = dt

    def _trace_frame(self, res: PageResult) -> None:
        """每帧轻量轨迹：只写 JSONL（带 ts/wall 时间戳 + 命中摘要），不刷 GUI。

        用途：前期详细记录，供后期调判定速度/准确度。主循环每帧调用，经
        TraceWriter 异步写盘，不阻塞抢单节奏。
        """
        if self.ctx.trace is not None:
            self.ctx.trace.put("frame", res.summary(), pending=res.pending)

    def _note_state(self, res: PageResult) -> None:
        """同一态连续多轮 → 记一笔（卡死检测，供 GUI 展示与自动升级处理）。"""
        self.ctx.last_state = res.state.value  # GUI 预览按需刷新用（仅列表/详情帧）
        if res.pending:
            return  # 过渡帧不参与卡死统计，否则每屏都会误报「卡住」
        if res.state is self._last_state:
            streak = self.ctx.bump("same_screen_streak")
            limit = self.ctx.limit("same_screen_streak", 5)
            if streak == limit:
                self.ctx.emit("warning", f"连续 {streak} 轮停在 {res.state.value}，可能卡住")
        else:
            self.ctx.reset("same_screen_streak")
            self._last_state = res.state
            self.ctx.emit("info", f"页面状态: {res.summary()}", state=res.state.value)

    # ---------------------------------------------------------------- 恢复

    def _check_config(self) -> None:
        """配置热重载：硬件段变化需重建设备，采集段变化需重开摄像头。"""
        changed = self.ctx.store.reload_if_changed()
        if not changed:
            return
        self.ctx.emit("info", f"配置已热重载: {', '.join(changed)}")
        if self.ctx.store.needs_restart(changed):
            self.ctx.arm.reload_config()
        if self.ctx.store.needs_rebuild_capture(changed):
            # 只有分辨率可变的取帧源（摄像头）才需要重建；采集卡由手机输出决定
            if self.ctx.source.resolution_changed():
                self.ctx.source.close()
                self.ctx.source.reload_config()
                self.ctx.source.open()
            else:
                self.ctx.source.reload_config()

    def _recover_source(self) -> None:
        if self.ctx.stopped:
            return
        for i in range(1, self.ctx.limit("recover_max", 3) + 1):
            self.ctx.emit("warning", f"尝试恢复取帧源 ({i})")
            self.ctx.source.close()
            self.ctx.source.reload_config()
            if self.ctx.source.open():
                return
            self.ctx.sleep(2.0)
        self.ctx.emit("error", "取帧源恢复失败，停止运行")
        self.ctx.stop("取帧源恢复失败")

    def _recover_arm(self) -> None:
        if self.ctx.stopped:
            return
        for i in range(1, self.ctx.limit("recover_max", 3) + 1):
            self.ctx.emit("warning", f"尝试恢复机械臂 ({i})")
            if self.ctx.arm.recover():
                return
            self.ctx.sleep(2.0)
        self.ctx.emit("error", "机械臂恢复失败，停止运行")
        self.ctx.stop("机械臂恢复失败")

    # ---------------------------------------------------------------- 休息

    def _schedule_cfg(self) -> dict:
        return self.ctx.store.section("runtime").get("schedule", {}) or {}

    def _should_rest(self) -> bool:
        """该休息了吗（工作时段之外，或已连续运行够久）。

        反风控要点：24 小时匀速不停是最明显的机器特征。真人会睡觉、会吃饭、
        会走神。宁可少抢几单，也不要因为节奏太完美被风控识别。
        """
        cfg = self._schedule_cfg()
        if not cfg.get("enabled", False):
            return False

        windows = cfg.get("work_hours") or []
        if windows:
            hour = time.localtime().tm_hour
            if not any(int(a) <= hour < int(b) for a, b in windows):
                return True

        every = float(cfg.get("rest_every_min", 45) or 45)
        started = self.ctx.get("run_started_at")
        if started and every > 0 and (time.time() - started) >= every * 60:
            return True
        return False

    def _rest(self) -> None:
        """休息一会儿，并重置连续运行计时。"""
        import random

        cfg = self._schedule_cfg()
        rng = cfg.get("rest_range_sec", [60, 300]) or [60, 300]
        seconds = random.uniform(float(rng[0]), float(rng[1]))
        outside = bool(
            (cfg.get("work_hours") or [])
            and not any(
                int(a) <= time.localtime().tm_hour < int(b)
                for a, b in cfg.get("work_hours")
            )
        )
        if outside:
            self.ctx.emit("info", "不在工作时段，等待下一轮（反风控：真人不连轴转）")
            self.ctx.bump("rest_outside_hours")
            # 时段外按固定 5 分钟一轮轮询，不要长睡（否则停止信号要等很久）
            self.ctx.sleep(min(300.0, seconds))
            return
        self.ctx.emit("info", f"休息 {seconds:.0f}s（反风控：真人不连轴转）")
        self.ctx.bump("rest_count")
        self.ctx.sleep(seconds)
        self.ctx.counters["run_started_at"] = int(time.time())

    def stop(self, reason: str = "") -> None:
        self.ctx.stop(reason)

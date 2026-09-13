"""任务装配：配置 → 设备 → 视觉 → 域模型 → 状态机，一条龙拼起来。

GUI 与命令行都只调这里，不自己 new 设备 —— 装配顺序（先机械臂后摄像头、
先服务重启后开串口）是有讲究的，散落在各处必然出错。

扫描钩子的工作节奏（重要）：
    主循环每 0.2s 判一次页面，但整屏 OCR 很贵，所以列表页按 scan_cooldown_sec
    节流：扫到命中就点进去（交给状态机的详情页分支处理），没命中就上滑一屏。
    这样「判定快、扫描稳」，不会因为一屏 OCR 卡住整条流水线。
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, Optional, Tuple

from core.config_store import DATA_DIR, ConfigStore
from core.devices.arm import RobotArm
from core.devices.frame_source import create_frame_source
from core.domain.capacity import Capacity
from core.domain.regions import RegionStore
from core.domain.route_plan import RoutePlan, load_plan
from core.domain.rules import RuleEngine
from core.flow.actions import ActionKit
from core.flow.context import FlowContext
from core.flow import handlers
from core.flow.trace import TraceWriter
from core.flow.watchdog import Watchdog
from core.flow.detail import DetailHandler
from core.flow.handlers import LIST_STATES, refresh_list
from core.flow.order_store import OrderStore
from core.flow.machine import FlowMachine
from core.flow.scanner import ListScanner
from core.vision.matcher import TemplateMatcher
from core.vision.ocr import OcrEngine
from core.vision.page_state import PageDetector, ScreenState

LogFn = Optional[Callable[[str, str], None]]


def _behavior(context: FlowContext) -> dict:
    """取当前行为画像（反风控参数）。

    画像由 runtime.behavior_profiles.default 选择：
    balanced（均衡）/ extreme（激进）/ stealth（隐蔽，含回滑与长停顿）。
    """
    profiles = context.store.section("runtime").get("behavior_profiles", {}) or {}
    name = profiles.get("default", "balanced")
    return profiles.get(name, {}) or {}


def make_scan_hook(ctx: FlowContext, scanner: Optional[ListScanner] = None) -> Callable[[FlowContext], None]:
    """列表页扫描钩子（带冷却 + 真人节奏）。

    为什么要有"真人节奏"：
        我们唯一会被平台抓到的是**行为特征**——硬件层面（摄像头 + 机械臂）
        不留任何痕迹，但"每 1.8 秒滑一屏、24 小时不停、路径完全重复"
        这种完美节奏本身就是最明显的机器特征。
        所以停顿、回滑、走神都要随机，且强度可通过画像配置。
    """

    scanner = scanner or ListScanner(ctx)
    state = {"last_scan": 0.0, "swipes": 0, "last_route_check": 0.0}
    # 最近进详情的落点（**多个**，不只是上一次）：兜底防止「指纹被 OCR 抖动冲破 →
    # 反复点同一张卡」。去重靠文本/参数/订单库三级指纹，但列表重排 + OCR 抖动会漏网。
    #
    # 为什么必须记多个：2026-09-07 第三轮实测，本屏只剩两张卡时，只记「上一次」会
    # 在 A、B 之间**来回弹**——每次都比较上一张，永远不相等，两张卡被轮流点了 11 次。
    recent_pts: Deque[Tuple[Tuple[float, float], float, Optional[str]]] = deque(maxlen=8)
    # 本屏命中缓存：详情返回后直接点下一个坐标（不重新整屏 OCR，提速关键）。
    # 命中列表用完/上滑后清空。点进去后详情页仍会重新读字段+判定（二道核），
    # 所以即使列表在详情停留期间被 App 刷新、坐标点到了别的卡，也会被正确判定+去重兜住。
    pending_hits: List[OrderCard] = []
    # 暴露给 handlers.handle_order_list：「推荐货源」判据必须等本屏命中卡都点完再判，
    # 否则推荐货源分隔条与本屏最后一张符合条件的卡同屏时，会把那张卡漏掉
    # （2026-09-13 加推荐货源检测时同步加的护栏）。
    ctx.scan_pending_hits = pending_hits

    def _route_ok(context: FlowContext) -> bool:
        """【已禁用】校验列表顶部筛选条是否匹配当前路线。

        2026-09-12 二次误判后停用：列表上滑后筛选条滚出屏幕，ROI 框到订单卡片，
        省级出发地（如 A 路线「江苏」）在卡片上普遍出现，无法与真实筛选条区分，
        导致「出发地匹配但目的地不匹配」误判偏离 → 反复重设路线死循环。
        现在 hook 里已不再调用本函数；保留定义仅供未来若需恢复时参考。
        真正兜底「App 崩溃重置筛选」的是：启动 _should_skip_setup + 返回落桌面
        route_setup_pending（见 detail._leave）。
        """
        plan = getattr(context, "plan", None)
        if plan is None or getattr(plan, "current", None) is None:
            return True
        route = plan.current
        origins = [p[-1] for p in route.origin] if route.origin else []
        dests = [p[-1] for p in route.dest] if route.dest else []
        if not origins or not dests:
            return True
        fs = context.kit.frame()
        if fs is None or getattr(context.kit, "ocr", None) is None:
            return True
        try:
            h, w = fs.raw.shape[:2]
            roi = (0, int(0.11 * h), w, int(0.07 * h))
            boxes = context.kit.ocr_frame(fs, roi=roi)
            text = "".join(getattr(b, "text", "") or "" for b in boxes)
        except Exception:
            return True
        # 读不到任何「出发地」城市 → 无法确认偏离，跳过本次自检（返回 True）。
        # 为什么以出发地为准：列表上滑后筛选条滚出屏幕（top=off），ROI 框到订单
        # 卡片内容——卡片常含目的地城市（如「南京→杭州」里的「杭州」），但出发地
        # 城市（南京/无锡/苏州）读不到。旧判据「读不到任何城市才跳过」漏掉这种
        # 「只读到目的地」的情况 → has_origin=False 误判偏离 → 反复重设路线
        # （2026-09-12 实测死循环，一轮 route_setup_ok=8）。正常筛选条左侧必有
        # 出发地，读不到出发地就说明筛选条已滚走或 OCR 漏读，均不应判偏离（宁可漏报）。
        if not any(str(o) in text for o in origins):
            return True
        # 出发地读到了（筛选条可见且匹配），再检查目的地是否也还在
        return any(str(d) in text for d in dests)

    def _pick(hits):
        """从本屏命中里挑一张要点的：跳过疑似刚点过的（对比**最近多次**落点）。

        ⚠️ 这个兜底是**最后一道保险**，必须收得很紧：2026-09-06 第二轮实测
        半径 40px 时误伤了 6 次——列表里相邻两张卡的落点差本来就在 40px 量级，
        把「位置相近的另一张卡」当成「同一张卡」跳掉，等于制造新的漏单。
        所以半径保持 20px，并且要求**参数指纹也相同**才跳过。
        """
        cfg = ctx.store.section("runtime").get("scan", {}) or {}
        radius = float(cfg.get("repeat_point_radius_px", 20) or 20)
        window = float(cfg.get("repeat_point_window_sec", 120) or 120)
        now = time.time()
        fresh = [(pt, spec) for pt, ts, spec in recent_pts if now - ts < window]
        for card in hits:
            pt = card.click_point
            if pt is None:
                continue
            spec = getattr(card, "spec_key", None)
            dup = None
            for prev, prev_spec in fresh:
                if (
                    abs(pt[0] - prev[0]) <= radius
                    and abs(pt[1] - prev[1]) <= radius
                    and prev_spec is not None
                    and prev_spec == spec
                ):
                    dup = prev
                    break
            if dup is not None:
                # 降级为 debug：实际去重保险是上面的 dup 判定 + ctx.bump 计数器，
                # 行为不受影响；warning 只是刷屏噪音（监护 repeat_point 规则是
                # counter_delta，数的是下面的 bump 计数器，与日志级别无关）。
                ctx.emit(
                    "debug",
                    f"跳过 {card.describe()}：落点 {pt} 与刚点过的 {dup} 几乎相同且参数一致，疑似同一张卡",
                )
                ctx.bump("scan_repeat_point_skipped")
                continue
            return card
        return None

    def _enter_detail(context: FlowContext, card: OrderCard) -> None:
        """点一张命中卡进详情（抽公共逻辑，供缓存队列消费复用）。"""
        point = card.click_point
        context.emit("info", f"进入详情: {card.describe()} @ {point}")
        # 记下卡片：详情页左下角读不到价格时，用它上面的价格回退
        context.last_card = card
        # 同步卡片级标签（整车/电议）供详情页透传给 GUI 展示（见 detail._emit_candidate）
        context.card_tags = {
            "whole_vehicle": bool(getattr(card, "has_whole_vehicle", False)),
            "dianyi": bool(getattr(card, "has_dianyi", False)),
        }
        context.kit.click_point(point[0], point[1], reason="进详情")
        # 等详情页出现；若落到桌面说明 App 闪退/误触退出，存现场截图帮助定位
        # （2026-09-12 实测进详情偶发退桌面，恢复仍由主循环 handle_desktop 兜底）。
        fs, after = context.kit.wait_state(
            (ScreenState.DETAIL, ScreenState.DESKTOP), timeout=4.0, interval=0.4
        )
        if getattr(after, "state", None) is ScreenState.DESKTOP:
            context.emit(
                "warning",
                f"进详情 {card.describe()} 后落到桌面（App 可能闪退/误触退出），已存现场截图",
            )
            tr = getattr(context, "trace", None)
            if tr is not None and getattr(tr, "enabled", False) and fs is not None:
                try:
                    tr.shot(fs.raw, "detail_enter_to_desktop", always=True)
                except Exception:  # noqa: BLE001 截图失败绝不中断主流程
                    pass
        recent_pts.append((point, time.time(), getattr(card, "spec_key", "")))
        # 点完从缓存移除：否则返回后 _pick 又要靠 recent_pts 跳过它，
        # 每次跳过都 bump scan_repeat_point_skipped，计数暴增成噪音（实测 6→40）。
        try:
            pending_hits.remove(card)
        except ValueError:
            pass

    def hook(context: FlowContext) -> None:
        cooldown = float(context.store.get("runtime", "loop.scan_cooldown_sec", 1.0) or 1.0)
        now = time.time()
        if now - state["last_scan"] < cooldown:
            return
        state["last_scan"] = now

        # 路线自检已禁用（2026-09-12 二次误判）：列表上滑后筛选条滚出屏幕，ROI 框到
        # 订单卡片，省级出发地（如 A 路线「江苏」）在卡片上普遍出现，无法与真实筛选条
        # 区分 → 误判偏离 → 反复重设路线（route_setup_ok=7）→ 连锁「筛选条无法展开」。
        # 「App 崩溃重置筛选」已由启动 _should_skip_setup + 返回落桌面 route_setup_pending
        # 兜底（见 detail._leave），每 30s 自检纯属有害（宁可漏报，不可误报）。

        # 注：「进入列表页先刷新」由 handlers.handle_order_list 负责
        # （必须在 ensure_top_visible 之前，顺序为：刷新 → 下滑展顶 → 听单）。
        # 这里只管扫描过程中的保鲜刷新。

        # 1. 优先消费本屏缓存的命中：详情返回后直接点下一个，不重新整屏 OCR
        if pending_hits:
            card = _pick(pending_hits)
            if card is not None:
                _enter_detail(context, card)
                return
            pending_hits.clear()  # 缓存的都点过/重复了 → 重新扫描

        # 2. 重新扫描本屏（整屏 OCR + 切卡判定）
        hits = scanner.scan_once()
        if hits:
            pending_hits.extend(hits)
            card = _pick(pending_hits)
            if card is not None:
                _enter_detail(context, card)
                return

        # —— 本屏无命中：上滑一屏继续找 ——
        context.kit.swipe_up()
        pending_hits.clear()  # 上滑后列表位置变了，缓存坐标失效
        context.bump("scan_swipe")
        state["swipes"] += 1

        # 列表保鲜改为事件驱动（首进列表 / 选完路线 / 换路线时刷新），
        # 不再按屏数自动刷新（用户 2026-09-06 明确要求：不是每屏都刷）。
        # state["swipes"] 仍用于反风控节奏计数，此处不再触发刷新。

        prof = _behavior(context)

        # 每屏停顿必须随机，固定间隔是最容易被识别的特征
        rng = prof.get("swipe_pause_range") or [1.2, 2.6]
        context.sleep(random.uniform(float(rng[0]), float(rng[1])))

        # 偶尔回滑一小段（像人翻回去看刚才看过的）
        if random.random() < float(prof.get("scroll_back_prob", 0.0) or 0.0):
            context.kit.swipe_down(distance_ratio=0.15)
            context.bump("behavior_scroll_back")
            context.sleep(random.uniform(0.8, 2.0))
            context.kit.swipe_up()
            context.sleep(random.uniform(float(rng[0]), float(rng[1])))

        # 偶尔长停顿（真人会走神、喝水、看手机）。
        # 随机概率触发（平均每 idle_every 屏一次），而非固定 swipes%20==0 ——
        # 固定周期是最明显的机器特征（7.5 反风控建议「改随机概率触发」）。
        idle_rng = prof.get("idle_range") or []
        idle_every = int(prof.get("idle_every", 20) or 20)
        if idle_rng and idle_every > 0 and random.random() < 1.0 / idle_every:
            sec = random.uniform(float(idle_rng[0]), float(idle_rng[1]))
            context.emit("info", f"停顿 {sec:.0f}s（反风控：模拟真人走神）")
            context.bump("behavior_idle")
            context.sleep(sec)

    return hook


class RobotTask:
    """一次完整任务的生命周期管理（开设备 → 跑流程 → 关设备）。"""

    def __init__(
        self,
        store: Optional[ConfigStore] = None,
        log: LogFn = None,
        stop_event: Optional[threading.Event] = None,
        plan: Optional[RoutePlan] = None,
        max_run_sec: Optional[float] = None,
    ) -> None:
        self.store = store or ConfigStore()
        self.log = log
        self.stop_event = stop_event or threading.Event()
        # 本轮最长运行秒数（0/None = 不限时）。循环测试按「跑一轮→分析→优化→再跑」
        # 推进，到点自动收工并留下摘要，避免无人值守空转。
        self.max_run_sec = float(max_run_sec or 0) or None
        # 取帧源按 vision.capture.source 选择（camera / capture_card），
        # 业务层一律按 FrameSource 抽象使用，将来换采集卡不动这里。
        self.source = create_frame_source(self.store, log=log)
        self.arm = RobotArm(self.store, log=log)
        self.matcher = TemplateMatcher(self.store, log=log)
        self.detector = PageDetector(self.store, self.matcher, log=log)
        self.ocr = OcrEngine(self.store, log=log)
        # 判态器也要 OCR：列表到底靠底部「推荐货源」文字认（见 page_state._reached_bottom）
        self.detector.ocr = self.ocr
        self.kit = ActionKit(
            self.store, self.source, self.arm, self.matcher, self.detector, self.ocr, log=log
        )
        self.regions = RegionStore(self.store)
        self.rules = RuleEngine(self.store)
        self.capacity = Capacity.from_store(self.store)
        self.plan = plan if plan is not None else self._load_plan()
        self.trace = TraceWriter(self.store)
        self._wire_capacity_persist()

        self.ctx = FlowContext(
            store=self.store,
            source=self.source,
            arm=self.arm,
            kit=self.kit,
            regions=self.regions,
            rules=self.rules,
            capacity=self.capacity,
            log=log,
            stop_event=self.stop_event,
            plan=self.plan,
            trace=self.trace,
        )
        self.detail_handler = DetailHandler(self.ctx)
        self.ctx.on_detail = self.detail_handler
        # 扫描器在详情与列表之间共享：详情处理完回写去重指纹，列表才认得出来
        self.scanner = ListScanner(self.ctx)
        self.detail_handler.attach_scanner(self.scanner)
        self.ctx.on_scan = make_scan_hook(self.ctx, self.scanner)
        # 订单库（跨运行去重）：此前只 import 了却从没实例化，ctx.order_store 恒为
        # None → 跨运行去重与「已处理」记录全是空转，同一个单隔天还会再点。
        self.ctx.order_store = self._open_order_store()
        # 运行监护最后挂：它要吃所有模块的日志，且上面的装配日志不该被判成异常
        self.ctx.watchdog = Watchdog.from_config(self.ctx, max_run_sec=self.max_run_sec)
        self.machine = FlowMachine(self.ctx)
        self._thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- 装配

    def _open_order_store(self) -> Optional[OrderStore]:
        """打开订单去重库（SQLite，data/orders.sqlite3），并清掉过期记录。

        为什么在装配层开：库是「跨运行」的，必须由运行入口持有并在结束时关闭；
        之前它只被 import、从没实例化，ctx.order_store 一直是 None，于是
        「今天看过的单明天不再点」这条能力从来没生效过。
        """
        cfg = self.store.section("runtime").get("dedup", {}) or {}
        if not bool(cfg.get("use_order_db", True)):
            return None
        try:
            path = str(cfg.get("db_path", "orders.sqlite3") or "orders.sqlite3")
            p = Path(path)
            db_path = p if p.is_absolute() else DATA_DIR / p.name
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db = OrderStore(
                str(db_path), ttl_days=float(cfg.get("ttl_days", 3.0) or 3.0)
            )
        except Exception as exc:  # noqa: BLE001 建库失败只降级，不阻断运行
            self.log and self.log("warning", f"订单库不可用，跨运行去重降级: {exc}")
            return None
        if db.enabled:
            try:
                removed = db.cleanup()
                if removed:
                    self.log and self.log("info", f"订单库清理过期记录 {removed} 条")
            except Exception:  # noqa: BLE001
                pass
            # 试跑数据补救：dry_ 前缀的记录不参与去重，避免试跑把真单锁死
            if bool(self.store.get("runtime", "dry_run", True)) and bool(
                cfg.get("retro_dry_run", True)
            ):
                try:
                    n = db.mark_dry_run()
                    if n:
                        self.log and self.log(
                            "warning",
                            f"试跑模式：订单库 {n} 条历史记录已标记为 dry_（不参与去重，"
                            f"正式跑前请把 runtime.dedup.retro_dry_run 设为 false）",
                        )
                except Exception:  # noqa: BLE001
                    pass
        return db

    def _wire_capacity_persist(self) -> None:
        """拼单余量持久化：启动时恢复，变更时落盘。

        路径按【取帧源__机型】隔离（config/calib/camera__iphone_x/capacity.json），
        换机型或换取帧源时各用各的，绝不串号。
        不串号很重要：串了会按别的情况下的余量继续抢，可能一路抢超重单。
        """
        path = self.store.calib_dir() / "capacity.json"
        self._capacity_path = str(path)
        # 是否重启恢复余量：由 rules.pindan.restore_on_restart 控制。
        #   false（默认，测试阶段）：重启即空车（满载），不继承上次拼单扣掉的余量。
        #   true（正式运行）：重启恢复上次余量，防拼过单后重启重复抢超重单（亏钱）。
        pindan = (self.store.section("rules") or {}).get("pindan") or {}
        restore = bool(pindan.get("restore_on_restart", False))
        if restore and path.is_file():
            before = self.capacity.describe()
            self.capacity = Capacity.load(self._capacity_path, fallback=self.capacity)
            self.log and self.log("info", f"已恢复拼单余量: {self.capacity.describe()}")
            if before != self.capacity.describe():
                self.log and self.log("info", f"（新建账本为 {before}）")
        else:
            self.log and self.log(
                "info", f"重启即空车（restore_on_restart=false）：{self.capacity.describe()}"
            )
        self.capacity.on_change = lambda cap: cap.save(self._capacity_path)

    def _load_plan(self) -> Optional[RoutePlan]:
        try:
            plan = load_plan(self.store)
            return plan if plan.routes else None
        except Exception:
            return None

    def open(self, on_step=None) -> bool:
        """打开设备。机械臂先开（会先重启服务），成功后 arm 归位让开取帧视野。

        on_step(p, msg)：可选进度回调，用于 GUI 分步显示「载入进度」，避免黑盒等待像死机。
        """
        def step(p: int, m: str) -> None:
            if on_step:
                on_step(p, m)

        step(8, "① 重启 JxbService 服务 / 连接机械臂…")
        if not self.arm.open():
            return False
        step(45, "② 机械臂已连接，正在归位让出视野…")
        self.arm.back_to_home()
        step(65, "③ 打开摄像头…")
        if not self.source.open():
            self.arm.close()
            return False
        step(85, "④ 初始化坐标换算 / 启动轨迹记录…")
        self.trace.start()
        # 机械臂坐标换算以 fast 帧尺寸为基准（全工程点击坐标一律 fast 帧）
        fast = tuple(self.store.get("vision", "capture.fast_size", (480, 640)))
        self.arm.set_frame_size(int(fast[0]), int(fast[1]))
        step(100, "设备就绪")
        return True

    def close(self) -> None:
        # trace 队列满了会静默丢事件，事后复盘时日志是不完整的——必须让人看见
        if self.trace is not None and getattr(self.trace, "dropped", 0):
            self.log and self.log(
                "warning",
                f"轨迹写入丢弃 {self.trace.dropped} 条（队列满），本轮 trace 不完整；"
                f"可调大 runtime.trace.queue_size 或调小 frame_sample_rate",
            )
        self.trace.stop()
        self.source.close()
        self.arm.close()
        # 订单库必须显式关：SQLite 连接不关，Windows 上 data/orders.sqlite3 会被
        # 进程占用，下次启动（或备份/删除）都会失败。
        if self.ctx.order_store is not None:
            self.ctx.order_store.close()

    # ---------------------------------------------------------------- 运行

    def run(self, setup_first: bool = True) -> None:
        """阻塞运行（供命令行使用）。

        智能 setup_first：若 setup_first=True 但启动时已在列表页且路线已配
        （routes.json 有 current_index 指向有效路线），自动跳过 setup——
        避免强制回桌面 + 重复设路线。iPhone 没物理 Home 键，每次按 Home 都是动作；
        且 App 当前界面若就是合法开始点，不该再做无意义的导航。
        """
        # 清理上一轮残留的停止标志：stop_run.py 写的标志可能因上轮进程已退出而
        # 没被消费，残留会让本轮一启动就被「外部停止标志」立刻停掉。
        # 2026-09-12 实测：残留 stop.flag 让 GUI「开始」后主循环 3s 即停。
        _stop_flag = Path(self.store.root) / "data" / "stop.flag"
        if _stop_flag.exists():
            _stop_flag.unlink(missing_ok=True)
            _log = self.log or (lambda *a, **kw: None)
            _log("info", "已清理上一轮残留的停止标志")

        if setup_first and self._should_skip_setup():
            _log = self.log or (lambda *a, **kw: None)
            _log("info", "启动时已在列表页 + 路线已配，自动跳过 setup（避免强制回桌面）")
            setup_first = False
        try:
            self.machine.run(setup_first=setup_first)
        finally:
            self.close()

    def _should_skip_setup(self) -> bool:
        """启动时 detect 当前状态：仅在「已在列表页 且 App 筛选条已匹配当前路线」时跳过 setup。

        2026-09-11 修正：之前只查「在列表页 + routes.json 有路线」就跳过，但 App 实际
        筛选条可能是「当前位置」（用户上次手动设的），导致直接扫了当前位置的订单而非
        目标路线（用户实测反馈「出发地是当前，没设路线」）。

        2026-09-12 修正：判据加固为「列表页 + 顶部筛选条可见（top=on）+ 筛选条真的
        匹配路线」。top=off 时筛选条已滚出屏幕，那个 ROI 框到的是订单卡片——卡片常含
        出发地/目的地城市，会造成误判匹配 → 误跳过 setup → 直接扫 App 残留的「当前→
        江苏」列表（实测白扫 7 分钟河南/山西单）。判定全过程写日志，便于事后核对。
        """
        try:
            fs = self.kit.frame()
            if fs is None:
                return False
            res = self.kit.detect(fs)
            if res.state not in handlers.LIST_STATES:
                self._setup_log(f"启动跳过判定：不是列表页（{res.state.value}）→ 执行 setup")
                return False
            if not res.top_visible:
                self._setup_log("启动跳过判定：顶部筛选条未展开（top=off）→ 执行 setup")
                return False
            plan = self.ctx.plan
            if plan is None or plan.current is None:
                self._setup_log("启动跳过判定：没有可用路线 → 执行 setup")
                return False
            matched, text = handlers.filter_bar_matches_route(self.ctx, fs, plan.current)
            self._setup_log(
                f"启动跳过判定：筛选条={text!r} 目标={plan.current.describe()} "
                f"匹配={'是→跳过 setup' if matched else '否→执行 setup'}"
            )
            return matched
        except Exception as exc:  # noqa: BLE001 判定失败一律走 setup，宁可重设
            self._setup_log(f"启动跳过判定异常（{exc}）→ 执行 setup")
            return False

    def _setup_log(self, msg: str) -> None:
        """启动判定日志：走 ctx.emit（进 trace + 喂监护），无 ctx 时退回 log 回调。

        2026-09-12：此前这类判定完全没有日志（且 run() 里误用 self._log 导致
        「自动跳过 setup」也丢失），是排查启动未设路线时最卡的一环。
        """
        try:
            self.ctx.emit("info", msg)
        except Exception:  # noqa: BLE001
            (self.log or (lambda *a, **k: None))("info", msg)

    def start(self, setup_first: bool = True) -> threading.Thread:
        """后台线程运行（供 GUI 使用，避免卡界面）。"""
        self._thread = threading.Thread(
            target=self.run, kwargs={"setup_first": setup_first}, daemon=True
        )
        self._thread.start()
        return self._thread

    def stop(self, reason: str = "") -> None:
        self.ctx.stop(reason)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def summary(self) -> str:
        return self.ctx.summary()

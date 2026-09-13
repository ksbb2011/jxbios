"""流程上下文：handler 与 machine 之间唯一的数据通道。

用 dataclass 而不是全局变量，是为了让「一次跑批」的状态可整体丢弃重建
（换车、恢复运行、GUI 重启都要用到这一点）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from core.config_store import ConfigStore
from core.devices.arm import RobotArm
from core.devices.frame_source import FrameSource
from core.domain.capacity import Capacity
from core.domain.orders import OrderDetail
from core.domain.regions import RegionStore
from core.domain.route_plan import RoutePlan
from core.domain.rules import RuleEngine
from core.flow.actions import ActionKit
from core.flow.trace import TraceWriter
from core.vision.ocr import OcrEngine
from core.vision.page_state import PageDetector, ScreenState

LogFn = Optional[Callable[[str, str], None]]


@dataclass
class FlowContext:
    """一次运行的全部可变状态。"""

    store: ConfigStore
    # 取帧源（抽象）：当前是摄像头，将来可换成 HDMI 采集卡，业务层代码零改动
    source: FrameSource
    arm: RobotArm
    kit: ActionKit
    regions: RegionStore
    rules: RuleEngine
    capacity: Capacity
    log: LogFn = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    plan: Optional[RoutePlan] = None
    counters: Dict[str, int] = field(default_factory=dict)
    trace: Optional["TraceWriter"] = None  # noqa: F821

    # 暂停事件：详情页候选合格后暂停在详情页等待人工确认（审核模式）。
    # 与 stop_event 区分——暂停可被 GUI 一键恢复，且恢复后流程继续。
    pause_event: threading.Event = field(default_factory=threading.Event)
    # 最近一次候选的关键参数（供 GUI 实时展示）。
    candidate: Optional[dict] = None
    # 最近一次点进详情的**列表卡片**：详情页左下角读不到价格时，用它上面的价格回退
    # （2026-09-07 用户反馈：部分订单详情页无价格，但列表卡片上有）。
    last_card: Optional["OrderCard"] = None  # noqa: F821
    # 切路线后由 handlers 置 True：要求主循环**重新执行城市选择**。
    # 只刷新列表是不够的——App 的出发地/目的地筛选仍是上一条路线，拉到的还是
    # 旧货源，等于没切。主循环见此标志会调 setup_routes() 重设始发地/目的地。
    route_setup_pending: bool = False

    # 扫描/详情由 m5 接入；未接入时流程仍能跑通骨架（列表页空滑）
    on_scan: Optional[Callable[["FlowContext"], None]] = None
    on_detail: Optional[Callable[["FlowContext"], None]] = None
    # 订单去重库（SQLite）：结构化字段去重，跨运行持久检索；未接入时为 None
    order_store: Optional["OrderStore"] = None  # noqa: F821
    # 运行监护（watchdog）：日志/计数器异常达到阈值就自动停；未接入时为 None
    watchdog: Optional["Watchdog"] = None  # noqa: F821
    # 停止原因（监护触发 / 人工停止），供 GUI 与运行摘要展示
    stop_reason: str = ""
    # 当前页面状态（供 GUI 预览按需刷新：仅列表/详情帧显示，避免连续实时流卡顿）
    last_state: Optional[str] = None
    # 各阶段最新耗时（毫秒），供 GUI「耗时明细」展示：列表扫描 / OCR / 详情页 / 单轮
    timings: Dict[str, float] = field(default_factory=dict)
    # 首次详情页标记：运行启动后遇到的第一个详情页不按「命中订单」暂停，直接返回继续扫单
    first_detail_seen: bool = False
    # 审核模式暂停时由 detail 写入「当前这个合格单」（供 GUI 三按钮状态机判定暂停令牌，
    # 以及拼单动作扣减用）。恢复时清空。None 表示当前没有待人工决策的候选。
    held_candidate: Optional["OrderDetail"] = None
    # 审核暂停期间 GUI 设的「人工决策意图」：当前支持 "pindan"（拼单=人工已接此单，扣减余量后继续）。
    # 由 detail 恢复分支消费一次（pop 后清空）。None 表示仅「继续找单」（跳过，不扣减）。
    _review_action: Optional[str] = None
    # 当前选中卡片的列表级标签标志（整车/电议等）：扫描选卡时写入，详情页 _candidate_dict
    # 读它合并进候选字典供 GUI 展示。字典式便于扩展更多标签而不改签名。
    card_tags: Dict[str, bool] = field(default_factory=dict)

    # ---------------------------------------------------------------- 工具

    def emit(self, level: str, msg: str, **extra: object) -> None:
        """写日志（GUI 回调）+ 轨迹（JSONL，带结构化字段）+ 喂给运行监护。"""
        if self.log:
            self.log(level, msg)
        if self.trace is not None:
            self.trace.put(level, msg, **extra)
        if self.watchdog is not None:
            # 监护只在真正的运行期生效；它自己 emit 的日志会被内部重入保护忽略
            try:
                self.watchdog.on_log(level, msg)
            except Exception:  # noqa: BLE001 监护绝不能反过来搞挂主流程
                pass

    def stop(self, reason: str = "") -> None:
        """停止运行（带原因）。人工点停止和监护急停都走这里，原因要能查。"""
        if reason:
            self.stop_reason = reason
        self.stop_event.set()

    def bump(self, key: str, n: int = 1) -> int:
        self.counters[key] = int(self.counters.get(key, 0)) + n
        return self.counters[key]

    def reset(self, key: str) -> None:
        self.counters[key] = 0

    def get(self, key: str, default: int = 0) -> int:
        return int(self.counters.get(key, default))

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    @property
    def paused(self) -> bool:
        return self.pause_event.is_set()

    def pause(self) -> None:
        """暂停运行（停在详情页等人工确认）。stop() 仍会打断。"""
        self.pause_event.set()

    def resume(self) -> None:
        """人工确认后恢复运行。"""
        self.pause_event.clear()

    def set_review_action(self, action: Optional[str]) -> None:
        """审核暂停期间由 GUI 设置人工决策意图（如 "pindan"），detail 恢复时消费。"""
        self._review_action = action

    def pop_review_action(self) -> Optional[str]:
        """取出并清空审核意图（消费一次）。"""
        a = self._review_action
        self._review_action = None
        return a

    def wait_resume(self, timeout: float = 1.0) -> None:
        """阻塞直到恢复或收到停止信号（主循环每 timeout 醒来检查 stop）。"""
        while not self.stopped and self.paused:
            if self.stop_event.wait(timeout):
                return
            # 暂停期间主循环停在 wait_resume，watchdog.tick 不再被调用，导致
            # elapsed「定时收工」规则（--max-run-sec）永远不触发 → 到点不退、
            # 串口不释放（真机踩坑：review_mode 暂停在详情页，进程卡 30 分钟）。
            # 这里补一针：暂停等待期间也过一遍监护，让定时收工照常生效。
            # tick(None)：state_dwell 因 res=None 跳过（暂停不该被滞留规则强停）；
            # counter/counter_delta/stall 因暂停期间计数器不变而不会误触发 stop。
            wd = getattr(self, "watchdog", None)
            if wd is not None:
                wd.tick(None)

    def sleep(self, seconds: float) -> bool:
        """可中断的 sleep：收到停止信号立即返回 False。"""
        return not self.stop_event.wait(max(0.0, seconds))

    def limit(self, key: str, default: int = 3) -> int:
        return int(self.store.get("runtime", f"limits.{key}", default) or default)

    def current_route(self) -> str:
        """当前路线描述串（去重 key 的 route 部分）。没有路线计划时返回 ""。"""
        try:
            if self.plan is not None and self.plan.current is not None:
                return self.plan.current.describe()
        except Exception:  # noqa: BLE001 路线读取失败不该中断扫描
            pass
        return ""

    # ---------------------------------------------------------------- 运行态

    def start_run(self) -> None:
        self.counters.clear()
        self.counters["run_started_at"] = int(time.time())

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in sorted(self.counters.items()) if k != "run_started_at"]
        started = self.counters.get("run_started_at")
        if started:
            parts.append(f"时长={int(time.time()) - started}s")
        if self.stop_reason:
            parts.append(f"停止原因={self.stop_reason}")
        return " ".join(parts)

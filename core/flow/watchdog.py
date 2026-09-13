"""运行监护（watchdog）：测试期的自动急停。

为什么要有它：
    这是机械臂 + 真机 App 的无人值守流程，一次异常（点不动返回、卡在桌面、
    机械臂掉线、判定抽风）不会被任何人及时发现——程序会带着错误状态一直跑，
    轻则空转几小时，重则把手机点进奇怪的页面、误触订阅线路横条。测试期
    尤其需要「一发现异常就停」，让人来看一眼，而不是让机器人自己瞎扛。

设计原则（每一条都对应一个真实的坑）：
    * **只观察、不动作**：监护本身绝不点击、不滑动，唯一动作是置 stop_event；
    * **判据走配置和日志流**：不改业务代码就能加减规则，新出现的异常模式
      只要往 runtime.watchdog.rules 里加一条正则即可，不需要动主流程；
    * **默认保守**：宁可多跑一会儿，也不要因为一条普通 warning 就停——
      规则必须带窗口 + 次数阈值，「一次即停」只留给明确致命的信号；
    * **触发即冷却**：同一规则在 cooldown_sec 内不重复触发，避免刷屏和
      「stop → emit → 又被自己匹配到」的递归。

三类判据：
    log_match    —— 日志文本正则 + 时间窗口内次数（最灵活，覆盖绝大多数异常）
    counter      —— 计数器当前值（如 same_screen_streak）
    counter_delta—— 计数器在窗口内的增量（如 detail_return_failed 累计）
    state_dwell  —— 某个页面状态连续停留超过 dwell_sec
    stall        —— 长时间没有任何「进展」计数器增长（默认只告警）
    elapsed      —— 跑满 max_run_sec 秒即停（一轮 20 分钟的循环测试：到点自动收工
                    并留下本轮摘要，便于逐轮分析优化，非异常）
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from core import __version__
from core.flow.context import FlowContext


@dataclass
class WatchRule:
    """一条监护规则（来自 runtime.watchdog.rules 的某一项）。"""

    name: str
    kind: str = "log_match"
    # log_match
    pattern: str = ""
    level: str = ""
    # counter / counter_delta
    counter: str = ""
    # state_dwell
    states: Tuple[str, ...] = ()
    dwell_sec: float = 0.0
    # stall
    progress_counters: Tuple[str, ...] = ()
    stall_sec: float = 0.0
    # elapsed：跑满 N 秒即停（「一轮 20 分钟」循环测试用，与异常无关，纯粹定时收工）
    max_run_sec: float = 0.0
    # 通用
    threshold: int = 1
    window_sec: float = 300.0
    action: str = "stop"  # stop | warn
    cooldown_sec: float = 60.0
    regex: Optional[re.Pattern] = field(default=None, repr=False)

    @classmethod
    def from_dict(cls, name: str, raw: dict) -> "WatchRule":
        def _tup(key: str) -> Tuple[str, ...]:
            val = raw.get(key) or []
            return tuple(str(x) for x in val) if isinstance(val, (list, tuple)) else ()

        rule = cls(
            name=name,
            kind=str(raw.get("kind", "log_match") or "log_match").lower(),
            pattern=str(raw.get("pattern", "") or ""),
            level=str(raw.get("level", "") or "").lower(),
            counter=str(raw.get("counter", "") or ""),
            states=_tup("states"),
            dwell_sec=float(raw.get("dwell_sec", 0) or 0),
            progress_counters=_tup("progress_counters"),
            stall_sec=float(raw.get("stall_sec", 0) or 0),
            max_run_sec=float(raw.get("max_run_sec", 0) or 0),
            threshold=int(raw.get("threshold", 1) or 1),
            window_sec=float(raw.get("window_sec", 300.0) or 300.0),
            action=str(raw.get("action", "stop") or "stop").lower(),
            cooldown_sec=float(raw.get("cooldown_sec", 60.0) or 60.0),
        )
        if rule.pattern:
            try:
                rule.regex = re.compile(rule.pattern)
            except re.error:
                rule.regex = None  # 正则写错就当这条规则不存在，绝不能抛异常炸主循环
        return rule


class Watchdog:
    """吃日志与计数器，按规则判定异常，必要时停止运行。"""

    def __init__(self, ctx: FlowContext, rules: List[WatchRule], enabled: bool = True) -> None:
        self.ctx = ctx
        # 正则写坏的 log_match 规则直接丢弃：一条配错的规则不该让整个监护失效
        self.rules = [
            r for r in rules if r.kind != "log_match" or (r.kind == "log_match" and r.regex)
        ]
        self.enabled = bool(enabled)
        self.stop_reason = ""

        self._log_hits: Dict[str, Deque[float]] = {}
        self._counter_hist: Dict[str, Deque[Tuple[float, int]]] = {}
        self._progress: Dict[str, int] = {}
        self._started_at = time.time()
        self._last_progress_ts = time.time()
        self._fired_at: Dict[str, float] = {}
        self._cur_state = ""
        self._state_since = time.time()
        self._in_trigger = False
        # 上一次 tick 的时刻 + 「tick 间隔超过它就认为主循环被长阻塞」的阈值（秒）。
        # 主循环在 setup_routes（在城市面板上操作数分钟）、AI 决策等待等期间不会 tick，
        # 恢复后若直接拿旧的 _state_since 算 dwell，会把"阻塞耗时"误算成"状态停留"。
        self._last_tick_ts = 0.0
        self._tick_gap_reset_sec = 15.0

    # ---------------------------------------------------------------- 装配

    @classmethod
    def from_config(
        cls, ctx: FlowContext, max_run_sec: Optional[float] = None
    ) -> Optional["Watchdog"]:
        cfg = ctx.store.section("runtime").get("watchdog", {}) or {}
        if not bool(cfg.get("enabled", True)):
            return None
        rules: List[WatchRule] = []
        for name, raw in (cfg.get("rules") or {}).items():
            if str(name).startswith("_") or not isinstance(raw, dict):
                continue  # _note 之类的说明项
            if raw.get("enabled") is False:
                continue
            rules.append(WatchRule.from_dict(str(name), raw))
        # 命令行 --max-run-sec 优先：覆盖配置里的定时收工时长（0/None = 不限时）
        if max_run_sec:
            rules = [r for r in rules if not (r.kind == "elapsed" and r.name == "round_time_limit")]
            rules.append(
                WatchRule(
                    name="round_time_limit",
                    kind="elapsed",
                    max_run_sec=float(max_run_sec),
                    action="stop",
                )
            )
        # 版本打在启动第一条：改了代码但 GUI 没重启时会跑旧逻辑，
        # 有版本号就能一眼看出「当前跑的是哪一版」，不用靠猜。
        ctx.emit("info", f"===== 程序版本 v{__version__} =====")
        wd = cls(ctx, rules, enabled=True)
        if rules:
            ctx.emit("info", f"运行监护已启用（{len(rules)} 条规则，动作={cfg.get('action', 'stop')}）")
        return wd

    # ---------------------------------------------------------------- 输入

    def reset(self) -> None:
        """新一轮运行开始：清掉历史，别拿上一轮的计数来判这一轮。"""
        self._log_hits.clear()
        self._counter_hist.clear()
        self._progress.clear()
        self._fired_at.clear()
        self._started_at = time.time()
        self._last_progress_ts = time.time()
        self._state_since = time.time()
        self._cur_state = ""
        self.stop_reason = ""

    def on_log(self, level: str, msg: str) -> None:
        """每条日志都过一遍 log_match 规则（由 ctx.emit 调用）。"""
        if not self.enabled or self._in_trigger:
            return
        now = time.time()
        for rule in self.rules:
            if rule.kind != "log_match" or rule.regex is None:
                continue
            if rule.level and rule.level != str(level).lower():
                continue
            if not rule.regex.search(msg or ""):
                continue
            q = self._log_hits.setdefault(rule.name, deque())
            q.append(now)
            self._trim(q, now, rule.window_sec)
            if len(q) >= rule.threshold:
                self._trigger(
                    rule,
                    f"{len(q)} 次/{rule.window_sec:.0f}s 命中「{rule.pattern}」"
                    f"（最近：{(msg or '')[:40]}）",
                )

    def tick(self, res=None) -> None:
        """主循环每帧调用：计数器 / 滞留 / 停滞判据。"""
        if not self.enabled or self._in_trigger:
            return
        now = time.time()
        # 主循环曾被长阻塞（setup_routes 在城市面板上操作数分钟、AI 决策等待等）时，
        # 两次 tick 之间会有大跳变——此时"状态并没有真停留那么久"，绝不能拿旧基准判
        # dwell。先把基准挪到现在（2026-09-13 实测：设路线阻塞 ~4分45秒 后首次 tick
        # 即误报 detail_dwell=319s，把正常的城市面板操作当成"详情页卡死"而停跑）。
        if self._last_tick_ts and (now - self._last_tick_ts) > self._tick_gap_reset_sec:
            self._state_since = now
        self._last_tick_ts = now
        for rule in self.rules:
            if rule.kind == "elapsed" and rule.max_run_sec > 0:
                # 定时收工：跑满 max_run_sec 就停（一轮 20 分钟的循环测试用）。
                # 这不是异常，是正常的「到点下钟」，原因要写清楚便于区分。
                if now - self._started_at >= rule.max_run_sec:
                    self._trigger(
                        rule,
                        f"已运行 {now - self._started_at:.0f}s，达到本轮上限 "
                        f"{rule.max_run_sec:.0f}s（定时收工，非异常）",
                    )
            elif rule.kind == "counter" and rule.counter:
                val = self.ctx.get(rule.counter)
                if val >= rule.threshold:
                    self._trigger(rule, f"{rule.counter}={val}（阈值 {rule.threshold}）")
            elif rule.kind == "counter_delta" and rule.counter:
                hist = self._counter_hist.setdefault(rule.name, deque())
                val = self.ctx.get(rule.counter)
                hist.append((now, val))
                # 多留一个窗口起点之前的采样，才能算出窗口内的增量
                while len(hist) > 2 and now - hist[1][0] > rule.window_sec:
                    hist.popleft()
                if len(hist) >= 2:
                    delta = max(0, val - hist[0][1])
                    if delta >= rule.threshold:
                        self._trigger(
                            rule, f"{rule.counter} 近 {rule.window_sec:.0f}s 增加 {delta}"
                        )
            elif rule.kind == "state_dwell" and res is not None:
                state = str(getattr(res.state, "value", res.state))
                if not rule.states or state in rule.states:
                    if now - self._state_since >= rule.dwell_sec > 0:
                        self._trigger(rule, f"停在 {state} 已 {now - self._state_since:.0f}s")
            elif rule.kind == "stall" and rule.stall_sec > 0:
                moved = False
                for key in rule.progress_counters:
                    val = self.ctx.get(key)
                    if val > self._progress.get(key, 0):
                        self._progress[key] = val
                        moved = True
                if moved:
                    self._last_progress_ts = now
                elif now - self._last_progress_ts >= rule.stall_sec:
                    self._trigger(rule, f"已 {now - self._last_progress_ts:.0f}s 没有任何进展")

        # 状态切换在最后统一记录，保证上面 dwell 用的是「上一个状态的起点」
        if res is not None and not getattr(res, "pending", False):
            state = str(getattr(res.state, "value", res.state))
            if state != self._cur_state:
                self._cur_state = state
                self._state_since = now

    # ---------------------------------------------------------------- 输出

    def _trigger(self, rule: WatchRule, detail: str) -> None:
        now = time.time()
        if now - self._fired_at.get(rule.name, 0.0) < rule.cooldown_sec:
            return  # 冷却中：不重复触发，也不重复打日志
        self._fired_at[rule.name] = now
        action = rule.action if rule.action in ("stop", "warn") else "stop"

        # 触发过程中自己 emit 的日志必须被忽略，否则「stop 的 error 日志」
        # 会再次喂回 on_log，形成递归（真实踩过的坑）。
        self._in_trigger = True
        try:
            head = f"[监护] {rule.name}"
            if action == "stop":
                reason = f"{rule.name}: {detail}"
                self.stop_reason = reason
                self.ctx.bump("watchdog_stop")
                self.ctx.emit("error", f"{head} 触发，自动停止运行 —— {detail}")
                self.ctx.stop(reason)
            else:
                self.ctx.bump("watchdog_warn")
                self.ctx.emit("warning", f"{head} 告警（不停止） —— {detail}")
        finally:
            self._in_trigger = False

    @staticmethod
    def _trim(q: Deque[float], now: float, window: float) -> None:
        while q and now - q[0] > window:
            q.popleft()

    def describe(self) -> str:
        return self.stop_reason or "监护未触发"

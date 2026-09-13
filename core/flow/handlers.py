"""九态各自的处理器：判定结果 → 动作。

纪律（每一条都对应一次真机事故）：
    * 处理器只做「把当前态推到下一个更安全/更接近目标的态」，不追求一次到位；
    * 任何点击后必须复判，失败按「重试上限 → 升级手段 → 暂停告警」三级处理；
    * 未知页**先静置重判**再动手 —— 页面加载过渡期模板分数会短暂跌破阈值，
      此时误滑会把整个流程带跑偏；
    * 宁可停下告警，也不盲点：所有兜底动作都必须在配置里显式登记。
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

from core.devices.frame_source import FrameSet
from core.flow.city_picker import confirm_button_clickable, top_red_entry_point
from core.flow.context import FlowContext
from core.vision.page_state import PageResult, ScreenState

LIST_STATES = (ScreenState.ORDER_LIST, ScreenState.ORDER_LIST_NO_MORE)
CITY_STATES = (ScreenState.CITY_ORIGIN, ScreenState.CITY_DEST)
DIALOG_STATES = (ScreenState.DIALOG_LIST, ScreenState.DIALOG_DETAIL)


# -------------------------------------------------------------------- 桌面

def handle_desktop(ctx: FlowContext, fs: FrameSet, res: PageResult) -> None:
    """桌面：点运满满图标进 App。连续失败到上限就暂停（多半是图标位置变了）。"""
    limit = ctx.limit("desktop_click_max", 3)
    hit = ctx.kit.click_template("desktop_ymm_icon", fs=fs, reason="进入运满满")
    if hit is None:
        ctx.bump("desktop_miss")
        ctx.emit("warning", f"桌面未找到运满满图标（第 {ctx.get('desktop_miss')} 次）")
        return
    wait = float(ctx.store.get("runtime", "loop.desktop_enter_wait_sec", 8.0) or 8.0)
    _, after = ctx.kit.wait_state(LIST_STATES + CITY_STATES, timeout=wait, interval=0.6)
    if after.state in LIST_STATES + CITY_STATES:
        # 落回列表/城市面板：App 没被杀（只是退到后台再前台），筛选未重置，不重设路线。
        # App 被杀时 wait_state 会超时落到 home_other（下方置重设标志）。
        ctx.reset("desktop_miss")
        ctx.bump("app_entered")
        return
    # wait_state 超时（after.state 是 home_other 或其他，不是目标状态）：
    # 说明 App 被杀、筛选被重置（出发地变「当前位置」），必须重设路线。
    # （2026-09-12 实测退桌面重进到 home_other 后乱扫，见 run_20260912_195918）
    ctx.route_setup_pending = True
    if ctx.get("desktop_miss") >= limit:
        ctx.bump("desktop_fail_pause")
        ctx.emit("error", f"连续 {limit} 次进不去 App，暂停 {ctx.limit('desktop_fail_pause_sec', 60)}s")
        ctx.sleep(float(ctx.limit("desktop_fail_pause_sec", 60)))
        ctx.reset("desktop_miss")


# -------------------------------------------------------------------- 首页非列表

def handle_home_other(ctx: FlowContext, fs: FrameSet, res: PageResult) -> None:
    """底部白色全国货源 Tab：在首页但不在订单列表，点红 Tab 回列表。"""
    if ctx.kit.click_template("source_white", fs=fs, reason="回订单列表"):
        _, after = ctx.kit.wait_state(LIST_STATES, timeout=5.0, interval=0.5)
        if after.state in LIST_STATES:
            ctx.bump("back_to_list")
            return
    # 白 Tab 点了没反应：多半是首页其他 Tab，按返回键再试一次
    ctx.kit.press_back()
    ctx.bump("home_other_fallback")


# -------------------------------------------------------------------- 列表页

# 顶部筛选条特征词：筛选条右侧固定有「智能排序」按钮。OCR 命中它才说明 ROI 真的
# 框到了筛选条——否则 top=off（筛选条滚出屏幕）时会框到订单卡片，卡片上的出发地/
# 目的地城市会造成路线匹配误判（2026-09-12 实测启动因此误跳过 setup，白扫 7 分钟
# 非目标路线订单）。
FILTER_BAR_HINTS = ("智能排序",)


def filter_bar_text(ctx: FlowContext, fs) -> str:
    """OCR 列表顶部筛选条并返回文本（无 OCR/失败返回空串）。

    顶部筛选条实测 y≈0.146（见 fields.json entries._note_label_roi）；ROI 取
    y=0.11h~0.18h 覆盖这一行。
    """
    kit = getattr(ctx, "kit", None)
    if fs is None or kit is None or getattr(kit, "ocr", None) is None:
        return ""
    try:
        h, w = fs.raw.shape[:2]
        boxes = kit.ocr_frame(fs, roi=(0, int(0.11 * h), w, int(0.07 * h)))
        return "".join(getattr(b, "text", "") or "" for b in boxes)
    except Exception:  # noqa: BLE001 读不到就按空处理，由调用方决定放过/重设
        return ""


def filter_bar_matches_route(ctx: FlowContext, fs, route) -> Tuple[bool, str]:
    """判断顶部筛选条是否已匹配目标路线，返回 (是否匹配, 筛选条OCR文本)。

    安全前提：必须先确认 ROI 真的框到筛选条（命中 FILTER_BAR_HINTS），否则
    top=off 时会拿订单卡片文本冒充筛选条 → 误判匹配。

    判据用路径最深一级（path[-1]）：App 筛选条显示的是最深层选择（选到市级显示
    市名、只到省级显示省名），故 C 路线「南京」与 D 路线「江苏」都能对上。
    """
    text = filter_bar_text(ctx, fs)
    if not text:
        return False, ""
    if not any(k in text for k in FILTER_BAR_HINTS):
        return False, text
    origins = [p[-1] for p in route.origin] if route.origin else []
    dests = [p[-1] for p in route.dest] if route.dest else []
    if not origins or not dests:
        return False, text
    matched = (
        any(str(o) in text for o in origins)
        and any(str(d) in text for d in dests)
    )
    return matched, text


def ensure_top_visible(ctx: FlowContext, res: PageResult) -> bool:
    """顶部筛选条（始发地/目的地）是否展开。

    展开判定顺序（用户 2026-09-07 纠正）：
        ① 已展开 → 直接返回，绝不滑（下滑会把筛选条滚走，固定偏移点入口会误点进详情）。
        ② 未展开 → 先点红色「全国货源」/「返回顶部」回顶（让筛选条进入可展开状态）；
        ③ 回顶后【先等刷新渲染完】再下滑展开。

    2026-09-12 修正：回顶后判「是否回到第一行」要看「返回顶部」按钮消失
    （back_to_top 不再命中），**不是**等 top_visible——top_visible 是「筛选条已
    展开」，回顶后筛选条仍收起（恒为 false），等它永远等不到，白等还会让列表滚走。
    """
    if res.top_visible:
        return True
    # ① 先点红色全国货源 / 返回顶部 回顶（同时拉新数据，让顶部筛选条进入可展开态）
    cfg = ctx.store.get("runtime", "list_refresh", {}) or {}
    ctx.emit("info", "顶部筛选条未展开：先点红色全国货源/返回顶部回顶")
    refresh_list(ctx, cfg)
    # ② 等列表回到第一行：判据是「返回顶部」按钮消失（back_to_top 不再命中），
    # 而不是等 top_visible（那是「筛选条已展开」，回顶后仍收起、恒为 false）。
    deadline = time.time() + 8.0
    while time.time() < deadline:
        _, res = ctx.kit.wait_state(LIST_STATES, timeout=1.5, interval=0.4)
        if res.top_visible:
            return True  # 意外已直接展开
        if res.state not in LIST_STATES:
            break  # 不在列表页，下滑只会滚乱
        if "back_to_top" not in getattr(res, "hits", {}):
            break  # 返回顶部按钮消失 = 已回到第一行，跳出等下滑
    # ③ 等够了仍不展开 → 下滑展开（最多 3 次，减少空滑）
    for _ in range(3):
        ctx.kit.swipe_down()
        ctx.sleep(0.5)
        _, res = ctx.kit.wait_state(LIST_STATES, timeout=1.5, interval=0.4)
        if res.top_visible:
            return True
    ctx.emit("warning", "回顶+下滑后顶部筛选条仍未展开")
    return False


def ensure_tingdan_off(ctx: FlowContext, res: PageResult) -> bool:
    """听单开启会接管页面（自动抢单），扫描前必须关掉。

    状态取自 res.tingdan_on——由 page_state 用「红色像素占比」判定，
    比模板匹配可靠：模板在开/关两态下分数贴得太近（实测听单开着时
    tingdan_on=0.60、tingdan_off=0.73），会误判成「已关」从而不去关它。
    """
    if not getattr(res, "tingdan_on", False):
        # 听单已关：补一条 debug，让「程序是否检查过听单」可从日志直接确认，
        # 消除「没判断听单」的假象（2026-09-12 用户反馈）。
        ctx.emit("debug", "听单已关（无需操作）")
        return True
    pt = getattr(res, "tingdan_point", None)
    if pt and ctx.kit.click_point(pt[0], pt[1], reason="关闭听单"):
        _, after = ctx.kit.wait_state(LIST_STATES, timeout=4.0, interval=0.4)
        if not getattr(after, "tingdan_on", False):
            ctx.bump("tingdan_closed")
            return True
    ctx.emit("warning", "听单仍处于开启态")
    return False


def refresh_list(ctx: FlowContext, cfg: dict) -> bool:
    """点红色「全国货源」Tab 刷新列表，拿最新货源。

    为什么必须刷新：货源是实时的，从上往下扫的过程中前面扫过的订单随时
    会被别人抢走或下架——扫到后面再点进去就是「已被抢」弹窗，白浪费几秒。

    为什么用【点击】而不是下拉手势：
        机械臂做下拉滑动精度差，且容易被 App 当成普通滑动而不触发刷新；
        点底部红色「全国货源」Tab 是 App 的标准刷新入口，点一下既回顶部
        又拉新数据。找不到红 Tab 时回退点「返回顶部」按钮。
    """
    primary = cfg.get("method") or "source_red"
    fallback = cfg.get("fallback") or "back_to_top"
    settle = float(cfg.get("settle_sec", 2.0) or 2.0)

    for name in (primary, fallback):
        if not name:
            continue
        ctx.emit("info", f"刷新列表：点击 {name}")
        if ctx.kit.click_template(name, reason="刷新列表"):
            ctx.bump("list_refreshed")
            ctx.sleep(settle)   # 等列表重新加载，太短会取到加载中的旧画面
            _, res = ctx.kit.wait_state(LIST_STATES, timeout=5.0, interval=0.4)
            if res.state in LIST_STATES:
                return True
            ctx.emit("warning", f"点 {name} 后未回到列表（{res.state.value}），试下一个入口")
        else:
            ctx.emit("warning", f"未找到刷新入口 {name}，试下一个")

    ctx.emit("warning", "刷新列表失败：两个入口都没点上，继续扫描")
    ctx.bump("list_refresh_fail")
    return False


def maybe_refresh_on_enter(ctx: FlowContext) -> bool:
    """事件驱动刷新：仅本次列表会话【首次进入】时刷一次。

    用户明确口径（2026-09-06）：不是每次进列表页都刷，而是——
        ① 首次进到这个列表页 → 点红色全国货源刷新
        ② 刚选完出发地+目的地 → 点红色全国货源（由路线设置对端触发）
        ③ 列表到底（换路线）→ 点刷新拉新（见 _next_route_or_finish）
    中途从详情退回列表页【不】刷新（列表数据仍在）。用会话标志防同一列表
    页内每帧重复刷。
    """
    if getattr(ctx, "_list_refreshed_this_enter", False):
        return False
    ctx._list_refreshed_this_enter = True
    cfg = ctx.store.get("runtime", "list_refresh", {}) or {}
    return refresh_list(ctx, cfg)


# 「本线路无更多货源，为您推荐」页的特征词（App 显示在筛选条**下方**，即屏幕顶部）。
_RECOMMEND_KEYWORDS = ("推荐货源", "暂无货源", "没有货源", "该线路无更多", "无更多符合条件")


def _list_is_recommend(ctx: FlowContext, fs: FrameSet) -> bool:
    """列表页是否已变成 App 的「推荐货源」页（= 当前路线没有货源）。

    为什么不能只靠 `page_state._reached_bottom`：它只 OCR 屏幕**底部 25%**，而该提示
    出现在**顶部（筛选条正下方）**——整页推荐时永远判不到"到底"，于是程序既不换路线、
    又一直点推荐单（2026-09-13 用户截图实证）。这里用整屏 OCR（走 ActionKit 帧级缓存，
    与扫描/判态共用同一帧结果，几乎零额外成本）。
    """
    kit = getattr(ctx, "kit", None)
    if fs is None or kit is None or getattr(kit, "ocr", None) is None:
        return False
    try:
        boxes = kit.ocr_frame(fs)
        text = "".join(getattr(b, "text", "") or "" for b in boxes)
    except Exception:  # noqa: BLE001 OCR 失败不阻断（宁可继续扫）
        return False
    return any(kw in text for kw in _RECOMMEND_KEYWORDS)


def handle_order_list(ctx: FlowContext, fs: FrameSet, res: PageResult) -> None:
    """列表页：就位 → 扫描。到底（NO_MORE）或变成「推荐货源」页则换路线/换车。

    顶部显隐 + 关听单【只在本列表会话首次进入】做（用户口径 2026-09-06）：
    上滑扫描过程中顶部自然隐藏是正常行为，绝不再下滑强拉——否则与滚动打架、
    产生"一顿一顿搓屏"，还把刚滚到的位置拉回顶部。
    """
    if maybe_refresh_on_enter(ctx):
        # 刷新会把列表带回顶部，此时顶部工具栏必定可见，
        # 不必再靠模板去猜——实测听单按钮在「听单中」状态下只有 0.46 分
        # （阈值 0.88），靠它判定必然误判成隐藏、触发无意义的 3 连下滑。
        _, res = ctx.kit.wait_state(LIST_STATES, timeout=5.0, interval=0.4)
        ctx.bump("top_visible_by_refresh")

    if not getattr(ctx, "_list_top_setup_done", False):
        ctx._list_top_setup_done = True
        ensure_top_visible(ctx, res)   # 仅首次进入：顶部未展开才下滑

    # 关听单从「仅首次进入」改为「每次进列表都检查」：运行中途听单可能被
    # App 崩溃重进/自动打开（2026-09-12 实测 tingdan=ON 但程序没关）。
    # ensure_tingdan_off 在听单已关时直接返回，无额外开销；只有开着才点一下。
    ensure_tingdan_off(ctx, res)

    # ① 列表已变成 App 的「推荐货源」页（该线路无更多货源）→ 等同到底，换下一条路线。
    #    带 2s 冷却：整屏 OCR 很贵，而本函数每帧都被调用（扫描 hook 另有 scan_cooldown）。
    #    ⚠️ 必须等本屏已扫出的命中卡都点完再判：推荐货源分隔条常与「本屏最后一张符合
    #    条件的卡」同屏，抢先在扫描前切路线会把那张卡漏掉（宁可多扫一屏，不可漏单）。
    now = time.time()
    pending_hits = getattr(ctx, "scan_pending_hits", None)
    if not pending_hits and now - getattr(ctx, "_recommend_check_ts", 0.0) >= 2.0:
        ctx._recommend_check_ts = now
        if _list_is_recommend(ctx, fs):
            ctx.bump("list_recommend_no_route")
            ctx.emit("info", "列表为「推荐货源」（本路线无货源）→ 按到底处理，切换下一条路线")
            if not _next_route_or_finish(ctx):
                return

    if res.state is ScreenState.ORDER_LIST_NO_MORE:
        ctx.bump("list_no_more")
        if not _next_route_or_finish(ctx):
            return

    if ctx.on_scan is not None:
        ctx.on_scan(ctx)
    else:
        # 扫描模块未接入时保持骨架可跑：滑一屏继续监听
        ctx.kit.swipe_up()
        ctx.bump("list_idle_swipe")


def _next_route_or_finish(ctx: FlowContext) -> bool:
    """列表到底：按 runtime.list_no_more.action 处置。

    两种口径（用户 2026-09-06 明确）：
        refresh_restart（默认，**测试期用这个**）——点红色全国货源 / 返回顶部
            刷新列表，回到顶部重新开始扫。测试阶段不配路线、也不想程序停，
            到底就刷新再来一轮。
        next_route（正式）——切下一条路线；全部跑完则提示换车并停下。
    """
    cfg = ctx.store.get("runtime", "list_no_more", {}) or {}
    if str(cfg.get("action", "refresh_restart") or "").lower() != "next_route":
        return _restart_from_top(ctx, cfg)
    if ctx.plan is None or not ctx.plan.routes:
        ctx.emit("warning", "未配置路线，列表已到底，停止")
        ctx.stop("未配置路线且列表已到底")
        return False
    nxt = ctx.plan.advance()
    if nxt is None:
        ctx.emit("info", f"全部路线已跑完：{ctx.plan.describe()}")
        reason = "全部路线已跑完"
        if ctx.capacity.exhausted():
            ctx.emit("info", "载重余量已用尽，请换车后重新开始")
            reason = "全部路线已跑完且载重余量用尽"
        ctx.stop(reason)
        return False
    ctx.emit("info", f"切换路线：{nxt.describe()}")
    ctx.bump("route_switched")
    ctx._list_refreshed_this_enter = False  # 换路线 = 新列表会话，下次进列表刷
    ctx._list_top_setup_done = False        # 新会话也要重新做首次进入的置位（顶部+听单）
    # 关键（2026-09-07）：只刷新列表不够——App 的出发地/目的地筛选仍是上一条路线，
    # 拉到的还是旧货源，等于没切。置此标志让主循环下一轮执行 setup_routes()
    # 重新选城市（它内部会 ensure_top_visible，故先刷新回顶更稳）。
    ctx.route_setup_pending = True
    refresh_list(ctx, ctx.store.get("runtime", "list_refresh", {}) or {})  # 回到顶部，为重设城市铺路
    return True


def _restart_from_top(ctx: FlowContext, cfg: dict) -> bool:
    """测试期口径：列表到底 → 刷新列表（红 Tab / 回顶）→ 回到顶部重新扫。

    为什么带冷却：真到底时刷新拿到的还是同一批（或更少）货源，若每帧都刷
    就会变成「疯狂点红 Tab」——既伤号又让机械臂空转。刷新一次后至少等
    restart_interval_sec（默认 30s）再允许刷下一次；冷却期内轻轻歇一下，
    让主循环继续判态（货源可能会自己刷新出来）。
    """
    gap = float(cfg.get("restart_interval_sec", 30.0) or 30.0)
    last = float(getattr(ctx, "_no_more_restart_at", 0.0) or 0.0)
    if time.time() - last < gap:
        ctx.bump("list_no_more_cooled")
        ctx.sleep(float(cfg.get("cool_down_sleep_sec", 2.0) or 2.0))
        return False

    ctx._no_more_restart_at = time.time()
    ctx.emit("warning", "列表已到底 → 刷新列表（红 Tab / 回顶）后从头再扫")
    ctx.bump("list_no_more_restart")
    ok = refresh_list(ctx, ctx.store.get("runtime", "list_refresh", {}) or {})
    # 刷新本身已把列表带回顶部：重做首次进入的置位，并让会话标志保持已刷
    ctx._list_top_setup_done = False
    ctx._list_refreshed_this_enter = True
    ctx.reset("same_screen_streak")
    return ok


# -------------------------------------------------------------------- 详情页

def handle_detail(ctx: FlowContext, fs: FrameSet, res: PageResult) -> None:
    """详情页：交给详情处理器（m5）；未接入时直接返回列表。"""
    if ctx.on_detail is not None:
        ctx.on_detail(ctx)
        return
    ctx.kit.press_back()
    ctx.bump("detail_back_out")


# -------------------------------------------------------------------- 城市面板

# 非设置阶段停在城市面板时的「上一次收起尝试落点」：同一个无效目标反复点会让机械臂
# 空转、还掩盖真实故障（2026-09-12 实测反复点灰色 Tab 点了 4 轮才被监护停）。
_last_city_escape_pt: Optional[Tuple[int, int]] = None


def _same_point(a, b, tol: int = 12) -> bool:
    """两个像素落点是否几乎相同（容差 tol 像素）。"""
    if a is None or b is None:
        return False
    return abs(int(a[0]) - int(b[0])) <= tol and abs(int(a[1]) - int(b[1])) <= tol


def _click_top_red_text(ctx: FlowContext, fs: FrameSet):
    """点顶部红字收起城市面板（铁律 §2.3）——**不改**原有出发地/目的地。

    定位逻辑见 `city_picker.top_red_entry_point`：顶部 ~33% 区域 + 放宽档红字判定 +
    **含地区名**（`is_collapse_red_text`）的框里取**最靠上**的。灰色 Tab「出发地/
    目的地」已在候选阶段被挡掉（[0831-G]）。

    返回 `(是否成功回到列表, 本次点击落点或 None)`——落点供调用方做"同一无效目标不
    重复点"的防护。
    """
    pt = top_red_entry_point(
        ctx.kit, ctx.regions, fs, log=lambda lvl, msg: ctx.emit(lvl, msg)
    )
    if pt is None:
        return False, None
    cx, cy, text = pt
    ctx.emit("info", f"城市面板：点顶部红字 {text!r} @({cx:.0f},{cy:.0f}) 收起面板")
    if ctx.kit.click_point(cx, cy, reason="点顶部红字收起城市面板"):
        _, after = ctx.kit.wait_state(LIST_STATES, timeout=4.0, interval=0.4)
        if after.state in LIST_STATES:
            return True, (int(cx), int(cy))
    return False, (int(cx), int(cy))


def handle_city_panel(ctx: FlowContext, fs: FrameSet, res: PageResult) -> None:
    """非设置阶段却停在城市面板：按铁律 §2.3 收起面板回列表。

    退出优先级（铁律 §2.3 + 犯错记录 [0831-G]/[0831-H]）：
      1) **顶部红字**（含地区名/「当前定位」）→ 点它收起面板，**不改**原路线（首选）；
      2) 无红字、且确认**正红可点** → 点确认兜底退出（= 提交改动，仅此情形才用）；
      3) 都没有 → 告警并停下不乱点。

    两条血泪结论：
    * [0831-G] 灰色「出发地/目的地」是 Tab 标题，点了只切编辑侧、面板根本没关
      → 下一轮仍判城市面板 → 死循环。**绝不点灰字**（现在由 `is_collapse_red_text`
      在候选阶段就挡掉）。
    * [0831-H] **面板展开态（下方有城市网格）时点「确认」不会退出**，必须先点顶部
      红字收起；直接点确认毫无作用 → 死循环。所以红字优先、确认仅正红才兜底。

    2026-09-12 加固：
    * 同一个无效落点**不重复点**（否则机械臂空转、故障迟迟暴露不出来）；
    * 点确认前先校验按钮**正红可点**（复用 `confirm_button_clickable`），避免像之前
      那样"模板 score=1.00 却点到无效位置"。
    """
    global _last_city_escape_pt

    ok, pt = _click_top_red_text(ctx, fs)
    if ok:
        _last_city_escape_pt = None
        return

    # 同一落点已试过且无效 → 不再重复点，直接按"退不出去"计数告警
    if pt is not None and _same_point(pt, _last_city_escape_pt):
        ctx.bump("city_panel_stuck")
        ctx.emit(
            "warning",
            f"城市面板：同一落点 @{pt} 上轮已点过且无效，不再重复点（按铁律停止不乱点）",
        )
        return
    _last_city_escape_pt = pt

    # 无可用红字：只有确认按钮**正红可点**才点它兜底（浅红/展开态点了也没用）
    if confirm_button_clickable(ctx.store, fs):
        if ctx.kit.click_template("filter_confirm_red", fs=fs, reason="城市面板兜底退出"):
            ctx.emit("warning", "城市面板无顶部红字，改点正红「确认」兜底退出（= 提交当前选择）")
            _, after = ctx.kit.wait_state(LIST_STATES, timeout=4.0, interval=0.4)
            if after.state in LIST_STATES:
                _last_city_escape_pt = None
                return
    else:
        ctx.emit("warning", "城市面板：确认按钮非正红（浅红/展开态），不点")
    ctx.bump("city_panel_stuck")
    ctx.emit("warning", "城市面板：无可用顶部红字、确认也非正红，按铁律停止不乱点")


# -------------------------------------------------------------------- 弹窗

def handle_dialog(ctx: FlowContext, fs: FrameSet, res: PageResult) -> None:
    """弹窗/异常页：按「我知道了 → 关闭按钮候选 → 返回键」三级收掉。"""
    if ctx.kit.click_text("我知道了", fs=fs, reason="关弹窗") is None:
        dialog = res.dialog
        if dialog is not None and dialog.name == "page_add_route":
            ctx.kit.click_template("page_add_route_back", fs=fs, reason="关添加线路页")
        else:
            ctx.kit.click_close_candidate(fs=fs)
    _, after = ctx.kit.wait_state(
        LIST_STATES + (ScreenState.DETAIL, ScreenState.CHAT), timeout=4.0, interval=0.4
    )
    if after.state in DIALOG_STATES:
        streak = ctx.bump("dialog_streak")
        if streak >= ctx.limit("dialog_max_streak", 5):
            ctx.emit("error", f"弹窗连续 {streak} 次关不掉，按返回键并暂停")
            ctx.kit.press_back()
            ctx.reset("dialog_streak")
            ctx.sleep(5.0)
        return
    ctx.reset("dialog_streak")
    ctx.bump("dialog_closed")


# -------------------------------------------------------------------- 聊天页

def handle_chat(ctx: FlowContext, fs: FrameSet, res: PageResult) -> None:
    """聊天页（误触咨询）：退出回列表。

    返回箭头 chat_back 在聊天页/详情页同形，模板对比区分不了页面（聊天页判定靠
    chat_voice 语音按钮，不靠 chat_back）；但当前已判为 CHAT，命中的就是聊天页的
    返回箭头。用户 2026-09-10 定：先模板对比，对比不行就固定坐标，再不行物理键。
    """
    if ctx.kit.click_template("chat_back", fs=fs, reason="退出聊天") is None:
        pt = ctx.store.get("coords", "controls.chat_back.norm", None) or [0.0747, 0.0837]
        h, w = fs.fast.shape[:2]
        if not ctx.kit.click_point(
            float(pt[0]) * w, float(pt[1]) * h, reason="退出聊天(固定坐标)"
        ):
            ctx.kit.press_back()
    _, after = ctx.kit.wait_state(
        LIST_STATES + (ScreenState.DETAIL,), timeout=4.0, interval=0.4
    )
    if after.state is ScreenState.CHAT:
        ctx.bump("chat_stuck")
        ctx.kit.press_back()
    else:
        ctx.bump("chat_exited")


# -------------------------------------------------------------------- 未知页

def _close_grabbed_dialog(ctx: FlowContext, fs: FrameSet, boxes) -> None:
    """关「已被抢/已下架」弹窗并尽量回列表（供 handle_unknown 复用）。

    背景（2026-09-12 实测）：这类弹窗盖在详情页上时 detail_share 被遮罩，
    详情判定失败；dialog_grabbed 模板真实分仅 0.62 < body_low，弹窗判定也失败，
    于是详情页被误判成 unknown。必须在 unknown 分支里自行识别并收掉，否则走
    press_home 切桌面 → 重进 App 又撞同一张卡 → 死循环。
    """
    h, w = fs.fast.shape[:2]
    kws = ("已被抢", "货源已定", "已下架")
    anchor = None
    for b in (boxes or []):
        t = getattr(b, "text", "") or ""
        if any(kw in t for kw in kws):
            cx, cy = b.center
            anchor = (min(max(cx + 136, 0), w - 1), cy)
            break

    if anchor is not None:
        cx, cy = anchor
        if ctx.kit.click_point(cx, cy, reason="关闭已被抢弹窗(OCR锚点)"):
            _, res = ctx.kit.wait_state(LIST_STATES, timeout=2.0, interval=0.4)
            if res.state in LIST_STATES:
                return

    cfg = ctx.store.section("runtime").get("dialog_close_points", {}) or {}
    pt = cfg.get("detail_grabbed", [0.792, 0.330])
    if isinstance(pt, (list, tuple)) and len(pt) == 2:
        if ctx.kit.click_point(
            int(float(pt[0]) * w), int(float(pt[1]) * h), reason="关闭已被抢弹窗(固定坐标)"
        ):
            _, res = ctx.kit.wait_state(LIST_STATES, timeout=2.0, interval=0.4)
            if res.state in LIST_STATES:
                return

    ctx.kit.swipe_back_gesture()
    _, res = ctx.kit.wait_state(LIST_STATES, timeout=2.0, interval=0.4)
    if res.state in LIST_STATES:
        return
    if ctx.kit.click_close_candidate(fs=fs) is None:
        ctx.kit.press_back()
    ctx.kit.wait_state(LIST_STATES, timeout=2.5, interval=0.4)


def handle_unknown(ctx: FlowContext, fs: FrameSet, res: PageResult) -> None:
    """未知页：开发模式下直接停下来问人；正式模式再按升级链自动兜底。"""
    # 开发铁律（2026-09-10 用户明确）：遇到未知页直接停，不要自己在后台反复尝试
    # （右滑/返回/回桌面）。看清现场是开发期第一优先级，自动兜底是正式运行才做的事。
    if bool(ctx.store.get("runtime", "development.unknown_pause", True)):
        ctx.kit.save_debug(fs, "unknown_pause")
        ctx.emit(
            "warning",
            "【开发模式】遇到未知页，已停止等待人工查看（确认页面后手动重跑；"
            "要启用自动兜底请把 runtime.development.unknown_pause 设为 false）",
        )
        ctx.stop("dev_unknown_pause")
        return

    settle = float(ctx.store.get("runtime", "loop.unknown_settle_sec", 2.0) or 2.0)
    _, again = ctx.kit.wait_state(
        LIST_STATES + CITY_STATES + DIALOG_STATES + (ScreenState.DETAIL, ScreenState.CHAT),
        timeout=settle,
        interval=0.5,
    )
    if again.state is not ScreenState.UNKNOWN:
        ctx.emit("info", f"未知页静置后恢复: {again.state.value}")
        ctx.reset("unknown_streak")
        return

    # 城市面板的「自定义装货点/卸货点」标签页（2026-09-08 用户真机实测卡在此页）：
    # 点入口后 App 默认打开它；它没有底部「清空筛选/确认」按钮 → 页面判定认不出
    # → 被当 unknown；且右滑/返回键都退不出去。正确出路是点页签「出发地/目的地」
    # 切回省市网格 → 由城市面板正常流程接管（继续选路线或点顶部红字收起）。
    try:
        boxes = ctx.kit.ocr_frame(fs)
    except Exception:  # noqa: BLE001 OCR 不可用时走原恢复链
        boxes = []
    _texts = [(b.text or "") for b in (boxes or [])]
    # 「已被抢/已下架/货源已定」弹窗盖在详情上 → 详情判定失败落进 unknown。
    # 先按弹窗处置（关掉回列表），绝不往下走 press_home 切桌面（2026-09-12 用户禁切桌面）。
    if any(kw in _texts for kw in ("已被抢", "货源已定", "已下架")):
        ctx.bump("unknown_grabbed_dialog")
        ctx.emit("warning", "未知页实为「已被抢/已下架」弹窗（OCR 命中），关闭并回列表")
        _close_grabbed_dialog(ctx, fs, boxes)
        return
    if any(
        ("自定义装货点" in t or "自定义卸货点" in t or "选择装货点" in t) for t in _texts
    ):
        label = "出发地" if any("出发地" in t for t in _texts) else "目的地"
        roi = ctx.store.get(
            "fields", "entries.panel_tab_roi", (0.0, 0.24, 1.0, 0.30)
        )
        ctx.emit("info", f"识别为城市面板「自定义装/卸货点」标签页，点「{label}」切回网格")
        if ctx.kit.click_text_norm(label, roi, reason="装货点页→切回省市区网格"):
            ctx.reset("unknown_streak")
            return

    if ctx.kit.click_close_candidate(fs=fs) is not None:
        _, again = ctx.kit.wait_state(LIST_STATES + (ScreenState.DETAIL,), timeout=3.0, interval=0.4)
        if again.state is not ScreenState.UNKNOWN:
            ctx.reset("unknown_streak")
            return

    ctx.kit.swipe_back_gesture()
    _, again = ctx.kit.wait_state(LIST_STATES + (ScreenState.DETAIL,), timeout=3.0, interval=0.4)
    if again.state is not ScreenState.UNKNOWN:
        ctx.bump("unknown_exit_by_swipe")
        ctx.reset("unknown_streak")
        return

    streak = ctx.bump("unknown_streak")
    ctx.emit("warning", f"未知页第 {streak} 次，按返回键")
    ctx.kit.press_back()
    if streak >= ctx.limit("recover_max", 3):
        # 2026-09-12 用户明确：绝不切桌面。切桌面会退出 App，重进又撞同一张卡，
        # 形成「桌面 ⇄ 已被抢详情」死循环。未知页无法恢复就停下等人，不再按主页键。
        ctx.emit("error", "未知页无法恢复，停止等待人工（不切桌面）")
        ctx.stop("unknown_unrecoverable")
        ctx.reset("unknown_streak")
        ctx.kit.save_debug(fs, "unknown_unrecoverable")

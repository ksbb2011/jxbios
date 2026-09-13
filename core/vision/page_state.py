"""页面九态判定 —— 全系统唯一判定入口。

纪律（历史为违反这些栽过多次）：
    * 每帧判定固定顺序，任何 handler 不得绕过：
      ⓪ 白色全国货源 Tab（最优先，压过弹窗判定）
      ① 弹窗 / 异常页扫描
      ② 详情页（左上返回 且 右上分享 同时命中，不是「或」）
      ③ 城市面板（清空筛选 且 确认 同时命中；用红色标题字区分出发地/目的地侧）
      ④ 列表页（红色全国货源 或 红色返回顶部 —— 同一位置两种显示状态）
      ⑤ 聊天页（语音按钮，聊天页唯一可靠判据）
      ⑥ 桌面
      ⑦ UNKNOWN
    * 图标类阈值一律 >=0.88（阈值实际值全部来自配置，这里不写死）；
    * 本模块只做判定与打分，不做任何动作；动作由 flow 层根据结果执行。

⚠️ 列表页顶部隐藏判定（2026-09-09 用户明确修正）：
    顶部是否隐藏，靠「找货记录 find_records」+「司机课堂 driver_school」组合判定 ——
    这两个图标在顶部并排；隐藏时都在屏幕外，任一命中即说明顶部已显示。
    此前「用听单开/关判顶部」的旧规则已作废：听单仅作顶部显示后的子状态。
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from core.config_store import ConfigStore
from core.vision.matcher import MatchResult, TemplateMatcher

LogFn = Optional[Callable[[str, str], None]]


class ScreenState(str, enum.Enum):
    """九态（外加 NO_MORE 变体）。"""

    DESKTOP = "desktop"
    HOME_OTHER = "home_other"  # 首页但非订单列表（白色 Tab）
    ORDER_LIST = "order_list"
    ORDER_LIST_NO_MORE = "order_list_no_more"  # 列表已到底，只能换路线/换车
    DETAIL = "detail"
    CITY_ORIGIN = "city_origin"
    CITY_DEST = "city_dest"
    DIALOG_LIST = "dialog_list"
    DIALOG_DETAIL = "dialog_detail"
    CHAT = "chat"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str) -> "ScreenState":
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


# 弹窗/异常页模板（命中即 DIALOG_*，origin 决定 list/detail）
DIALOG_TEMPLATES = ("dialog_offline", "dialog_grabbed", "dialog_know", "page_add_route")

# 弹窗共证的关闭信号（正常页面几乎不出现，绝不包含页面级返回箭头模板：
# back_arrow_top_left 在详情/列表页 100% 命中，无区分信息量）
_DIALOG_CLOSE_SIGNALS = ("close_x", "offline_back", "page_add_route_back")


@dataclass
class PageResult:
    """一次判定的完整结果。"""

    state: ScreenState
    hits: Dict[str, MatchResult] = field(default_factory=dict)
    top_visible: bool = False
    dialog: Optional[MatchResult] = None
    city_side_unknown: bool = False
    note: str = ""
    # 听单是否开启（True=听单中）。由颜色判定得出，见 _detect_tingdan。
    tingdan_on: bool = False
    # 听单按钮中心点，供 ensure_tingdan_off 点击关闭
    tingdan_point: Optional[Tuple[int, int]] = None
    # 判定尚未稳定（StableDetector 连续帧未达成一致）。
    # pending=True 时主循环**只等下一帧、绝不做任何动作**——
    # 不能当成 UNKNOWN，否则正常页面会被走未知页分支（关闭/左滑/返回键）反复折腾。
    pending: bool = False

    def get(self, name: str) -> Optional[MatchResult]:
        return self.hits.get(name)

    def has(self, name: str) -> bool:
        return name in self.hits

    def is_list_like(self) -> bool:
        return self.state in (ScreenState.ORDER_LIST, ScreenState.ORDER_LIST_NO_MORE)

    def summary(self) -> str:
        parts = [self.state.value]
        if self.state in (ScreenState.ORDER_LIST, ScreenState.ORDER_LIST_NO_MORE):
            parts.append(f"top={'on' if self.top_visible else 'off'}")
            parts.append(f"tingdan={'ON' if self.tingdan_on else 'off'}")
        if self.dialog is not None:
            parts.append(f"dialog={self.dialog.name}:{self.dialog.score:.2f}")
        top = sorted(self.hits.items(), key=lambda kv: kv[1].score, reverse=True)[:3]
        for name, hit in top:
            parts.append(f"{name}={hit.score:.2f}")
        return " ".join(parts)


# 命名（与 templates.json 逻辑名一致）
_T_SOURCE_WHITE = ("source_white",)
_T_SOURCE_RED = ("source_red",)
_T_DETAIL_BACK = ("detail_back",)
_T_DETAIL_SHARE = ("detail_share",)
_T_FILTER = ("filter_clear", "filter_confirm_red", "filter_confirm_pale")
_T_LIST_STRONG = ("back_to_top",)  # 返回顶部：无红 Tab 时的列表强证
# 顶部隐藏判定组合（2026-09-09 修正）：任一命中即顶部已显示
_T_FIND_RECORDS = ("find_records",)
_T_DRIVER_SCHOOL = ("driver_school",)


def _detect_tingdan(frame, cfg: dict):
    """用「红色像素占比」判定听单开关，返回 (是否听单中, 红色占比, 区域中心)。

    为什么不用模板匹配：
        「听单中」和「关」两个状态都是顶部右侧的小横条，灰度结构高度相似，
        TM_CCOEFF_NORMED 分数总是贴得很近——实测听单**开着**时
        tingdan_on=0.60、tingdan_off=0.73（反而 off 更高），完全区分不开。
        后果很实际：程序会误判成「已关」从而不去关它，听单一直开着自动抢单。

    颜色差异则是压倒性的，实测两个模板图：
        听单中  红色占比 24.9%   R-B = +28.3
        关      红色占比  0.0%   R-B =  +1.5
    零重叠，且不受右侧柱状图标的动画影响（动画改的是形状，红色始终在）。
    """
    region = cfg.get("region")
    if not region or len(region) != 4:
        return False, 0.0, None
    h, w = frame.shape[:2]
    x1, y1 = max(0, int(float(region[0]) * w)), max(0, int(float(region[1]) * h))
    x2, y2 = min(w, int(float(region[2]) * w)), min(h, int(float(region[3]) * h))
    if x2 <= x1 or y2 <= y1:
        return False, 0.0, None

    roi = frame[y1:y2, x1:x2]
    b, g, r = (roi[:, :, c].astype(np.float32) for c in range(3))
    min_red = float(cfg.get("min_red", 90) or 90)
    factor = float(cfg.get("red_factor", 1.25) or 1.25)
    red = (r > min_red) & (r > b * factor) & (r > g * factor)
    ratio = float(red.mean())
    thr = float(cfg.get("red_ratio_threshold", 0.08) or 0.08)
    center = (int((x1 + x2) / 2), int((y1 + y2) / 2))
    return ratio >= thr, ratio, center


class PageDetector:
    """九态判定器。构造后每帧调 detect()，结果不可缓存跨帧使用。"""

    def __init__(self, store: ConfigStore, matcher: TemplateMatcher, log: LogFn = None) -> None:
        self._store = store
        self._matcher = matcher
        self._log = log
        self._specs: Dict[str, object] = {}
        # 详情页双按钮阈值（AND 同时命中，数值可配置）
        self._detail_back_thr = float(store.get("thresholds", "detail.back", 0.86))
        self._detail_share_thr = float(store.get("thresholds", "detail.share", 0.86))
        # 列表页兜底信号：结算行锚点（结算X爽约X评价X去抢单 行）。
        # 阈值 0.60：介于非列表页误命中上限与真列表下限之间。
        self._list_settle_thr = float(store.get("thresholds", "list.settle_anchor", 0.60))
        # 到底判据（列表页专用）：见 _reached_bottom
        self._eol_cfg = store.get("thresholds", "list.no_more", {}) or {}
        self._eol_key: Optional[tuple] = None
        self._eol_val: bool = False
        self._eol_ts: float = 0.0
        self._eol_logged: Optional[tuple] = None
        # OCR 引擎（可选，由 task 装配注入）。为 None 时**不判到底**。
        self.ocr = None
        # 城市面板兜底阈值
        self._city_clear_thr = float(store.get("thresholds", "city.clear", 0.83))
        self._city_confirm_thr = float(store.get("thresholds", "city.confirm", 0.83))
        self._city_key: Optional[str] = None
        self._city_ts: float = 0.0
        self._city_res: Optional["PageResult"] = None
        # 桌面 OCR 兜底缓存
        self._desktop_key: Optional[str] = None
        self._desktop_ts: float = 0.0
        self._desktop_res: Optional["PageResult"] = None
        # 详情页 OCR 兜底缓存（导航栏图标因背景色变异判不出时用稳定字段识别）
        self._detail_ocr_key: Optional[str] = None
        self._detail_ocr_ts: float = 0.0
        self._detail_ocr_val: bool = False

    def _reached_bottom(self, frame: np.ndarray) -> bool:
        """列表是否真的滚到底：底部出现「推荐货源」分隔条（OCR 文字判据）。

        OCR 未注入（self.ocr=None）时返回 False——不判到底，宁可多滑一屏。
        """
        cfg = self._eol_cfg or {}
        if not bool(cfg.get("require_eol", True)):
            return True
        if self.ocr is None:
            return False

        h, w = frame.shape[:2]
        roi = cfg.get("roi") or [0.0, 0.75, 1.0, 1.0]
        x0 = max(0, int(float(roi[0]) * w))
        y0 = max(0, int(float(roi[1]) * h))
        x1 = min(w, int(float(roi[2]) * w))
        y1 = min(h, int(float(roi[3]) * h))
        if x1 <= x0 or y1 <= y0:
            return False

        patch = np.ascontiguousarray(frame[y0:y1, x0:x1])
        try:
            import hashlib

            key = (patch.shape, hashlib.md5(patch.tobytes()).hexdigest())
        except Exception:  # noqa: BLE001
            key = None
        if key is not None and self._eol_key == key:
            return self._eol_val
        now = time.time()
        gap = float(cfg.get("min_interval_sec", 1.0) or 1.0)
        if self._eol_key is not None and now - self._eol_ts < gap:
            return self._eol_val

        text = ""
        try:
            boxes = self.ocr.ocr(patch) or []
            text = "".join(str(getattr(b, "text", "") or "") for b in boxes)
        except Exception as exc:  # noqa: BLE001 OCR 失败不能影响页面判定
            self._emit("warning", f"到底判据 OCR 失败: {exc}")
            text = ""

        keywords = cfg.get("keywords") or ["推荐货源", "没有更多", "已加载全部", "到底了"]
        hit = any(str(kw) in text for kw in keywords)
        self._eol_ts = now
        if key is not None:
            self._eol_key, self._eol_val = key, hit
        else:
            self._eol_val = hit
        if hit and key is not None and key != self._eol_logged:
            self._eol_logged = key
            # 打出**命中的关键词**而不只是 OCR 前 20 字：底部 ROI 里订单卡片文字常排在
            # 关键词前面，只打前 20 字会让人误以为"拿卡片文字当到底判据"（2026-09-13
            # 排查时就被误导过一次）。
            kw = next((str(k) for k in keywords if str(k) in text), "")
            self._emit("info", f"列表到底：底部命中「{kw}」（OCR 片段: {text[:30]}）")
        return hit

    def _tpl(self, name: str):
        """按逻辑名取注册模板（带缓存）。"""
        spec = self._specs.get(name)
        if spec is None:
            spec = self._store.template(name)
            self._specs[name] = spec
        return spec

    def _emit(self, level: str, msg: str) -> None:
        if self._log:
            self._log(level, msg)

    # ---------------------------------------------------------------- 预处理

    def _preprocess(self, frame: np.ndarray) -> np.ndarray:
        """判定前预处理（目前只有可选 CLAHE）。默认关闭。"""
        cfg = self._store.section("vision").get("preprocess", {}) or {}
        clahe_cfg = cfg.get("clahe", {}) or {}
        if not clahe_cfg.get("enabled", False):
            return frame
        try:
            import cv2

            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            l_ch, a_ch, b_ch = cv2.split(lab)
            clahe = cv2.createCLAHE(
                clipLimit=float(clahe_cfg.get("clip_limit", 2.0) or 2.0),
                tileGridSize=tuple(int(x) for x in clahe_cfg.get("tile_grid", (8, 8))),
            )
            merged = cv2.merge([clahe.apply(l_ch), a_ch, b_ch])
            return cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)
        except Exception:
            return frame

    # ---------------------------------------------------------------- 判定

    def detect(
        self,
        frame: np.ndarray,
        origin: Optional[ScreenState] = None,
        red_texts: Optional[Sequence[str]] = None,
    ) -> PageResult:
        """在帧上做一次九态判定。

        frame     —— 手机逻辑点分辨率帧（iPhone X = 375x812）
        origin    —— 上一稳定态（用于区分列表/详情弹窗，仅提示性）
        red_texts —— 城市面板红色标题 OCR 文本（区分出发地/目的地侧），可省略
        """
        frame = self._preprocess(frame)
        hits: Dict[str, MatchResult] = {}

        # ⓪ 白色 Tab 最优先（压过弹窗）
        white = self._matcher.find(frame, self._tpl(_T_SOURCE_WHITE[0]))
        if white is not None:
            hits["source_white"] = white
            return PageResult(state=ScreenState.HOME_OTHER, hits=hits)

        # ⓪·⑤ 详情页快速确认（origin=列表页时，即「点进详情后的场景」）：
        # 只匹配 detail_back + detail_share 两个模板，命中即返回 DETAIL。
        # 这是「进详情→检测到 detail 状态 ≈11s」的最大提速点——完整 detect 每帧
        # 无差别匹配 15+ 模板要 1~2s，而真实运行 90% 时间在列表⇄详情之间切，
        # 只需 2 个模板（约 0.3s）就能定态。不命中则回退下方完整判定（弹窗/城市等）。
        if origin in (ScreenState.ORDER_LIST, ScreenState.ORDER_LIST_NO_MORE):
            back = self._matcher.find(
                frame, self._tpl(_T_DETAIL_BACK[0]), threshold=self._detail_back_thr
            )
            if back is not None:
                hits["detail_back"] = back
                share = self._matcher.find(
                    frame, self._tpl(_T_DETAIL_SHARE[0]), threshold=self._detail_share_thr
                )
                if share is not None:
                    hits["detail_share"] = share
                    return PageResult(state=ScreenState.DETAIL, hits=hits)

        # ① 弹窗 / 异常页（双信号：主体高分直判，或低分+关闭按钮共证）
        body_high = float(self._store.get("thresholds", "dialog.body_high", 0.90))
        body_low = float(self._store.get("thresholds", "dialog.body_low", 0.80))
        close_confirm = float(self._store.get("thresholds", "dialog.close_confirm", 0.86))
        dialog_hit: Optional[MatchResult] = None
        for name in DIALOG_TEMPLATES:
            hit = self._matcher.find(frame, self._tpl(name), threshold=body_low)
            if hit is not None:
                hits[name] = hit
                if dialog_hit is None or hit.score > dialog_hit.score:
                    dialog_hit = hit
        if dialog_hit is not None:
            confirmed = dialog_hit.score >= body_high and dialog_hit.name != "dialog_know"
            if not confirmed:
                for cname in _DIALOG_CLOSE_SIGNALS:
                    chit = self._matcher.find(frame, self._tpl(cname), threshold=close_confirm)
                    if chit is not None:
                        hits[cname] = chit
                        confirmed = True
                        break
            if confirmed:
                if origin in (ScreenState.DETAIL, ScreenState.CHAT):
                    state = ScreenState.DIALOG_DETAIL
                else:
                    state = ScreenState.DIALOG_LIST
                return PageResult(state=state, hits=hits, dialog=dialog_hit)

        # ② 详情页：返回 且 分享 同时命中（数值来自配置 thresholds.detail.*）
        back = self._matcher.find(frame, self._tpl(_T_DETAIL_BACK[0]), threshold=self._detail_back_thr)
        if back is not None:
            hits["detail_back"] = back
            share = self._matcher.find(frame, self._tpl(_T_DETAIL_SHARE[0]), threshold=self._detail_share_thr)
            know = hits.get("dialog_know")
            # 变异详情页：背景色变（蓝/粉红/黄）导致 share 图标判不出时，用底部稳定字段
            # （立即抢单/净得运费详情等，与背景色无关）OCR 兜底——否则会落进 unknown。
            if share is not None or know is not None or self._detail_ocr_backup(frame):
                if share is not None:
                    hits["detail_share"] = share
                return PageResult(state=ScreenState.DETAIL, hits=hits)

        # ③ 城市面板：清空筛选（强信号，城市面板独有控件）+ 确认（红/浅红任一）
        clear = self._matcher.find(frame, self._tpl(_T_FILTER[0]), threshold=self._city_clear_thr)
        if clear is not None:
            hits["filter_clear"] = clear
            confirm_red = self._matcher.find(frame, self._tpl(_T_FILTER[1]), threshold=self._city_confirm_thr)
            confirm_pale = self._matcher.find(frame, self._tpl(_T_FILTER[2]))
            confirm = confirm_red or confirm_pale
            # red_texts 原本由调用方传入，但全工程无人传（StableDetector 只传 origin），
            # 等于恒为 None → 城市面板永远 side_unknown → 异常恢复时会把「目的地」
            # 当成「出发地」去设，路线直接错。故调用方没给就自己 OCR 一次。
            if red_texts is None:
                red_texts = self._city_red_texts(frame)
            side_unknown = True
            side_state = ScreenState.CITY_ORIGIN
            if red_texts:
                for text in red_texts:
                    if "目的地" in text:
                        side_state = ScreenState.CITY_DEST
                        side_unknown = False
                        break
                    if "出发地" in text:
                        side_state = ScreenState.CITY_ORIGIN
                        side_unknown = False
                        break
            if confirm is not None:
                hits["filter_confirm"] = confirm
                # 可点性走颜色判定：红/浅红灰度模板区分不开（浅红态下 confirm_red 也 0.997），
                # 若按模板会把「不可点」当「可点」去死点确认。正红/浅红只有 G/B 通道差得开。
                clickable, r_ratio = self._confirm_clickable(frame)
                return PageResult(
                    state=side_state,
                    hits=hits,
                    city_side_unknown=side_unknown,
                    note=f"confirm={'red' if clickable else 'pale'}(r={r_ratio:.2f})",
                )
            backup = self._try_city_ocr_backup(frame, red_texts)
            if backup is not None:
                backup.hits.update(hits)
                return backup

        # ④ 列表页：红色全国货源 / 返回顶部 / 结算行锚点（三路 OR，任一命中即列表页）
        candidates: List[tuple] = []
        red = self._matcher.find(frame, self._tpl(_T_SOURCE_RED[0]))
        if red is not None:
            candidates.append(("source_red", red))
        top_btn = self._matcher.find(frame, self._tpl(_T_LIST_STRONG[0]))
        if top_btn is not None:
            candidates.append(("back_to_top", top_btn))
        settle = self._matcher.find(
            frame, self._tpl("settle_anchor"), threshold=self._list_settle_thr
        )
        if settle is not None:
            candidates.append(("settle_anchor", settle))
        # ===== 顶部可见性（2026-09-09 用户修正）=====
        # 靠 find_records / driver_school 组合判定：这两个图标在顶部并排，
        # 隐藏时都在屏幕外，任一命中即说明顶部已显示。
        # ⚠️ 此查询**独立于**列表页判据：它们只在列表页顶部出现，命中即可反推
        # 「列表页 + 顶部显示」，即便 source_red/back_to_top 模板缺失也能兜住。
        # 旧规则「用听单开/关判顶部」已作废（听单仅作顶部显示后的子状态）。
        fr = self._matcher.find(frame, self._tpl(_T_FIND_RECORDS[0]))
        if fr is not None:
            hits["find_records"] = fr
        ds = self._matcher.find(frame, self._tpl(_T_DRIVER_SCHOOL[0]))
        if ds is not None:
            hits["driver_school"] = ds
        top_visible = ("find_records" in hits) or ("driver_school" in hits)

        if candidates or top_visible:
            if candidates:
                tab_name, list_hit = max(candidates, key=lambda kv: kv[1].score)
                hits[tab_name] = list_hit
                note_name = list_hit.name
            else:
                note_name = "top_icons_only"  # 列表页判据缺失，靠顶部图标反推列表页

            # 听单状态（顶部显示后的子状态，用于决定要不要关听单）
            # 图标阈值放宽到 0.70：与「听单中」模板在同帧下的 0.36 仍有足够区分度。
            for ting in ("tingdan_on", "tingdan_off"):
                t = self._matcher.find(frame, self._tpl(ting), threshold=0.70)
                if t is not None:
                    hits[ting] = t

            if top_btn is not None:
                hits["back_to_top"] = top_btn
                state = (
                    ScreenState.ORDER_LIST_NO_MORE
                    if self._reached_bottom(frame)
                    else ScreenState.ORDER_LIST
                )
            else:
                state = ScreenState.ORDER_LIST

            # 听单状态走颜色判定（模板匹配在这两个状态间区分不开）
            # calibration 在 hardware.json 下，不是顶层 section（曾误写 section("calibration")，
            # 读的是不存在的 config/calibration.json，导致听单配置静默失效）。
            cal = self._store.get("hardware", "calibration", {}) or {}
            ting_cfg = cal.get("tingdan", {}) or {}
            if ting_cfg.get("enabled", False):
                ting_on, _ratio, ting_pt = _detect_tingdan(frame, ting_cfg)
            else:
                ting_on, ting_pt = False, None

            return PageResult(
                state=state,
                hits=hits,
                # 顶部可见性：find_records / driver_school 任一命中即顶部已显示
                top_visible=top_visible,
                tingdan_on=ting_on,
                tingdan_point=ting_pt,
                note=note_name,
            )

        # ⑤ 聊天页：语音按钮（聊天页唯一可靠判据）
        voice = self._matcher.find(frame, self._tpl("chat_voice"))
        if voice is not None:
            hits["chat_voice"] = voice
            return PageResult(state=ScreenState.CHAT, hits=hits)

        # ⑥ 桌面：图标模板 或 OCR「运满满」文字 任一命中即桌面态（双保险）
        desk = self._matcher.find(frame, self._tpl("desktop_ymm_icon"))
        if desk is not None:
            hits["desktop_ymm_icon"] = desk
            return PageResult(state=ScreenState.DESKTOP, hits=hits)
        desk_ocr = self._try_desktop_ocr_backup(frame)
        if desk_ocr is not None:
            desk_ocr.hits.update(hits)
            return desk_ocr

        # ⑦ UNKNOWN 前：OCR 文字兜底城市面板（filter 模板全没命中时的救场）
        backup = self._try_city_ocr_backup(frame, red_texts)
        if backup is not None:
            backup.hits.update(hits)
            return backup

        return PageResult(state=ScreenState.UNKNOWN, hits=hits)

    def _city_red_texts(self, frame: np.ndarray) -> List[str]:
        """取城市面板的红色标题文字（「请选择出发地」/「请选择目的地」）。

        只在城市分支触发（清空筛选命中之后），不是每帧都跑，代价可控。
        红字判定用放宽档 city_red_text_boxes（细红字落在白底大框里，严格档永远筛不出，
        见 ocr.py 里 [0831-H] 的教训）；放宽档也筛不出时退回关键词粗筛。
        """
        if self.ocr is None:
            return []
        try:
            boxes = self.ocr.ocr(frame) or []
        except Exception as exc:  # noqa: BLE001 OCR 失败不能影响页面判定
            self._emit("warning", f"城市面板红字 OCR 失败: {exc}")
            return []
        reds = self.ocr.city_red_text_boxes(frame, boxes)
        if not reds:
            reds = self.ocr.filter_red_text(boxes, frame)
        return [str(getattr(b, "text", "") or "") for b in reds]

    def _confirm_clickable(self, frame: np.ndarray) -> Tuple[bool, float]:
        """确认按钮可点性：正红=可点，浅红=不可点（灰度模板区分不开）。

        实测（2026-09-10）：浅红确认按钮平均 RGB=[249,193,202]——R 很高但 G/B 也
        接近 200，用「R 显著高于 G/B」的宽松红判据照样命中（占比 44%），故必须加
        G/B 上界：正红 = R>180 且 G<120 且 B<120。此判据下浅红态占比 0.0，正红态
        应显著高（待实测定阈值，默认 0.15）。
        """
        cfg = self._store.get("thresholds", "city.confirm_clickable", {}) or {}
        roi = cfg.get("roi", [0.5, 0.88, 1.0, 0.99])
        thr = float(cfg.get("red_ratio", 0.15) or 0.15)
        h, w = frame.shape[:2]
        x0, y0 = max(0, int(roi[0] * w)), max(0, int(roi[1] * h))
        x1, y1 = min(w, int(roi[2] * w)), min(h, int(roi[3] * h))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return False, 0.0
        patch = frame[y0:y1, x0:x1].astype(np.int16)
        b, g, r = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
        ratio = float(((r > 180) & (g < 120) & (b < 120)).mean())
        return ratio >= thr, ratio

    def _detail_ocr_backup(self, frame: np.ndarray) -> bool:
        """详情页 OCR 兜底：导航栏图标因背景色变异（蓝/粉红/黄底白图标）判不出时，
        用底部稳定字段识别。

        稳定字段来自回归集 OCR 抽样（2026-09-10）：「立即抢单」「净得运费详情」等
        每张详情页都稳定出现，与导航栏背景色无关。只在 detail_back 命中而
        detail_share 未命中时调用（变异详情页场景），带帧 hash 缓存避免反复整屏 OCR。
        """
        if self.ocr is None:
            return False
        cfg = self._store.get("thresholds", "detail.ocr_backup", {}) or {}
        if not bool(cfg.get("enabled", True)):
            return False
        # 用短子串而非整词：OCR 会把「净得运费详情」错认成「净得运费译情」，
        # 故取「净得运费」前四字；命中 2 个即判详情页（任一单词都可能因 OCR 抖动漏识）。
        keywords = cfg.get("keywords", ["立即抢单", "净得运费", "货主要求", "申请加价"])
        min_hits = int(cfg.get("min_hits", 2) or 2)

        h, w = frame.shape[:2]
        try:
            import hashlib

            patch = frame[int(0.5 * h):, :]
            key = hashlib.md5(np.ascontiguousarray(patch).tobytes()).hexdigest()
        except Exception:  # noqa: BLE001
            key = None
        now = time.time()
        if key is not None and self._detail_ocr_key == key and now - self._detail_ocr_ts < 1.5:
            return self._detail_ocr_val

        try:
            boxes = self.ocr.ocr(frame, roi=[0, int(0.5 * h), w, int(0.5 * h)]) or []
            texts = [str(getattr(b, "text", "") or "") for b in boxes]
        except Exception as exc:  # noqa: BLE001
            self._emit("warning", f"详情页 OCR 兜底失败: {exc}")
            texts = []
        hit = sum(1 for kw in keywords if any(kw in t for t in texts))
        val = hit >= min_hits
        self._detail_ocr_key = key
        self._detail_ocr_ts = now
        self._detail_ocr_val = val
        return val

    def _try_city_ocr_backup(self, frame: np.ndarray, red_texts=None) -> Optional["PageResult"]:
        """OCR 文字兜底识别城市面板（filter 模板漏命中时救场）。"""
        if self.ocr is None:
            return None
        cfg = self._store.get("thresholds", "city.ocr_backup", {}) or {}
        h, w = frame.shape[:2]
        roi = cfg.get("roi") or [0.0, 0.78, 1.0, 1.0]
        x0 = max(0, int(float(roi[0]) * w))
        y0 = max(0, int(float(roi[1]) * h))
        x1 = min(w, int(float(roi[2]) * w))
        y1 = min(h, int(float(roi[3]) * h))
        if x1 <= x0 or y1 <= y0:
            return None
        patch = np.ascontiguousarray(frame[y0:y1, x0:x1])
        try:
            import hashlib

            key = hashlib.md5(patch.tobytes()).hexdigest()
        except Exception:  # noqa: BLE001
            key = None
        now = time.time()
        gap = float(cfg.get("min_interval_sec", 1.0) or 1.0)
        if key is not None and self._city_key == key and now - self._city_ts < gap:
            return self._city_res
        try:
            boxes = self.ocr.ocr(patch) or []
            text = "".join(str(getattr(b, "text", "") or "") for b in boxes)
        except Exception as exc:  # noqa: BLE001 OCR 失败不能影响页面判定
            self._emit("warning", f"城市面板 OCR 兜底失败: {exc}")
            text = ""
        self._city_ts = now
        self._city_key = key
        if "清空选择" in text and "确认" in text:
            side_unknown = True
            side_state = ScreenState.CITY_ORIGIN
            if "目的地" in text:
                side_state = ScreenState.CITY_DEST
                side_unknown = False
            elif "出发地" in text:
                side_state = ScreenState.CITY_ORIGIN
                side_unknown = False
            if red_texts:
                for t in red_texts:
                    if "目的地" in t:
                        side_state = ScreenState.CITY_DEST
                        side_unknown = False
                        break
                    if "出发地" in t:
                        side_state = ScreenState.CITY_ORIGIN
                        side_unknown = False
                        break
            res = PageResult(
                state=side_state,
                city_side_unknown=side_unknown,
                note="ocr_backup:清空选择+确认",
            )
            self._city_res = res
            return res
        self._city_res = None
        return None

    def _try_desktop_ocr_backup(self, frame: np.ndarray) -> Optional["PageResult"]:
        """OCR 文字兜底识别桌面态（图标模板漏命中时救场）。"""
        if self.ocr is None:
            return None
        cfg = self._store.get("thresholds", "desktop.ocr_backup", {}) or {}
        h, w = frame.shape[:2]
        roi = cfg.get("roi") or [0.0, 0.0, 1.0, 1.0]
        x0 = max(0, int(float(roi[0]) * w))
        y0 = max(0, int(float(roi[1]) * h))
        x1 = min(w, int(float(roi[2]) * w))
        y1 = min(h, int(float(roi[3]) * h))
        if x1 <= x0 or y1 <= y0:
            return None
        patch = np.ascontiguousarray(frame[y0:y1, x0:x1])
        try:
            import hashlib

            key = hashlib.md5(patch.tobytes()).hexdigest()
        except Exception:  # noqa: BLE001
            key = None
        now = time.time()
        gap = float(cfg.get("min_interval_sec", 1.0) or 1.0)
        if key is not None and self._desktop_key == key and now - self._desktop_ts < gap:
            return self._desktop_res
        try:
            boxes = self.ocr.ocr(patch) or []
            text = "".join(str(getattr(b, "text", "") or "") for b in boxes)
        except Exception as exc:  # noqa: BLE001 OCR 失败不能影响页面判定
            self._emit("warning", f"桌面 OCR 兜底失败: {exc}")
            text = ""
        self._desktop_ts = now
        self._desktop_key = key
        if "运满满" in text:
            res = PageResult(state=ScreenState.DESKTOP, hits={"desktop_ocr": True}, note="ocr_backup:运满满")
            self._desktop_res = res
            return res
        self._desktop_res = None
        return None

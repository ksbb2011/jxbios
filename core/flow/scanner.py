"""列表页扫描：切卡 → 探字段 → 过规则 → 去重入队。

切卡为什么用「多信号融合」而不是单一锚点（旧工程真机结论）：
    * 结算行模板 settle_anchor 受分辨率与字重影响，单靠它会整屏漏切；
    * OCR 常把「结算」错识成「运页/结置/算」，单靠文字同样会漏；
    * 于是把「模板锚点 + 结算/评价关键词 + 标题箭头(→)」三路信号合并：
      前两路定卡片下边界，箭头定上边界，任一路缺失仍能切出来。

字段定位为什么用「结算行 ± 偏移」而不是全卡 OCR：
    一屏十几张卡，全卡 OCR 慢且会把相邻卡的文字混进来。
    车型/车长/吨位/方数在卡片里的相对位置是固定的，按偏移取小 ROI 又准又快。
    偏移量在 config/fields.json（calibrated=false 表示尚未实机标定，
    必须用 tools/probe_cards.py 在真机上校准后才可信）。

去重为什么用内容指纹而不是坐标：
    上滑有 20%~40% 重叠，同一张卡会以不同 y 反复出现；坐标去重必然漏单或重复。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from core.config_store import ConfigStore
from core.devices.frame_source import FrameSet
from core.domain.orders import OrderCard
from core.flow.actions import ActionKit
from core.flow.context import FlowContext
from core.flow.order_store import OrderStore
from core.vision.ocr import TextBox
from core.vision.stable_state import has_content

# 结算行的 OCR 别名（旧工程实测错字，缺一个就少切一张卡）
SETTLE_KEYWORDS = ("结算", "评价", "结置", "运页")
TITLE_ARROW = ("→", "->")


def proc_memory_mb() -> float:
    """当前进程物理内存占用（MB）；取不到返回 -1。

    用途：定位「OCR 越跑越慢」是否内存累积/泄漏。2026-09-08 打包版实测一轮
    20 分钟内整屏 OCR 从 3s 退化到 19s（6 倍），需先确认内存是持续上涨还是平稳
    ——把内存与增量附在耗时日志里，跑一轮即可判定，不必额外挂分析器。

    取不到（非 Windows 且无 psutil）时返回 -1，调用方据此跳过内存字段，
    绝不影响主流程。
    """
    try:
        import psutil  # type: ignore

        return psutil.Process().memory_info().rss / 1048576.0
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb
        )
        if ok:
            return pmc.WorkingSetSize / 1048576.0
    except Exception:
        pass
    return -1.0


def proc_handle_count() -> int:
    """当前进程打开的句柄数（仅 Windows 可靠）；取不到返回 -1。

    配合 proc_memory_mb 一起看：句柄只涨不跌往往意味着资源（线程/文件/事件/
    串口句柄）泄漏，是比内存更灵敏的泄漏信号。psutil 不提供该指标，直接用
    Win32 GetProcessHandleCount；非 Windows 直接 -1，绝不拖累主流程。
    """
    try:
        import ctypes
        from ctypes import wintypes

        n = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.GetProcessHandleCount(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(n)
        )
        if ok:
            return int(n.value)
    except Exception:
        pass
    return -1


@dataclass
class CardGeometry:
    """切卡几何约束（来自 config/fields.json.card.geometry）。"""

    min_height: int = 90
    max_height: int = 420
    first_card_min_y: int = 90


class FingerprintCache:
    """订单指纹去重：内容哈希 + 过期淘汰。

    带 TTL 是必须的：跑一整天会积累上万个指纹，纯 LRU 也会把「同一张卡隔很久
    重新出现」误判为重复（货源会刷新，价格可能变了，该重新看一眼）。
    """

    def __init__(self, max_size: int = 500, ttl_sec: float = 1800.0) -> None:
        self.max_size = int(max_size)
        self.ttl_sec = float(ttl_sec)
        self._items: Dict[str, float] = {}

    # 卡片上的相对时间（「刚刚」「59分前」「3分钟前」「2小时前」）过一会儿就变，
    # 不剔除的话同一张订单的指纹每分钟都不同、去重完全失效——
    # 实测表现：刚看完的详情返回列表，又被原样点进去一次。
    #
    # 2026-09-07 实测（第一轮 20 分钟）：260元/56km 那张单被点进详情 **22 次**
    # （≈每分钟一次），根因就是旧正则只认「分钟|小时|天 前」，而真机写的是
    # 「59分前」——「N分前」漏剔除，指纹随时间跳变，硬判据形同虚设。
    _RELATIVE_TIME = re.compile(
        r"(?:刚刚|\d+\s*(?:秒|分|分钟|小时|钟头|天|周|月|年)前)"
    )

    @staticmethod
    def make(text: str) -> str:
        """内容指纹：剔除相对时间后只留中英文数字（防 OCR 抖动导致漏去重）。"""
        text = FingerprintCache._RELATIVE_TIME.sub("", text or "")
        cleaned = re.sub(r"[\s\W_]+", "", text)
        if not cleaned:
            return ""
        return hashlib.md5(cleaned.encode("utf-8")).hexdigest()[:16]

    def seen(self, fp: str) -> bool:
        if not fp:
            return False
        self._purge()
        return fp in self._items

    def add(self, fp: str) -> None:
        if not fp:
            return
        self._items[fp] = time.time()
        if len(self._items) > self.max_size:
            # 淘汰最老的一批，保留最近的一半
            for key, _ in sorted(self._items.items(), key=lambda kv: kv[1])[: self.max_size // 2]:
                self._items.pop(key, None)

    def _purge(self) -> None:
        if not self.ttl_sec:
            return
        now = time.time()
        for key, ts in [kv for kv in self._items.items() if now - kv[1] > self.ttl_sec]:
            self._items.pop(key, None)

    def __len__(self) -> int:
        return len(self._items)


class CardSplitter:
    """把一屏切成若干订单卡片矩形（fast 帧坐标）。"""

    def __init__(self, store: ConfigStore, kit: ActionKit) -> None:
        self.store = store
        self.kit = kit
        geo = store.get("fields", "card.geometry", {}) or {}
        self.geo = CardGeometry(
            min_height=int(geo.get("min_height_px", 90) or 90),
            max_height=int(geo.get("max_height_px", 420) or 420),
            first_card_min_y=int(geo.get("first_card_min_y", 90) or 90),
        )
        cfg = store.get("fields", "card.anchor", {}) or {}
        self.anchor_name = str(cfg.get("primary", "settle_anchor"))
        self.anchor_thr = float(cfg.get("threshold", 0.72) or 0.72)

    # ---------------------------------------------------------------- 边界

    def settle_bottoms(self, fs, boxes: Sequence[TextBox]) -> List[int]:
        """三路信号合并出「卡片下边界」y 列表。"""
        ys: List[int] = []

        # ① 模板锚点（多目标峰值迭代）
        spec = self.store.template(self.anchor_name)
        for hit in self.kit.matcher.find_all(
            fs.fast, spec, threshold=self.anchor_thr, min_gap=24
        ):
            ys.append(hit.rect[1] + hit.rect[3])

        # ② OCR 关键词（结算/评价/错字别名）
        for box in sorted(boxes, key=lambda b: b.bottom):
            if not any(kw in box.text for kw in SETTLE_KEYWORDS):
                continue
            if ys and box.bottom - ys[-1] < 25:
                ys[-1] = max(ys[-1], box.bottom)
            else:
                ys.append(box.bottom)

        # 聚类：多尺度匹配会在同一结算行附近产出多个近重复峰（相差 <30px），
        # 而真实卡片下边界间距 >= min_height(90px)。按 y 合并，避免一张卡被切成多张。
        ys = sorted(int(y) for y in ys)
        merged: List[int] = []
        for y in ys:
            if merged and y - merged[-1] < 28:
                merged[-1] = max(merged[-1], y)
            else:
                merged.append(y)
        return merged

    def title_tops(self, boxes: Sequence[TextBox]) -> List[int]:
        """标题行（始发地→目的地）上边界，用于修正首卡顶部与拆分超大卡。"""
        tops = [
            b.top
            for b in boxes
            if any(a in b.text for a in TITLE_ARROW)
            and b.top >= self.geo.first_card_min_y - 20
        ]
        return sorted(set(tops))

    # ---------------------------------------------------------------- 切分

    def split(self, fs, boxes: Sequence[TextBox]) -> List[Tuple[int, int]]:
        """返回 [(top, bottom), ...]（fast 帧 y 区间）。"""
        bottoms = self.settle_bottoms(fs, boxes)
        height = int(fs.fast.shape[0])
        if not bottoms:
            return self._split_by_titles(boxes, height)

        cards: List[Tuple[int, int]] = []
        prev = self._first_top(boxes, bottoms[0])
        for bottom in bottoms:
            if bottom <= prev + 10:
                continue
            cards.append((prev, bottom))
            prev = bottom
        # 最后一条结算行下方若还有内容，也切成一张（可能未显示结算行）
        if prev + self.geo.min_height < height:
            cards.append((prev, height))

        return [(t, b) for t, b in cards if self.geo.min_height <= (b - t) <= self.geo.max_height]

    def _first_top(self, boxes: Sequence[TextBox], first_bottom: int) -> int:
        """首卡顶部：取第一条结算行上方 40~160px 内最近的箭头标题，再上移 15px
        容纳「电议」标签；找不到就从 first_card_min_y 起（排除搜索栏与历史栏）。"""
        cands = [
            b.top
            for b in boxes
            if first_bottom - 160 <= b.top < first_bottom - 40
            and any(a in b.text for a in TITLE_ARROW)
        ]
        if cands:
            return max(self.geo.first_card_min_y, min(cands) - 15)
        return self.geo.first_card_min_y

    def _split_by_titles(self, boxes: Sequence[TextBox], height: int) -> List[Tuple[int, int]]:
        """降级：只靠标题行切（边界不如下边界准，但总比不切强）。"""
        tops = self.title_tops(boxes)
        if not tops:
            return []
        cards = []
        for i, top in enumerate(tops):
            bottom = tops[i + 1] if i + 1 < len(tops) else height
            if self.geo.min_height <= (bottom - top) <= self.geo.max_height:
                cards.append((max(self.geo.first_card_min_y, top - 15), bottom))
        return cards


class FieldProbe:
    """按「结算行 ± 偏移」探卡片字段（config/fields.json.card.offsets）。"""

    def __init__(self, store: ConfigStore, kit: ActionKit) -> None:
        self.store = store
        self.kit = kit

    def offsets(self) -> Dict[str, dict]:
        return dict(self.store.get("fields", "card.offsets", {}) or {})

    def probe(
        self,
        fs,
        top: int,
        bottom: int,
        index: int,
        boxes: Sequence[TextBox],
        anchor_score: float = 0.0,
    ) -> OrderCard:
        raw_scale = self._raw_scale(fs)
        width = int(fs.fast.shape[1])
        anchor = (width // 2, bottom)  # 参考点：卡片水平中心 + 结算行底部

        card = OrderCard(
            index=index,
            rect=(0, top, width, max(bottom - top, 1)),
            anchor_score=anchor_score,
            texts=[b.text for b in boxes if top <= b.center[1] <= bottom],
        )

        # 参数行搜索带：结算行上方 12~130px。2026-09-10 从 100 放宽到 130：最后一张卡
        # bottom=812 时 band_top=682，覆盖车型行 y≈685；原 100 漏掉最后卡。
        # band 内按字段正则筛 OCR 框，抗卡片高度抖动同时排除底部导航栏干扰。
        band_top = bottom - 130
        band_bot = bottom - 12

        for name, cfg in self.offsets().items():
            if name.startswith("_"):
                continue  # offsets 里的 _note / _note_regex 是说明文字，不是字段
            dx = int(cfg.get("dx", 0) or 0)
            w = int(cfg.get("w", 60) or 60)
            x_fast = anchor[0] + dx
            x1_fast = x_fast + w
            regex = cfg.get("regex")
            if cfg.get("mode") == "image":
                # 图色字段（电议标签）仍在 band 的 raw ROI 内匹配
                y_raw = int(band_top * raw_scale)
                h_raw = int(max(band_bot - band_top, 1) * raw_scale)
                x_raw = int(x_fast * raw_scale)
                w_raw = int(w * raw_scale)
                if self._hit_template(fs, name, cfg, x_raw, y_raw, w_raw, h_raw):
                    setattr(card, "has_dianyi", True)
                continue
            # OCR 字段：在 band 内、x 范围内筛框，再用字段正则只保留命中框
            # （底部导航栏同在 band 内但文本不含 米/吨/车型，被正则自动排除）。
            cand = [
                b for b in boxes
                if x_fast <= b.center[0] <= x1_fast
                and band_top <= b.center[1] <= band_bot
            ]
            matched = [b for b in cand if regex and re.search(regex, b.text)] if regex else cand
            boxes_in = matched if matched else cand
            joined = " ".join(b.text for b in boxes_in)
            # 车型：保留参数行原文（去空格）供规则词正向查询；findall 抠词仅作展示。
            # 正向查询不依赖抠词，OCR 把「高栏」拆成「高 栏」也不漏（去空格仍是高栏）。
            if name == "che_type":
                card.param_text = re.sub(r"\s+", "", joined)
                if regex:
                    vals = re.findall(regex, joined)
                    value = "/".join(dict.fromkeys(vals)) if vals else None
                else:
                    value = self._extract(joined, None)
            else:
                value = self._extract(joined, regex)
            if value is None and name in ("tonnage", "volume"):
                # 退化：OCR 偶发把「粘在车型后的吨/方数字」丢字，回退到参数行全文
                value = self._extract(self._param_line(card), regex)
            if value is None:
                continue
            if name == "che_length":
                card.che_length = value
            elif name == "che_type":
                card.che_type = value
            elif name == "tonnage":
                card.tonnage = _to_float(value)
            elif name == "volume":
                card.volume = _to_float(value)
            if boxes_in:
                # 记下字段行位置（fast 帧坐标）：点击点要落在真实参数行上，
                # 而不是按固定偏移猜——猜出来的点会越界误触订阅线路横条
                card.field_points[name] = boxes_in[0].center
        self._probe_any(card, boxes, top, bottom)
        self._probe_price(card)
        return card

    @staticmethod
    def _probe_price(card: OrderCard) -> None:
        """从卡片**全文**提取价格（元），供详情页读不到价格时回退。

        为什么扫全文而不是按 ROI 定位：列表卡片布局随内容变化（有无方数/备注
        会导致行数不同），固定偏移会取错行；而详情页左下角那套价格 ROI 在列表
        页毫无意义。正则与详情页价格同源（fields.json detail.price.regex_yuan）。

        卡片价格常写「电议/面议」——没有数字，此时保持 None，调用方仍按
        「算不出单价转人工」处理，不会拿错值乱判。
        """
        blob = card.text_blob()
        if not blob:
            return
        m = re.search(r"(\d+(?:\.\d+)?)\s*元", blob)
        if m:
            card.price = _to_float(m.group(1))

    @staticmethod
    def _probe_any(card: OrderCard, boxes, top: int, bottom: int) -> None:
        """「不限车型 / 车长不限」的卡：字段正则认不出，但它确实**符合**要求。

        这类卡以前落进「未取到车型/车长参数行 → 跳过不盲点」，而被跳过的卡里
        有相当一部分就是「不限」（用户 2026-09-06 指出）。这里在卡片范围内找
        含「不限」的文本行：命中就当作车型/车长都是「不限」，并把该行记为
        che_any 锚点——点击点就能落在这一行上（仍然遵守「落点必须在本卡内」）。
        """
        if card.che_length or card.che_type:
            return
        for b in boxes:
            if "不限" not in (b.text or ""):
                continue
            cy = b.center[1]
            if cy < top or cy > bottom:
                continue
            card.che_length = "不限"
            card.che_type = "不限"
            card.field_points["che_any"] = b.center
            return

    # ---------------------------------------------------------------- 内部

    def _raw_scale(self, fs) -> float:
        """fast 像素 → raw 像素的倍数。"""
        return float(fs.raw.shape[1]) / float(fs.fast.shape[1])

    def _ocr_boxes(self, fs, x: int, y: int, w: int, h: int) -> List[TextBox]:
        """在 raw 帧矩形内 OCR，返回 **fast 帧坐标** 的文本框。

        坐标换算不能省：偏移配置写在 fast 帧，OCR 跑在 raw 帧上。
        只取文本时无所谓，一旦取坐标（点击落点）就必须换算，
        否则点击点会差 raw/fast 的倍数——这是最难排查的一类偏移事故。
        """
        if self.kit.ocr is None:
            return []
        h_img, w_img = fs.raw.shape[:2]
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(w_img, int(x + w)), min(h_img, int(y + h))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return []
        scale = 1.0 / self._raw_scale(fs)
        return self.kit.ocr.ocr_roi(
            fs.raw, (x0, y0, x1 - x0, y1 - y0), coord_scale=(scale, scale)
        )

    def _hit_template(self, fs, name: str, cfg: dict, x: int, y: int, w: int, h: int) -> bool:
        tpl_name = cfg.get("template") or name
        spec = self.store.template(str(tpl_name))
        rect = (x, y, w, h)
        hit = self.kit.matcher.find(fs.fast, spec, search_rect=self._to_fast(fs, rect))
        return hit is not None

    def _to_fast(self, fs, rect: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
        s = 1.0 / self._raw_scale(fs)
        x, y, w, h = rect
        return (int(x * s), int(y * s), max(int(w * s), 1), max(int(h * s), 1))

    @staticmethod
    def _extract(text: str, pattern: Optional[str]) -> Optional[str]:
        if not text:
            return None
        if not pattern:
            return text.strip()
        m = re.search(pattern, text)
        return m.group(1) if m else None

    @staticmethod
    def _param_line(card) -> str:
        """卡片里含「米」的那一行（车长/车型/吨位挤在一起的参数行），全帧 OCR 最稳。"""
        for t in getattr(card, "texts", []) or []:
            if "米" in t:
                return t
        return ""


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip()
    if "-" in s or "~" in s or "—" in s:
        # 吨位/方数偶见范围写法（如「8-9吨」）：拼单按上限算，不超载最稳妥
        nums: List[float] = []
        for part in re.split(r"[-~—]", s):
            try:
                nums.append(float(part))
            except (TypeError, ValueError):
                continue
        return max(nums) if nums else None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


class ListScanner:
    """一屏扫描：切卡 → 探字段 → 过规则 → 去重，返回本屏命中卡片。"""

    def __init__(self, ctx: FlowContext) -> None:
        self.ctx = ctx
        self.splitter = CardSplitter(ctx.store, ctx.kit)
        self.probe = FieldProbe(ctx.store, ctx.kit)
        self.seen = FingerprintCache()
        # 留证屏序号：每屏 +1，保证截图 tag 唯一（同秒内多张不互相覆盖）
        self._shot_seq = 0
        # 列表 OCR 裁剪 ROI（归一化 [l,t,r,b]）：跳过顶部搜索栏/筛选栏与底部导航，
        # 只识别订单卡片区，省掉无用的 OCR 文本行（实测整屏 3.35s → ROI 2.83s，省 15%）。
        roi_cfg = ctx.store.get("fields", "card.list_ocr_roi", None)
        self.list_ocr_roi = tuple(float(v) for v in roi_cfg) if roi_cfg else None

    def _list_roi(self, fs):
        """列表 OCR 裁剪 ROI（raw 像素 [x,y,w,h]）；未配置返回 None=整屏。"""
        if not self.list_ocr_roi:
            return None
        h, w = fs.raw.shape[:2]
        l, t, r, b = self.list_ocr_roi
        x0 = int(round(l * w))
        y0 = int(round(t * h))
        x1 = int(round(r * w))
        y1 = int(round(b * h))
        return (x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))

    # ------------------------------------------------------------ 去重

    @staticmethod
    def spec_fingerprint(che_len, che_type, tonnage, volume) -> str:
        """「车型参数」指纹：车长|车型|吨位|方数（不含价格/路线/相对时间）。

        为什么要有第二套指纹：整卡 OCR 文本指纹会因一行地址/时间抖动而变，
        详情页与列表页的文本构成又完全不同（详情写不进列表的 seen），于是
        「刚在详情里看过/已下架」的单回到列表又被点一次。参数串是全流程
        唯一两处都能稳定读到的东西，用它做跨页面判重。
        """
        parts = [
            "" if che_len is None else str(che_len).strip(),
            "" if che_type is None else str(che_type).strip(),
            "" if tonnage is None else f"{float(tonnage):.2f}",
            "" if volume is None else f"{float(volume):.2f}",
        ]
        if not any(parts):
            return ""
        return "spec:" + FingerprintCache.make("|".join(parts))

    def remember(self, fp: str) -> None:
        """外部（详情处理器）登记「这单已处理过」，回列表后不再点进去。"""
        self.seen.add(fp)

    def _dedup_reason(self, card: "OrderCard", fp: str, spec: str) -> Optional[str]:
        """判该卡是否**确定**处理过；返回跳过原因，None 表示可以点。

        这里只放**能确定是同一张单**的判据（命中就真的跳过）：
            ① 内存文本指纹（卡面文字完全一样）
            ② 订单库精确 key（路线 + 始发地 + 目的地 + 参数全对上）

        「只是参数相同」的判据**不在这里**——见 is_soft_dup，那种只降权。
        """
        if fp and self.seen.seen(fp):
            return "本轮已看过"

        cfg = self.ctx.store.section("runtime").get("dedup", {}) or {}
        if not bool(cfg.get("use_order_db", True)):
            return None
        store = self.ctx.order_store
        if store is None:
            return None

        route = self.ctx.current_route()
        level = str(cfg.get("spec_level", "route") or "route").lower()
        if level == "off":
            return None

        origin, dest = OrderStore.extract_od(card.texts)
        if origin and dest:
            key = OrderStore.dup_key(
                route, origin, dest, card.che_length, card.che_type,
                card.tonnage, card.volume,
            )
            if store.is_seen(key):
                return "订单库已处理（含路线）"

        # 上面的精确 key（含真实 A/B）需要 extract_od(card.texts) 能从卡片里读出路线行。
        # 路线行本来就在 card.texts 里（2026-09-07 复盘：之前数据库层没触发不是因为
        # 「卡片没路线行」，而是 extract_od 的分隔符只认 →，OCR 读出全角－/=> 等就
        # 解析失败）。现已把分隔符扩全（→/-/—/－/=>/到 + 跨框兜底），故对绝大多数卡片
        # 这条精确 key 能正常拼出、数据库去重以「卡片真实 A/B」为准生效——正是用户要求。
        # 仅当路线行实在读不出（如首卡被切卡上界裁掉）时才退回下面的「已知路线+参数」
        # 兜底：路线用 ctx.current_route()（当前扫的就是这条，必然读得到），参数为
        # 列表卡稳定可读字段。spec_level="route" 才启用；参数全空则跳过（避免误杀）。
        if level == "route":
            if store.is_spec_seen(
                route, card.che_length, card.che_type, card.tonnage, card.volume
            ):
                return "订单库已处理（路线+参数）"
        return None

    def is_soft_dup(self, card: "OrderCard", spec: str) -> bool:
        """「软重复」：只是参数相同（车长/车型/吨位/方数一样），不是同一张单。

        2026-09-06 第二轮实测教训：把参数相同的卡当重复**直接跳过**是漏单元凶——
        列表里「4.2米 高栏 3.0吨」这种同参数的不同货源一屏能有好几张，
        一轮 98 张命中卡有 49 张被这么跳掉了（去重跳过 144 次 vs 命中 98 次）。
        所以参数相同只**降权**（排到本屏命中最后），绝不丢弃：
        本屏有没见过的卡就先点新的，只剩同参数卡时也照样点。
        """
        if spec and self.seen.seen(spec):
            return True
        cfg = self.ctx.store.section("runtime").get("dedup", {}) or {}
        if not bool(cfg.get("use_order_db", True)):
            return False
        store = self.ctx.order_store
        if store is None:
            return False
        level = str(cfg.get("spec_level", "route") or "route").lower()
        if level == "off":
            return False
        return store.is_spec_seen(
            self.ctx.current_route() if level == "route" else "",
            card.che_length, card.che_type, card.tonnage, card.volume,
        )

    # ------------------------------------------------------------ 留证

    def _list_screen_shot(self, fs) -> None:
        """每屏列表整图留证（trace.list_screen_shot 开关控制）。

        测试阶段准确率验证用：每屏必存一张整图，跑完可回放「这一屏有哪些订单」，
        与命中记录对比数漏判。异步写盘（TraceWriter 后台线程），不阻塞主流程。
        """
        if not bool(self.ctx.store.get("runtime", "trace.list_screen_shot", True)):
            return
        tr = getattr(self.ctx, "trace", None)
        if tr is None or not getattr(tr, "enabled", False):
            return
        self._shot_seq += 1
        try:
            tr.shot(fs.raw, f"list_screen_{self._shot_seq:04d}", always=True)
        except Exception:  # noqa: BLE001 留证失败绝不中断扫描
            pass

    def _hit_card_shot(self, fs, card, top: int, bottom: int, idx: int, verdict) -> None:
        """命中卡片裁剪留证 + 结构化 JSONL（trace.hit_card_shot 开关控制）。

        裁剪卡片区域（fast y 坐标 × raw/fast 缩放 → raw 坐标），存裁剪图；
        同时写一条 level=hit 的 JSONL（车长/车型/吨位/方数/坐标/判定原因），
        供结束后对比筛选「误判/漏判」。异步写盘，失败静默。
        """
        if not bool(self.ctx.store.get("runtime", "trace.hit_card_shot", True)):
            return
        tr = getattr(self.ctx, "trace", None)
        if tr is None or not getattr(tr, "enabled", False):
            return
        try:
            scale = float(fs.raw.shape[1]) / float(fs.fast.shape[1])
            y0 = max(0, int(top * scale))
            y1 = min(fs.raw.shape[0], int(bottom * scale))
            if y1 > y0:
                tr.shot(fs.raw[y0:y1, :], f"hit_{self._shot_seq:04d}_{idx}", always=True)
            tr.put(
                "hit",
                card.describe(),
                screen_seq=self._shot_seq,
                idx=idx,
                che_length=card.che_length,
                che_type=card.che_type,
                tonnage=card.tonnage,
                volume=card.volume,
                top=top,
                bottom=bottom,
                reason=getattr(verdict, "reason", ""),
            )
        except Exception:  # noqa: BLE001
            pass

    def scan_once(self, fs: Optional[FrameSet] = None) -> List[OrderCard]:
        ctx = self.ctx
        # 优先用调用方已抓取的帧（scan 命令的只读 kit 无摄像头源，重取会静默失败）；
        # run 主流程的 kit 自带摄像头源，这里传 None 时回退到实时取帧。
        if fs is None:
            fs = ctx.kit.frame()
        if fs is None or ctx.kit.ocr is None:
            return []
        if not self._has_content(fs):
            ctx.bump("scan_empty_screen")
            return []
        # 每屏列表整图留证（测试阶段准确率验证：跑完可回放数漏判）
        self._list_screen_shot(fs)
        # 扫描计时：从整屏 OCR 起、到切卡完成止——把一屏画面变成卡片列表的视觉
        # 采集成本（OCR 是大头 + 切卡）。扫描慢还是判定慢，拆开一眼可见。
        t_scan = time.perf_counter()
        boxes = ctx.kit.ocr_frame(fs, roi=self._list_roi(fs))
        # 内存/句柄趋势：OCR 变慢时先确认是否资源累积——把占用与增量写进耗时
        # 日志，跑一轮就能看出是持续上涨还是平稳（what 只影响显示，不影响计时统计）。
        mb = proc_memory_mb()
        hd = proc_handle_count()
        what = "整屏OCR"
        parts: List[str] = []
        if mb >= 0:
            prev = float(getattr(self, "_last_mem_mb", -1.0) or -1.0)
            self._last_mem_mb = mb
            delta = f" {mb - prev:+.0f}" if prev >= 0 else ""
            parts.append(f"内存 {mb:.0f}MB{delta}")
        if hd >= 0:
            prev_h = int(getattr(self, "_last_hd", -1) or -1)
            self._last_hd = hd
            delta_h = f" {hd - prev_h:+d}" if prev_h >= 0 else ""
            parts.append(f"句柄 {hd}{delta_h}")
        if parts:
            what = f"整屏OCR（{'｜'.join(parts)}）"
        self._log_timing(ctx, what, (time.perf_counter() - t_scan) * 1000.0)
        spans = self.splitter.split(fs, boxes)
        ctx.bump("cards_split", len(spans))
        ctx.timings["扫描"] = (time.perf_counter() - t_scan) * 1000.0

        hits: List[OrderCard] = []
        # 软重复（仅参数相同）的卡排到本屏最后：先点没见过的，没有新的再点它们
        soft: List[OrderCard] = []
        # 判定计时：从切卡完成起，到本屏命中全部算完止——逐卡探字段 + 过规则 + 去重，
        # 是决策成本。与上面「扫描」拆开，能直接看出列表扫描慢在采集还是判定。
        t_judge = time.perf_counter()
        for i, (top, bottom) in enumerate(spans):
            card = self.probe.probe(fs, top, bottom, i, boxes)
            fp = FingerprintCache.make(card.text_blob())
            spec = self.spec_fingerprint(
                card.che_length, card.che_type, card.tonnage, card.volume
            )
            if not fp:
                continue
            repeat = self._dedup_reason(card, fp, spec)
            if repeat:
                # 打出来：去重是否生效只能靠日志判断，静默 continue 会让
                # 「为什么这张卡不点了」变成黑盒。
                ctx.emit("info", f"重复 {card.describe()} | {repeat}，跳过")
                ctx.bump("cards_deduped")
                continue
            # 参数指纹/订单库同参数只判「软重复」→ 降权不丢弃（见 is_soft_dup）
            is_soft = self.is_soft_dup(card, spec)
            # 整车标签检测：列表卡片右侧橙底「整车」/「一口价·整车」由 OCR 文本承载，
            # 命中即标记不可拼单（不阻断判定，仅作标签透传给详情页展示与拼单提示）。
            if "整车" in card.text_blob():
                card.has_whole_vehicle = True
            verdict = ctx.rules.screen_card(
                card,
                remaining_ton=ctx.capacity.remaining_ton,
                remaining_m3=ctx.capacity.remaining_m3,
                hit_templates=("dianyi_tag",) if card.has_dianyi else (),
            )
            if verdict:
                point = self._click_point(fs, card, top, bottom)
                if point is None:
                    # 取不到参数行 → 不盲点（越界会误触订阅线路横条）
                    ctx.bump("cards_skipped_no_anchor")
                    continue
                card.click_point = point
                card.fingerprint = fp
                card.spec_key = spec
                # 仅对「真正要点进详情」的卡片去重：避免同屏其他合格卡（#2/#3）
                # 因 hits[0] 被先点而永远错过（旧逻辑对每张卡都 add，会污染 seen）。
                self.seen.add(fp)
                # 参数指纹同步登记：详情页回写的是参数指纹，两处口径必须一致，
                # 否则详情出来后又被列表当新单点一次。
                self.seen.add(spec)
                (soft if is_soft else hits).append(card)
                # 命中卡片留证：裁剪图 + 结构化 JSONL（测试阶段准确率验证）
                self._hit_card_shot(fs, card, top, bottom, i, verdict)
                ctx.emit(
                    "info",
                    f"命中 {card.describe()} | {verdict.reason}"
                    + ("（同参数，排本屏最后）" if is_soft else ""),
                )
                if is_soft:
                    ctx.bump("cards_soft_dup")
            else:
                ctx.emit("info", f"跳过 {card.describe()} | {verdict.stage}: {verdict.reason}")
        # 新的排前面、同参数的排后面：本屏有没见过的卡就先点它
        ordered = hits + soft
        ctx.bump("cards_hit", len(ordered))
        ctx.timings["判定"] = (time.perf_counter() - t_judge) * 1000.0
        return ordered

    @staticmethod
    def _log_timing(ctx: FlowContext, what: str, ms: float) -> None:
        """把关键步骤耗时打进日志——「感觉慢」必须先能看见时间花在哪。"""
        if not bool(ctx.store.get("runtime", "trace.timing", True)):
            return
        ctx.counters.setdefault("_timing_samples", 0)
        ctx.counters["_timing_samples"] += 1
        ctx.counters["_timing_ms_total"] = float(ctx.counters.get("_timing_ms_total", 0.0)) + ms
        ctx.emit("info", f"[耗时] {what} {ms:.0f}ms")

    def _has_content(self, fs) -> bool:
        """扫描前先判断这屏有没有内容，空屏跳过整屏 OCR。

        整屏 OCR 是全流程最贵的一步（数百毫秒），一屏十几张卡时尤其明显。
        空屏（加载页 / 纯色页 / 采集卡无信号的黑屏）直接跳过，是速度关键。
        """
        cfg = self.ctx.store.section("vision").get("fast_scan", {}) or {}
        if not cfg.get("enabled", True):
            return True
        method = str(cfg.get("method", "edge") or "edge").lower()
        if method == "brightness":
            return float(fs.fast.mean()) >= float(cfg.get("dark_brightness", 12) or 12)
        return has_content(
            fs.fast,
            min_edge_density=float(cfg.get("min_edge_density", 0.01) or 0.01),
            min_std=float(cfg.get("min_std", 8.0) or 8.0),
        )

    def _click_point(
        self, fs, card: OrderCard, top: int, bottom: int
    ) -> Optional[Tuple[int, int]]:
        """算点击点：优先车型参数行，其次车长行；都取不到返回 None（该卡跳过）。

        铁律（历史教训）：点击点必须落在本卡片区域内。
        越界会误触顶部的订阅线路横条——那是一条横向 banner，
        点进去会跳到完全无关的页面，且不自动回列表，必须人工介入恢复。
        """
        cfg = self.ctx.store.get("fields", "card.click", {}) or {}
        dy_in_row = int(cfg.get("dy_in_row", -3) or -3)
        width = int(fs.fast.shape[1])

        for key in (
            cfg.get("field"),
            cfg.get("fallback_field"),
            cfg.get("fallback_field2"),
        ):
            if not key:
                continue
            pt = card.field_points.get(str(key))
            if pt is None:
                continue
            cx, cy = int(pt[0]), int(pt[1]) + dy_in_row
            if cx < 0 or cx >= width or cy < top or cy > bottom:
                self.ctx.emit(
                    "warning",
                    f"#{card.index} 字段[{key}]点击点 ({cx},{cy}) 越界"
                    f"（本卡 y {top}~{bottom}），跳过该卡",
                )
                continue
            # 底部保护：点击点太靠屏幕底部，机械臂落点贴近 iOS Home 手势区，
            # press/release 的微小位移可能被识别为「底部上滑回桌面」
            # （2026-09-12 实测 y=718 退桌面）。上移到卡片中部（不跳过）——
            # 参数行接近底部，差一个 tab 栏距离，移到中部既不触发 Home 手势又能进详情。
            height = int(fs.fast.shape[0])
            guard_ratio = float(cfg.get("bottom_guard_ratio", 0.86) or 0.86)
            if cy > int(height * guard_ratio):
                safe_cy = max(top + 20, (top + cy) // 2)
                self.ctx.emit(
                    "info",
                    f"#{card.index} 点击点 ({cx},{cy}) 太靠底"
                    f"（>{guard_ratio:.0%} 屏高），向上移至 ({cx},{safe_cy}) 避开 iOS 底部手势",
                )
                return (cx, safe_cy)
            return (cx, cy)

        if not bool(cfg.get("must_inside_region", True)):
            # 仅在配置明确允许时才用卡片中心兜底
            return (width // 2, min(max(bottom + dy_in_row, top + 5), bottom - 5))

        self.ctx.emit("info", f"#{card.index} 未取到车型/车长参数行，跳过不盲点")
        return None

"""详情页：读价格/公里数/重量 → 算单价 → 判定 → 抢单并扣减拼单余量。

价格必须进详情才能算：列表页看不到真实价格与距离（只有「电议/面议」这类），
所以详情页是唯一能算单价的地方，也是整个流程里唯一「值得慢一点」的环节。

两个坑（旧工程都踩过）：
    * 公里数有两个：左边是「车辆当前距装货点」，右边才是「装货点→目的地」。
      取错一个，单价能差好几倍 —— 必须按 pick=rightmost 取最右那个；
    * 拼单余量要在**抢单成功之后**才扣减。抢单可能失败（被别人抢走/网络），
      预扣了不回滚，跑一天下来余量会莫名其妙归零。
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

import cv2
import numpy as np
from core.domain.orders import OrderDetail, Verdict
from core.flow.context import FlowContext
from core.flow.order_store import OrderStore
from core.vision.page_state import ScreenState


class DetailReader:
    """按 config/fields.json.detail 的 ROI 读字段（OCR 走 raw 帧）。"""

    def __init__(self, ctx: FlowContext) -> None:
        self.ctx = ctx

    def _cfg(self, key: str, default=None):
        return self.ctx.store.get("fields", f"detail.{key}", default)

    def read(self, fs) -> OrderDetail:
        """读详情字段：先第一屏 OCR，再上滑读第二屏 OCR，两屏内容整合。

        为什么读两屏：运满满详情页的「货物」行常在屏幕下方（车型行之上/之下），
        一屏 OCR 经常只覆盖到价格/距离/吨方，货物被截在可视区外 → 取不到。
        上滑一屏把下方内容滚进可视区再 OCR 一次，两屏文本合并后抽字段，
        货物命中率显著提升（用户 2026-09-07 要求）。
        """
        kit = self.ctx.kit
        if kit.ocr is None:
            return OrderDetail()

        # 第一屏：全屏 OCR 只做一次（上下文多、识别质量最高）；后续按 ROI 过滤。
        # ⚠️ 必须不带 roi 调用，否则绕过 ActionKit 的帧级 OCR 缓存，一趟做两次全屏
        # OCR（弹窗判据一次、这里一次），实测「详情读字段」中位 4082ms，一半是浪费。
        t0 = time.perf_counter()
        boxes1 = kit.ocr_frame(fs)
        self.ctx.timings["OCR识别"] = (time.perf_counter() - t0) * 1000.0
        detail = self._parse(fs, boxes1)

        # 优化（2026-09-10）：第二屏只为补「货物」和漏读字段，而货物不参与判定
        # （rules.check_detail 只用 price/distance/che_len/che_type/weight）。
        # 判定核心字段第一屏已取到就跳过第二屏，省一次整屏 OCR（~3s）——
        # 「详情读字段」8.2s 里约一半是这次第二屏 OCR。仅核心字段缺失才上滑补读。
        if (
            detail.price is not None
            and detail.distance_km is not None
            and detail.che_len
            and detail.che_type
        ):
            return detail

        # 第二屏：上滑把下方内容滚进可视区再 OCR，整合进第一屏结果。
        fs2 = self._scroll_detail_page2(fs)
        if fs2 is not None:
            try:
                boxes2 = kit.ocr_frame(fs2)
            except Exception as exc:  # noqa: BLE001 第二屏 OCR 失败不中断
                self.ctx.emit("warning", f"详情第二屏 OCR 失败（已用第一屏）: {exc}")
                boxes2 = None
            if boxes2:
                d2 = self._parse(fs2, boxes2)
                # 第一屏优先，第二屏只补缺（货物常在第二屏）
                if detail.cargo is None:
                    detail.cargo = d2.cargo
                detail.price = detail.price if detail.price is not None else d2.price
                detail.distance_km = detail.distance_km if detail.distance_km is not None else d2.distance_km
                detail.weight_ton = detail.weight_ton if detail.weight_ton is not None else d2.weight_ton
                detail.volume_m3 = detail.volume_m3 if detail.volume_m3 is not None else d2.volume_m3
                detail.che_len = detail.che_len or d2.che_len
                detail.che_type = detail.che_type or d2.che_type
                detail.texts = detail.texts + d2.texts  # 整合两屏原文（详情原文框/货物回退）
        return detail

    def _scroll_detail_page2(self, fs) -> Optional["FrameSet"]:
        """详情页上滑一屏、回到详情页后取新帧（不在此 OCR）。

        仅确认仍停在详情页才取帧——上滑若误触发返回/刷新把页面弄丢，
        直接返回 None（不影响第一屏已解析的结果）。失败一律静默降级。
        """
        kit = self.ctx.kit
        try:
            kit.swipe_up(distance_ratio=0.4, start_y_ratio=0.667)  # 详情页内滚动看下一屏（起点在屏幕下 1/3，避免划不动）
            if not self.ctx.sleep(0.6):
                return None
            _, res = kit.wait_state((ScreenState.DETAIL,), timeout=2.0, interval=0.3)
            if res.state is not ScreenState.DETAIL:
                self.ctx.emit("debug", "详情上滑后已不在详情页，放弃读第二页")
                return None
            return kit.frame()
        except Exception as exc:  # noqa: BLE001 读第二页失败绝不中断主流程
            self.ctx.emit("warning", f"详情读第二页失败（已用第一屏）: {exc}")
            return None

    def _parse(self, fs, boxes) -> OrderDetail:
        """从一屏 OCR 文本框抽全部详情字段（不负责取帧/OCR）。"""
        detail = OrderDetail()
        detail.texts = [b.text for b in boxes]
        detail.cargo = self._read_cargo(detail.texts)
        # 价格：优先全屏「数字+元」取最大（总价永远大于预估净得/需支付/服务费/订金）。
        # 2026-09-10 两次真机踩坑：详情页价格位置随订单类型变化——有的「总价 X元」
        # 在左下角 y≈0.89 大字、有的「总运费 X元」在右侧 y≈0.63，固定 ROI 必错
        # （读成「预估净得 7.14 元」「您需支付 4.2 元」这类小额，单价全算成 0.02）。
        # 全屏取最大天然筛掉小额，只留总价。兜底：OCR 把「元」吞掉时退回 ROI。
        detail.price = self._read_number_blob(
            boxes, self._cfg("price.regex_yuan", r"(\d+(?:\.\d+)?)\s*元"),
        )
        if detail.price is None:
            detail.price = self._read_number(
                fs, boxes, (self._cfg("price.roi", (0.0, 0.87, 0.5, 0.95))),
                self._cfg("price.regex", r"(\d+(?:\.\d+)?)"),
            )
        detail.distance_km = self._read_km(fs, boxes)
        weight, volume = self._read_weight(fs, boxes)
        detail.weight_ton = weight
        detail.volume_m3 = volume
        che_len, che_type = self._read_vehicle(boxes)
        detail.che_len = che_len
        detail.che_type = che_type
        return detail

    # ---------------------------------------------------------------- 字段

    def _roi_fast(self, fs, norm_roi):
        """归一化 ROI → fast 帧坐标 (x0,y0,x1,y1)，与 ocr_frame 返回的框同量纲。

        ocr_frame 内部已按 ocr_scale 把框坐标换算到 fast 帧；norm_to_raw_roi 返回
        的是 raw 帧坐标。两者量纲不同，直接比会全 False——这里把 ROI 也乘 scale。
        """
        sx, sy = self.ctx.kit.ocr_scale
        x0, y0, w, h = self.ctx.kit.norm_to_raw_roi(fs, norm_roi)
        x0, y0, w, h = int(round(x0 * sx)), int(round(y0 * sy)), int(round(w * sx)), int(round(h * sy))
        return x0, y0, x0 + w, y0 + h

    def _read_number(self, fs, boxes, norm_roi, pattern: str) -> Optional[float]:
        """按 ROI 位置过滤文本行后取数字最大值（左下角价格用，避开地址数字）。

        与 _read_km 同机制（_read_km 实测 100% 命中，说明 ROI 位置过滤是稳的）。
        取最大值：价格行通常是该 ROI 内最大的数字，小字（如「元」「运费」）不会胜出。
        """
        x0, y0, x1, y1 = self._roi_fast(fs, norm_roi)
        best: Optional[float] = None
        for box in boxes:
            if not box.overlaps(x0, y0, x1, y1):
                continue
            val = self._first_match(box.text, pattern)
            if val is not None and (best is None or val > best):
                best = val
        return best

    def _read_number_blob(self, boxes, pattern: str) -> Optional[float]:
        """全屏 OCR 文本 blob 正则（脱坐标），作为 ROI 取数的兜底。"""
        blob = " ".join(b.text for b in boxes)
        nums = [self._to_float(x) for x in re.findall(pattern, blob)]
        return max(nums) if nums else None

    _CHE_LEN_RE = re.compile(r"([0-9./]+)\s*米")
    # 完整车型词（长词优先，避免「高低板」被「高栏」抢配）。旧实现用字符类
    # [平板高栏飞翼厢式冷藏保温自卸低集爬梯]+，导致「卸/保/式/自/温」等单字碎片
    # 也混进 che_type（日志实测 che_type=卸/高栏/保/保/卸温/保/式/自）。
    _CHE_TYPE_WORDS = (
        "高低板", "平板车", "高栏", "平板", "厢式", "飞翼",
        "冷藏", "保温", "自卸", "低栏", "集装", "爬梯", "仓栅",
    )
    _CHE_TYPE_RE = re.compile("|".join(sorted(_CHE_TYPE_WORDS, key=len, reverse=True)))

    def _read_vehicle(self, boxes) -> Tuple[Optional[str], Optional[str]]:
        """从详情页全屏 OCR 抽车型参数（车长/车型）。

        详情页几乎总能读到车型（实测 che_type 100%、che_len 94%），比列表页更全——
        列表页常漏车型/吨数。多值串如「4.2/5米」只取数字部分交给 RuleEngine 按 / 拆；
        车型取所有命中的完整车型词用 / 拼起来（如「平板/高栏」），便于做成员匹配。
        """
        blob = " ".join(b.text for b in boxes)
        m = self._CHE_LEN_RE.search(blob)
        che_len = m.group(1) if m else None
        types = self._CHE_TYPE_RE.findall(blob)
        # 去重且保持首次出现顺序（同一车型在多行重复出现只留一个）
        seen: set = set()
        uniq: list = []
        for t in types:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        che_type = "/".join(uniq) if uniq else None
        return che_len, che_type

    _CARGO_RE = re.compile(r"(?:货物|货源|货品|货名|物品|承运)\s*[:：]?\s*([^，,。；;、（）()]*)")

    def _read_cargo(self, texts) -> Optional[str]:
        """从详情页 OCR 文本行里抽货物内容（最佳努力）。

        运满满详情页多有「货物：钢材」之类一行；取不到（布局差异/无标签）时返回
        None，GUI 会回退展示「详情原文」框——货物内容一定在其中可见。
        正则允许货物名内含空格（如「电子产品 7-7.2吨 纸」），以逗号/句号等强分隔符截止；
        内容允许为空（标签单独成行、内容在下一行时，取下一行作为货物内容）。
        """
        for i, t in enumerate(texts):
            m = self._CARGO_RE.search(t.strip())
            if m:
                cargo = m.group(1).strip()
                if not cargo and i + 1 < len(texts):
                    # 「货物」标签单独成行、货物内容在下一行（OCR 分行常见）：取下一行
                    cargo = texts[i + 1].strip()
                return cargo or None
        return None

    @staticmethod
    def _to_float(x: str) -> Optional[float]:
        try:
            return float(x.replace(",", ""))
        except (TypeError, ValueError):
            return None

    def _read_km(self, fs, boxes) -> Optional[float]:
        """公里数：右列有两个，取**靠下**那个（上面的是车辆到装货点的距离）。

        pick 可配：lowest（默认，取最靠下）/ rightmost（取最右）/ largest（取数值最大）。
        """
        roi = self._cfg("distance_km.roi", (0.5, 0.22, 1.0, 0.68))
        pattern = self._cfg("distance_km.regex", r"(\d+(?:[.,]\d+)?)\s*(?:km|公里|KM)")
        pick = str(self._cfg("distance_km.pick", "lowest") or "lowest")
        x0, y0, x1, y1 = self._roi_fast(fs, roi)
        best_key: Optional[float] = None
        best_val: Optional[float] = None
        for box in boxes:
            if not box.overlaps(x0, y0, x1, y1):
                continue
            value = self._first_match(box.text, pattern)
            if value is None:
                continue
            if pick == "rightmost":
                key = float(box.right)
            elif pick == "largest":
                key = float(value)
            else:
                key = float(box.bottom)
            if best_key is None or key > best_key:
                best_key, best_val = key, value
        return best_val

    def _read_weight(self, fs, boxes) -> Tuple[Optional[float], Optional[float]]:
        """全屏 OCR 文本 blob 正则（脱坐标），避开 ROI 量纲漂移导致的漏抽。

        取所有吨/方值的上限（范围取上限保守安全，如「4-5吨」取 5）。
        车型「载重 X 吨」若出现在全屏文本里会一并被抽到——这是已知的噪声来源，
        实机标定阶段若发现车型载重频繁误吞，再改用「货物/货主」行限定。
        """
        blob = " ".join(b.text for b in boxes)
        pattern = self._cfg("weight.regex", r"(\d+(?:\.\d+)?)\s*(吨|方|立方米)")
        ton = m3 = None
        for m in re.finditer(pattern, blob):
            try:
                value = float(m.group(1).replace(",", ""))
            except (TypeError, ValueError):
                continue
            unit = m.group(2)
            if unit == "吨":
                ton = value if ton is None else max(ton, value)
            else:
                m3 = value if m3 is None else max(m3, value)
        return ton, m3

    @staticmethod
    def _first_match(text: str, pattern: str) -> Optional[float]:
        m = re.search(pattern, text or "")
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", ""))  # 页面上千分位逗号要去掉
        except (TypeError, ValueError):
            return None


class DetailHandler:
    """详情页完整处理：读 → 判 → 抢/退。作为 ctx.on_detail 挂到状态机上。"""

    def __init__(self, ctx: FlowContext) -> None:
        self.ctx = ctx
        self.reader = DetailReader(ctx)
        # 近一小时抢单时间戳（供 max_grab_per_hour 护栏计数）
        self._grab_times: Deque[float] = deque()
        # 当前停在详情页等待人工确认的订单指纹；用于「继续找单」后识别同一单不再重停。
        self._held_fp: Optional[str] = None
        self._t0 = 0.0
        # 列表扫描器（由 task 装配时注入）：详情处理完把「已处理」回写它的去重
        # 缓存，回列表后同一单不会被再点一次（用户实测：同一个 #1 连点三次）。
        self._scanner = None

    def attach_scanner(self, scanner) -> None:
        """注入列表扫描器（详情 ↔ 列表共享去重缓存）。"""
        self._scanner = scanner

    def __call__(self, ctx: FlowContext) -> None:
        self.handle()

    def _finish_held(self, ctx: FlowContext, detail: OrderDetail) -> None:
        """收尾「已决策的暂停单」：消费决策动作 → 扣减/换车/跳过 → 回列表。

        抽成独立方法，是为了让「暂停恢复快路径」（跳过重读字段）与「读完字段后的
        兜底路径」共用同一套收尾逻辑，避免两处各写一遍产生语义分歧。
        """
        self._held_fp = None
        action = ctx.pop_review_action() if hasattr(ctx, "pop_review_action") else None
        # 拼单/继续找单 共享 GUI 补录的 held_candidate（pindan 时用补录的吨/方扣减）
        held = getattr(ctx, "held_candidate", None)
        use_ton = held.weight_ton if (held and held.weight_ton is not None) else detail.weight_ton
        use_m3 = held.volume_m3 if (held and held.volume_m3 is not None) else detail.volume_m3
        if action == "pindan":
            # 拼单：capacity.commit + 标记 grabbed + 继续找单
            if ctx.capacity.can_take(use_ton, use_m3):
                ctx.capacity.commit(
                    f"{detail.price or '?'}元/{detail.distance_km or '?'}km",
                    ton=use_ton, m3=use_m3,
                )
                ctx.bump("pindan_committed")
                ctx.emit("info", f"★ 拼单已接：{use_ton}吨/{use_m3}方，{ctx.capacity.describe()}")
                self._mark_handled(detail, "grabbed")
            else:
                ctx.bump("pindan_overflow")
                ctx.emit("warning", f"拼单余量不足：{use_ton}吨/{use_m3}方（自动继续找单）")
                self._mark_handled(detail, "skipped")
        elif action == "swap":
            # 换车（AI 决策「模拟当前车辆订单完成」）：车辆参数已在 _apply_swap 里
            # 换成新车、余量已清零，所以这单**不再扣减**（扣的是新车余量，语义错误），
            # 仅登记为已完成。
            ctx.bump("vehicle_swapped")
            ctx.emit("info", f"★ 换车完成，新车：{ctx.capacity.describe()}")
            self._mark_handled(detail, "grabbed")
        else:
            # 继续找单：跳过、不扣减
            ctx.emit("info", "人工已确认，返回列表继续找单")
            ctx.bump("candidate_resumed")
            self._mark_handled(detail, "skipped")
        ctx.held_candidate = None
        self._leave()

    def handle(self) -> None:
        ctx = self.ctx
        fs = ctx.kit.frame()
        if fs is None:
            return
        # 详情页可能盖着「该货源已被抢 / 已下架」弹窗：先把它关掉再按正常详情处理，
        # 否则会被判成合格详情、反复停留甚至误抢（旧工程踩过的坑）。
        t0 = time.perf_counter()
        gone = self._dismiss_detail_dialog(fs)
        if gone == "gone":
            # 已下架/被抢：登记成 gone，回列表后不再点进去（否则反复踩同一张死单）
            self._mark_handled_fs(fs, "gone")
            # 若这单正处于「已暂停等决策」状态：订单已死，决策作废，必须清掉挂起状态，
            # 否则下一轮恢复快路径会把决策（如拼单扣减余量）错用到这张死单上。
            if self._held_fp is not None:
                self._held_fp = None
                ctx.held_candidate = None
                if hasattr(ctx, "pop_review_action"):
                    ctx.pop_review_action()  # 丢弃作废的决策
            self._timing("详情(已下架/被抢)", (time.perf_counter() - t0) * 1000.0)
            return
        if gone:
            # 「我知道了」只是提示，订单还有效：留在详情，下一轮继续正常读详情
            return
        # 首屏即详情页：运行启动后第一个识别到的详情页，不读 OCR、不判定、不暂停，
        # 直接返回列表继续扫单（避免 App 停在某个详情页上一启动就卡住等确认）。
        if not ctx.first_detail_seen:
            ctx.first_detail_seen = True
            ctx.emit("info", "首屏即详情页：不检测不暂停，直接返回列表继续扫单")
            self._leave()
            return
        # ---- 暂停恢复快路径（2026-09-13 提速）----
        # _held_fp 非空 = 刚刚就停在这一单上等人决策；暂停期间机械臂不动、画面没变
        # （可能的弹窗已在上面的 _dismiss_detail_dialog 处理掉），所以暂停时存下的
        # held_candidate 就是当前这一单，可以直接收尾，**不必重新读一遍字段**。
        # 实测每次决策后重读多花 ~5.5s 的「整屏 + 第二屏 OCR」，是用户反馈
        # 「详情页里步骤间隔特别长」的最大一笔开销。
        if self._held_fp is not None:
            held = getattr(ctx, "held_candidate", None)
            if held is not None and held.fingerprint() == self._held_fp:
                ctx.emit("debug", "暂停恢复快路径：用已读到的字段收尾，跳过重复读详情")
                self._finish_held(ctx, held)
                return
            # 兜底：held_candidate 丢失或对不上（异常/被外部清空）→ 清掉标记老实重读
            self._held_fp = None
        detail = self.reader.read(fs)
        self._timing("详情读字段+弹窗判据", (time.perf_counter() - t0) * 1000.0)
        # 详情页左下角没价格 → 回退用列表卡片上的价格（2026-09-07 用户要求）。
        # 卡片价格也可能是 None（写「电议/面议」无数字），那就仍走「算不出单价转人工」。
        if detail.price is None:
            card = getattr(ctx, "last_card", None)
            card_price = getattr(card, "price", None)
            if card_price is not None:
                detail.price = card_price
                ctx.emit(
                    "warning",
                    f"详情页未读到价格，回退用列表卡片价格 {card_price:g} 元",
                )
        fp = detail.fingerprint()

        # 已对这单做过决策（继续找单 / 拼单 / 换车）：直接按决策收尾回列表。
        # 正常路径下上面的「暂停恢复快路径」已经消费掉了；这里只是读完字段后的兜底
        # （例如 held_candidate 丢失、被外部清空等异常情况）。
        if self._held_fp is not None and self._held_fp == fp:
            self._finish_held(ctx, detail)
            return

        ctx.emit(
            "info",
            f"详情: 车长={detail.che_len} 车型={detail.che_type} "
            f"货重={detail.weight_ton}方={detail.volume_m3} "
            f"价格={detail.price} 距离={detail.distance_km} 单价={detail.unit_price()}",
        )

        # 详情页全维度判定：车长/车型 → 载重上限 → 单价。
        verdict: Verdict = ctx.rules.check_detail(detail)
        self._emit_candidate(detail, verdict)
        self._shot_detail(fs, detail, verdict)

        if not verdict:
            ctx.emit("info", f"放弃: {verdict.reason}")
            ctx.bump("detail_rejected")
            self._mark_handled(detail, "rejected")
            self._leave()
            return

        unit = detail.unit_price()
        ctx.emit("info", f"★ 候选合格: {verdict.reason}（单价 {unit:.2f} 元/km）")
        if self._review_mode():
            if self._no_pause_test():
                # 不停止测试模式：合格也**不暂停、不抢单**，停留 hold 秒直接返回列表继续扫。
                # 用户 2026-09-10：长时间连跑测试要「停留 2-3s 就返回」，停留时长可配
                # runtime.no_pause_hold_sec（默认 2.5s），不再硬编码 5s。
                hold = float(ctx.store.get("runtime", "no_pause_hold_sec", 2.5) or 2.5)
                ctx.emit("info", f"不停止测试模式：候选合格，停留 {hold:.1f}s 后自动返回列表")
                ctx.bump("candidate_hold_nopause")
                # 合格单也登记「已看过」：否则跨运行去重缺失，下轮测试会重新点同一单。
                # 与 review_mode 暂停分支一致（那里 _mark_handled(detail, "reviewed")）。
                self._mark_handled(detail, "reviewed")
                ctx.sleep(hold)
                self._leave()
                return
            # 审核模式（默认开）：停在详情页等人工确认，绝不自作主张抢单。
            # （首屏即详情页的不暂停逻辑已在上面读字段之前处理：直接返回列表。）
            # 机器臂不动作、不返回，画面就停在详情页，方便人看。
            self._held_fp = fp
            # 存 detail 到 ctx：GUI「拼单」补录吨/方时改这个，pindan 恢复时用补录值扣减余量
            ctx.held_candidate = detail
            ctx.bump("candidate_hold")
            ctx.pause()
            # 也登记「已看过」：人工点继续找单后，列表刷新把同一单推上来时不再停一次
            self._mark_handled(detail, "reviewed")
            ctx.emit("info", "已暂停在详情页，等待人工确认（GUI 点『继续找单』才回列表）")
            # AI 随机测试模式（runtime.ai_review）：无人值守时由 AI 代理脚本读候选、
            # 随机决策、写决策文件，这里阻塞轮询并自动恢复，替代 GUI 人工点按钮。
            if self._ai_review():
                self._ai_decide(ctx, detail)
            return

        self._grab(detail)
        self._mark_handled(detail, "grabbed")
        self._leave()

    # ---------------------------------------------------------------- 入库

    def _timing(self, what: str, ms: float) -> None:
        """关键步骤耗时日志——「感觉慢」得先能看见时间花在哪。

        同时把各步骤最新耗时写进 ctx.timings（按步骤名覆盖存储最新值），
        供 GUI「耗时明细」面板实时展示列表扫描 / OCR / 详情页 / 单轮等时间。
        """
        if not bool(self.ctx.store.get("runtime", "trace.timing", True)):
            return
        self.ctx.timings[what] = float(ms)
        self.ctx.counters["_timing_ms_total"] = (
            float(self.ctx.counters.get("_timing_ms_total", 0.0)) + ms
        )
        self.ctx.emit("info", f"[耗时] {what} {ms:.0f}ms")

    def _status_of(self, status: str) -> str:
        """试跑（dry_run）的记录要打上 dry_ 前缀。

        为什么：dry_run 下 `_grab` 只记录不点击，但订单库照样写成了 grabbed。
        库是**跨运行**去重的，于是「试跑看过的单」在正式跑时被判成已处理而被
        跳过——3 天内这些真单再也不会被看（实测试跑一次就写进 29 条 grabbed）。
        dry_ 前缀的记录不参与去重查询（见 OrderStore.is_seen）。
        """
        if bool(self.ctx.store.get("runtime", "dry_run", True)):
            return f"dry_{status}"
        return status

    def _mark_handled(self, detail: "OrderDetail", status: str) -> None:
        """统一出口：写订单库 + 把「参数指纹」回写给扫描器。

        为什么必须两处都写：
            * 订单库管**跨运行**（今天看过的单明天不再点）；
            * 扫描器内存管**本次运行**（刚从详情出来，回列表那一屏立刻生效——
              此时订单库也查得到，但内存多一层保险，且列表卡读不到路线时也能拦）。
        之前只写库、不回写内存，且列表扫描根本不查库，于是出现
        「进详情 → 放弃 → 回列表 → 又点同一张 #1」的死循环（用户实测连点三次）。
        """
        self._record_order(detail, self._status_of(status))
        self._remember_spec(detail.che_len, detail.che_type, detail.weight_ton, detail.volume_m3)

    def _mark_handled_fs(self, fs, status: str) -> None:
        """弹窗分支（已被抢/已下架）专用：当前帧抽参数后按 _mark_handled 处理。

        弹窗遮挡会让部分字段读不到（参数为空就不登记，宁可重复看也不误杀）；
        异常一律静默——标记失败只影响去重，绝不能中断主流程。
        """
        try:
            detail = self.reader.read(fs)
        except Exception as exc:  # noqa: BLE001
            self.ctx.emit("warning", f"弹窗页参数抽取失败，跳过登记: {exc}")
            return
        self._mark_handled(detail, status)

    def _remember_spec(self, che_len, che_type, tonnage, volume) -> None:
        if self._scanner is None:
            return
        try:
            from core.flow.scanner import ListScanner  # 局部导入：避免与 handlers 成环

            fp = ListScanner.spec_fingerprint(che_len, che_type, tonnage, volume)
            if fp:
                self._scanner.remember(fp)
        except Exception as exc:  # noqa: BLE001
            self.ctx.emit("warning", f"扫描器去重登记失败: {exc}")

    def _record_order(self, detail: "OrderDetail", status: str) -> None:
        """把订单写入去重库（跨运行持久）。失败静默降级，绝不中断主流程。

        dup_key 用结构化稳定字段（路线+始发地+目的地+车长+车型+货重+方数），
        price/distance 浮动不进 key——见 order_store.OrderStore.dup_key。
        """
        store = self.ctx.order_store
        if store is None:
            return
        try:
            route = ""
            if self.ctx.plan is not None and self.ctx.plan.current is not None:
                route = self.ctx.plan.current.describe()
            od = OrderStore.extract_od(detail.texts)
            odk = OrderStore.dup_key(
                route, od[0], od[1], detail.che_len, detail.che_type,
                detail.weight_ton, detail.volume_m3,
            )
            store.record(
                odk, route=route, origin=od[0], dest=od[1],
                che_len=detail.che_len, che_type=detail.che_type,
                tonnage=detail.weight_ton, volume=detail.volume_m3,
                price=detail.price, distance=detail.distance_km,
                unit_price=detail.unit_price(), status=status,
            )
            self.ctx.bump("order_db_recorded")
        except Exception as exc:  # noqa: BLE001
            self.ctx.emit("warning", f"订单入库失败: {exc}")

    # ---------------------------------------------------------------- 审核/展示

    def _review_mode(self) -> bool:
        """是否只审不抢：停在详情页等人工。默认 True（安全），关掉才真正抢单。

        注意：dry_run 是另一道保险——即便 review_mode=False，dry_run=True 时 _grab
        也只会记录不点击。两层叠加，正式测试期绝不会误抢真单。
        """
        return bool(self.ctx.store.get("runtime", "review_mode", True))

    def _no_pause_test(self) -> bool:
        """不停止测试模式：详情合格也不暂停，等 2 秒自动返回列表继续扫。

        与 review_mode 配合：review_mode=True 时，本开关决定「合格是停等人还是
        自动返回」。配置 runtime.no_pause_test（GUI 按钮热切换），默认 False。
        """
        return bool(self.ctx.store.get("runtime", "no_pause_test", False))

    def _ai_review(self) -> bool:
        """AI 随机测试模式：详情候选合格后交给 AI 代理决策（不等人）。

        配置 runtime.ai_review（默认 False）。开启时 _ai_decide 把候选写进
        data/review_state.json 并轮询 data/review_decision.json（AI 代理脚本写），
        读到 continue/pindan/swap 后自动恢复流程——用于无人值守的长时间随机测试。
        ⚠️ 需同时关闭 no_pause_test，否则合格单走「停留自动返回」分支，到不了这里。
        """
        return bool(self.ctx.store.get("runtime", "ai_review", False))

    def _ai_decide(self, ctx: FlowContext, detail: OrderDetail) -> None:
        """写候选状态 → 阻塞轮询 AI 决策文件 → 执行并恢复。

        决策协议（data/review_decision.json）：
            {"action": "continue"}                       继续扫单（跳过，不扣减）
            {"action": "pindan"}                         拼单（扣减余量）
            {"action": "swap", "ton":.., "m3":..,
             "che_length":[..], "che_type":[..]}         结束换车（换新车 + 清空余量）

        超时（runtime.ai_review_timeout_sec，默认 180s）按 continue 处理，绝不卡死。
        决策文件读到即删除，避免恢复分支重复消费。
        """
        import json
        from pathlib import Path

        def _cfg(key: str, default):
            return ctx.store.get("runtime", key, default)

        # 相对路径锚定项目根（打包后 = exe 同级）：否则 AI 代理脚本（走
        # core.config_store.PROJECT_ROOT）与这里会指向不同目录，两边永远对不上，
        # 每单白等 ai_review_timeout_sec 后按 continue 处理（2026-09-13 打包适配）。
        from core.config_store import PROJECT_ROOT as _ROOT

        def _anchor(p) -> Path:
            q = Path(str(p))
            return q if q.is_absolute() else (_ROOT / q)

        state_path = _anchor(_cfg("ai_review_state_file", "data/review_state.json"))
        decision_path = _anchor(_cfg("ai_review_decision_file", "data/review_decision.json"))
        timeout = float(_cfg("ai_review_timeout_sec", 180) or 180)

        route = ""
        if ctx.plan is not None and ctx.plan.current is not None:
            route = ctx.plan.current.describe()
        od = OrderStore.extract_od(detail.texts)
        state = {
            "ts": time.time(),
            "route": route,
            "origin": od[0],
            "dest": od[1],
            "che_len": detail.che_len,
            "che_type": detail.che_type,
            "weight_ton": detail.weight_ton,
            "volume_m3": detail.volume_m3,
            "price": detail.price,
            "distance_km": detail.distance_km,
            "unit_price": detail.unit_price(),
            "vehicle_ton": ctx.capacity.max_ton,
            "vehicle_m3": ctx.capacity.max_m3,
            "left_ton": ctx.capacity.ton_left,
            "left_m3": ctx.capacity.m3_left,
        }
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            # 先清掉上一单残留的决策文件，避免误读旧决策
            decision_path.unlink(missing_ok=True)
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            ctx.emit("warning", f"AI 决策：写候选状态失败 {exc}，按继续扫单处理")
            return

        ctx.emit(
            "info",
            f"AI 决策模式：候选已写入 {state_path}（{od[0]}→{od[1]} "
            f"{detail.che_len}米{detail.che_type} 单价 {detail.unit_price()} 元/km），等待决策…",
        )
        deadline = time.time() + timeout
        while time.time() < deadline and not ctx.stopped:
            if decision_path.exists():
                try:
                    data = json.loads(decision_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    ctx.emit("warning", f"AI 决策：读决策文件失败 {exc}，忽略")
                    decision_path.unlink(missing_ok=True)
                    ctx.sleep(0.5)
                    continue
                decision_path.unlink(missing_ok=True)
                action = str(data.get("action", "continue")).lower()
                ctx.emit("info", f"AI 决策：{action}（{data.get('reason', '')}）")
                if action == "pindan":
                    ctx.set_review_action("pindan")
                elif action == "swap":
                    self._apply_swap(ctx, data)
                    ctx.set_review_action("swap")
                else:
                    ctx.set_review_action(None)
                ctx.resume()
                return
            ctx.sleep(0.5)

        ctx.emit("warning", "AI 决策超时，按继续扫单处理")
        ctx.set_review_action(None)
        ctx.resume()

    def _apply_swap(self, ctx: FlowContext, data: dict) -> None:
        """换车：同步更新 RuleEngine 与 Capacity 的车辆参数，并清空余量。

        两处必须同步——rules 管「车长/车型/容量判定」，capacity 管「拼单余量账本」，
        只改一处会出现「判定按旧车、扣减按新车」的不一致。
        """
        def _pick(key, default):
            v = data.get(key, default)
            return v if v is not None else default

        che_length = _pick("che_length", list(getattr(ctx.rules, "che_length", ()) or []))
        che_type = _pick("che_type", list(getattr(ctx.rules, "che_type", ()) or []))
        ton = float(_pick("ton", getattr(ctx.rules, "max_load_ton", 0) or 0) or 0)
        m3 = float(_pick("m3", getattr(ctx.rules, "max_volume_m3", 0) or 0) or 0)
        if che_length:
            ctx.rules.che_length = tuple(str(x) for x in che_length)
        if che_type:
            ctx.rules.che_type = tuple(str(x) for x in che_type)
        if ton > 0:
            ctx.rules.max_load_ton = ton
            ctx.capacity.max_ton = ton
        if m3 > 0:
            ctx.rules.max_volume_m3 = m3
            ctx.capacity.max_m3 = m3
        ctx.capacity.reset()
        ctx.emit(
            "info",
            f"换车：车长={list(ctx.rules.che_length)} 车型={list(ctx.rules.che_type)} "
            f"{ctx.capacity.max_ton:g}吨/{ctx.capacity.max_m3:g}方，余量已清零",
        )

    def _emit_candidate(self, detail: OrderDetail, verdict: Verdict) -> None:
        """把候选关键参数写进 ctx.candidate，供 GUI 实时展示（人工核对用）。"""
        self.ctx.candidate = self._candidate_dict(detail, verdict)
        unit = detail.unit_price()
        line = (
            f"[候选] 车长={detail.che_len} 车型={detail.che_type} "
            f"货重={detail.weight_ton}吨/{detail.volume_m3}方 "
            f"价格={detail.price}元 距离={detail.distance_km}km 单价={unit}元/km "
            f"判定={'合格' if verdict else '淘汰'} 原因={verdict.reason}"
        )
        self.ctx.emit("info", line, stage="candidate")

    @staticmethod
    def _candidate_dict(detail: OrderDetail, verdict: Verdict) -> dict:
        unit = detail.unit_price()
        return {
            "che_len": detail.che_len,
            "che_type": detail.che_type,
            "cargo": detail.cargo,
            "weight_ton": detail.weight_ton,
            "volume_m3": detail.volume_m3,
            "price": detail.price,
            "distance_km": detail.distance_km,
            "unit_price": unit,
            "pass_": bool(verdict),
            "reason": verdict.reason,
            # 详情页 OCR 原文（截到 600 字），GUI「详情原文」框回退展示货物内容等
            "raw_text": " | ".join(t for t in detail.texts if t)[:600],
        }

    def _shot_detail(self, fs, detail: "OrderDetail", verdict: Verdict) -> None:
        """详情页留证截图（测试阶段回放分析用）。

        走 TraceWriter.shot(always=True)：每条进过详情的订单必存一张，文件名带
        判定结果与始/目的地，跑完一轮能按图回放「哪张单、判成什么、为什么」。
        异步写盘（独立线程），绝不阻塞主流程；失败静默——留证是锦上添花。
        """
        tr = getattr(self.ctx, "trace", None)
        if tr is None or not getattr(tr, "enabled", False):
            return
        try:
            od = OrderStore.extract_od(detail.texts)
            o = re.sub(r"[^\w\u4e00-\u9fff]", "", od[0] or "")[:12] or "x"
            d = re.sub(r"[^\w\u4e00-\u9fff]", "", od[1] or "")[:12] or "y"
            tag = f"detail_{'pass' if verdict else 'reject'}_{o}_{d}"
            tr.shot(fs.raw, tag, always=True)
        except Exception:  # noqa: BLE001 截图失败绝不中断主流程
            pass

    # ---------------------------------------------------------------- 弹窗

    def _has_overlay_veil(self, fs) -> bool:
        """视觉兜底：检测画面是否被**半透明灰色蒙版**覆盖（盖层弹窗的标志）。

        为什么需要：这种「该货源已被抢 / 已下架」弹窗是盖在详情页上的蒙层，
        左上返回箭头、右上分享按钮仍在（模板照样命中），page_state 因此把它判成
        正常 DETAIL。主识别靠 OCR 命中弹窗标题文字（见 _dismiss_detail_dialog），
        但蒙版太厚时红色标题字可能 OCR 不出来 → 漏检 → 程序停在死单上。
        于是加一层纯视觉判据：蒙版会让**屏幕四周（弹窗卡片外）变成均匀灰色**，
        而正常详情页四周是白底(V≈240)或彩色内容(方差大)。用「四周灰度
        均值偏暗 + 方差很小」识别蒙版，与 OCR 文字判据互为兜底。

        ⚠️ 默认关闭（dialog_veil.enabled=false）：2026-09-06 实测正常详情页
        （白底/灰底）也会满足 mean/std 阈值 → 误判蒙版、误关健康详情页。在拿到
        真机标定的可靠阈值前，主判据只用 OCR 文字（已验证可靠）。若将来遇到
        OCR 确实漏检弹窗停死单，再把 enabled 打开并一起调准阈值。

        注意：本函数只作**触发信号**，真正关弹窗仍走 _close_dialog（点×后
        会 wait_state 验证是否回列表）；真没弹窗时点×无反应、不回列表，
        自动放弃、继续正常详情处理，不会对健康详情页造成误伤。
        """
        cfg = self.ctx.store.section("runtime").get("dialog_veil", {}) or {}
        if not bool(cfg.get("enabled", False)):
            return False
        gray = cv2.cvtColor(fs.fast, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        m = max(8, int(min(h, w) * 0.12))  # 四周条带宽度
        bands = [gray[0:m], gray[h - m : h], gray[:, 0:m], gray[:, w - m : w]]
        flat = np.concatenate([b.ravel() for b in bands]).astype(np.float32)
        mean = float(flat.mean())
        std = float(flat.std())
        # 阈值可配：蒙版下四周均值偏暗(<215)且高度均匀(std<20)
        cfg = self.ctx.store.section("runtime").get("dialog_veil", {}) or {}
        mean_thr = float(cfg.get("mean_max", 215))
        std_thr = float(cfg.get("std_max", 20))
        return mean < mean_thr and std < std_thr

    def _dismiss_detail_dialog(self, fs) -> Optional[str]:
        """详情页上的「已被抢 / 已下架」弹窗：命中就关掉并回列表，返回处置类型。

        返回值：
            "gone" —— 已下架/被抢（订单失效，调用方应登记去重，别再点）；
            "know" —— 只是「我知道了」提示（订单有效，留在详情页继续处理）；
            None   —— 没有弹窗。
        旧签名只返回 bool，调用方无法区分「订单死了」和「只是个提示」，
        会把「我知道了」的健康订单也登记成已处理 → 真单被永久跳过。

        为什么在详情处理器里单独处理：这类弹窗是盖在详情页上的，详情页左上返回箭头
        和右上分享仍稳定命中（阈值 0.86），page_state 会把它判成 DETAIL 而非
        DIALOG_DETAIL，于是主循环走不到 handle_dialog。必须在这里自行识别并收掉，
        否则程序会停在「死单」上不动（用户实测反馈）。

        识别双判据：
            1. OCR 文字命中"已被抢/已下架/货源已定"（主，最准）；
            2. 视觉检测半透明灰蒙版（兜底，OCR 漏检时使用，见 _has_overlay_veil）。
        两者任一命中即尝试关闭；关闭后 wait_state 验证是否回列表，未回则放弃（不误伤）。
        """
        ctx = self.ctx
        if fs is None or getattr(fs, "fast", None) is None:
            return False
        text = self._ocr_text(fs)
        # 「我知道了」黄色按钮提示：盖在详情页上的小弹窗，点它关闭后【仍在详情页】
        # （与「已被抢/下架」不同——后者关后回列表）。只用 OCR 文字认，避免正常详情页
        # 被误关（dialog_know 模板在正常页误命中率高，这里只作点击目标，见 page_state 注释）。
        if text and "我知道了" in text:
            ctx.emit("info", "详情页「我知道了」提示，点黄色按钮关闭（留在详情）")
            ctx.bump("detail_know_dismissed")
            self._close_know(fs)
            return "know"
        # 主判据：OCR 命中弹窗标题
        for kw in self._dialog_keywords():
            if text and kw in text:
                ctx.emit("warning", f"详情页弹窗（OCR 命中「{kw}」），关闭并回列表")
                ctx.bump("detail_dialog_dismissed")
                self._close_dialog(fs)
                return "gone"
        # 兜底判据：OCR 没认出标题文字，但画面有半透明灰蒙版 → 仍尝试关（抗漏检）
        if text is not None and self._has_overlay_veil(fs):
            ctx.emit("warning", "详情页疑似盖层弹窗（OCR 未命中标题，但检测到灰蒙版），尝试关闭")
            ctx.bump("detail_dialog_veil")
            self._close_dialog(fs)
            return "gone"
        return None

    @staticmethod
    def _dialog_keywords() -> tuple:
        """弹窗标题特征字。取短词以抗 OCR 偶发错字，正常详情页不会出现。"""
        return ("已被抢", "货源已定", "已下架")

    def _ocr_text(self, fs) -> str:
        """把当前帧 OCR 成一段连续文本（弹窗判据用）。失败返回空串，绝不抛异常。"""
        try:
            boxes = self.ctx.kit.ocr_frame(fs)
        except Exception as exc:  # noqa: BLE001 OCR 失败不能中断主流程
            self.ctx.emit("warning", f"弹窗判据 OCR 失败: {exc}")
            return ""
        return "".join(getattr(b, "text", "") or "" for b in (boxes or []))

    def _find_dialog_close_point(self, fs) -> Optional[Tuple[int, int]]:
        """用 OCR 定位弹窗标题（已被抢/已下架/货源已定）→ 取标题中心 → × = 中心X + 固定偏移。

        弹窗 UI 布局固定：标题文字与关闭 × 在同一水平线，相对位置恒定。
        实测锚点：标题"该货源已被抢/已下架"中心 X≈245，× 中心 X≈381，
        偏移 = 381-245 = 136px。用「标题中心X + offset」算 ×，不依赖屏幕宽度，
        且能自动跟随弹窗上下浮动（键盘/机型差异）。

        返回 fast 帧 (x, y) 或 None（未找到弹窗标题时退化回固定坐标）。
        """
        ctx = self.ctx
        h, w = fs.fast.shape[:2]

        # 从配置读偏移策略
        cfg = ctx.store.section("runtime").get("dialog_close_points", {}) or {}
        offset_x = int(cfg.get("offset_from_center", 136))   # × 相对标题中心X的偏移
        mode = str(cfg.get("close_mode", "anchor") or "anchor")  # anchor=OCR锚点, fixed=纯固定坐标

        if mode != "anchor":
            # 退化回旧版固定归一化坐标
            pt = cfg.get("detail_grabbed", [0.792, 0.330])
            return (int(float(pt[0]) * w), int(float(pt[1]) * h))

        try:
            boxes = ctx.kit.ocr_frame(fs)
        except Exception:  # noqa: BLE001 OCR 失败 → 退化
            return None

        keywords = self._dialog_keywords()
        for box in (boxes or []):
            text = getattr(box, "text", "") or ""
            if any(kw in text for kw in keywords):
                # 找到弹窗标题 → 用标题文字中心算 × 坐标
                cx, cy = box.center          # 标题中心 (≈245, ≈211)
                close_x = cx + offset_x      # × = 标题中心X + 136 (≈381)
                close_x = min(max(close_x, 0), w - 1)  # 约束在帧内
                return (close_x, cy)

        return None  # 未找到弹窗关键词 → 调用方退化

    def _close_dialog(self, fs) -> None:
        """关弹窗（优先级从高到低）：

        1. **OCR 锚点+偏移**：找"已被抢/已下架/货源已定"文字 → 取 Y →
           X 用固定偏移（默认距右边缘 22px）算出 × 坐标 → 点击。
           原因：弹窗可能上下浮动，OCR 自动跟随；× 与标题同行、X 偏差恒定。
        2. **固定归一化坐标**（OCR 未找到标题时的兜底）。
        3. **右滑返回手势**（iOS 盖层页吃这个手势）。
        4. **模板匹配关闭按钮** / **物理返回键**（最终兜底）。
        """
        ctx = self.ctx
        h, w = fs.fast.shape[:2]

        # ---- 1. OCR 锚点模式 ----
        pt = self._find_dialog_close_point(fs)
        if pt is not None:
            cx, cy = pt
            reason = f"关闭弹窗(OCR锚点+偏移) ({cx},{cy})"
            if ctx.kit.click_point(cx, cy, reason=reason):
                # 缩短验证超时：iOS 弹窗关闭动画 1~1.5s，2s 够用。原 3s 太保守，
                # 「详情(已下架/被抢)」均值 11.4s 的主要耗时大头就是这里多次重试。
                _, res = ctx.kit.wait_state(self._list_states(), timeout=2.0, interval=0.4)
                if res.state in self._list_states():
                    return

        # ---- 2. 固定归一化坐标兜底 ----
        cfg = ctx.store.section("runtime").get("dialog_close_points", {}) or {}
        fixed_pt = cfg.get("detail_grabbed", [0.792, 0.330])
        if isinstance(fixed_pt, (list, tuple)) and len(fixed_pt) == 2:
            fx = int(float(fixed_pt[0]) * w)
            fy = int(float(fixed_pt[1]) * h)
            if ctx.kit.click_point(fx, fy, reason="关闭弹窗(固定坐标)"):
                _, res = ctx.kit.wait_state(self._list_states(), timeout=2.0, interval=0.4)
                if res.state in self._list_states():
                    return

        # ---- 3. 右滑返回 ----
        ctx.kit.swipe_back_gesture()
        _, res = ctx.kit.wait_state(self._list_states(), timeout=2.0, interval=0.4)
        if res.state in self._list_states():
            return
        if ctx.kit.click_close_candidate(fs=fs) is None:
            ctx.kit.press_back()
        _, res = ctx.kit.wait_state(self._list_states(), timeout=2.5, interval=0.4)
        if res.state not in self._list_states():
            ctx.kit.press_back()
            ctx.kit.wait_state(self._list_states(), timeout=2.5, interval=0.4)

    def _close_know(self, fs) -> None:
        """点「我知道了」黄色按钮关闭提示。三级降级：OCR 文字 → 模板 → 固定坐标。

        关后调用方返回 True、仍在详情页（不回列表），下一轮重新判态会再走正常详情读取。

        2026-09-11 踩坑：旧实现只有「模板 + 固定坐标 [0.5,0.82]=(188,666)」，实测
        「我知道了」按钮在 y≈460（归一化 0.57），固定坐标点偏 206px → 连点 26 次
        关不掉 → same_screen 触发停止。故加 OCR 文字定位为第一优先。
        """
        ctx = self.ctx
        # 1. OCR 找「我知道了」文字并点其中心（最稳，按钮位置变化也能跟）
        if ctx.kit.click_text_norm("我知道了", (0.15, 0.4, 0.85, 0.7), fs=fs, reason="我知道了"):
            return
        # 2. 模板兜底
        if ctx.kit.click_template("dialog_know", fs=fs, reason="我知道了"):
            return
        # 3. 固定坐标兜底（2026-09-11 实测更新为 [0.5,0.57]）
        h, w = fs.fast.shape[:2]
        pt = ctx.store.get("runtime", "dialog_close_points.detail_know", [0.5, 0.57])
        if isinstance(pt, (list, tuple)) and len(pt) == 2:
            ctx.kit.click_point(float(pt[0]) * w, float(pt[1]) * h, reason="我知道了兜底")

    def _log_dialog_scores(self, fs, note: str) -> None:
        """把当前画面上弹窗模板的**实际分数**打进日志（**只作诊断参考，不参与
        判定**——弹窗判据已改用 OCR 文字，见 _dismiss_detail_dialog）。

        为什么保留：模板分数反映「屏幕上到底有没有弹窗那个图案」，与 OCR 文字
        互为佐证。2026-09-05 正是靠它发现 dialog_grabbed 在真弹窗上只有 0.62、
        在正常详情页上却有 0.979，才断定模板方案对此类弹窗不可用。
        """
        if fs is None or getattr(fs, "fast", None) is None:
            return
        parts = []
        for name in ("dialog_grabbed", "dialog_offline", "close_x", "detail_back"):
            try:
                spec = self.ctx.store.template(name)
                hit = self.ctx.kit.matcher.find(fs.fast, spec)
                score = float(hit.score) if hit is not None else 0.0
            except Exception:
                parts.append(f"{name}=n/a")
                continue
            thr = float(getattr(spec, "threshold", 0.0) or 0.0)
            parts.append(f"{name}={score:.2f}/{thr:.2f}")
        self.ctx.emit("info", f"[{note}] 画面分数：{' '.join(parts)}")

    # ---------------------------------------------------------------- 动作

    def _guard_blocked(self, detail: OrderDetail) -> str:
        """抢单护栏：单价下限 + 每小时上限。返回空串表示放行，否则返回拦截原因。

        为什么要有护栏：判定不可能 100% 准，一旦误判就是真金白银抢错单。
        护栏是 dry-run 之外的第二道保险——即使有人忘了关 dry_run 之外，
        还有一个"单价太低不抢""一小时抢太多暂停"的兜底。
        """
        guard = self.ctx.store.section("rules").get("price", {}).get("grab_guard", {}) or {}
        floor = guard.get("require_unit_price_above")
        unit = detail.unit_price()
        if floor is not None and unit is not None and unit < float(floor):
            self.ctx.bump("guard_blocked_price")
            return f"单价 {unit:.2f} 低于护栏下限 {float(floor):.2f}"

        limit = int(guard.get("max_grab_per_hour", 0) or 0)
        if limit > 0 and self._grabbed_last_hour() >= limit:
            self.ctx.bump("guard_blocked_rate")
            return f"近一小时已抢 {limit} 单，达到上限"
        return ""

    def _grabbed_last_hour(self) -> int:
        now = time.time()
        while self._grab_times and now - self._grab_times[0] > 3600.0:
            self._grab_times.popleft()
        return len(self._grab_times)

    def _grab(self, detail: OrderDetail) -> None:
        """点抢单并确认结果。抢到才扣余量，没抢到不扣。"""
        ctx = self.ctx
        unit = detail.unit_price()

        blocked = self._guard_blocked(detail)
        if blocked:
            ctx.emit("warning", f"护栏拦截，不抢：{blocked}")
            return

        if bool(ctx.store.get("runtime", "dry_run", True)):
            # 试运行：全流程照跑，就是不点那一下。
            # 人工核对 dry_run_would_grab 计数与判定原因，确认准确后再关掉。
            ctx.emit(
                "info",
                f"[DRY-RUN] 将抢单 单价={unit:.2f} "
                f"余量推演={'够' if ctx.capacity.can_take(detail.weight_ton, detail.volume_m3) else '不够'} "
                f"({ctx.capacity.describe()})",
            )
            ctx.bump("dry_run_would_grab")
            return

        roi = ctx.store.get("fields", "entries.confirm_roi", (0.0, 0.62, 1.0, 0.99))
        if ctx.kit.click_text_norm("立即抢单", roi, reason="抢单") is None:
            ctx.emit("warning", "未找到「立即抢单」按钮，放弃本单")
            ctx.bump("grab_button_missing")
            return
        time.sleep(1.0)

        fs, res = ctx.kit.wait_state(
            (ScreenState.DETAIL, ScreenState.DIALOG_DETAIL, ScreenState.CHAT, *self._list_states()),
            timeout=5.0,
            interval=0.5,
        )
        # 抢单成功通常伴随「确认接单」类弹窗或跳转聊天页
        grabbed = res.state in (ScreenState.DIALOG_DETAIL, ScreenState.CHAT)
        if grabbed:
            ctx.capacity.commit(
                f"{detail.price or '?'}元/{detail.distance_km or '?'}km",
                ton=detail.weight_ton,
                m3=detail.volume_m3,
            )
            self._grab_times.append(time.time())
            ctx.bump("grabbed")
            ctx.emit("info", f"抢单成功，{ctx.capacity.describe()}")
            ctx.kit.save_debug(fs, "grabbed") if fs is not None else None
        else:
            ctx.bump("grab_failed")
            ctx.emit("warning", f"抢单结果不明确（{res.state.value}），按未抢到处理")

    def _leave(self) -> None:
        """回列表。核心原则：**每次动作前重新判态，按屏幕实际画面决定下一步**；
        绝不底部上滑回桌面——那会退出 App，重进要重新导航，对扫描节奏是毁灭性的。

        血泪链（2026-09-05 用户实测）：详情停留期间弹出「已被抢」弹窗 → 该弹窗
        盖在详情上、返回箭头仍被模板看到（score 0.98）但点击被弹窗遮罩吃掉 →
        死点返回 3 次无效 → 右滑在弹窗页横切进相邻订单详情 → 底部上滑退到桌面
        → 桌面被 settle_anchor 误判成列表 → 继续刷新/下滑把手机搞进搜索页。
        整条链的根因就是动作前不看屏幕。
        """
        ctx = self.ctx
        base_z = float(ctx.store.get("hardware", "calibration.z_press", 5.7) or 5.7)
        boost = float(ctx.store.get("hardware", "calibration.press_boost_delta", 0.2) or 0.2)
        from core.flow import handlers as H

        from core.vision.page_state import ScreenState

        list_states = self._list_states()

        def _settle() -> bool:
            """返回后判落点：到订单列表即成功；落到首页（白色全国货源 Tab）
            或桌面则分别点白 Tab / 重新进 App 回到订单列表——否则会反复右滑、
            卡死在详情页，甚至在桌面干刷右滑（2026-09-06 实测）。

            血泪：本 App 详情页右滑/返回常回「首页」而非订单列表，旧逻辑
            只认 LIST_STATES，把 home_other 当成失败，于是不停右滑空转；
            更糟的是退到桌面时旧逻辑完全不认 DESKTOP，于是在桌面右滑半天。
            """
            _, res = ctx.kit.wait_state(
                list_states + (ScreenState.HOME_OTHER, ScreenState.DESKTOP),
                timeout=4.0,
                interval=0.25,
            )
            if res.state in list_states:
                ctx.bump("detail_returned")
                ctx.reset("leave_fail_streak")
                self._after_detail_return()
                return True
            if res.state is ScreenState.HOME_OTHER:
                # 落点是首页（白色全国货源 Tab），点它切回订单列表
                H.handle_home_other(ctx, ctx.kit.frame(), res)
                _, after = ctx.kit.wait_state(list_states, timeout=5.0, interval=0.5)
                if after.state in list_states:
                    ctx.bump("detail_returned_via_home")
                    ctx.reset("leave_fail_streak")
                    self._after_detail_return()
                    return True
            if res.state is ScreenState.DESKTOP:
                # 返回手势把 App 退到后台了：重新进 App，不要停在桌面右滑
                ctx.emit("warning", "返回落到了桌面，重新进入 App（运满满）")
                ctx.bump("detail_returned_via_desktop")
                H.handle_desktop(ctx, ctx.kit.frame(), res)
                _, after = ctx.kit.wait_state(list_states, timeout=6.0, interval=0.5)
                if after.state in list_states:
                    ctx.reset("leave_fail_streak")
                    # 重新进 App 后筛选可能被重置（出发地变「当前」）——置重设路线
                    # 标志，让主循环重新设路线，绝不在「当前位置」的订单上继续扫。
                    # 2026-09-12 实测：返回落桌面→重进 App→筛选清空+听单被打开。
                    ctx.route_setup_pending = True
                    self._after_detail_return()
                    return True
            return False

        # 逐次加深：压不响就压深一点（+0.2、+0.4），绝不往浅了试——
        # 电容屏触发不了的原因只会是「压得不够实」，往浅试等于白点一次。
        for i, dz in enumerate((0.0, boost, boost * 2), start=1):
            # 第一次直接点（正常详情页一次就中）；失败后再看屏幕——
            # OCR 判据有开销，只走异常路径，不给每张详情都加一次 OCR。
            if i > 1 and self._dismiss_detail_dialog(ctx.kit.frame()):
                if _settle():
                    return
                continue  # 弹窗收掉但还没回列表，下一轮继续正常返回

            z = None if dz == 0.0 else base_z + dz
            ctx.kit.press_back(z=z)
            if _settle():
                return
            # 点不动就把画面实际分数打出来：区分「模板过期」和「弹窗还没弹出」
            self._log_dialog_scores(ctx.kit.frame(), "点返回无效")

        # 点击无效 → 快速右滑返回手势（iOS 标准返回；250~350ms 快速轻扫，
        # 慢了 iOS 识别为拖拽，页面被拖出一半又弹回——即「卡顿、搓屏幕」）。
        # 单次右滑有时不导航，最多试两次；落点若是首页则 _settle 自动点白 Tab 切回。
        for _ in range(2):
            ctx.emit("warning", "返回箭头多次点击无效，改用右滑返回手势")
            ctx.kit.swipe_back_gesture()
            if _settle():
                return

        # 仍失败：重新判态，按实际画面升级处理——绝不 press_home 退出 App，
        # 真落到桌面就重新进 App，绝不能停在桌面右滑。
        ctx.bump("detail_return_failed")
        fs = ctx.kit.frame()
        res = ctx.kit.detect(fs)
        if res.state is ScreenState.DESKTOP:
            ctx.emit("error", "详情页退不出、且已在桌面：重新进入 App（运满满）")
            ctx.bump("detail_exit_to_desktop")
            H.handle_desktop(ctx, fs, res)
            ctx.route_setup_pending = True  # 重进 App 后筛选可能被重置，重设路线
            return
        if res.state is ScreenState.HOME_OTHER:
            ctx.emit("warning", "详情页退不出、落在首页：点白 Tab 回列表")
            H.handle_home_other(ctx, fs, res)
            return
        if res.state in H.DIALOG_STATES:
            ctx.emit("warning", "详情页无法退出：当前画面是弹窗，转弹窗处理器关闭")
            H.handle_dialog(ctx, fs, res)
            return
        # 仍是详情/未知/其它：先再试收一次弹窗，仍不行就交回主循环重判
        # （不在此死循环；主循环下一帧会重新 dispatch，watchdog 是最后保险）。
        if self._dismiss_detail_dialog(fs):
            if _settle():
                return
        ctx.emit("warning", f"详情页无法退出（落点 {res.state.value}），交回主循环重新判态")
        streak = ctx.bump("leave_fail_streak")
        if streak >= 2:
            ctx.emit("error", f"连续 {streak} 次无法退出详情页，暂停 5s（请人工观察机械臂/手机）")
            ctx.reset("leave_fail_streak")
            ctx.sleep(5.0)

    def _after_detail_return(self) -> None:
        """详情返回列表后**不滑动**，把「要不要翻页」交回扫描钩子。

        这里曾经无条件下滑一屏，实测是最大的漏单源（2026-09-06 trace 分析）：
            一屏命中 1~4 张，只点 hits[0] 就滑走，61 张命中卡里有 32 张（52%）
            **从头到尾没进过详情**——它们既没被点，也没进 seen，滑走后再没回来。

        现在靠去重保证不原地打转：返回后本屏剩下的命中（#2/#3）会被依次点开，
        点过的卡已进 seen（文本指纹 + 参数指纹 + 订单库三级），再出现会被跳过；
        全屏都判重时扫描钩子自然下滑。万一指纹被 OCR 抖动冲破，扫描钩子里还有
        「同一落点短时内不重复点」的兜底（见 make_scan_hook）。
        """
        self.ctx.bump("detail_returned_keep_screen")
        # 返回列表后清空「当前检测到的订单详情」：详情区先空出来，下一轮扫描到
        # 新单再填充（避免停留显示上一张已离开的详情，误导人工核对）。
        self.ctx.candidate = None

    @staticmethod
    def _list_states() -> tuple:
        from core.flow import handlers

        return handlers.LIST_STATES

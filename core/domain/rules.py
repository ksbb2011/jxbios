"""订单筛选规则：排除项 → 车长/车型 → 载重余量 →（过线）进详情算单价。

判定顺序与 config/rules.json 的 _note 一致，顺序不能换：
    文字排除 → 图色排除 → 车长车型 → 吨位方数是否超剩余 → 进详情算单价。
前两步是「看一眼就能否掉」的便宜判定，最后一步最贵（要进详情页 OCR），
所以必须放最后 —— 旧工程把单价判定提前，一屏要进十几个详情页，慢到没法用。
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Sequence

from core.config_store import ConfigStore
from core.domain.orders import OrderCard, OrderDetail, Verdict

_NUM = re.compile(r"(\d+(?:\.\d+)?)")
_MULTI_SPLIT = re.compile(r"[/|、,，\s]+")


def _split_multi(text: str) -> list:
    """把「4.2/5/6.2」「平板/高栏」这类多值串拆成成员列表（去空白与单位字）。"""
    members = []
    for part in _MULTI_SPLIT.split(text or ""):
        part = part.strip().strip("米吨方")
        if part:
            members.append(part)
    return members


def parse_number(text: Optional[str]) -> Optional[float]:
    """从文本里抠第一个数字（'4.2米' → 4.2）。失败返回 None，不猜、不返回 0。"""
    if not text:
        return None
    m = _NUM.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


class RuleEngine:
    """筛选规则引擎。配置从 config/rules.json 读，改配置即改行为。"""

    STAGE_TEXT = "text_exclude"
    STAGE_IMAGE = "image_exclude"
    STAGE_VEHICLE = "vehicle"
    STAGE_CAPACITY = "capacity"
    STAGE_PRICE = "price"
    # 数值明显是 OCR 读错（不是这单不合规）——跳过该卡并转人工，不算「淘汰」
    STAGE_SANITY = "sanity"

    def __init__(self, store: Optional[ConfigStore] = None) -> None:
        self._store = store or ConfigStore()
        rules = self._store.section("rules")
        vehicle = rules.get("vehicle") or {}
        self.che_length: Sequence[str] = tuple(str(x) for x in vehicle.get("che_length") or [])
        self.che_type: Sequence[str] = tuple(str(x) for x in vehicle.get("che_type") or [])
        self.max_load_ton: float = float(vehicle.get("max_load_ton", 0) or 0)
        self.max_volume_m3: float = float(vehicle.get("max_volume_m3", 0) or 0)
        self.sanity_max_ton: float = float(vehicle.get("sanity_max_ton", 0) or 0)
        self.sanity_max_m3: float = float(vehicle.get("sanity_max_m3", 0) or 0)
        # 按车长分档的容积上限（只有多辆车时才填，见 _note_volume_by_len）
        by_len = vehicle.get("max_volume_by_che_length") or {}
        self.volume_by_len: Dict[str, float] = {}
        self.volume_by_len_default: float = 0.0
        for k, v in (by_len.items() if isinstance(by_len, dict) else []):
            key = str(k)
            try:
                val = float(v)
            except (TypeError, ValueError):
                continue
            if key.startswith("_") or key == "_default":
                self.volume_by_len_default = val
            else:
                self.volume_by_len[key] = val
        price = rules.get("price") or {}
        self.min_unit_price: float = float(price.get("min_unit_price", 0) or 0)
        self.pass_when_below: bool = bool(price.get("pass_when_below", True))
        # 上限不是业务筛选（单价越高越好），而是 OCR 误读护栏：见 check_price
        self.max_unit_price: float = float(price.get("max_unit_price", 0) or 0)
        self.price_sanity_max: float = float(price.get("price_sanity_max", 0) or 0)
        self.exclude_keywords: Sequence[str] = tuple(
            str(x) for x in rules.get("exclude_keywords") or []
        )
        self.exclude_templates: Sequence[str] = tuple(
            str(x) for x in rules.get("exclude_templates") or []
        )

    # ---------------------------------------------------------------- 分步判定

    def check_text(self, text: str) -> Verdict:
        """文字排除：命中任一关键词直接否掉。"""
        blob = text or ""
        for kw in self.exclude_keywords:
            if kw and kw in blob:
                return Verdict.reject(self.STAGE_TEXT, f"命中排除词「{kw}」")
        return Verdict.ok(self.STAGE_TEXT)

    def check_image(self, hit_templates: Sequence[str]) -> Verdict:
        """图色排除：调用方把命中的模板逻辑名传进来（如 dianyi_tag）。"""
        for name in self.exclude_templates:
            if name in hit_templates:
                return Verdict.reject(self.STAGE_IMAGE, f"命中排除图「{name}」")
        return Verdict.ok(self.STAGE_IMAGE)

    def check_vehicle(self, card: OrderCard) -> Verdict:
        """车长/车型匹配（列表卡片版）。委托给 check_vehicle_params。"""
        return self.check_vehicle_params(
            card.che_length, card.che_type,
            param_text=getattr(card, "param_text", ""),
        )

    def check_vehicle_params(self, che_len: Optional[str], che_type: Optional[str],
                             param_text: str = "") -> Verdict:
        """车长/车型匹配（参数版，详情页也可复用）。

        订单未标车长车型时放行（列表页常缺失，进详情再看）。

        订单上的参数常是多值（如车长 4.2/5/6.2米、车型 平板/高栏/飞翼车），
        必须按分隔符拆开做**精确成员匹配**，不能用子串包含——
        子串会误匹配：规则「2」会命中「4.2/5」，规则「5」会命中「6.5」。
        """
        if self.che_length and che_len:
            lengths = _split_multi(che_len)
            # 「不限车长」就是符合要求（以前这类卡会因取不到参数行被整张跳过）
            if "不限" not in lengths:
                if not any(v in lengths for v in self.che_length):
                    # 容错：OCR 常把小数点弄丢（实测「4.2」读成「42」被判不符）。
                    # 去掉小数点再比一次——4.2→42、6.8→68，不会跨车长误匹配。
                    flat_len = {v.replace(".", "") for v in lengths}
                    if not any(a.replace(".", "") in flat_len for a in self.che_length):
                        return Verdict.reject(
                            self.STAGE_VEHICLE, f"车长 {che_len} 不在 {list(self.che_length)}"
                        )
        if self.che_type:
            # 正向查询（用户 2026-09-11 方案）：以规则为准，直接查「参数行原文/车型串」
            # 里是否含规则车型词。不再抠词拆分——OCR 把「高栏」拆成「高 栏」也不漏
            # （去空格后仍是高栏）；噪声词（卸/保/温…）完全不影响，因为只查规则词。
            # blob 为空（订单没标车型）则放行，进详情页再核（宽松策略，不误杀）。
            blob = (param_text or che_type or "").replace(" ", "")
            if blob and "不限" not in blob:
                if not any(v and v in blob for v in self.che_type):
                    return Verdict.reject(
                        self.STAGE_VEHICLE,
                        f"车型 {che_type or param_text} 不在 {list(self.che_type)}",
                    )
        return Verdict.ok(self.STAGE_VEHICLE)

    def check_detail(self, detail: OrderDetail) -> Verdict:
        """详情页全维度判定：车长/车型 → 载重上限 → 单价。

        详情页 OCR 几乎总能读到车型（实测 che_type 100%），所以这里用详情页读到的
        车型参数**再核一遍**列表页的判定——列表页常漏车型（用户已确认列表页采集不到
        车型/吨数），详情页反而更全，二道关卡更稳。

        详情页没读到车型/车长时不拦（OCR 偶发漏读），把判定权交回单价；
        但读到且不符合就直接否，绝不放宽。载重上限只在详情页货重可读且超限时否，
        漏读货重不误杀。
        """
        if detail.che_len or detail.che_type:
            v = self.check_vehicle_params(detail.che_len, detail.che_type)
            if not v:
                return v
        if self.sanity_max_ton and detail.weight_ton is not None and detail.weight_ton > self.sanity_max_ton:
            return Verdict.reject(
                self.STAGE_SANITY,
                f"货重 {detail.weight_ton:g} 吨超出常理（>{self.sanity_max_ton:g}），疑似读取错误，转人工",
            )
        if (
            self.sanity_max_m3
            and detail.volume_m3 is not None
            and detail.volume_m3 > self.sanity_max_m3
        ):
            return Verdict.reject(
                self.STAGE_SANITY,
                f"方数 {detail.volume_m3:g} 方超出常理（>{self.sanity_max_m3:g}），疑似读取错误，转人工",
            )
        if self.max_load_ton and detail.weight_ton is not None and detail.weight_ton > self.max_load_ton:
            return Verdict.reject(
                self.STAGE_CAPACITY,
                f"货重 {detail.weight_ton} 吨 > 车辆载重上限 {self.max_load_ton} 吨",
            )
        return self.check_price(detail)

    def volume_limit_for(self, che_len: Optional[str]) -> float:
        """按订单车长取容积上限；0 = 不额外收窄（未启用分档或读不到车长）。

        车长是多值时取**最宽松**的一档——订单写「4.2/5」表示两种车都行，
        按 5 米档算才不会把能接的单误杀。
        """
        if not self.volume_by_len or not che_len:
            return 0.0
        best = 0.0
        for v in _split_multi(che_len):
            if v in self.volume_by_len:
                best = max(best, self.volume_by_len[v])
        return best or self.volume_by_len_default

    def check_capacity(self, card: OrderCard, remaining_ton: Optional[float],
                       remaining_m3: Optional[float]) -> Verdict:
        """是否还装得下。余量为 None 表示未启用拼单，只看车辆上限。

        数值离谱时**不按「装不下」淘汰**，而是判「读取异常，转人工」：
        实测把「12.8 方」读成「128.0 方」，容量判定直接把它淘汰了——
        那不是这单不合规，是我们没读对。淘汰会永久错过，转人工才是对的。
        """
        if card.tonnage is not None and self.sanity_max_ton and card.tonnage > self.sanity_max_ton:
            return Verdict.reject(
                self.STAGE_SANITY,
                f"货重 {card.tonnage:g} 吨超出常理（>{self.sanity_max_ton:g}），疑似读取错误，转人工",
            )
        if card.volume is not None and self.sanity_max_m3 and card.volume > self.sanity_max_m3:
            return Verdict.reject(
                self.STAGE_SANITY,
                f"方数 {card.volume:g} 方超出常理（>{self.sanity_max_m3:g}），疑似读取错误，转人工",
            )
        if card.tonnage is not None:
            limit = self.max_load_ton if remaining_ton is None else remaining_ton
            if limit and card.tonnage > limit:
                return Verdict.reject(
                    self.STAGE_CAPACITY, f"需 {card.tonnage} 吨 > 余量 {limit} 吨"
                )
        if card.volume is not None:
            limit_m3 = self.max_volume_m3 if remaining_m3 is None else remaining_m3
            by_len = self.volume_limit_for(card.che_length)
            if by_len:
                # 分档只会**收窄**上限：车就那么大，订单车长再长也变不出容积
                limit_m3 = min(limit_m3, by_len) if limit_m3 else by_len
            if limit_m3 and card.volume > limit_m3:
                return Verdict.reject(
                    self.STAGE_CAPACITY, f"需 {card.volume} 方 > 余量 {limit_m3} 方"
                )
        return Verdict.ok(self.STAGE_CAPACITY)

    def check_price(self, detail: OrderDetail) -> Verdict:
        """单价判定：价格 / 公里数 >= 下限才要。

        算不出单价（缺价格或距离）时**不放行** —— 宁可人工看一眼，
        也不能把「不知道多少钱」的单抢下来。

        上限（max_unit_price / price_sanity_max）同样是**转人工**，不是「嫌贵」：
        2026-09-06 实测详情页把价格读成 42168 元 / 193km = 218 元/km，
        单价下限判定直接放行进了「将抢单」。真实货源到不了这个量级，
        超过上限一律判异常转人工，绝不盲抢。
        """
        unit = detail.unit_price()
        if unit is None:
            return Verdict.reject(
                self.STAGE_PRICE,
                f"算不出单价（价格={detail.price} 距离={detail.distance_km}），转人工",
            )
        if self.price_sanity_max and detail.price is not None and detail.price > self.price_sanity_max:
            return Verdict.reject(
                self.STAGE_PRICE,
                f"价格 {detail.price:g} 元 > 异常阈值 {self.price_sanity_max:g}，疑似读取错误，转人工",
            )
        if self.max_unit_price and unit > self.max_unit_price:
            return Verdict.reject(
                self.STAGE_PRICE,
                f"单价 {unit:.2f} > 异常上限 {self.max_unit_price:g}"
                f"（价格={detail.price} 距离={detail.distance_km}），转人工",
            )
        if unit < self.min_unit_price:
            too_low = not self.pass_when_below
            if too_low:
                return Verdict.reject(self.STAGE_PRICE, f"单价 {unit:.2f} < 下限 {self.min_unit_price}")
        return Verdict.ok(self.STAGE_PRICE, f"单价 {unit:.2f} 元/公里")

    # ---------------------------------------------------------------- 组合

    def screen_card(
        self,
        card: OrderCard,
        remaining_ton: Optional[float] = None,
        remaining_m3: Optional[float] = None,
        hit_templates: Sequence[str] = (),
    ) -> Verdict:
        """列表页阶段能做的全部判定。通过则进详情。"""
        v = self.check_text(card.text_blob())
        if not v:
            return v
        v = self.check_image(hit_templates)
        if not v:
            return v
        v = self.check_vehicle(card)
        if not v:
            return v
        return self.check_capacity(card, remaining_ton, remaining_m3)

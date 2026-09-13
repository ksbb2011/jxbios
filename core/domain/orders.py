"""订单数据模型：列表卡片 / 详情字段。

这里的单位是全工程唯一约定，别在别处再定义一遍：
    吨 = ton，方 = m3，距离 = km，价格 = 元，单价 = 元/公里。
字段取不到一律用 None（不是 0）—— 0 会被当成「货重 0 吨」而误判。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class OrderCard:
    """列表页扫到的一张订单卡片。"""

    index: int
    rect: Tuple[int, int, int, int]  # (x, y, w, h) fast 帧像素
    anchor_score: float = 0.0
    che_length: Optional[str] = None  # 如 "4.2"
    che_type: Optional[str] = None  # 如 "高栏"
    tonnage: Optional[float] = None
    volume: Optional[float] = None
    # 列表卡片上的价格（元）：详情页左下角读不到价格时的**回退来源**
    # （2026-09-07 用户反馈：部分订单详情页无价格，但列表卡片上有）。
    # 卡片上写「电议/面议」时为 None（无数字可提），此时仍走「算不出单价转人工」。
    price: Optional[float] = None
    texts: List[str] = field(default_factory=list)
    # 参数行原文（去空格，如「4.2米高栏/厢式5.5-6吨」）：车型/车长正向查询用。
    # 不再依赖 findall 抠词——OCR 把「高栏」拆成「高 栏」也不漏；噪声词不影响
    # （匹配时只查规则配置的车型词是否在原文里出现）。
    param_text: str = ""
    has_dianyi: bool = False  # 电议标签（图色命中）
    # 整车标签（列表卡片右侧橙底「整车」/「一口价·整车」）：OCR 命中卡片文本「整车」二字。
    # 整车单不可拼单——GUI 详情区显示并提示，但拼单按钮仍可点（点了仅继续不扣减）。
    has_whole_vehicle: bool = False
    click_point: Optional[Tuple[int, int]] = None
    fingerprint: str = ""
    # 参数指纹（车长|车型|吨位|方数）：只用于「软重复」降权与同落点兜底识别，
    # 不做硬去重——同参数的不同货源很常见（见 ListScanner.is_soft_dup）
    spec_key: str = ""
    # 各字段被 OCR 到时的位置（fast 帧坐标），用于按「车型参数行」定位点击点。
    # 存它是为了不盲点：取不到车型/车长参数行就跳过该卡，绝不按固定偏移猜一个。
    field_points: Dict[str, Tuple[int, int]] = field(default_factory=dict)

    @property
    def center_y(self) -> int:
        return self.rect[1] + self.rect[3] // 2

    def text_blob(self) -> str:
        return " ".join(self.texts)

    def describe(self) -> str:
        parts = [f"#{self.index}"]
        if self.che_length:
            parts.append(f"{self.che_length}米")
        if self.che_type:
            parts.append(self.che_type)
        if self.tonnage is not None:
            parts.append(f"{self.tonnage}吨")
        if self.volume is not None:
            parts.append(f"{self.volume}方")
        return " ".join(parts)


@dataclass
class OrderDetail:
    """详情页读出的关键字段。"""

    price: Optional[float] = None  # 元
    distance_km: Optional[float] = None  # 装货点→目的地
    weight_ton: Optional[float] = None
    volume_m3: Optional[float] = None
    # 详情页也能读到的车型参数（比列表页更稳，列表页常缺失）。
    # 多值串如 "4.2/5"，由 RuleEngine 按 / 拆开做成员匹配。
    che_len: Optional[str] = None
    che_type: Optional[str] = None
    # 货物内容（货物/货源/货品 行OCR提取，最佳努力；取不到为 None）
    cargo: Optional[str] = None
    texts: List[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """稳定指纹：同一单不同帧应一致，用于「暂停在详情页」时识别是否已处理过。"""
        return "|".join(
            str(x) for x in (
                self.price, self.distance_km, self.weight_ton,
                self.che_len, self.che_type,
            )
        )

    def unit_price(self) -> Optional[float]:
        """单价（元/公里）。距离或价格缺失/为 0 时返回 None，由调用方判人工。

        统一保留两位小数：在源头取整，日志、判定、入库三处取值完全一致，避免
        「日志显示 2.46 但库里存 2.4671052631578947」这类不一致（2026-09-07 要求）。
        两位小数对元/公里量级足够，不影响下限/上限判定。
        """
        if self.price is None or not self.distance_km:
            return None
        return round(self.price / self.distance_km, 2)


@dataclass(frozen=True)
class Verdict:
    """一次筛选判定结果。pass_=True 才继续往下走。"""

    pass_: bool
    reason: str
    stage: str

    @classmethod
    def ok(cls, stage: str, reason: str = "") -> "Verdict":
        return cls(True, reason, stage)

    @classmethod
    def reject(cls, stage: str, reason: str) -> "Verdict":
        return cls(False, reason, stage)

    def __bool__(self) -> bool:
        return self.pass_

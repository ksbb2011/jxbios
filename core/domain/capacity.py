"""拼单载重余量：吨与方分别记账，任一归零即提示结单换车。

规则（config/rules.json 的 pindan 段）：
    * remaining_* 为 null 表示「还没接过单」，按车辆满载算起；
    * 抢下一单前先问 can_take()，扣减只在真正抢到之后才 commit()；
      —— 抢单可能失败，预扣了不回滚就会越扣越少，这是旧工程踩过的坑；
    * 重量未知（OCR 没抽到）时不扣减，但要记一笔 unknown，交给人工补录。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.config_store import ConfigStore


@dataclass
class TakenOrder:
    """已成交的一单（用于回看与回滚）。"""

    label: str
    ton: Optional[float] = None
    m3: Optional[float] = None

    def as_dict(self) -> Dict[str, object]:
        return {"label": self.label, "ton": self.ton, "m3": self.m3}


@dataclass
class Capacity:
    """车辆载重账本。"""

    max_ton: float
    max_m3: float
    remaining_ton: Optional[float] = None
    remaining_m3: Optional[float] = None
    # 拼单开关（来自 rules.pindan.enabled）。关闭时账本不启用：余量恒等于整车满载，
    # 既不收窄上限也不扣减；开着时才按已接货量累积、超余量拒单。
    # 见 from_store / commit —— 此前这个开关是死 flag（谁都不读），点了等于没点。
    enabled: bool = True
    taken: List[TakenOrder] = field(default_factory=list)
    # 余量变更时的落盘回调（由 RobotTask 注入）。
    # 为什么要持久化：余量只在内存时，进程一重启就归零，
    # 正式跑起来会重复抢超重订单——这是会亏钱的功能性缺陷。
    on_change: Optional[Callable[["Capacity"], None]] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.on_change is not None and not callable(self.on_change):
            raise TypeError("Capacity.on_change 必须是可调用对象或 None")

    # ---------------------------------------------------------------- 构造

    @classmethod
    def from_store(cls, store: Optional[ConfigStore] = None) -> "Capacity":
        cfg = store or ConfigStore()
        rules = cfg.section("rules")
        vehicle = rules.get("vehicle") or {}
        pindan = rules.get("pindan") or {}
        enabled = bool(pindan.get("enabled", True))
        # 拼单关闭：账本不启用，余量恒等于整车满载——既不收窄上限也不扣减。
        # （运满满里拼单=在同一辆车上累加多张小单；关掉就只按「整车能否装下」判，
        # 每张单都对着空车满载衡量，不累积已接货量。）
        rem_ton = _opt_float(pindan.get("remaining_ton")) if enabled else None
        rem_m3 = _opt_float(pindan.get("remaining_m3")) if enabled else None
        return cls(
            max_ton=float(vehicle.get("max_load_ton", 0) or 0),
            max_m3=float(vehicle.get("max_volume_m3", 0) or 0),
            remaining_ton=rem_ton,
            remaining_m3=rem_m3,
            enabled=enabled,
        )

    # ---------------------------------------------------------------- 查询

    @property
    def ton_left(self) -> float:
        return self.max_ton if self.remaining_ton is None else self.remaining_ton

    @property
    def m3_left(self) -> float:
        return self.max_m3 if self.remaining_m3 is None else self.remaining_m3

    def can_take(self, ton: Optional[float] = None, m3: Optional[float] = None) -> bool:
        """还装得下吗？未知重量按 0 计（不阻塞，由人工补录后校准）。"""
        if ton is not None and self.max_ton and ton > self.ton_left:
            return False
        if m3 is not None and self.max_m3 and m3 > self.m3_left:
            return False
        return True

    def exhausted(self, floor: float = 0.0) -> bool:
        """余量是否已到临界（<= floor 即视为装满，该结单换车）。"""
        if self.max_ton and self.ton_left <= floor:
            return True
        if self.max_m3 and self.m3_left <= floor:
            return True
        return False

    # ---------------------------------------------------------------- 记账

    def commit(self, label: str, ton: Optional[float] = None,
               m3: Optional[float] = None) -> TakenOrder:
        """抢单/拼单成功后扣减。返回记账条目（可用于 rollback）。

        拼单未启用（enabled=False）：不记账、不扣减、不落盘——容量按整车满载衡量。

        吨/方联动（拼单核心）：两维不能各扣各的——只扣吨、方停在整车上限，
        后续扫描的方条件就不会收窄，等于「没拼」。补盲规则：
            * 两维都已知 → 各自独立扣减（物理上两约束独立，扫描同时卡两维）；
            * 只知吨、方未知 → 方按「扣减后载重余量占比」缩放：
              remaining_m3 = max_m3 * (remaining_ton / max_ton)；
            * 只知方、吨未知 → 对称地  remaining_ton = max_ton * (remaining_m3 / max_m3)。
        max_ton/max_m3 任一为 0 时不缩放（避免除零）。扣减后 round + 落盘。
        """
        if not self.enabled:
            return TakenOrder(label=label, ton=ton, m3=m3)
        ton_known = ton is not None
        m3_known = m3 is not None
        new_ton = self.remaining_ton
        new_m3 = self.remaining_m3
        if ton_known and self.max_ton:
            new_ton = round(self.ton_left - ton, 3)
        if m3_known and self.max_m3:
            new_m3 = round(self.m3_left - m3, 3)
        # 联动补盲：只知一维时，另一维按已用载重/方比例缩放收窄上限
        if self.max_ton and self.max_m3:
            if ton_known and not m3_known and new_ton is not None:
                new_m3 = round(self.max_m3 * (new_ton / self.max_ton), 3)
            elif m3_known and not ton_known and new_m3 is not None:
                new_ton = round(self.max_ton * (new_m3 / self.max_m3), 3)
        self.remaining_ton = new_ton
        self.remaining_m3 = new_m3
        order = TakenOrder(label=label, ton=ton, m3=m3)
        self.taken.append(order)
        self._persist()
        return order

    def rollback(self, order: TakenOrder) -> None:
        """抢单没成 → 把预扣的加回去。"""
        if order in self.taken:
            self.taken.remove(order)
        if order.ton is not None and self.max_ton:
            self.remaining_ton = round(self.ton_left + order.ton, 3)
        if order.m3 is not None and self.max_m3:
            self.remaining_m3 = round(self.m3_left + order.m3, 3)
        self._persist()

    def reset(self) -> None:
        """换车：清空余量与已成交记录。"""
        self.remaining_ton = None
        self.remaining_m3 = None
        self.taken.clear()
        self._persist()

    # ---------------------------------------------------------------- 持久化

    def _persist(self) -> None:
        """余量变更时落盘。写失败只记日志，绝不抛异常阻塞抢单流程。"""
        if self.on_change is None:
            return
        try:
            self.on_change(self)
        except Exception:
            pass

    def as_json(self) -> str:
        data: Dict[str, Any] = {
            "max_ton": self.max_ton,
            "max_m3": self.max_m3,
            **self.as_dict(),
            "_note": "拼单余量持久化（按 config/calib/<取帧源>__<机型>/ 隔离）。"
                     "手动改这里等同于修改余量，改完重启即可生效。",
        }
        return json.dumps(data, ensure_ascii=False, indent=2)

    def save(self, path: str) -> bool:
        """写到指定路径。返回是否成功（失败不抛异常）。"""
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(self.as_json() + "\n", encoding="utf-8")
            return True
        except OSError:
            return False

    @classmethod
    def load(cls, path: str, fallback: Optional["Capacity"] = None) -> "Capacity":
        """从文件恢复账本。

        三种情况都返回可用对象，绝不因文件问题中断启动：
            ① 文件不存在 → 返回 fallback（通常是按配置新建的空账本）
            ② 文件损坏    → 同上
            ③ 车辆参数变了（换车）→ 旧余量作废，返回 fallback
        """
        base = fallback if fallback is not None else cls(max_ton=0.0, max_m3=0.0)
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return base
        if not isinstance(data, dict):
            return base

        max_ton = _opt_float(data.get("max_ton")) or base.max_ton
        max_m3 = _opt_float(data.get("max_m3")) or base.max_m3
        if max_ton != base.max_ton or max_m3 != base.max_m3:
            # 车辆参数变了 = 换车了，旧余量必须作废，否则会按旧车的余量继续抢
            return base

        cap = cls(
            max_ton=max_ton,
            max_m3=max_m3,
            remaining_ton=_opt_float(data.get("remaining_ton")),
            remaining_m3=_opt_float(data.get("remaining_m3")),
        )
        for item in data.get("taken") or []:
            if not isinstance(item, dict):
                continue
            cap.taken.append(
                TakenOrder(
                    label=str(item.get("label", "")),
                    ton=_opt_float(item.get("ton")),
                    m3=_opt_float(item.get("m3")),
                )
            )
        # 文件只存余量，不存开关；开关以配置（fallback=from_store 建的那份）为准，
        # 否则关掉拼单后只要磁盘上有旧 capacity.json 就会把开关覆盖回「开」。
        if fallback is not None:
            cap.enabled = fallback.enabled
        return cap

    # ---------------------------------------------------------------- 输出

    def describe(self) -> str:
        state = "拼单开" if self.enabled else "拼单关"
        return f"{state} 余量 {self.ton_left:.2f}/{self.max_ton:.2f} 吨，{self.m3_left:.2f}/{self.max_m3:.2f} 方，已接 {len(self.taken)} 单"

    def as_dict(self) -> Dict[str, object]:
        return {
            "remaining_ton": self.remaining_ton,
            "remaining_m3": self.remaining_m3,
            "taken": [o.as_dict() for o in self.taken],
        }


def _opt_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

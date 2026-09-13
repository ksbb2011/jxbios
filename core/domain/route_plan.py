"""路线解析与选择计划：多值路线文本 → 可直接驱动城市面板的选择步骤。

支持用户直接粘贴的四形态（config/routes.json 的 sample_input）：
    A路线：苏州-广州                      单对单
    B路线：苏州-广州、中山、揭阳           一发多到
    C路线：苏州、无锡、扬州-广州           多发一到
    D路线：苏州、无锡、扬州-广州、中山、揭阳 多发多到

三条从旧工程继承的硬规则：
    1. 同省必须连着选 —— 城市面板里省份只能进一次，隔省回头选会清掉已选；
    2. 选择路径补全到 [省, 市] —— 只给市名 App 找不到条目；
    3. 每侧最多 3 个 selection —— App 限制，超出的要显式报错而不是静默丢弃。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from core.config_store import ConfigStore
from core.domain.regions import RegionStore

# 「A路线：」/「路线A：」/「A：」三种前缀都认，冒号中英文皆可
_NAME_PREFIX = re.compile(
    r"^\s*(?:([A-Za-z0-9]{1,4})\s*路线|路线\s*([A-Za-z0-9]{1,4})|([A-Za-z0-9]{1,4}))\s*[：:]\s*(.+)$",
    re.S,
)
# 出发地/目的地分隔：横杠、中文破折号、箭头、=>、中文「到/至」、波浪号都认
# （手写横杠个数常不一致；中文路线习惯写「苏州到广州」「苏州至广州」）
_SIDE_SPLIT = re.compile(r"\s*(?:-{1,}|—{1,}|→|=>|到|至|~)\s*")
# 多值分隔：顿号 / 逗号 / 斜杠 / 竖线 / 空白
_CITY_SPLIT = re.compile(r"[、,，/|\s]+")

DEFAULT_NAMES = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class RoutePlanError(ValueError):
    """路线文本解析失败（行号会带上，便于 GUI 直接提示用户改哪一行）。"""


def split_cities(text: str) -> List[str]:
    """「广州、中山、揭阳」→ ['广州', '中山', '揭阳']。"""
    return [x for x in _CITY_SPLIT.split(text.strip()) if x]


def group_by_province(paths: Sequence[Sequence[str]]) -> List[List[str]]:
    """同省归拢连选：保持省内相对顺序与各省首次出现顺序。

    App 城市面板选中一个新省时，会把上一个省已选的市清掉，所以
    [江苏/苏州, 广东/广州, 江苏/无锡] 这种顺序选完会只剩广州 + 无锡。
    归拢后变成 [江苏/苏州, 江苏/无锡, 广东/广州]，一次选完不丢。
    """
    groups: Dict[str, List[List[str]]] = {}
    for path in paths:
        if not path:
            continue
        groups.setdefault(str(path[0]), []).append([str(x) for x in path])
    out: List[List[str]] = []
    for prov in groups:
        out.extend(groups[prov])
    return out


def common_prefix_len(a: Sequence[str], b: Sequence[str]) -> int:
    """两条选择路径的公共前缀长度（决定选下一条时要不要重选上几级）。"""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


@dataclass(frozen=True)
class Route:
    """一条路线。origin/dest 均为「选择路径」列表，每条路径已补全到 [省, 市...]。"""

    name: str
    origin: Tuple[Tuple[str, ...], ...]
    dest: Tuple[Tuple[str, ...], ...]

    def side(self, which: str) -> Tuple[Tuple[str, ...], ...]:
        return self.origin if which == "origin" else self.dest

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "origin": [list(p) for p in self.origin],
            "dest": [list(p) for p in self.dest],
        }

    def describe(self) -> str:
        def side(paths: Tuple[Tuple[str, ...], ...]) -> str:
            return "、".join(p[-1] for p in paths) or "(空)"

        return f"{self.name}路线：{side(self.origin)}-{side(self.dest)}"


@dataclass
class SelectionStep:
    """城市面板的一次选择动作。

    skip_prefix 是旧工程的关键经验：与上一条同省时不能重选省（会清掉已选的市），
    同省市只选区。它由两条路径的公共前缀长度直接得出。
    """

    path: Tuple[str, ...]
    skip_prefix: int

    @property
    def levels(self) -> Tuple[str, ...]:
        """本次要逐级点击的条目（已跳过与上一条重合的层级）。"""
        skip = max(0, min(self.skip_prefix, len(self.path) - 1))
        return self.path[skip:]

    @property
    def target(self) -> str:
        """本次最终选中的条目（最末一级），用于核对「已选地区」。"""
        return self.path[-1] if self.path else ""


class RoutePlan:
    """路线集合 + 当前进度。可序列化回 config/routes.json。"""

    def __init__(
        self,
        routes: Sequence[Route],
        max_stops_per_side: int = 3,
        current_index: int = 0,
        active: bool = False,
    ) -> None:
        self.routes: List[Route] = list(routes)
        self.max_stops_per_side = int(max_stops_per_side)
        self.current_index = int(current_index)
        self.active = bool(active)

    # ---------------------------------------------------------------- 进度

    @property
    def current(self) -> Optional[Route]:
        if 0 <= self.current_index < len(self.routes):
            return self.routes[self.current_index]
        return None

    def advance(self) -> Optional[Route]:
        """切到下一条路线，返回切换后的当前路线；已到最后一条返回 None。"""
        if self.current_index + 1 >= len(self.routes):
            return None
        self.current_index += 1
        return self.current

    def reset(self) -> None:
        self.current_index = 0

    # ---------------------------------------------------------------- 序列化

    def as_dict(self) -> Dict[str, object]:
        return {
            "routes": [r.as_dict() for r in self.routes],
            "max_stops_per_side": self.max_stops_per_side,
            "current_index": self.current_index,
            "active": self.active,
            "last_parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "RoutePlan":
        routes = []
        for item in (data.get("routes") or []):
            if not isinstance(item, dict):
                continue
            routes.append(
                Route(
                    name=str(item.get("name", "")),
                    origin=tuple(tuple(str(x) for x in p) for p in item.get("origin") or []),
                    dest=tuple(tuple(str(x) for x in p) for p in item.get("dest") or []),
                )
            )
        return cls(
            routes=routes,
            max_stops_per_side=int(data.get("max_stops_per_side", 3) or 3),
            current_index=int(data.get("current_index", 0) or 0),
            active=bool(data.get("active", False)),
        )

    # ---------------------------------------------------------------- 输出

    def describe(self) -> str:
        if not self.routes:
            return "(未配置路线)"
        lines = [f"共 {len(self.routes)} 条，当前第 {self.current_index + 1} 条："]
        for i, r in enumerate(self.routes):
            mark = "->" if i == self.current_index else "  "
            lines.append(f"  {mark} {r.describe()}")
        return "\n".join(lines)


def parse_route_line(line: str, regions: RegionStore, name: str = "") -> Route:
    """解析单行路线文本 → Route（路径已补全 + 同省归拢）。"""
    body = line
    label = name
    m = _NAME_PREFIX.match(line)
    if m:
        label = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        body = m.group(4)

    parts = _SIDE_SPLIT.split(body.strip(), maxsplit=1)
    if len(parts) != 2:
        raise RoutePlanError(f"缺少「出发地-目的地」分隔：{line.strip()}")
    raw_origin, raw_dest = split_cities(parts[0]), split_cities(parts[1])
    if not raw_origin or not raw_dest:
        raise RoutePlanError(f"出发地或目的地为空：{line.strip()}")

    origin = group_by_province([regions.resolve_path([c]) for c in raw_origin])
    dest = group_by_province([regions.resolve_path([c]) for c in raw_dest])
    return Route(
        name=label or name,
        origin=tuple(tuple(p) for p in origin),
        dest=tuple(tuple(p) for p in dest),
    )


def parse_routes_text(
    text: str,
    regions: RegionStore,
    max_stops_per_side: int = 3,
) -> RoutePlan:
    """解析多行路线文本（GUI 粘贴框的原样内容）。# 开头为注释，空行跳过。"""
    routes: List[Route] = []
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            route = parse_route_line(line, regions, name=DEFAULT_NAMES[len(routes): len(routes) + 1])
        except RoutePlanError as exc:
            raise RoutePlanError(f"第 {lineno} 行：{exc}") from exc
        if not route.name:
            route = Route(DEFAULT_NAMES[len(routes): len(routes) + 1], route.origin, route.dest)
        for side_name, side in (("出发地", route.origin), ("目的地", route.dest)):
            if len(side) > max_stops_per_side:
                raise RoutePlanError(
                    f"第 {lineno} 行：{side_name} 有 {len(side)} 个，"
                    f"App 每侧最多选 {max_stops_per_side} 个"
                )
        routes.append(route)
    if not routes:
        raise RoutePlanError("没有解析到任何路线")
    return RoutePlan(routes, max_stops_per_side=max_stops_per_side, active=True)


def selection_steps(paths: Sequence[Sequence[str]]) -> List[SelectionStep]:
    """把一侧的若干选择路径展开成城市面板的动作序列（带 skip_prefix）。"""
    steps: List[SelectionStep] = []
    prev: Tuple[str, ...] = ()
    for path in paths:
        if not path:
            continue
        skip = common_prefix_len(prev, path) if steps else 0
        steps.append(SelectionStep(path=tuple(str(x) for x in path), skip_prefix=skip))
        prev = tuple(str(x) for x in path)
    return steps


def validate_plan(plan: RoutePlan, regions: RegionStore) -> List[str]:
    """解析成功后的软校验：返回「字典里查不到」的告警（不阻断，供 GUI 提示）。"""
    warns: List[str] = []
    for route in plan.routes:
        for side_name, side in (("出发地", route.origin), ("目的地", route.dest)):
            for path in side:
                if path and not regions.is_known(str(path[0])):
                    warns.append(
                        f"{route.name}路线 {side_name}「{path[0]}」不在行政区字典，"
                        f"App 里可能找不到（请在 config/regions.json 补）"
                    )
    return warns


def load_plan(store: Optional[ConfigStore] = None) -> RoutePlan:
    """从 config/routes.json 读取当前路线（主程序启动时用）。"""
    cfg_store = store or ConfigStore()
    data = dict(cfg_store.section("routes"))
    data.setdefault("max_stops_per_side", 3)
    return RoutePlan.from_dict(data)

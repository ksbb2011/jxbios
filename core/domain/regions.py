"""行政区字典：省份补全、同省分组、OCR 容错。

数据全部来自 config/regions.json（旧工程真机验证过的字典），本模块只提供查询与
补全逻辑，不内置任何一个城市名。

三条实战经验写死在逻辑里，别改：
    1. 城市面板「省份只能进一次」：隔省回头选会把已选项清掉，所以同省必须连着选；
    2. 选择路径要补全到 [省, 市]，只写市名 App 里找不到对应条目；
    3. OCR 错字容错只能用于「包含判断」（如核对已选地区），
       绝不能用于点击目标匹配 —— 用容错文本去点会点错条目。
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from core.config_store import ConfigStore


class RegionStore:
    """行政区字典查询。构造时一次性建索引，之后查询是 O(1)。"""

    def __init__(self, store: Optional[ConfigStore] = None) -> None:
        self._store = store or ConfigStore()
        raw = self._store.section("regions")
        self._provinces: Dict[str, Tuple[str, ...]] = {}
        for prov, cities in (raw.get("provinces") or {}).items():
            self._provinces[str(prov)] = tuple(str(c) for c in cities)
        self._city_to_province: Dict[str, str] = {}
        for prov, cities in self._provinces.items():
            for city in cities:
                # 直辖市「北京: [北京]」自身既是省又是市，省份映射优先给省
                self._city_to_province.setdefault(city, prov)
        typo_raw = raw.get("ocr_typo") or {}
        self._typo: Dict[str, Tuple[str, ...]] = {}
        for name, variants in typo_raw.items():
            vals = [str(v) for v in variants] or [str(name)]
            if str(name) not in vals:
                vals.insert(0, str(name))
            self._typo[str(name)] = tuple(vals)
        # 全部地区名（省+市）惰性缓存：供 contains_region_name 做子串匹配，
        # 首次访问时构建一次（约 300+ 项），低频调用，开销可忽略。
        self._region_names: Optional[FrozenSet[str]] = None

    # ---------------------------------------------------------------- 基础查询

    @property
    def provinces(self) -> Tuple[str, ...]:
        return tuple(self._provinces.keys())

    def cities(self, province: str) -> Tuple[str, ...]:
        return self._provinces.get(province, ())

    def province_of(self, name: str) -> Optional[str]:
        """市名 → 省名；本身是省名则返回自身；未知返回 None。"""
        if name in self._provinces:
            return name
        return self._city_to_province.get(name)

    def is_province(self, name: str) -> bool:
        return name in self._provinces

    def is_known(self, name: str) -> bool:
        return self.province_of(name) is not None

    def province_names(self) -> FrozenSet[str]:
        """城市面板 OCR 判定用的省份名集合（判「是否已进入省份网格」）。"""
        return frozenset(self._provinces.keys())

    @property
    def region_names(self) -> FrozenSet[str]:
        """全部地区名（省名 + 市名）集合，供「文本里是否含地区名」的子串匹配。

        为什么要它：城市面板退出时要把筛选条上的**真红字**与面板里的**灰色 Tab**
        区分开——真红字必含地区名（OCR 常读成「南京...一南京..」这种带噪点形态），
        而 `is_known` 只做整体匹配、对带噪点文本判不过，所以需要"含任意地区名"的
        子串判据。构造时一次建好并缓存（约 300+ 项）。
        """
        if self._region_names is None:
            names = set(self._provinces.keys())
            for cities in self._provinces.values():
                names.update(cities)
            self._region_names = frozenset(n for n in names if n)
        return self._region_names

    def contains_region_name(self, text: str, min_len: int = 2) -> bool:
        """`text` 里是否含任意已知地区名（子串匹配，容忍 OCR 噪点/箭头拼接）。

        `min_len` 用于过滤过短名称（如单字别名），默认 2 字起，避免误命中。
        典型：`'南京...一南京..'` 含 `'南京'` → True；`'出发地'`（面板灰色 Tab）
        不含任何地区名 → False。
        """
        if not text:
            return False
        for name in self.region_names:
            if len(name) >= min_len and name in text:
                return True
        return False

    # ---------------------------------------------------------------- 路径补全

    def resolve_path(self, selection: Sequence[str]) -> List[str]:
        """把选择项补全成完整路径 [省, 市, 区...]。

        已以省开头 → 原样返回；只写市名 → 补上省；完全未知 → 原样返回并交给
        调用方报错（宁可显式失败，也不要猜一个省出来点错条目）。
        """
        if not selection:
            return []
        head = str(selection[0]).strip()
        if head in self._provinces:
            return [str(s).strip() for s in selection if str(s).strip()]
        prov = self._city_to_province.get(head)
        if prov:
            return [prov] + [str(s).strip() for s in selection if str(s).strip()]
        return [str(s).strip() for s in selection if str(s).strip()]

    # ---------------------------------------------------------------- OCR 容错

    def typo_variants(self, name: str) -> Tuple[str, ...]:
        return self._typo.get(name, (name,))

    def text_contains(self, needle: str, haystack: str) -> bool:
        """带 OCR 错字容错的包含判断（仅用于核对，不用于点击匹配）。"""
        if not needle:
            return True
        if not haystack:
            return False
        if needle in haystack:
            return True
        return any(v in haystack for v in self.typo_variants(needle))

    def detect_provinces(self, texts: Iterable[str]) -> List[str]:
        """从 OCR 文本里挑出命中的省份名（判断是否已进入省份网格）。"""
        names = self.province_names()
        found = []
        for text in texts:
            for prov in names:
                if prov in text and prov not in found:
                    found.append(prov)
        return found

"""配置访问层：加载、热重载与依赖注入入口（imouse 版）。

背景与定位：
    本项目（iMouse 投屏版）起初只有 hardware.json + templates.json 两个配置，
    ConfigStore 是极简实现。搬来 flow/ 与 domain/ 之后，业务层需要完整的多
    section 配置（vision / thresholds / rules / regions / routes / fields /
    runtime）以及 DATA_DIR 等模块级常量，因此这里升级为
    「多 section 加载 + 宽松校验 + 兼容旧极简 API」。

    与保底版 robot_order_picker_new 的差异（有意为之，不是疏漏）：
      * 校验是**宽松**的：缺 section / 缺模板文件只记录到 errors()，
        不阻止启动。投屏版模板要边采边补，启动即崩会让采集工作无法进行；
      * hardware.calibration 里的**视觉侧参数**（screen_roi / x_scale /
        y_scale / top_y_adjust / z_compensation）在投屏版整体作废，坐标映射走
        calibration.actuation（双线性模型），故不再校验这些字段；
      * 保留 imouse 项目自有的 logical_size / template_dir(property) / root，
        以免破坏 matcher / page_state / arm / action 的既有用法。

纪律（本工程最重要的一条）：
    任何模块都不得硬编码路径、阈值、分辨率、OCR 参数或坐标偏移，
    一律通过 ConfigStore 获取。改配置即改行为，不动一行代码。

坐标体系：
    唯一对外坐标空间 = 手机逻辑点（iPhone X = 375x812）；
    模板 ROI 为归一化 [l, t, r, b]。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# iPhone X 逻辑点；换机型需调整（或由 device 注入）。iMouse 标定基于此分辨率。
_LOGICAL_SIZE = (375, 812)


def _project_root() -> Path:
    """定位项目根目录（config/ 与 data/ 的父目录）。

    三种环境：
      * 源码运行：core/config_store.py 的上上级目录；
      * PyInstaller 打包（sys.frozen）：exe 所在目录（资源与 exe 同级且可写）；
      * 环境变量 ROBOT_PICKER_HOME 显式指定（安装到别处 / 多实例）。
    """
    env = os.environ.get("ROBOT_PICKER_HOME", "").strip()
    if env:
        return Path(env).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = _project_root()
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
TEMPLATE_DIR = DATA_DIR / "templates"

SECTIONS: Tuple[str, ...] = (
    "hardware",
    "vision",
    "templates",
    "thresholds",
    "rules",
    "regions",
    "fields",
    "routes",
    "runtime",
)

# coords.json 是 imouse 项目自有的控件坐标表（由 core/action.py 独立读写），
# 不属于上面的业务 section，单独加载以免与业务配置耦合。
EXTRA_FILES: Tuple[str, ...] = ("coords",)

RESTART_REQUIRED_SECTIONS: Tuple[str, ...] = ("hardware",)
REBUILD_CAPTURE_KEYS: Tuple[str, ...] = (
    "vision.capture.capture_resolution",
    "vision.capture.source",
)


class ConfigError(Exception):
    """配置缺失或非法。"""


def _dig(data: Any, dotted: str, default: Any = None) -> Any:
    """按 a.b.c 路径取值，取不到返回 default。"""
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _as_scales(v: Any) -> Optional[Tuple[float, ...]]:
    if isinstance(v, (list, tuple)) and len(v) > 0:
        out = tuple(float(x) for x in v)
        return out if out else None
    return None


def _strip_template_dir(rel: str, base_dir: str) -> str:
    """模板 path 统一为「相对 template_dir」的形式。

    历史上 items[].path 混用两种写法：相对（"_new/x.png"）与带 template_dir
    前缀（"data/templates/_new/x.png"）。后者再与模板根目录拼接会变成
    data/templates/data/templates/... 双重路径，读图必然失败，故统一剥掉前缀，
    两种写法都能正常工作（不必去改已有的 templates.json）。
    """
    prefixes = [p for p in (base_dir, "data/templates", "data\\templates") if p]
    for p in prefixes:
        plow = p.rstrip("/\\").lower()
        for sep in ("/", "\\"):
            if rel.lower().startswith(plow + sep):
                return rel[len(plow) + 1:]
    return rel


@dataclass
class TemplateSpec:
    """单个模板的规格（与 templates.json 的 items[name] 对应）。

    path / alts 保持 str：imouse 项目的 matcher 直接把它们交给
    imgio.imread_unicode 与缓存 key，用 str 最省心（Path 亦可，但没必要）。
    """

    name: str
    path: str
    alts: Tuple[str, ...] = ()
    threshold: float = 0.88
    roi: Optional[Sequence[float]] = None
    scales: Optional[Sequence[float]] = None
    kind: str = "icon"
    match_mode: str = "gray"
    enabled: bool = True
    purpose: str = ""

    # ------------------------------------------------------------ 兼容源版 API
    def all_paths(self) -> Tuple[str, ...]:
        return (self.path,) + tuple(self.alts)

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def missing_alts(self) -> Tuple[str, ...]:
        return tuple(p for p in self.alts if not os.path.isfile(p))

    def resolve_roi(self, frame_w: int, frame_h: int) -> Tuple[int, int, int, int]:
        """归一化 ROI → 帧像素 ROI；未配置则返回全屏。"""
        if not self.roi:
            return (0, 0, frame_w, frame_h)
        left, top, right, bottom = self.roi
        return (
            int(round(left * frame_w)),
            int(round(top * frame_h)),
            int(round(right * frame_w)),
            int(round(bottom * frame_h)),
        )


class ConfigStore:
    """线程安全的配置容器（多 section + 宽松校验）。"""

    def __init__(self, root: Optional[str] = None, strict: bool = False) -> None:
        self.root = Path(root) if root else PROJECT_ROOT
        self._dir = self.root / "config"
        self._strict = strict
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {}
        self._mtime: Dict[str, float] = {}
        self._templates: Dict[str, TemplateSpec] = {}
        self._errors: List[str] = []
        self.load_all()

    # ------------------------------------------------------------------ 加载
    @staticmethod
    def _read_json(path: Path) -> Any:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"{path.name} 读取/解析失败: {exc}") from exc

    def load_all(self) -> None:
        with self._lock:
            self._data = {}
            self._mtime = {}
            self._errors = []
            for name in SECTIONS:
                path = self._dir / f"{name}.json"
                if not path.is_file():
                    # 宽松：缺文件只记录，不阻止启动（模板边采边补的场景需要）
                    self._errors.append(f"缺少配置文件: {path.name}")
                    continue
                obj = self._read_json(path)
                if not isinstance(obj, dict):
                    self._errors.append(f"{path.name} 顶层必须是 JSON 对象")
                    continue
                self._data[name] = obj
                self._mtime[name] = path.stat().st_mtime
            for name in EXTRA_FILES:
                path = self._dir / f"{name}.json"
                if path.is_file():
                    obj = self._read_json(path)
                    if isinstance(obj, dict):
                        self._data[name] = obj
                        self._mtime[name] = path.stat().st_mtime
            self._rebuild_templates()
            if self._strict:
                errs = self.validate()
                if errs:
                    raise ConfigError(
                        "配置校验未通过，已阻止启动：\n  - " + "\n  - ".join(errs)
                    )

    def _rebuild_templates(self) -> None:
        specs: Dict[str, TemplateSpec] = {}
        raw = self._data.get("templates", {}) or {}
        base_dir = str(raw.get("template_dir", "")).strip()
        root = (self.root / base_dir) if base_dir else (self.root / "data" / "templates")
        for name, item in (raw.get("items") or {}).items():
            if not isinstance(item, dict):
                self._errors.append(f"templates.items.{name} 必须是对象")
                continue
            rel = _strip_template_dir(str(item.get("path", "")).strip(), base_dir)
            if not rel:
                self._errors.append(f"templates.items.{name} 缺少 path")
                continue
            alts = tuple(
                str(root / _strip_template_dir(str(a).strip(), base_dir))
                for a in (item.get("alt") or [])
                if str(a).strip()
            )
            roi_raw = item.get("roi")
            roi: Optional[Tuple[float, float, float, float]] = None
            if isinstance(roi_raw, (list, tuple)) and len(roi_raw) == 4:
                roi = tuple(float(v) for v in roi_raw)  # type: ignore[arg-type]
            specs[name] = TemplateSpec(
                name=name,
                path=str(root / rel),
                alts=alts,
                threshold=self._resolve_threshold(item),
                roi=roi,
                scales=_as_scales(item.get("scales")),
                kind=str(item.get("kind", "icon")),
                match_mode=str(item.get("match_mode", "gray")).strip().lower(),
                enabled=bool(item.get("enabled", True)),
                purpose=str(item.get("purpose", "")),
            )
        self._templates = specs

    def _resolve_threshold(self, item: Dict[str, Any]) -> float:
        """阈值优先级：item.threshold → thresholds.exceptions[key].current → kind 默认。"""
        th = item.get("threshold")
        if _is_num(th):
            return float(th)
        key = item.get("threshold_key")
        if key:
            for exc in self._data.get("thresholds", {}).get("exceptions", []) or []:
                if exc.get("key") == key:
                    cur = exc.get("current")
                    if _is_num(cur):
                        return float(cur)
                    break
        kind = str(item.get("kind", "icon"))
        ths = self._data.get("thresholds", {}) or {}
        if kind == "text":
            return float(ths.get("text_label_default", 0.60))
        return float(ths.get("icon_default", 0.88))

    # ------------------------------------------------------------------ 读取
    def section(self, name: str) -> Dict[str, Any]:
        with self._lock:
            return self._data.get(name) or {}

    def get(self, section: str, key: str = "", default: Any = None) -> Any:
        """读取配置，key 支持 'a.b.c' 路径。"""
        with self._lock:
            data = self._data.get(section, {})
            if not key:
                return data
            val = _dig(data, key, default)
            return default if val is None else val

    def template(self, name: str) -> TemplateSpec:
        with self._lock:
            spec = self._templates.get(name)
            if spec is None:
                known = ", ".join(sorted(self._templates)) or "(空)"
                raise ConfigError(
                    f"模板未注册: {name}（请检查 config/templates.json；已注册: {known}）"
                )
            return spec

    def templates(self, *names: str) -> List[TemplateSpec]:
        return [self.template(n) for n in names]

    def close_candidates(self) -> List[TemplateSpec]:
        """未知页/弹窗的关闭按钮候选。

        优先用 templates.json 的 close_candidates 显式列表；未配置时回退为
        「名称含 close / dialog / offline / back 且已启用」的模板（宁可多点一次，
        也不能把弹窗判成正常页——见业务铁律）。
        """
        with self._lock:
            names = self._data.get("templates", {}).get("close_candidates", []) or []
            specs = [self._templates[n] for n in names if n in self._templates]
            if specs:
                return specs
            keys = ("close", "dialog", "offline", "back")
            return [
                s
                for s in self._templates.values()
                if s.enabled and any(k in s.name.lower() for k in keys)
            ]

    def threshold(self, key: str, default: float = 0.88) -> float:
        with self._lock:
            for exc in self._data.get("thresholds", {}).get("exceptions", []) or []:
                if exc.get("key") == key:
                    cur = exc.get("current")
                    return float(cur) if _is_num(cur) else default
            val = _dig(self._data.get("thresholds", {}), key, None)
            return float(val) if _is_num(val) else float(default)

    # ------------------------------------------------------------------ 热重载
    def reload_if_changed(self) -> List[str]:
        changed: List[str] = []
        with self._lock:
            all_files = tuple(SECTIONS) + tuple(EXTRA_FILES)
            for name in all_files:
                path = self._dir / f"{name}.json"
                if not path.is_file():
                    continue
                mtime = path.stat().st_mtime
                if self._mtime.get(name) != mtime:
                    old = self._mtime.get(name)
                    self._mtime[name] = mtime
                    if old is not None:
                        changed.append(name)
            if changed:
                backup = self._errors
                try:
                    for name in changed:
                        obj = self._read_json(self._dir / f"{name}.json")
                        if isinstance(obj, dict):
                            self._data[name] = obj
                    self._rebuild_templates()
                    self._errors = []
                    self._errors.extend(self._validate_structure())
                    if self._errors:
                        changed = [f"{c}(校验告警)" for c in changed]
                except ConfigError as exc:
                    self._errors = backup + [f"热重载失败: {exc}"]
                    return []
        return changed

    def needs_restart(self, changed: Sequence[str]) -> bool:
        return any(str(c).split("(")[0] in RESTART_REQUIRED_SECTIONS for c in changed)

    def needs_rebuild_capture(self, changed: Sequence[str]) -> bool:
        return any(str(c).startswith("vision") for c in changed)

    # ------------------------------------------------------------------ 校验
    def _validate_structure(self) -> List[str]:
        """宽松校验：只报数据性问题，不阻止启动。"""
        errs: List[str] = []

        # hardware：actuation（投屏版坐标映射的唯一来源）若存在必须是 4 系数
        act = self.get("hardware", "calibration.actuation", None)
        if isinstance(act, dict) and act:
            for axis in ("ax", "ay"):
                val = act.get(axis)
                if not (isinstance(val, list) and len(val) == 4 and all(_is_num(v) for v in val)):
                    errs.append(f"hardware.calibration.actuation.{axis} 必须是 4 个数字，当前 {val}")
        z_press = self.get("hardware", "calibration.z_press", None)
        if _is_num(z_press) and not 0 < float(z_press) <= 6.2:
            errs.append(
                f"hardware.calibration.z_press={z_press} 超出安全区间(0, 6.2]："
                f"Z 越大压得越深，超过 6.2 会点碎屏幕"
            )

        # vision：分辨率必须是正整数对
        cap = self.get("vision", "capture", {}) or {}
        for key in ("capture_resolution", "fast_size"):
            val = cap.get(key)
            if val is not None and not (
                isinstance(val, list) and len(val) == 2 and all(_is_num(v) and v > 0 for v in val)
            ):
                errs.append(f"vision.capture.{key} 必须是 [宽, 高]，当前 {val}")

        # templates：启用的模板主文件应存在（缺文件只告警，采集阶段很常见）
        for name, spec in self._templates.items():
            if spec.enabled and not spec.exists():
                errs.append(f"模板文件不存在: {name} -> {spec.path}")
            if spec.roi is not None:
                left, top, right, bottom = spec.roi
                if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
                    errs.append(f"templates.items.{name}.roi 非法: {spec.roi}")

        # thresholds：数值范围
        ths = self._data.get("thresholds", {}) or {}
        for key in ("icon_default", "text_label_default"):
            val = ths.get(key)
            if _is_num(val) and not 0.0 <= float(val) <= 1.0:
                errs.append(f"thresholds.{key} 必须在 0~1 之间，当前 {val}")
        for exc in ths.get("exceptions", []) or []:
            cur = exc.get("current")
            if _is_num(cur) and not 0.0 <= float(cur) <= 1.0:
                errs.append(f"thresholds.exceptions[{exc.get('key')}].current 非法: {cur}")

        # regions：省 → 市列表
        provinces = self._data.get("regions", {}).get("provinces") or {}
        if not isinstance(provinces, dict) or not provinces:
            errs.append("regions.provinces 缺失或不是对象")
        else:
            for prov, cities in provinces.items():
                if not isinstance(cities, list) or not all(isinstance(c, str) for c in cities):
                    errs.append(f"regions.provinces.{prov} 必须是字符串数组")

        return errs

    def validate(self) -> List[str]:
        with self._lock:
            merged = list(self._errors) + self._validate_structure()
            return list(dict.fromkeys(merged))

    def errors(self) -> List[str]:
        return list(self._errors)

    # ------------------------------------------------------------------ 写回
    def save_section(self, name: str, data: Dict[str, Any]) -> None:
        if name not in SECTIONS and name not in EXTRA_FILES:
            raise ConfigError(f"未知配置段: {name}")
        path = self._dir / f"{name}.json"
        text = json.dumps(data, ensure_ascii=False, indent=2)
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:  # pragma: no cover
            raise ConfigError(f"写回内容不是合法 JSON: {exc}") from exc
        path.write_text(text + "\n", encoding="utf-8")
        with self._lock:
            self._data[name] = data
            self._mtime[name] = path.stat().st_mtime
            if name == "templates":
                self._rebuild_templates()

    # ------------------------------------------------------------------ 辅助
    @property
    def logical_size(self) -> Tuple[int, int]:
        """手机逻辑点分辨率（唯一对外坐标空间）。"""
        return _LOGICAL_SIZE

    @property
    def template_dir(self) -> str:
        """模板根目录（保持 property，core/action.py 与工具脚本依赖此写法）。"""
        base = str((self._data.get("templates", {}) or {}).get("template_dir", "")).strip()
        return str(self.root / base) if base else str(self.root / "data" / "templates")

    def config_dir(self) -> Path:
        return self._dir

    def calib_dir(self) -> Path:
        """标定目录：config/calib/<取帧源>__<机型>/（不存在则创建）。

        按【取帧源 × 机型】二维隔离：标定值同时取决于画面来源与手机机型，
        任一变化旧标定即作废。分开存绝不串号——串号会导致点击整体偏移。
        """
        source = str(self.get("vision", "capture.source", "imouse") or "imouse").lower()
        model = str(self.get("hardware", "device.model", "unknown") or "unknown")
        slug = _slugify(f"{source}__{model}")
        path = self._dir / "calib" / slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    def describe(self) -> str:
        with self._lock:
            lines = [
                f"config_dir = {self._dir}",
                f"templates  = {len(self._templates)} 项",
                f"logical    = {self.logical_size}",
                f"source     = {self.get('vision', 'capture.source')}",
                f"fast_size  = {self.get('vision', 'capture.fast_size')}",
                f"ocr        = {self.get('vision', 'ocr.backend')}",
                f"icon_thr   = {self.get('thresholds', 'icon_default')}",
            ]
            if self._errors:
                lines.append("校验错误:")
                lines.extend(f"  - {e}" for e in self._errors)
            return "\n".join(lines)


def _slugify(text: str) -> str:
    """转目录名：'imouse__iPhone X' -> 'imouse__iphone_x'（保留下划线分隔符）。"""
    slug = re.sub(r"[^0-9a-zA-Z_]+", "_", text).strip("_").lower()
    return slug or "default"


_STORE: Optional[ConfigStore] = None


def get_store(reload: bool = False) -> ConfigStore:
    """进程内共享的配置实例（依赖注入的默认来源）。"""
    global _STORE
    if _STORE is None or reload:
        _STORE = ConfigStore()
    return _STORE

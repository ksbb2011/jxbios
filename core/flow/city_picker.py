"""城市选择器：把「选择路径列表」变成 App 里的一串点击。

真机流程（用户 2026-09-07 截图确认）是「有状态的层级钻探」：
    层级由顶部红字决定 ——
      「请选择出发地/目的地」= 省级，下方是省份网格；
      「XX > 请选择市」      = 市级，下方是市网格，同行右侧有红色『返回上一级』；
      「XX > XX > 请选择区」 = 区级，下方是区网格，同样有『返回上一级』。
    每点一层，界面就向下钻一层（点省→市列表、点市→区列表…），所以选完一条
    path 后界面停在「path 末节点的子列表」。切换下一条 path 时，必须按两 path 的
    最长公共前缀回退到对应层级再继续点：
      * 同省换市（浙江/杭州 → 浙江/宁波）：回 1 级（区/市列表 → 市列表）直接点宁波；
      * 跨省换省（浙江/杭州 → 江苏/南京）：回 2 级（区/市 → 省列表）点江苏再点南京。
    返回靠右侧红色『返回上一级』，省列表无此按钮（已是根，不会误点）。

其它铁律（沿用）：入口定位走『智能排序』锚点；清空筛选后要上滑一次把省拖全；
确认优先 OCR、兜底模板且命中坐标偏上 +confirm_dy；面板展开态点确认不退出要先点
顶部红字收起。
"""

from __future__ import annotations

import re
import threading
import time
from typing import List, Optional, Sequence

import numpy as np

from core.config_store import ConfigStore
from core.domain.regions import RegionStore
from core.domain.route_plan import SelectionStep
from core.flow.actions import ActionKit
from core.vision.page_state import ScreenState

# 城市面板网格搜索区（归一化）。关键：上界必须严格在「请选择出发地/目的地」红字
# 和「历史地区」行**下方**，否则 find_text 取「最靠上」会命中历史地区的同名省/市
# （2026-09-07 实测：省ROI上界0.26→点了历史地区行的江苏而非省份网格里的江苏）。
# 下界避开底部清空筛选+确认(≈0.85)。三级都全宽(x 0.02..0.98)。
# 若仍点偏请用 config fields.entries.grid_roi_province/city/district 覆盖（见 _grid_roi）。
GRID_ROI_PROVINCE = (0.02, 0.44, 0.98, 0.86)   # 省：跳过历史地区+双红字, y 从 0.44
GRID_ROI_CITY     = (0.02, 0.36, 0.98, 0.86)   # 市：「XX>请选择市」标题下方, y 从 0.36
GRID_ROI_DISTRICT = (0.02, 0.42, 0.98, 0.86)   # 区：「XX>XX>请选择区」标题下方, y 从 0.42
# 按「点该 level 时界面所处层级下标」取 ROI：idx0=省列表, idx1=市列表, idx2=区列表
GRID_ROIS = (GRID_ROI_PROVINCE, GRID_ROI_CITY, GRID_ROI_DISTRICT)

SIDE_LABEL = {"origin": "出发地", "dest": "目的地"}


def is_entry_text(regions, text: str) -> bool:
    """是否「出发地/目的地入口」上该有的文字。

    筛选条上两个入口显示的是**当前值**：可能是地区名（江苏/济源）、可能是「出发地/
    目的地」占位、也可能是默认的「当前定位」。而两入口之间的箭头「→」常被 OCR 认成
    '1'/'-' 之类，混进来会把目的地坐标算成箭头（2026-09-06 实测踩到），必须滤掉。
    """
    if not text:
        return False
    if "出发地" in text or "目的地" in text:
        return True
    if "当前" in text:  # 出发地默认值「当前定位」
        return True
    if any(p in text for p in regions.province_names()):
        return True
    # OCR 常在地区名后带噪点（「杭州..」「江苏·」「杭州...」），不清洗就判不过，
    # 候选不足 2 个 → 落到「整行聚类」兜底 → 而兜底若不过滤就会把箭头「→」
    # （常被认成 '1'/'-'）当成第二个入口，点开错误界面（2026-09-07：dest 连挂 2 次）。
    cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
    if cleaned and cleaned != text:
        try:
            if regions.is_known(cleaned):
                return True
        except Exception:
            pass
    try:
        return bool(regions.is_known(text))
    except Exception:
        return False


# 面板里的灰色 Tab 标题：点了只切编辑侧、**不关面板**（犯错记录 [0831-G]）。
# 它与筛选条上的真红字**字面可能相同**（都可能是「出发地」），所以必须靠"是否含
# 地区名"来区分，绝不能只凭"出发地"三个字就认。
_TAB_TITLES = ("出发地", "目的地")


def is_collapse_red_text(regions, text: str) -> bool:
    """是否是「点它可以收起城市面板」的顶部红字（比 is_entry_text 严）。

    与 `is_entry_text` 的区别（**不要合并**：入口定位那里必须放行占位文字）：
        * 真红字 = 筛选条上显示**当前值**的文字，必含地区名（「出发地·南京」→ 含南京），
          或「当前定位」；OCR 常带噪点，如 `'南京...一南京..'`；
        * 灰色 Tab = 面板里的「出发地/目的地」页签，只有这三个字，点了只切编辑侧、
          不关面板（[0831-G]）。

    2026-09-12 实测死循环就是这里判错：真红字 `'南京...一南京..'` 因 is_known 整体
    匹配判不过被滤掉，灰色 Tab `'出发地'` 因 `"出发地" in text` 被放行，候选只剩 Tab
    → 反复点 Tab、面板退不出 → city_panel_stuck 累到 4 触发监护停止。
    """
    if not text:
        return False
    stripped = text.strip()
    if stripped in _TAB_TITLES:  # 灰色 Tab 页签：绝不能作为收起按钮
        return False
    if "当前" in stripped:  # 「当前定位」也是筛选条上的真值（出发地默认值）
        return True
    try:
        if regions.contains_region_name(stripped):  # 含任意已知地区名（子串，容忍噪点）
            return True
    except Exception:
        pass
    cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", stripped)  # 去掉「..」「·」等噪点
    try:
        if cleaned and regions.is_known(cleaned):
            return True
    except Exception:
        pass
    return False


def confirm_button_clickable(store, fs) -> bool:
    """城市面板「确认」按钮是否正红可点（正红=可点，浅红=不可点，灰度模板区分不开）。

    实测（2026-09-10）：浅红确认按钮 RGB≈[249,193,202]，R 高但 G/B 也接近 200；
    正红 = R>180 且 G<120 且 B<120，此判据下浅红态占比 0.0。阈值共用
    `thresholds.city.confirm_clickable`。

    2026-09-12：从 `CityPicker._confirm_clickable` 提取为模块级，供 handlers 的城市
    面板兜底退出复用（避免两处各写一遍、口径分叉）。
    """
    if fs is None or getattr(fs, "raw", None) is None:
        return True  # 无帧时不拦，交给后续判态
    cfg = store.get("thresholds", "city.confirm_clickable", {}) or {}
    roi = cfg.get("roi", [0.5, 0.88, 1.0, 0.99])
    thr = float(cfg.get("red_ratio", 0.15) or 0.15)
    h, w = fs.raw.shape[:2]
    x0, y0 = max(0, int(roi[0] * w)), max(0, int(roi[1] * h))
    x1, y1 = min(w, int(roi[2] * w)), min(h, int(roi[3] * h))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return True
    patch = fs.raw[y0:y1, x0:x1].astype(np.int16)
    b, g, r = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
    ratio = float(((r > 180) & (g < 120) & (b < 120)).mean())
    return ratio >= thr


def grid_band(kit, fs=None, log=None):
    """自适应定位城市网格的垂直范围，返回 fast 帧的 (y_min, y_max)。

    这是「不再猜坐标」的核心：完全靠屏幕上的**文字语义**现场定位，零硬编码比例。

    上界（依次尝试，取最靠下的那个边界）：
      * 「历史地区」整行下方 —— 省列表里历史地区(江苏/广东/浙江/吉林)夹在网格上方，
        跳过它才不会把同名省市点成历史地区里的那个（2026-09-07 实测踩过两次）；
      * 最靠下的「请选择X」下方 —— 省列表有两个「请选择出发地」红字（红字1→历史地区
        →红字2→网格），市/区级是「XX>请选择市」，取最靠下即紧贴网格上方。
    下界：底部「清空筛选」/「确认」的上方。

    kit.ocr_frame() 返回的坐标已换算到 fast 帧，可直接用于 click_point，且整屏结果
    有帧缓存，重复调用不额外耗时。
    """
    if fs is None:
        fs = kit.frame()
    if fs is None:
        return None
    boxes = kit.ocr_frame(fs)  # fast 帧坐标
    fh = fs.fast.shape[0]
    if not boxes:
        if log:
            log("warning", "网格定位：本帧无 OCR 结果，回退静态默认")
        return None

    if log:
        log(
            "debug",
            "网格定位-全屏文字: " + " | ".join(f"{b.text}@{b.center}" for b in boxes[:30]),
        )

    y_min = None
    # 上界1：历史地区整行（标签与其按钮同一行，取该行最低的下沿）
    hist = [b for b in boxes if "历史地区" in (b.text or "")]
    for b in hist:
        bottom = b.rect[1] + b.rect[3]
        y_min = max(y_min or 0, bottom)
    # 上界2：最靠下的「请选择X」（红字/标题，紧贴网格上方）
    sel = [b for b in boxes if "请选择" in (b.text or "")]
    if sel:
        y_min = max(y_min or 0, max(b.rect[1] + b.rect[3] for b in sel))
    if y_min is not None:
        y_min += max(6, int(fh * 0.012))  # 留一点余量，避免贴边

    # 下界：底部操作按钮上方
    y_max = None
    for key in ("清空筛选", "确认"):
        kb = [b for b in boxes if key in (b.text or "")]
        if kb:
            y_max = min(b.rect[1] for b in kb)
            break
    if y_max is not None:
        y_max -= max(6, int(fh * 0.012))
    else:
        y_max = int(fh * 0.86)  # 只在这两个按钮都没认出时才兜底

    if y_min is None:
        y_min = int(fh * 0.30)  # 只在上界文字全没认出时才兜底
    if y_max <= y_min:  # 异常保护
        y_max = min(fh, y_min + int(fh * 0.40))

    if log:
        log("debug", f"网格定位结果: y_min={y_min} y_max={y_max} (屏高{fh})")
    return (y_min, y_max)


def top_red_entry_point(kit, regions, fs, log=None):
    """城市面板「顶部红字」= 关闭入口（铁律 §2.3 / 犯错记录 [0831-G][0831-H]）。

    取屏幕顶部 ~33% 区域内、经放宽档红字判定为真、且**含地区名**（或「当前定位」）的
    框里**最靠上**的那个，返回 (cx, cy, text)，坐标为 fast 帧。

    三个坑（都是实测踩过的）：
    * 严格档红字判定（占比≥0.25）对细红字**永远返回 False**（[0831-H]：细红字落在
      白底大 OCR 框里占比天然低），必须用放宽档 `city_red_text_boxes`；
    * 灰色「出发地/目的地」是面板 Tab，点了只切编辑侧、**不关面板**；放宽档下 Tab 也
      可能判成红，故用 `is_collapse_red_text`（要求含地区名）把它挡在候选之外；
    * **不能**再用 `is_entry_text`：它放行占位「出发地」，2026-09-12 实测因此选到灰色
      Tab、反复点不关面板，面板退不出累到 `city_panel_stuck=4` 被监护停止。
    """
    raw_h, raw_w = fs.raw.shape[:2]
    fast_h, fast_w = fs.fast.shape[:2]
    roi_raw = (0, 0, raw_w, int(raw_h * 0.33))
    boxes = kit.ocr.ocr(fs.raw, roi=roi_raw)  # raw 像素坐标（scale=1）
    reds = kit.ocr.city_red_text_boxes(fs.raw, boxes)

    cands = [tb for tb in reds if is_collapse_red_text(regions, tb.text or "")]
    if log:
        kept = set(map(id, cands))
        for tb in reds:
            mark = "收" if id(tb) in kept else "弃"
            log("debug", f"红字候选[{mark}] {tb.text!r} @{tb.rect}")
        if not cands:
            log(
                "warning",
                f"城市面板：{len(reds)} 个红字框均不含地区名（无可用的收起入口），"
                "不再点面板内页签",
            )
    if not cands:
        return None
    cands.sort(key=lambda tb: (tb.rect[1], tb.rect[0]))  # 最靠上、其次靠左
    tb = cands[0]
    x, y, w, h = tb.rect
    cx = (x + w / 2.0) * (fast_w / float(raw_w))
    cy = (y + h / 2.0) * (fast_h / float(raw_h))
    if log:
        log(
            "info",
            f"城市面板收起入口：选中 {tb.text!r} @({cx:.0f},{cy:.0f})（共 {len(cands)} 个候选）",
        )
    return (cx, cy, tb.text or "")


class CityPicker:
    """始发地/目的地选择。两侧共用同一套主干，只有入口与标签不同。"""

    def __init__(
        self,
        kit: ActionKit,
        regions: RegionStore,
        store: Optional[ConfigStore] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        self.kit = kit
        self.regions = regions
        self.store = store or kit.store
        self.stop_event = stop_event

    # ---------------------------------------------------------------- 配置

    def _entries(self, key: str, default=None):
        return self.store.get("fields", f"entries.{key}", default)

    def _grid_roi(self, idx: int, fs=None):
        """按层级取网格搜索 ROI。

        idx0=省列表 / idx1=市列表 / idx2=区列表。优先级：
          1) config fields.entries.grid_roi_province|city|district = [l,t,r,b]（真机校准用）；
          2) **动态推导** grid_top_ratio()：取最靠下的『请选择X』红字下沿，自动跳过
             「历史地区」行（2026-09-07 实测：静态上界 0.26/0.44 都会命中历史地区同名省市）；
          3) 静态默认值 GRID_ROIS（兜底）。
        """
        keys = ("grid_roi_province", "grid_roi_city", "grid_roi_district")
        key = keys[min(idx, len(keys) - 1)]
        val = self._entries(key)
        if isinstance(val, (list, tuple)) and len(val) == 4:
            return tuple(float(v) for v in val)
        if fs is None:
            fs = self.kit.frame()
        band = grid_band(self.kit, fs=fs, log=self.kit._emit)
        if band is not None and fs is not None:
            y_min, y_max = band
            fh = fs.fast.shape[0]
            # 归一化（fast 与 raw 同比例，转 raw ROI 后仍指向同一区域）
            return (0.0, y_min / float(fh), 1.0, y_max / float(fh))
        return GRID_ROIS[min(idx, len(GRID_ROIS) - 1)]

    # ------------------------------------------------- 网格扫描专用参数 / 层级归一

    def _grid_float(self, key: str, default: float) -> float:
        """读 runtime.swipe.<key>（网格扫描专用参数），坏值一律回落默认。"""
        try:
            v = self.store.get("runtime", f"swipe.{key}", default)
            return float(v if v is not None else default)
        except Exception:  # noqa: BLE001
            return float(default)

    def _grid_int(self, key: str, default: int) -> int:
        return int(self._grid_float(key, float(default)))

    def _grid_duration(self):
        """网格扫描的时长区间（小幅度必须配短时长，否则 iOS 当成拖拽、滑不动）。"""
        try:
            v = self.store.get("runtime", "swipe.grid_duration_ms_range", None)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                return [float(v[0]), float(v[1])]
        except Exception:  # noqa: BLE001
            pass
        return [180.0, 260.0]

    def _panel_level(self, fs) -> str:
        """判定面板层级：'root'=省级 / 'sub'=市或区列表 / 'unknown'。

        只用**正向证据**判"子级"（看到「返回上一级」或「请选择市/区」）；
        能确认是省级就返回 root；两者都认不出就返回 unknown ——
        调用方把 unknown 当 root 处理，保持与改动前一致（不误点「返回上一级」）。
        """
        try:
            for t in ("返回上一级", "请选择市", "请选择区"):
                if self.kit.find_text(t, fs) is not None:
                    return "sub"
            for t in ("请选择出发地", "请选择目的地"):
                if self.kit.find_text(t, fs) is not None:
                    return "root"
        except Exception:  # noqa: BLE001 - 判层级失败不能影响主流程
            return "unknown"
        return "unknown"

    def _ensure_root_level(self, max_back: int = 3) -> bool:
        """确保面板停在**省级（根级）**，否则逐层点「返回上一级」。

        为什么必须做：App 打开面板时会按"当前已选地区"决定钻进哪一层，而「清空筛选」
        只清选择、不会把层级带回根级（2026-09-13 实测：面板停在「湖南 > 益阳」，
        于是在省级 ROI 里找「江苏」必然失败）。失败绝不当成功：宁可不点，也不在错误的
        层级上盲找、盲滑。
        """
        for i in range(max_back + 1):
            fs = self.kit.frame()
            if fs is None:
                return False
            if self._panel_level(fs) != "sub":
                if i:
                    self.kit._emit("info", f"已退到省级根层级（回退 {i} 次）")
                return True
            if i == max_back:
                break
            self.kit._emit("info", f"面板停在子级，点「返回上一级」回退（第 {i + 1} 次）")
            if not self._back_one_level():
                self.kit._emit("warning", "「返回上一级」点击失败，无法回到省级")
                return False
            time.sleep(0.5)
        self.kit._emit("warning", f"连续回退 {max_back} 次仍在子级，放弃本次选择")
        return False

    def _grid_scroll_to_top(self, max_down: int = 3) -> None:
        """把网格滚回顶部（小幅下滑，滑到"画面不再变化"为止）。

        为什么必须先归顶：扫描只向上滑；若起点在列表中部，前面那些行永远不会经过扫描区。
        ⚠️ 用**小幅**下滑（默认 0.12 屏）而不是默认 down 幅度：面板多为底部弹层，
        大幅下滑有被系统当成"下拉关闭"的风险。
        """
        eps = self._grid_float("grid_move_epsilon", 0.35)
        ratio = self._grid_float("grid_top_down_ratio", 0.12)
        prev = None
        for _ in range(max_down):
            fs = self.kit.frame()
            if fs is None:
                return
            if prev is not None and self.kit._frame_delta(prev, fs) < eps:
                self.kit._emit("info", "网格已到顶部（画面不再变化）")
                return
            prev = fs
            self.kit.swipe_down(distance_ratio=ratio,
                                duration_range=self._grid_duration(),
                                start_y_ratio=0.6)
            time.sleep(0.4)

    # ---------------------------------------------------------------- 对外

    def select_side(self, side: str, steps: Sequence[SelectionStep]) -> bool:
        """选择一侧（origin/dest）。steps 是按 route_plan 展开的路径序列（path 含省/市/区）。

        有状态层级钻探（对齐用户 2026-09-07 真机截图）：
        current_path 记录「已点选的层级序列」，即界面当前钻探到的深度；每点一层界面向下
        钻一层，故选完一条 path 后界面停在 path 末节点的子列表。切换下一条 path 时：
          1) 算 current_path 与 step.path 的最长公共前缀 k；
          2) 回退 len(current_path)-k 次『返回上一级』退到公共前缀那一层；
          3) 从层级 k 起依次点 step.path[k..]；
          4) current_path = step.path。
        例：浙江/杭州→浙江/宁波（同省换市）回 1 级；浙江/杭州→江苏/南京（跨省）回 2 级。
        出发地 path 只到省，仅一步，自然不触发返回。
        """
        if not steps:
            return False
        label = SIDE_LABEL.get(side, side)
        self.kit._emit("info", f"开始选择{label}: {[list(s.path) for s in steps]}")

        if not self._open_entry(side):
            # 刚从上一侧确认返回列表页就点入口，页面可能还在过渡 → 点不开。
            # 2026-09-07 第二轮实测：dest 三次里有一次是「点了没反应，仍在 order_list」。
            self.kit._emit("warning", f"{label}入口未打开，1.2s 后重试一次")
            time.sleep(1.2)
            if not self._open_entry(side):
                self.kit._emit("warning", f"{label}入口仍未打开")
                return False
        if not self._ensure_grid(side):
            self.kit._emit("error", f"{label}未进入省市区网格选择模式")
            return False

        # 0) 先把面板退到省级（根级）——App 会按"当前已选地区"决定钻进哪一层，
        #    实测 2026-09-13：已选地区里有"益阳"→ 面板直接停在「湖南 > 益阳」的区列表，
        #    而「清空筛选」只清选择、**不会把层级带回根级** → 之后在省级 ROI 里找「江苏」
        #    必然找不到（还会误报"已到列表尽头"）。
        if not self._ensure_root_level():
            self.kit._emit("warning", f"{label}: 未能回到省级根层级，放弃本次选择")
            return False
        self._clear()
        # 1) **归顶**：先滚到列表最上方，再向上扫 —— 这样"每一行都经过扫描区"是可证明的。
        #    ⛔ 不要再做"盲滑一下"：那会把原本在扫描区顶部的省份顶出去，而扫描只向上滑、
        #    从不回滑 → 这些省"再也回不来"（2026-09-13 现场现象：有几个省没显示出来）。
        self._grid_scroll_to_top()

        # current_path = 已点选的层级序列，反映界面钻探深度。每点一层界面向下钻一层
        # （点省→市列表、点市→区列表…），故选完 path 后界面停在「path 末节点的子列表」。
        # 切换下一条 path 时先回退到两 path 的最长公共前缀那一层，再从那里继续点。
        current_path: List[str] = []
        ok = True
        for step in steps:
            path = list(step.path)
            if not path:
                continue
            # 1) 最长公共前缀
            k = 0
            while k < len(current_path) and k < len(path) and current_path[k] == path[k]:
                k += 1
            # 2) 回退到公共前缀层（省列表无『返回上一级』，回退次数恒等于需要退的层数）
            need_back = len(current_path) - k
            for _ in range(need_back):
                if self.stop_event is not None and self.stop_event.is_set():
                    return False
                if not self._back_one_level():
                    self.kit._emit("error", f"{label}: 返回上一级失败，无法继续选择")
                    return False
            # 3) 从层级 k 依次点选剩余层级（idx 即界面所处层级：0=省, 1=市, 2=区）
            for idx in range(k, len(path)):
                level = path[idx]
                roi = self._grid_roi(idx)
                # ⚠️ 网格扫描必须用**网格专用幅度 + 配对时长**（2026-09-13 二次修）：
                #   ① 幅度过大（默认翻页 0.32~0.5 屏高）→ 一次掠过整个省列表，目标被甩出
                #      扫描区再也找不到（"目的地选不上"）；
                #   ② 幅度过小也不行：上一版只给 0.05，而 amplitude_multiplier 已被砍到
                #      0.9~1.1（旧注释写的 1.7~2.3 已过期）→ 实际只有 36~45pt，却仍沿用
                #      翻页时长 490~700ms → 指尖速度只有正常上滑的约 1/7，iOS 当成"拖拽"，
                #      列表基本不动 → 现场表现就是"有几个省没显示出来"。
                #   ③ 所以：幅度 0.12 屏（≈97pt，≤ 省 ROI 高度 0.42 屏的一半 → 不漏行）、
                #      时长 180~260ms（与项目记录的"正常上滑速度"同量级）、最多 12 步。
                #   ④ ensure_text_visible 内部还会校验"画面真的动了"，没动先加大幅度重试，
                #      连续 2 次没动才判到底 —— 不再"一次没滚动就放弃"。
                #   （参数都可在 runtime.json 的 swipe.grid_* 调整，改完热重载生效。）
                if not self.kit.ensure_text_visible(
                    level, roi,
                    max_swipes=self._grid_int("grid_max_swipes", 12),
                    distance_ratio=self._grid_float("grid_distance_ratio", 0.12),
                    detect_end=True,
                    duration_range=self._grid_duration(),
                ):
                    self.kit._emit("warning", f"{label}: 网格未找到 {level}（当前层 {idx}）")
                    ok = False
                    break
                if self.kit.click_text_norm(level, roi, reason=f"select {level}") is None:
                    self.kit._emit("warning", f"{label}: 点击 {level} 失败")
                    ok = False
                    break
                time.sleep(0.5)
            if not ok:
                break
            current_path = list(path)

        if not ok:
            return False
        return self._confirm(side)

    # ---------------------------------------------------------------- 步骤

    def _entry_centers(self, fs: "FrameSet"):
        """定位出发地/目的地中心。

        主路径（用户指定算法）：OCR『智能排序』取中心 (sx,sy)，保持 Y、往左扫同行
        （|Δy|≤row_tol）且 x<sx 的文字，按 x 升序取前两段——最左=出发地、次左=目的地。
        『智能排序』小字易漏识，故锚点文字兼容全词/前两字『智能』/后两字『排序』。

        兜底：认不到锚点或左侧不足两段时，把筛选条整行文字按 x 排序取最左两段
        （智能排序 本身在最右，不影响前两段）。并打出屏上真实文字，便于真机核对。

        返回 (origin_center, dest_center) 或 None。
        """
        roi = self._entries("label_roi", (0.0, 0.04, 1.0, 0.15))
        raw_roi = self.kit.norm_to_raw_roi(fs, roi)
        boxes = self.kit.ocr_frame(fs, raw_roi)
        if not boxes:
            return None
        row_tol = float(self._entries("row_tol", 28) or 28)

        # 主路径：锚点 = 顶部筛选条的『智能排序』（固定 UI 文字，无『筛选』字样）。
        # 用户确认就是这 4 个字；小字 OCR 易漏识，故『对两个字就行』：依次试
        # 全词 → 前两字『智能』 → 后两字『排序』，任一命中即锚定。
        anchor_cands = ["智能排序", "智能", "排序"]
        sort_box = None
        for a in anchor_cands:
            b = self.kit.find_text(a, fs, raw_roi)
            if b is not None:
                sort_box = b
                self.kit._emit("debug", f"入口锚点文字命中: {a!r} @ {b.center}")
                break
        if sort_box is not None:
            sx, sy = sort_box.center
            left = [b for b in boxes if b.center[0] < sx and abs(b.center[1] - sy) <= row_tol]
            # 只保留「入口文字」：两入口之间的箭头「→」常被 OCR 认成 '1'/'-'，
            # 不滤掉就会被当成第二段，目的地坐标直接落到箭头上（2026-09-06 实测）。
            left = [b for b in left if is_entry_text(self.regions, b.text or "")]
            left.sort(key=lambda b: b.center[0])
            if len(left) >= 2:
                self.kit._emit(
                    "debug",
                    "筛选条左侧文字: " + " | ".join(f"{b.text}@{b.center}" for b in left[:4]),
                )
                return (left[0].center, left[1].center)

        # 兜底：整行聚类，取文字最多的一行，按 x 排序取最左两段
        self.kit._emit("warning", "未识别到『智能排序』锚点，改用整行聚类定位出发地/目的地")
        rows: list = []
        for b in boxes:
            y = b.center[1]
            placed = False
            for row in rows:
                if abs(row[0] - y) <= row_tol:
                    row[1].append(b)
                    placed = True
                    break
            if not placed:
                rows.append([y, [b]])
        if rows:
            rows.sort(key=lambda r: (len(r[1]), r[0]), reverse=True)
            rb = sorted(rows[0][1], key=lambda b: b.center[0])
            self.kit._emit(
                "debug",
                "聚类行文字: " + " | ".join(f"{b.text}@{b.center}" for b in rb[:6]),
            )
            # 兜底聚类**同样必须过滤**：箭头「→」常被 OCR 认成 '1'/'-' 混在筛选条里，
            # 不过滤就会被当成第二个入口（目的地），点开错误界面导致 dest 设置失败。
            # 过滤后不足两个宁可返回 None 走模板兜底——点错比不点更糟。
            rb2 = [b for b in rb if is_entry_text(self.regions, b.text or "")]
            self.kit._emit(
                "debug",
                "聚类行-过滤后: " + " | ".join(f"{b.text}@{b.center}" for b in rb2[:6]),
            )
            if len(rb2) >= 2:
                return (rb2[0].center, rb2[1].center)
        self.kit._emit("warning", "筛选条未识别到足够入口文字，无法定位出发地/目的地")
        return None

    def _open_entry(self, side: str) -> bool:
        """打开入口：智能排序中心 → 往左扫同行两段文字（最左=出发地、次左=目的地），
        直接点其文字中心；识别失败才退回 entry_anchor 模板 + 偏移兜底。"""
        fs = self.kit.frame()
        if fs is not None:
            centers = self._entry_centers(fs)
            if centers is not None:
                target = centers[0] if side == "origin" else centers[1]
                self.kit._emit(
                    "info",
                    f"入口[{side}] = ({target[0]:.0f},{target[1]:.0f})（筛选条左侧文字定位）",
                )
                if self.kit.click_point(target[0], target[1], reason=f"打开{side}入口"):
                    return True

            # 兜底：旧 entry_anchor 模板 + 偏移
            dx_key = "origin_dx" if side == "origin" else "dest_dx"
            # 默认值按投屏帧 375x812 由旧 480x640 值换算（x 方向 ×0.78125）；
            # 实际以 config/fields.json 的 entries.origin_dx / dest_dx 为准。
            dx = float(self._entries(dx_key, -95 if side == "origin" else -50) or 0)
            dy = float(self._entries("dy", 0) or 0)
            anchor_name = str(self._entries("anchor", "entry_anchor"))
            hit = self.kit.matcher.find(fs.fast, self.store.template(anchor_name))
            if hit is not None:
                self.kit._emit(
                    "info", f"锚点[{anchor_name}]命中 {hit.score:.2f}，{side} 偏移 {dx:.0f}"
                )
                if self.kit.click_point(hit.cx + dx, hit.cy + dy, reason=f"打开{side}入口"):
                    return True

        if self.kit.click_template("history_zone", reason=f"{side}入口兜底"):
            return True
        return False

    def _ensure_grid(self, side: str, max_retry: int = 3) -> bool:
        """确保处在省市区网格模式（底部有清空筛选+确认）；
        点入口后 App 可能默认打开「自定义装货点」模式，需点标签切回。"""
        want = (ScreenState.CITY_ORIGIN, ScreenState.CITY_DEST)
        for attempt in range(max_retry):
            _, res = self.kit.wait_state(want, timeout=4.0, interval=0.4)
            if res.state in want:
                return True
            label = SIDE_LABEL.get(side, side)
            self.kit._emit("warning", f"未进入网格模式({res.state.value})，点「{label}」切换")
            # 页签行（实测 y=166≈0.26）与顶部筛选条(y=139)不是同一行，必须用独立的
            # panel_tab_roi；沿用 label_roi 会框不到页签，导致切不回网格。
            roi = self._entries("panel_tab_roi", (0.0, 0.24, 1.0, 0.30))
            if not self.kit.click_text_norm(label, roi, reason="切网格模式"):
                self.kit.click_template("origin_popup", reason="切网格模式兜底")
            time.sleep(0.8)
        return False

    def _clear(self) -> None:
        """清空上一辆车的残留选择。"""
        if self.kit.click_text_norm("清空筛选", (0.0, 0.84, 0.5, 1.0), reason="清空筛选"):
            time.sleep(0.4)
            return
        self.kit.click_template("city_clear", reason="清空筛选兜底")
        time.sleep(0.4)

    def _back_one_level(self) -> bool:
        """点一次右侧红色『返回上一级』，退到上一层网格。

        用户 2026-09-07 真机：市/区列表顶部『XX > 请选择市』行右侧有红色『返回上一级』；
        省列表无此按钮（已是根）。

        2026-09-07 二修：上一版加了「固定坐标兜底 (0.82,0.30)」——坐标是猜的，实测点到
        错误位置且层级没回退，调用方却因返回 True 继续在错误层级盲找城市并疯狂上滑。
        现改为：OCR 全词 → 部分词(「返回」/「上一级」) → 模板 → config 可选坐标 → 报错。
        失败绝不再伪装成成功。
        """
        # 重试 2 轮（2026-09-07 补）：面板刚切换/惯性滚动未停时的那一帧常认不出
        # 「返回上一级」，单次失败就放弃会让整条路线报废（dest 连选 3 个市要回退
        # 2 次，任一瞬时抖动即整体失败）。重取帧再试一次，仍失败才停——不盲点。
        for attempt in range(2):
            if self._try_back_once():
                return True
            if attempt == 0:
                self.kit._emit(
                    "warning", "『返回上一级』本帧未命中，0.8s 后重取帧再试一次"
                )
                time.sleep(0.8)

        self.kit._emit("error", "『返回上一级』两次均未命中，停止（不再盲点）")
        return False

    def _try_back_once(self) -> bool:
        """一轮尝试：OCR 语义定位 → 模板 → config 坐标。返回是否点成功。"""
        # 全屏 OCR（fast 帧坐标，可直接点击；整屏结果有帧缓存，不额外耗时）
        fs = self.kit.frame()
        if fs is not None:
            boxes = self.kit.ocr_frame(fs)
            fh = fs.fast.shape[0]
            upper = [b for b in boxes if b.center[1] < fh * 0.55]
            self.kit._emit(
                "info",
                "返回上一级-上半屏文字: "
                + " | ".join(f"{b.text}@{b.center}" for b in upper[:24]),
            )
            # 尝试1/2: 文字语义定位（全词 → 断字兜底），不依赖任何预设 ROI
            for key in ("返回上一级", "上一级", "返回"):
                cands = [b for b in upper if key in (b.text or "")]
                if cands:
                    cands.sort(key=lambda b: b.center[1])  # 取最靠上
                    b = cands[0]
                    self.kit._emit("info", f"返回上一级命中 {b.text!r} @{b.center}")
                    if self.kit.click_point(
                        b.center[0], b.center[1], reason=f"返回上一级({key})"
                    ):
                        time.sleep(0.6)
                        return True

        # 尝试3: 模板
        if self.kit.click_template("city_back", reason="返回上一级(模板)"):
            time.sleep(0.6)
            return True
        # 尝试4: config 可选固定坐标（默认不启用；真机校准后填 fields.entries.back_level_point=[x,y]）
        pt = self._entries("back_level_point")
        if isinstance(pt, (list, tuple)) and len(pt) == 2:
            bx, by = int(pt[0]), int(pt[1])
            self.kit._emit("warning", f"『返回上一级』用 config 固定坐标 ({bx},{by})")
            if self.kit.click_point(bx, by, reason="返回上一级(config固定坐标)"):
                time.sleep(0.6)
                return True
        return False

    def _confirm_clickable(self, fs) -> bool:
        """确认按钮是否正红可点（实现已提到模块级 `confirm_button_clickable` 供 handlers 复用）。"""
        return confirm_button_clickable(self.store, fs)

    def _confirm(self, side: str, max_retry: int = 3) -> bool:
        """点确认并验证真的回到列表页（点了不等于成功）。

        2026-09-07 实机：目的地确认经常点不住（出发地基本都能点到）。根因多半是
        下压不够实（电容屏触发不了）或点完立刻判态、触点还没上报就误判「没退」。
        加固：逐次加深下压（+0.2/次，最多 +0.4），点完静置 0.25s 等触点稳定，
        面板展开态点确认不退出则先点顶部红字收起再确认。

        2026-09-12 加固：点确认前先判按钮正红可点性。面板**展开态**（下方有城市
        网格）的「确认」是浅红（不可点），死点无效还浪费时间——浅红时直接点顶部
        红字收起面板（铁律 [0831-H]），不做无用点击。
        """
        confirm_roi = self._entries("confirm_roi", (0.0, 0.62, 1.0, 0.99))
        confirm_dy = int(self._entries("confirm_dy", 3) or 3)
        base_z = float(self.store.get("hardware", "calibration.z_press", 5.7) or 5.7)
        label = SIDE_LABEL.get(side, side)

        for attempt in range(max_retry):
            fs, res = self.kit.wait_state(
                (ScreenState.ORDER_LIST, ScreenState.ORDER_LIST_NO_MORE), timeout=2.0, interval=0.4
            )
            if res.state in (ScreenState.ORDER_LIST, ScreenState.ORDER_LIST_NO_MORE):
                return True

            # 确认按钮浅红（展开态）→ 点了也无效，直接点顶部红字收起，不做无用点击
            if not self._confirm_clickable(fs):
                self.kit._emit("info", f"{label}确认按钮非正红（浅红/展开态），改点顶部红字收起")
                pt = top_red_entry_point(
                    self.kit, self.regions, fs,
                    log=lambda lvl, msg: self.kit._emit(lvl, msg),
                )
                if pt is not None:
                    cx, cy, text = pt
                    self.kit.click_point(
                        cx, cy, reason=f"{label}浅红确认→点顶部红字 {text!r} 收起"
                    )
                    _, res2 = self.kit.wait_state(
                        (ScreenState.ORDER_LIST, ScreenState.ORDER_LIST_NO_MORE),
                        timeout=4.0,
                        interval=0.5,
                    )
                    if res2.state in (ScreenState.ORDER_LIST, ScreenState.ORDER_LIST_NO_MORE):
                        self.kit._emit("info", f"{label}点红字后已回到列表页")
                        return True
                # 红字也没命中：保存现场供离线定位，避免死循环
                self.kit._emit("warning", f"{label}确认浅红且顶部红字未命中，保存现场后重试")
                if hasattr(self.kit, "save_debug"):
                    self.kit.save_debug(fs, f"city_confirm_unclickable_{side}")
                continue

            # 正红可点 → 逐次加深下压点确认（电容屏触发不了=压得不够实，
            # 往浅试等于白点一次）。第一次 base+0.2，再试 base+0.4。
            z = base_z + 0.2 + 0.2 * attempt
            clicked = self.kit.click_text_norm(
                "确认", confirm_roi, z=z, reason=f"{label}确认(压{z:.1f})"
            )
            if clicked is None:
                # OCR 找不到再用模板，命中坐标偏上，需 +confirm_dy
                self.kit.click_template(
                    "city_confirm", dy=confirm_dy, z=z, reason=f"{label}确认兜底(压{z:.1f})"
                )
            # 静置等触点稳定上报，避免「点完立刻判态」漏掉回列表的过渡
            time.sleep(0.25)
            fs, res = self.kit.wait_state(
                (ScreenState.ORDER_LIST, ScreenState.ORDER_LIST_NO_MORE), timeout=6.0, interval=0.5
            )
            if res.state in (ScreenState.ORDER_LIST, ScreenState.ORDER_LIST_NO_MORE):
                self.kit._emit("info", f"{label}确认后已回到列表页")
                return True
            self.kit._emit("warning", f"{label}确认后仍在 {res.state.value}，重试（加深下压）")

            # 铁律 [0831-H]：面板**展开态**（下方有城市网格）点「确认」不会退出，
            # 必须先点顶部红字收起面板才能回到列表。此处「确认」已提交选择，
            # 点红字只是收起面板（红字=收起不改），不会丢掉刚才的选择。
            if res.state in (ScreenState.CITY_ORIGIN, ScreenState.CITY_DEST):
                pt = top_red_entry_point(
                    self.kit, self.regions, fs,
                    log=lambda lvl, msg: self.kit._emit(lvl, msg),
                )
                if pt is not None:
                    cx, cy, text = pt
                    self.kit._emit("info", f"{label}确认后面板未收起，点顶部红字 {text!r} 收起")
                    self.kit.click_point(cx, cy, z=z, reason="确认后点顶部红字收起城市面板")
                    _, res2 = self.kit.wait_state(
                        (ScreenState.ORDER_LIST, ScreenState.ORDER_LIST_NO_MORE),
                        timeout=4.0,
                        interval=0.5,
                    )
                    if res2.state in (ScreenState.ORDER_LIST, ScreenState.ORDER_LIST_NO_MORE):
                        # 2026-09-12：点红字只「收起面板」，是否真提交了选择无从判断
                        # （历史上疑似出现「面板收起但筛选没变」的假成功）。这里降级为
                        # warning + 存现场，真正生效与否交给 machine.setup_routes 的
                        # 设置后复核兜底——那条链路会读筛选条核对，不符即重试/告警。
                        self.kit._emit(
                            "warning",
                            f"{label}确认未收起面板，改点红字收起"
                            "（仅收起、不保证已提交，待设置后复核）",
                        )
                        if hasattr(self.kit, "save_debug"):
                            self.kit.save_debug(fs, f"city_confirm_redtext_{side}")
                        return True
        return False

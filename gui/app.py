"""抢单机器人正式界面（PyQt5，多标签页）。

标签页：
    运行控制  —— 开始/停止/自检、实时状态、运行监护告警、实时统计面板
    候选订单  —— 详情页判定结果累积表（车长/车型/吨/方/价/距/单价/判定），合格行高亮
    路线      —— 路线列表（当前高亮）、粘贴解析、保存、一键切下一条
    参数      —— 最小单价/车长/车型/载重/容积等，保存后热重载
    日志      —— 实时分级日志（info/warning/error/debug 着色）

线程纪律：
    * 机器人跑在后台线程，日志通过 Qt 信号回到主线程再写控件
      （子线程直接操作 QTextEdit 会随机崩溃）；
    * 所有停止都是「置事件 + 等线程结束」，绝不 kill 线程
      （机械臂可能正压在屏幕上，强杀会留下未抬起的状态）；
    * 配置每 1 秒检测变化并热重载（改完即刻生效，不用重启）。
"""

from __future__ import annotations

import os
import sys
import ctypes
import collections
import time
import threading
from pathlib import Path

# 源码运行：项目根要进 sys.path 才能 import core/gui。
# 打包后（frozen）**必须跳过**：此时模块都在 PyInstaller 归档里、由 FrozenImporter
# 解析，与 sys.path 无关；而这里算出来的会是临时 _internal 目录，插到 sys.path[0]
# 反而可能遮蔽标准库/第三方包（2026-09-13 打包适配）。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 唯一正确的「项目根」基准：core.config_store 已处理 frozen（→ exe 所在目录）。
# 绝不能再自己用 __file__ 推导——打包后 __file__ 指向临时解压目录，会把截图/日志/
# 崩溃记录写到用户看不见的地方（2026-09-13 打包适配）。
from core.config_store import CONFIG_DIR, DATA_DIR, PROJECT_ROOT  # noqa: E402


def _boot_log(title: str, body: str) -> None:
    """把启动自检追加写入 <data>/gui_boot.log。

    为什么需要：打包后只有界面、没有控制台，DLL / OCR 这类启动期问题必须留下证据，
    否则只能看到「OCR 未加载」这种二手现象。写日志本身失败也绝不影响启动。
    """
    try:
        _p = Path(DATA_DIR) / "gui_boot.log"
        _p.parent.mkdir(parents=True, exist_ok=True)
        with open(_p, "a", encoding="utf-8") as _fh:
            _fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {title} =====\n")
            _fh.write(body.rstrip() + "\n")
    except Exception:  # noqa: BLE001 - 日志写不进去也不能影响启动
        pass


# ---------------------------------------------------------------------------
# 冻结（打包）环境专有处理：把 VC++ 运行库「钉死」成唯一那一套，并留下自检日志。
#
# 为什么必须做：PyQt5 的 wheel 自带一套旧版 MSVCP140 放在 PyQt5\Qt5\bin\ 下，它会把自己
# 的 Qt5\bin 加进 DLL 搜索路径；冻结环境下一旦让旧那套先占坑，onnxruntime 的
# onnxruntime_pybind11_state.pyd 就报 [WinError 1114]「动态链接库(DLL)初始化例程失败」
# → rapidocr 导入失败 → OCR 全废 → 页面判态失去依据（不点红色「全国」、不清空筛选、
# 乱滑、点不动）。
# spec 已经从包里剔除那套旧库（治本）；这里再加一道保险：**用完整路径显式预载**
# _internal 下的正确运行库——同名 DLL 一旦载入，后续同名加载只会复用已载入者，
# 将来搜索路径顺序再变也不怕。同时把清单与完整异常写进 gui_boot.log（可观测）。
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    _base = getattr(sys, "_MEIPASS", str(Path(sys.executable).parent))
    _vc_dirs = [_base, os.path.join(_base, "_internal")]
    for _d in _vc_dirs:
        try:
            if os.path.isdir(_d):
                os.add_dll_directory(_d)
        except Exception:  # noqa: BLE001 - 加不上目录也不致命的，后面还有显式预载
            pass

    # ① 显式预载正确运行库（顺序：concrt140/msvcp140 系 → vcruntime140 系，按名排序即可）
    _vc_report: list = []
    for _d in _vc_dirs:
        if not os.path.isdir(_d):
            continue
        for _fn in sorted(os.listdir(_d)):
            _low = _fn.lower()
            if not _low.endswith(".dll"):
                continue
            if not _low.startswith(("msvcp140", "vcruntime140", "concrt140")):
                continue
            _fp = os.path.join(_d, _fn)
            try:
                ctypes.WinDLL(_fp)  # 预载即目的，返回值不用
                _vc_report.append(f"  预载 OK   {_fp}  ({os.path.getsize(_fp) // 1024}KB)")
            except Exception as _exc:  # noqa: BLE001 - 记下来继续，别让启动挂掉
                _vc_report.append(f"  预载失败  {_fp}  -> {type(_exc).__name__}: {_exc}")

    # ② 全包扫描同名运行库：出现两套就是冲突前兆（spec 应已剔除 PyQt5\Qt5\bin 那套）
    _dupes: list = []
    for _root, _dirs, _files in os.walk(_base):
        for _fn in _files:
            if _fn.lower() in ("msvcp140.dll", "msvcp140_1.dll",
                               "vcruntime140.dll", "vcruntime140_1.dll"):
                _dupes.append(os.path.join(_root, _fn))

    # ③ 导入 onnxruntime —— 真正的原因就在这里，**绝不再静默吞掉**
    try:
        import onnxruntime  # noqa: F401 - 须早于 PyQt5：先锁定正确的 VC++ 运行库
        _ort_state = f"IMPORT_OK  version={onnxruntime.__version__}"
    except Exception as _exc:  # noqa: BLE001
        import traceback

        _ort_state = f"IMPORT_FAIL  {type(_exc).__name__}: {_exc}\n{traceback.format_exc()}"

    _capi_dir = os.path.join(_base, "onnxruntime", "capi")
    _capi_report: list = []
    if os.path.isdir(_capi_dir):
        for _fn in sorted(os.listdir(_capi_dir)):
            _fp = os.path.join(_capi_dir, _fn)
            try:
                _capi_report.append(f"  {_fn}  {os.path.getsize(_fp) // 1024}KB")
            except OSError:
                _capi_report.append(f"  {_fn}  （读取失败）")
    else:
        _capi_report.append(f"  ★目录不存在: {_capi_dir}")

    _boot_log(
        "启动自检（frozen）",
        "\n".join(
            [
                f"_MEIPASS   = {_base}",
                f"executable = {sys.executable}",
                "① VC 运行库预载（应全部 OK，且只来自 _internal）：",
                *(_vc_report or ["  （一个候选都没找到，包内缺运行库！）"]),
                "② 全包同名运行库扫描（正常只该有 1 套，PyQt5\\Qt5\\bin 下不应再有）：",
                *(_dupes or ["  （未找到）"]),
                f"③ onnxruntime: {_ort_state}",
                "④ onnxruntime\\capi 二进制：",
                *_capi_report,
            ]
        ),
    )
else:
    # 源码运行：保持原有「先 onnxruntime 再 PyQt5」的顺序即可（干净进程下能正常加载）。
    try:
        import onnxruntime  # noqa: F401  - 预加载以锁定正确的 VC++ 运行库
    except Exception:  # noqa: BLE001 - 源码模式没装 OCR 时允许降级，GUI 另有告警
        pass

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from core.config_store import ConfigStore, ConfigError  # noqa: E402
from core.domain.capacity import Capacity  # noqa: E402
from core.domain.regions import RegionStore  # noqa: E402
from core.domain.route_plan import (  # noqa: E402
    RoutePlanError,
    load_plan,
    parse_routes_text,
    validate_plan,
)
from core.domain.rules import RuleEngine  # noqa: E402

LEVEL_COLOR = {
    "info": "#2c3e50",
    "warning": "#e67e22",
    "error": "#e74c3c",
    "debug": "#95a5a6",
}

# 全局商务主题（浅色、商务蓝、正式简洁）。应用到 QApplication.setStyleSheet。
# 局部控件若单独 setStyleSheet 会覆盖此主题（Qt 局部样式优先级更高），
# 深色代码区（日志/详情原文/摄像头预览）刻意保留局部深色样式。
GLOBAL_QSS = """
QWidget {
    background: #f5f7fa;
    color: #2c3e50;
    font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog { background: #f5f7fa; }
QTabWidget::pane {
    border: 1px solid #dcdfe6;
    background: #ffffff;
    border-radius: 4px;
}
QTabBar::tab {
    background: #e8ecf1;
    color: #5a6b7b;
    padding: 10px 22px;
    border: 1px solid #dcdfe6;
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
    font-size: 14px;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #1a6fb0;
    font-weight: bold;
    border-bottom: 2px solid #1a6fb0;
}
QPushButton {
    background: #1a6fb0;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 8px 18px;
    font-size: 14px;
    font-weight: bold;
}
QPushButton:hover { background: #2c7fc4; }
QPushButton:pressed { background: #155a92; }
QPushButton:disabled { background: #c0c4cc; color: #f5f5f5; }
QCheckBox { color: #2c3e50; spacing: 6px; }
QGroupBox {
    border: 1px solid #dcdfe6;
    border-radius: 6px;
    margin-top: 14px;
    background: #ffffff;
    font-weight: bold;
    font-size: 14px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: #1a6fb0;
}
QTableWidget, QTableView {
    background: #ffffff;
    alternate-background-color: #f8fafc;
    gridline-color: #eef1f5;
    border: 1px solid #dcdfe6;
    border-radius: 4px;
}
QHeaderView::section {
    background: #eef3f8;
    color: #2c3e50;
    font-weight: bold;
    padding: 8px;
    border: none;
    border-bottom: 2px solid #dcdfe6;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit {
    background: #ffffff;
    border: 1px solid #dcdfe6;
    border-radius: 4px;
    padding: 6px 8px;
    color: #2c3e50;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid #1a6fb0;
}
QStatusBar {
    background: #eef3f8;
    color: #5a6b7b;
    border-top: 1px solid #dcdfe6;
}
QSplitter::handle { background: #dcdfe6; }
"""

# 实时统计面板要展示的计数器（顺序即展示顺序）
COUNTER_KEYS = [
    ("切卡", "cards_split"),
    ("命中", "cards_hit"),
    ("去重跳过", "cards_deduped"),
    ("软重(参数同)", "cards_soft_dup"),
    ("进详情", "detail_returned"),
    ("详情淘汰", "detail_rejected"),
    ("DRY-RUN将抢", "dry_run_would_grab"),
    ("真抢", "grabbed"),
    ("翻页", "scan_swipe"),
    ("落点重复拦", "scan_repeat_point_skipped"),
    ("整屏0命中", "scan_empty_screen"),
    ("路线设置OK", "route_setup_ok"),
    ("路线设置失败", "setup_fail"),
    ("面板卡死", "city_panel_stuck"),
    ("库已录", "order_db_recorded"),
]


class LogBridge(QtCore.QObject):
    """跨线程日志桥：子线程 emit → 主线程槽函数写控件。"""

    appended = QtCore.pyqtSignal(str, str)


class MainWindow(QtWidgets.QMainWindow):
    boot_finished = QtCore.pyqtSignal(bool, str)
    boot_progress = QtCore.pyqtSignal(int, str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("运满满抢单机器人 V2.8")
        self.resize(1400, 900)
        try:
            self.store = ConfigStore(strict=True)
        except ConfigError as exc:
            QtWidgets.QMessageBox.critical(self, "配置错误", str(exc))
            raise

        self.bridge = LogBridge()
        self.bridge.appended.connect(self._append_log)
        # GUI 也把日志落到 data/logs/run_*.log（原先只有命令行 main.py 会写）。
        # 必须落盘的原因：城市选择等细节走 kit._emit，只进 GUI 回调而**不进 trace**，
        # 于是「路线设置失败」这类问题在 trace 里查不到任何过程（2026-09-07 实测：
        # dest 连续失败 3 次自动停止，trace 里只有结果没有原因，只能靠人工复制日志）。
        self._log_path = None
        self._log_fp = self._open_run_log()
        self.log_fn = self._log_both
        self.boot_finished.connect(self._on_boot_finished)
        self.boot_progress.connect(self._on_boot_progress)

        self.task = None
        self._thread: threading.Thread | None = None
        self._stop_reason_shown = ""
        self._booting = False  # 设备打开中（防重复点「启动/继续扫单」导致双重 boot）
        self._setup_first = True  # 本次启动是否先设路线（继续扫单恢复时为 False）
        self._last_cand_sig = ""
        self._cand_rows: list = []

        # ---- 机械臂摄像头（GUI 自持，2026-09-13 用户要求）----
        # 人工操作期画面区直接显示**机械臂摄像头实景**：iMouse 桌面窗口本身就常开着看
        # 手机画面，GUI 这块空出来看机械臂/现场更实用，出问题还能一键截图留存。
        # 与业务取帧源解耦——业务识别仍走 vision.capture.source（当前 imouse 投屏，
        # HTTP），物理摄像头是空着的，故 GUI 可独占；若业务本身就用 camera 取帧，
        # 则不重复打开（会抢设备），改为复用任务取帧。
        self._arm_cam = None            # Camera 实例（打开成功后才有）
        self._arm_cam_state = "idle"    # idle/opening/ready/use_task/failed
        self._arm_cam_use_task = False  # True = 复用业务取帧（业务占用物理摄像头）
        self._last_shot_path = ""
        self._cam_visible = True        # 画面区是否显示（可一键隐藏，把空间让给右侧）
        self._cam_warn = ""             # 画面异常提示（空=正常；全黑/噪点，来自 camera.frame_quality）
        self._dark_check_ts = 0.0

        self._build_ui()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)

        # 摄像头实时预览：独立高频定时器（25fps）从摄像头缓存取帧显示，
        # 不阻塞、不抢设备；与每秒的状态刷新（_tick）解耦。
        self._preview_timer = QtCore.QTimer(self)
        self._preview_timer.timeout.connect(self._refresh_camera_preview)
        self._preview_timer.start(40)

        # 日志合并+节流：跨线程日志先入队（连续相同项合并计数），每 200ms 批量刷新，
        # 避免海量 warning/取帧异常刷屏时每条都 append QTextEdit 拖垮主线程（卡顿来源之一）
        self._log_queue: "collections.deque" = collections.deque()
        self._log_flush_timer = QtCore.QTimer(self)
        self._log_flush_timer.timeout.connect(self._flush_log)
        self._log_flush_timer.start(200)

        # 启动即写一行日志：否则「GUI 启动后没点开始就关闭」会留下 0 字节 run_*.log，
        # 与「启动即崩溃」无法区分（2026-09-12 观察到 3 个 0 字节日志，实为反复启停 GUI）。
        # 必须放在 _log_queue 初始化之后——否则 log_fn 经信号同步触发 _append_log，
        # 访问尚未初始化的 _log_queue 会 AttributeError 崩溃（2026-09-12 回归已修）。
        self.log_fn("info", "GUI 已启动，等待操作")

        # 机械臂摄像头放到后台线程打开：Camera.open() 会依次试 index（最长 1~2s），
        # 不能卡住界面。必须放在 _log_queue 初始化之后——线程里要打日志。
        threading.Thread(
            target=self._open_arm_camera, daemon=True, name="arm-cam-open"
        ).start()

    # ---------------------------------------------------------------- UI

    @staticmethod
    def _no_width_push(label: QtWidgets.QLabel) -> None:
        """让 QLabel 的文本**不参与**布局宽度计算（长文本不撑宽所在面板）。

        踩坑（2026-09-13 用户实测）：截图后「上次截图：shot_20260913_103541.png（480x640）」
        把左侧画面区顶宽，而且 QSplitter 再也拖不窄——QLabel 的 minimumSizeHint 直接
        成了左面板的最小宽度。SizePolicy.Ignored + minimumWidth=0 之后，文本只管显示，
        宽度完全由分割条决定（空间不够时裁切，不挤压布局）。
        """
        label.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred
        )
        label.setMinimumWidth(0)

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        outer = QtWidgets.QVBoxLayout(root)

        # —— 顶部：控制条 ——
        top = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton("开始")
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_check = QtWidgets.QPushButton("自检")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_check.clicked.connect(self.on_check)
        self.lbl_state = QtWidgets.QLabel("未启动")
        self.lbl_state.setStyleSheet("font-weight: bold; color: #27ae60;")
        for w in (self.btn_start, self.btn_stop, self.btn_check,
                  QtWidgets.QLabel("  |  状态："), self.lbl_state):
            top.addWidget(w)
        top.addStretch(1)
        # 不停止测试：右上角标准勾选。勾选后详情合格不暂停、
        # 等2s返回列表继续找单；不勾选则暂停等待人工操作（见 core/flow/detail.py::_no_pause_test）
        self.chk_no_pause = QtWidgets.QCheckBox("不停止测试（合格不暂停·等5s返回）")
        self.chk_no_pause.setChecked(bool(self.store.get("runtime", "no_pause_test", False)))
        self.chk_no_pause.setToolTip(
            "勾选后：详情页命中符合订单不暂停、不抢单，等5秒自动返回列表继续找单；"
            "不勾选则暂停等待人工操作（继续扫单/结束换车/拼单按钮）"
        )
        self.chk_no_pause.stateChanged.connect(self.on_toggle_no_pause)
        top.addWidget(self.chk_no_pause)
        # 运行监护（watchdog）开关：与上面同风格的右上角标准勾选。
        # 监护是「日志/计数器异常达阈值就自动急停」的安全网，默认开；
        # 调参或排查问题时可临时关掉，避免它频繁打断（关掉后需人工盯）。
        self.chk_watchdog = QtWidgets.QCheckBox("运行监护（异常自动急停）")
        self.chk_watchdog.setChecked(
            bool(self.store.get("runtime", "watchdog.enabled", True))
        )
        self.chk_watchdog.setToolTip(
            "勾选后：日志/计数器异常达到阈值会自动停止运行（如连续 N 次同一落点、"
            "详情页卡住、跑满本轮时长等），防止带着错误状态一直跑；\n"
            "取消勾选：监护不再自动急停，适合调参/排查问题时长时间观察，但需人工盯盘。\n"
            "规则明细见 config/runtime.json 的 watchdog 段。"
        )
        self.chk_watchdog.stateChanged.connect(self.on_toggle_watchdog)
        top.addWidget(self.chk_watchdog)
        outer.addLayout(top)

        # —— 标签页 ——
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._build_run_tab(), "运行控制")
        tabs.addTab(self._build_cand_tab(), "候选订单")
        tabs.addTab(self._build_route_tab(), "路线")
        tabs.addTab(self._build_param_tab(), "参数")
        tabs.addTab(self._build_log_tab(), "日志")
        outer.addWidget(tabs, stretch=1)

        self.statusBar().showMessage("就绪")
        self._load_routes_into_ui()

    # ---- 运行控制 ----
    def _build_run_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)

        # —— OCR 引擎降级醒目提示：缺失时一眼可见，并指明运行解释器 ——
        self.lbl_ocr_warn = QtWidgets.QLabel()
        self.lbl_ocr_warn.setStyleSheet(
            "background:#5a1d1d; color:#ffb3b3; font-weight:bold; font-size:14px; "
            "padding:6px 10px; border-radius:4px;"
        )
        self.lbl_ocr_warn.setWordWrap(True)
        self.lbl_ocr_warn.hide()
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except Exception:
            self.lbl_ocr_warn.setText(
                "⚠ OCR 引擎未加载：价格/重量/距离/单价等字段将无法识别（详情页判定会转人工）。"
                f" 运行解释器：{sys.executable}。请用「启动GUI.bat」或 py -3.11 启动本程序。"
            )
            self.lbl_ocr_warn.show()
        v.addWidget(self.lbl_ocr_warn)

        # —— 启动进度条（分步开设备，避免「点开始像死机」）——
        self.pb_boot = QtWidgets.QProgressBar()
        self.pb_boot.setRange(0, 100)
        self.pb_boot.setTextVisible(True)
        self.pb_boot.hide()
        v.addWidget(self.pb_boot)

        # —— 第一行：监护 + 状态（紧凑） ——
        top_row = QtWidgets.QHBoxLayout()
        self.lbl_guard = QtWidgets.QLabel("监护：未触发")
        self.lbl_guard.setStyleSheet("font-weight: bold; color: #27ae60;")
        top_row.addWidget(self.lbl_guard)
        top_row.addStretch(1)
        v.addLayout(top_row)

        # —— 第二行：实时统计（紧凑 4 列）——
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(2)
        self._counter_labels: dict = {}
        cols = 4
        for i, (label, key) in enumerate(COUNTER_KEYS):
            lab = QtWidgets.QLabel(label)
            lab.setStyleSheet("font-size: 14px; color: #999;")
            val = QtWidgets.QLabel("0")
            val.setStyleSheet("font-weight: bold; font-size: 18px;")
            row, col = divmod(i, cols)
            grid.addWidget(lab, row, col * 2)
            grid.addWidget(val, row, col * 2 + 1)
            self._counter_labels[key] = val
        box = QtWidgets.QGroupBox("实时统计（本轮）")
        box.setStyleSheet("QGroupBox { font-size: 14px; font-weight: bold; }")
        box.setLayout(grid)
        v.addWidget(box)

        # —— 第三行：当前路线 + 容量（一行横排）——
        info_row = QtWidgets.QHBoxLayout()
        self.lbl_route = QtWidgets.QLabel("当前路线：-")
        self.lbl_route.setStyleSheet("font-weight: bold;")
        self.lbl_cap = QtWidgets.QLabel("容量：-")
        info_row.addWidget(self.lbl_route)
        info_row.addStretch(1)
        info_row.addWidget(self.lbl_cap)
        v.addLayout(info_row)

        # —— 耗时明细（本轮 · 毫秒）：扫描 / 判定 / OCR / 详情页 / 单轮 / 城市 ——
        timing_box = QtWidgets.QGroupBox("耗时明细（本轮 · 毫秒）")
        timing_box.setStyleSheet("QGroupBox { font-size: 13px; font-weight: bold; }")
        tgrid = QtWidgets.QGridLayout(timing_box)
        tgrid.setSpacing(4)
        self._timing_labels: dict = {}
        for i, (key, short) in enumerate(
            [("扫描", "扫描"), ("判定", "判定"), ("整屏OCR", "OCR"), ("订单详情页", "详情页"),
             ("单轮循环", "单轮"), ("城市选择", "城市")]
        # 注：OCR 标签原绑 ctx.timings["OCR识别"]（仅详情页读字段时写入），列表扫描时
        # 常显 0/陈旧。改绑「整屏OCR」（scanner.py 每一轮整屏识别计时）——这才是扫描
        # ~4000ms 的主要来源（RapidOCR 整屏识别）。详情页 OCR 仍记在「OCR识别」key 里。
        ):
            lab = QtWidgets.QLabel(short)
            lab.setStyleSheet("font-size: 12px; color: #999;")
            val = QtWidgets.QLabel("-")
            val.setStyleSheet("font-weight: bold; font-size: 15px; color: #27ae60;")
            tgrid.addWidget(lab, 0, i * 2)
            tgrid.addWidget(val, 0, i * 2 + 1)
            self._timing_labels[key] = val
        v.addWidget(timing_box)

        # —— 主区域：左（摄像头预览） | 右（订单详情 + 操作按钮），分隔条可拖拽 ——
        main_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        main_split.setHandleWidth(6)
        main_split.setStyleSheet("QSplitter::handle { background: #dcdfe6; border-radius: 3px; }")
        # 左 420 / 右 800：详情右栏够宽，标题不被摄像头区遮挡；分隔条可拖
        main_split.setSizes([420, 800])
        self.main_split = main_split  # 供「隐藏画面」按钮恢复比例用

        # --- 左侧：摄像头预览 ---
        cam_box = QtWidgets.QGroupBox("摄像头画面")
        cam_box.setStyleSheet("QGroupBox { font-size: 15px; font-weight: bold; }")
        cam_layout = QtWidgets.QVBoxLayout(cam_box)
        cam_layout.setContentsMargins(6, 6, 6, 6)
        # 当前判定页面指示：紧跟「摄像头画面」标题，显示 GUI 当前识别到的页面
        self.lbl_page_state = QtWidgets.QLabel("当前页面：-")
        self.lbl_page_state.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #27ae60; padding: 2px 4px;"
        )
        cam_layout.addWidget(self.lbl_page_state)
        self.lbl_camera = QtWidgets.QLabel("（未启动）")
        self.lbl_camera.setAlignment(QtCore.Qt.AlignCenter)
        # 正常比例区域（竖屏帧内部 KeepAspectRatio 居中，不拉伸变扁）
        self.lbl_camera.setMinimumSize(200, 160)
        self.lbl_camera.setStyleSheet(
            "background: #000; color: #555; border: 2px solid #333; border-radius: 4px;"
        )
        self.lbl_camera.setScaledContents(False)
        cam_layout.addWidget(self.lbl_camera, stretch=1)
        # 画面来源提示：一眼看出「这块显示的是机械臂摄像头还是业务取帧源」
        self.lbl_cam_src = QtWidgets.QLabel("画面来源：机械臂摄像头（启动中…）")
        self.lbl_cam_src.setStyleSheet("font-size: 12px; color: #888; padding: 2px 4px;")
        self._no_width_push(self.lbl_cam_src)
        cam_layout.addWidget(self.lbl_cam_src)
        # 截图留存：用机械臂摄像头拍全分辨率照片（人工操作期出问题留证）。
        # 与预览同源，所以「看到什么就拍到什么」，不会拍到别的取帧源。
        shot_row = QtWidgets.QHBoxLayout()
        self.btn_shot = QtWidgets.QPushButton("📷 截图留存")
        self.btn_shot.setToolTip(
            "用机械臂摄像头拍一张全分辨率照片，保存到 data/shots/（出问题时留证）"
        )
        self.btn_shot.clicked.connect(self.on_shot)
        self.lbl_shot = QtWidgets.QLabel("上次截图：-")
        self.lbl_shot.setStyleSheet("font-size: 12px; color: #888; padding: 2px 4px;")
        self._no_width_push(self.lbl_shot)  # 长文件名不得撑宽左面板（2026-09-13 实测）
        shot_row.addWidget(self.btn_shot)
        shot_row.addWidget(self.lbl_shot, stretch=1)
        cam_layout.addLayout(shot_row)
        self.cam_box = cam_box  # 供「隐藏画面」按钮切换可见性
        # 允许分割条把画面区拖到很窄：否则 QGroupBox 的最小宽度会把左面板锁死成
        # 「拖不窄」（长文本的最小宽度经 _no_width_push 已不参与计算）
        cam_box.setMinimumWidth(0)
        main_split.addWidget(cam_box)

        # --- 右侧：订单详情参数 + 操作按钮 ---
        right_panel = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        # 拼单补录：OCR 吨/方不一定准，客服与货主沟通确认后手动补录（覆盖 OCR），
        # 点"拼单"时用补录值扣减余量。放在详情页内容这一页（与详情一起看更顺手）。
        grp_override = QtWidgets.QGroupBox("拼单补录（以货主沟通为准，吨和方都要填）")
        ov_layout = QtWidgets.QHBoxLayout(grp_override)
        ov_layout.addWidget(QtWidgets.QLabel("吨:"))
        self.spin_ton_override = QtWidgets.QDoubleSpinBox()
        self.spin_ton_override.setRange(0, 999)
        self.spin_ton_override.setSingleStep(0.5)
        self.spin_ton_override.setDecimals(2)
        ov_layout.addWidget(self.spin_ton_override)
        ov_layout.addWidget(QtWidgets.QLabel("方:"))
        self.spin_m3_override = QtWidgets.QDoubleSpinBox()
        self.spin_m3_override.setRange(0, 9999)
        self.spin_m3_override.setSingleStep(0.5)
        self.spin_m3_override.setDecimals(2)
        ov_layout.addWidget(self.spin_m3_override)
        self.btn_apply_override = QtWidgets.QPushButton("应用补录")
        self.btn_apply_override.clicked.connect(self.on_apply_override)
        ov_layout.addWidget(self.btn_apply_override)
        self.lbl_override_status = QtWidgets.QLabel("（未补录）")
        self.lbl_override_status.setStyleSheet("color: #7f8c8d;")
        ov_layout.addWidget(self.lbl_override_status, stretch=1)

        # 订单详情参数区（含货物内容行 + 详情原文 OCR 框）
        detail_box = QtWidgets.QGroupBox("当前检测到的订单详情")
        detail_box.setStyleSheet(
            "QGroupBox { font-size: 15px; font-weight: bold; } QLabel { font-size: 18px; }"
        )
        detail_form_left = QtWidgets.QFormLayout()
        detail_form_left.setSpacing(10)
        detail_form_right = QtWidgets.QFormLayout()
        detail_form_right.setSpacing(10)
        self.detail_fields: dict = {}
        # 详情页精简（2026-09-11）：列表页已经有车长/车型/价格，详情页只保留核心
        # 信息：货物内容 / 公里数（计算单价用）/ 补全的吨方 / 单价 / 判定 / 原因
        detail_keys = [
            ("货物内容", "cargo", "-"),
            ("公里数(km)", "distance_km", "-"),
            ("载重(吨)", "weight_ton", "-"),
            ("容积(方)", "volume_m3", "-"),
            ("单价(元/km)", "unit_price", "-"),
            ("判定结果", "pass_", "-"),
            ("原因", "reason", "-"),
        ]
        for i, (label, key, default) in enumerate(detail_keys):
            lbl_val = QtWidgets.QLabel(str(default))
            lbl_val.setStyleSheet("font-weight: bold; font-size: 15px;")
            if key == "pass_":
                lbl_val.setText("-")
            self.detail_fields[key] = lbl_val
            # 前 6 个放左列，后 5 个放右列（两列布局，高度减半，窗口可缩更小）
            (detail_form_left if i < 6 else detail_form_right).addRow(label + "：", lbl_val)
        # 详情原文框已移除（节省 GUI 空间 + 视觉清爽）。字段已覆盖主要信息，
        # OCR 全文读取仍在 detail.py 内部进行（用于字段抽取）。
        dvbox = QtWidgets.QVBoxLayout(detail_box)
        form_hbox = QtWidgets.QHBoxLayout()
        form_hbox.addLayout(detail_form_left, stretch=1)
        form_hbox.addLayout(detail_form_right, stretch=1)
        dvbox.addLayout(form_hbox)
        right_layout.addWidget(grp_override)
        right_layout.addWidget(detail_box, stretch=1)

        # ---- 三个大操作按钮（状态机：运行中全灰不可点；命中合格单暂停时全蓝可点；
        #      点任一后该按钮变灰且三按钮全锁（一次性）；新一次暂停自动解锁） ----
        self._RADIO_BASE = (
            "font-size: 16px; font-weight: bold; padding: 10px 20px; "
            "min-height: 44px; border-radius: 6px; border: none;"
        )
        self._RADIO_BLUE = self._RADIO_BASE + " background: #1a6fb0; color: white;"
        self._RADIO_GRAY = self._RADIO_BASE + " background: #b8b8b8; color: #f5f5f5;"
        self._pause_token = None   # 暂停令牌：id(ctx.held_candidate)，新候选→解锁
        self._action_taken = False  # 本次暂停是否已点过三按钮之一（一次性锁定）
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(12)

        self.btn_continue_scan = QtWidgets.QPushButton("▶ 继续扫单")
        self.btn_continue_scan.setStyleSheet(self._RADIO_GRAY)
        self.btn_continue_scan.setEnabled(False)
        self.btn_continue_scan.clicked.connect(self.on_continue_scan)
        btn_row.addWidget(self.btn_continue_scan, stretch=1)

        self.btn_stop_change = QtWidgets.QPushButton("⏹ 结束换车")
        self.btn_stop_change.setStyleSheet(self._RADIO_GRAY)
        self.btn_stop_change.setEnabled(False)
        self.btn_stop_change.clicked.connect(self.on_stop_and_change)
        btn_row.addWidget(self.btn_stop_change, stretch=1)

        # 拼单=独立动作按钮（非开关）：命中合格单且暂停时点它=人工已接此单→扣减余量→继续扫单。
        # 模式开关在「参数」页（chk_pindan），关→本按钮只继续不扣减。
        self.btn_pindan = QtWidgets.QPushButton("⊕ 拼单")
        self.btn_pindan.setStyleSheet(self._RADIO_GRAY)
        self.btn_pindan.setEnabled(False)
        self.btn_pindan.clicked.connect(self.on_pindan)
        btn_row.addWidget(self.btn_pindan, stretch=1)

        right_layout.addLayout(btn_row)

        # 上次操作反馈：点完三个大按钮后在此显示「做了什么 + 时间」，
        # 避免按钮变灰后用户分不清到底点没点过（用户明确要求）。
        self.lbl_last_action = QtWidgets.QLabel("上次操作：-")
        self.lbl_last_action.setStyleSheet("font-size: 13px; color: #e67e22; padding: 2px 0;")
        right_layout.addWidget(self.lbl_last_action)

        main_split.addWidget(right_panel)
        main_split.setStretchFactor(0, 1)
        main_split.setStretchFactor(1, 2)

        # 画面区显示/隐藏（2026-09-13 用户要求）：放在画面区**正上方**，两个字、按钮做小。
        # ⚠ 必须放在分割条外面：若放进画面区内部，一收起按钮就跟着消失，没法再打开。
        cam_bar = QtWidgets.QHBoxLayout()
        cam_bar.setContentsMargins(0, 0, 0, 0)
        cam_bar.setSpacing(4)
        self.btn_toggle_cam = QtWidgets.QPushButton("隐藏")
        self.btn_toggle_cam.setToolTip("显示/隐藏左侧的摄像头画面区")
        self.btn_toggle_cam.setFixedWidth(56)
        self.btn_toggle_cam.setStyleSheet("padding: 1px 4px; font-size: 12px;")
        self.btn_toggle_cam.clicked.connect(self.on_toggle_camera)
        cam_bar.addWidget(self.btn_toggle_cam)
        cam_bar.addStretch(1)
        v.addLayout(cam_bar)

        v.addWidget(main_split, stretch=1)
        return w

    # ---- 候选订单 ----
    def _build_cand_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        self.cand_table = QtWidgets.QTableWidget(0, 9)
        self.cand_table.setHorizontalHeaderLabels(
            ["时间", "车长", "车型", "吨", "方", "价(元)", "距(km)", "单价", "判定"]
        )
        self.cand_table.horizontalHeader().setStretchLastSection(True)
        self.cand_table.setColumnWidth(0, 90)
        self.cand_table.setColumnWidth(8, 220)
        v.addWidget(self.cand_table, stretch=1)
        # 拼单补录区已移到「运行控制」页（详情显示那一页），与详情一起看更顺手。
        self.btn_cand_clear = QtWidgets.QPushButton("清空候选表")
        self.btn_cand_clear.clicked.connect(
            lambda: (self.cand_table.setRowCount(0), self._cand_rows.clear())
        )
        self.btn_resume_review = QtWidgets.QPushButton("继续找单（审核暂停中）")
        self.btn_resume_review.setEnabled(False)
        self.btn_resume_review.clicked.connect(self.on_resume_review)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.btn_cand_clear)
        row.addWidget(self.btn_resume_review)
        row.addStretch(1)
        v.addLayout(row)
        return w

    def on_apply_override(self) -> None:
        """把补录的吨/方写到当前暂停的候选单 detail，pindan 时用此值扣减余量。"""
        if self.task is None:
            return
        ctx = self.task.ctx
        if ctx.held_candidate is None:
            self.lbl_override_status.setText("⚠ 当前没有暂停的候选单")
            self.lbl_override_status.setStyleSheet("color: #e67e22;")
            return
        ctx.held_candidate.weight_ton = self.spin_ton_override.value()
        ctx.held_candidate.volume_m3 = self.spin_m3_override.value()
        ton, m3 = ctx.held_candidate.weight_ton, ctx.held_candidate.volume_m3
        self.lbl_override_status.setText(
            f"✓ 已补录 {ton:g}吨/{m3:g}方（拼单扣减用此值）"
        )
        self.lbl_override_status.setStyleSheet("color: #27ae60; font-weight: bold;")
        self.log_fn("info", f"补录吨/方：{ton:g}吨/{m3:g}方")

    # ---- 路线 ----
    def _build_route_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        box = QtWidgets.QGroupBox("路线（直接粘贴，支持 A/B/C/D 四形态）")
        bv = QtWidgets.QVBoxLayout(box)
        self.txt_routes = QtWidgets.QPlainTextEdit()
        self.txt_routes.setPlaceholderText(
            "A路线：苏州-广州\nB路线：苏州-广州、中山、揭阳\n"
            "C路线：苏州、无锡、扬州-广州\nD路线：苏州、无锡、扬州-广州、中山、揭阳"
        )
        self.txt_routes.setFixedHeight(90)
        row = QtWidgets.QHBoxLayout()
        self.btn_parse = QtWidgets.QPushButton("解析")
        self.btn_save_routes = QtWidgets.QPushButton("保存路线")
        self.btn_advance = QtWidgets.QPushButton("切下一条路线")
        self.btn_parse.clicked.connect(self.on_parse)
        self.btn_save_routes.clicked.connect(self.on_save_routes)
        self.btn_advance.clicked.connect(self.on_advance_route)
        row.addWidget(self.btn_parse)
        row.addWidget(self.btn_save_routes)
        row.addWidget(self.btn_advance)
        row.addStretch(1)
        self.lbl_routes = QtWidgets.QLabel("(未解析)")
        self.lbl_routes.setWordWrap(True)
        bv.addWidget(self.txt_routes)
        bv.addLayout(row)
        bv.addWidget(self.lbl_routes)
        v.addWidget(box)

        self.route_list = QtWidgets.QListWidget()
        v.addWidget(self.route_list, stretch=1)
        return w

    # ---- 参数 ----
    def _build_param_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(w)
        rules = self.store.section("rules")
        vehicle = rules.get("vehicle") or {}
        price = rules.get("price") or {}
        self.spin_price = QtWidgets.QDoubleSpinBox()
        self.spin_price.setRange(0, 999)
        self.spin_price.setSingleStep(0.5)
        self.spin_price.setValue(float(price.get("min_unit_price", 3.0) or 3.0))
        self.edit_len = QtWidgets.QLineEdit(",".join(vehicle.get("che_length") or []))
        self.edit_type = QtWidgets.QLineEdit(",".join(vehicle.get("che_type") or []))
        self.spin_ton = QtWidgets.QDoubleSpinBox()
        self.spin_ton.setRange(0, 999)
        self.spin_ton.setValue(float(vehicle.get("max_load_ton", 10) or 10))
        self.spin_m3 = QtWidgets.QDoubleSpinBox()
        self.spin_m3.setRange(0, 9999)
        self.spin_m3.setValue(float(vehicle.get("max_volume_m3", 22) or 22))
        self.edit_m3_by_len = QtWidgets.QLineEdit(
            ",".join(
                f"{k}:{v}"
                for k, v in (vehicle.get("max_volume_by_che_length") or {}).items()
                if not str(k).startswith("_")
            )
        )
        self.edit_m3_by_len.setPlaceholderText("可选，如 4.2:22,6.8:42,9.6:55（多辆车才填）")
        self.chk_pindan = QtWidgets.QCheckBox("启用拼单（按余量扣减）")
        self.chk_pindan.setChecked(bool((rules.get("pindan") or {}).get("enabled", True)))
        self.btn_save_rules = QtWidgets.QPushButton("保存参数")
        self.btn_save_rules.clicked.connect(self.on_save_rules)

        form.addRow("最小单价（元/公里）", self.spin_price)
        form.addRow("车长（逗号分隔）", self.edit_len)
        form.addRow("车型（逗号分隔）", self.edit_type)
        form.addRow("总载重（吨）", self.spin_ton)
        form.addRow("总容积（方）", self.spin_m3)
        form.addRow("按车长分档（方）", self.edit_m3_by_len)
        form.addRow("", self.chk_pindan)
        form.addRow("", self.btn_save_rules)
        return w

    # ---- 日志 ----
    def _build_log_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        self.log_view = QtWidgets.QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background: #1b1f23; color: #d0d0d0; font-family: Consolas;")
        v.addWidget(self.log_view, stretch=1)
        return w

    # ---------------------------------------------------------------- 日志

    @QtCore.pyqtSlot(str, str)  # noqa: N802
    def _append_log(self, level: str, msg: str) -> None:
        """跨线程日志入队（合并连续相同项），由 _flush_log 定时批量刷新到控件。"""
        key = (level, msg)
        if self._log_queue and self._log_queue[-1][0] == key:
            self._log_queue[-1][1] += 1
        else:
            self._log_queue.append([key, 1])
        # 队列过长时丢弃最旧，避免极端情况下内存堆积
        while len(self._log_queue) > 500:
            self._log_queue.popleft()

    def _flush_log(self) -> None:
        """每 200ms 把队列日志批量写入控件，连续相同日志合并为「×N」。"""
        if not self._log_queue:
            return
        items = list(self._log_queue)
        self._log_queue.clear()
        parts = []
        for (level, msg), n in items:
            color = LEVEL_COLOR.get(level, "#d0d0d0")
            text = msg if n == 1 else f"{msg} （×{n}）"
            parts.append(f'<span style="color:{color}">[{level}] {text}</span>')
        self.log_view.append("<br>".join(parts))
        if self.log_view.document().blockCount() > 4000:
            cursor = self.log_view.textCursor()
            cursor.movePosition(QtGui.QTextCursor.Start)
            cursor.select(QtGui.QTextCursor.Line)
            cursor.removeSelectedText()

    # ---------------------------------------------------------------- 路线

    def _load_routes_into_ui(self) -> None:
        try:
            plan = load_plan(self.store)
            if plan.routes:
                self.txt_routes.setPlainText("\n".join(r.describe() for r in plan.routes))
                self.lbl_routes.setText(plan.describe().replace("\n", " | "))
                self._fill_route_list(plan)
        except Exception as exc:
            self.lbl_routes.setText(f"(读取路线失败: {exc})")

    def _fill_route_list(self, plan: object) -> None:
        self.route_list.clear()
        for i, r in enumerate(plan.routes):
            item = QtWidgets.QListWidgetItem(r.describe())
            if i == plan.current_index:
                item.setForeground(QtGui.QColor("#27ae60"))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.route_list.addItem(item)

    def _open_run_log(self):
        """打开本轮运行日志文件 data/logs/run_YYYYmmdd_HHMMSS.log（追加写）。"""
        try:
            from pathlib import Path
            import time as _t

            log_dir = DATA_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_path = log_dir / f"run_{_t.strftime('%Y%m%d_%H%M%S')}.log"
            return open(self._log_path, "a", encoding="utf-8")
        except Exception:  # noqa: BLE001 日志落盘失败绝不能影响主程序运行
            self._log_path = None
            return None

    def _log_both(self, level: str, msg: str) -> None:
        """日志同时进 GUI 与本轮日志文件（每条 flush，崩溃/强停也不丢）。"""
        try:
            self.bridge.appended.emit(level, msg)
        except Exception:  # noqa: BLE001
            pass
        fp = getattr(self, "_log_fp", None)
        if fp is None:
            return
        try:
            import time as _t

            fp.write(f"{_t.strftime('%H:%M:%S')} [{level}] {msg}\n")
            fp.flush()
        except Exception:  # noqa: BLE001
            pass

    def _show_error(self, title: str, exc: Exception) -> None:
        """把任何异常以弹窗 + 日志形式暴露，绝不静默闪退。"""
        import traceback as _tb

        text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
        self.log_fn("error", f"{title}：{exc}")
        QtWidgets.QMessageBox.critical(self, title, f"{type(exc).__name__}：{exc}")

    def on_parse(self) -> None:
        # RegionStore 构造 + 整条解析链路都在 try 内：任一非预期异常都弹窗，
        # 不再让 PyQt 槽函数未捕获异常直接 abort 掉整个进程。
        try:
            regions = RegionStore(self.store)
            plan = parse_routes_text(self.txt_routes.toPlainText(), regions)
        except RoutePlanError as exc:
            QtWidgets.QMessageBox.warning(self, "路线解析失败", str(exc))
            return
        except Exception as exc:
            self._show_error("路线解析异常", exc)
            return
        warns = validate_plan(plan, regions)
        self._parsed = plan
        text = plan.describe().replace("\n", " | ")
        if warns:
            text += "\n" + "\n".join(warns)
        self.lbl_routes.setText(text)
        self._fill_route_list(plan)
        self.log_fn("info", "路线解析成功")
        # 城市不在行政区字典属于「静默失效」：解析成功但运行时 App 选不到，
        # 这正是「路线设不上」最常见的根因，必须明确提示用户。
        if warns:
            QtWidgets.QMessageBox.warning(
                self,
                "路线解析成功（有告警）",
                "已解析成功，但以下城市不在行政区字典，运行时 App 可能选不到：\n\n"
                + "\n".join(warns),
            )

    def on_save_routes(self) -> None:
        if not hasattr(self, "_parsed"):
            self.on_parse()
        if not hasattr(self, "_parsed"):
            return
        data = dict(self.store.section("routes"))
        data.update(self._parsed.as_dict())
        self.store.save_section("routes", data)
        self._fill_route_list(self._parsed)
        self.log_fn("info", "路线已保存到 config/routes.json（运行中自动热重载）")

    def on_advance_route(self) -> None:
        """手动切到下一条路线并写回 routes.json（与 main.py 定时收工行为一致）。"""
        if self.task is not None and self.task.running:
            QtWidgets.QMessageBox.information(self, "提示", "运行中不能切换路线，请先停止")
            return
        plan = load_plan(self.store)
        if not plan.routes:
            return
        if plan.advance() is None:
            plan.reset()
            self.log_fn("info", "已是最后一条，回到第 1 条")
        else:
            self.log_fn("info", f"已切到：{plan.current.describe()}")
        data = dict(self.store.section("routes"))
        data.update(plan.as_dict())
        self.store.save_section("routes", data)
        self._fill_route_list(plan)

    # ---------------------------------------------------------------- 参数

    def on_save_rules(self) -> None:
        data = dict(self.store.section("rules"))
        data.setdefault("vehicle", {})
        data["vehicle"]["che_length"] = [x.strip() for x in self.edit_len.text().split(",") if x.strip()]
        data["vehicle"]["che_type"] = [x.strip() for x in self.edit_type.text().split(",") if x.strip()]
        data["vehicle"]["max_load_ton"] = self.spin_ton.value()
        data["vehicle"]["max_volume_m3"] = self.spin_m3.value()
        by_len = {}
        for part in self.edit_m3_by_len.text().split(","):
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            try:
                by_len[k.strip()] = float(v.strip())
            except ValueError:
                continue
        data["vehicle"]["max_volume_by_che_length"] = by_len
        data.setdefault("price", {})["min_unit_price"] = self.spin_price.value()
        data.setdefault("pindan", {})["enabled"] = self.chk_pindan.isChecked()
        self.store.save_section("rules", data)
        self._apply_rules_to_task()
        self.log_fn("info", "参数已保存并热重载")

    def _apply_rules_to_task(self) -> None:
        """让运行中的任务用上新参数（不重启、不打断当前动作）。"""
        if self.task is None:
            return
        self.task.rules = RuleEngine(self.store)
        self.task.capacity = Capacity.from_store(self.store)
        self.task.ctx.rules = self.task.rules
        self.task.ctx.capacity = self.task.capacity

    # ---------------------------------------------------------------- 运行

    def on_check(self) -> None:
        from core.devices.arm import RobotArm
        from core.devices.frame_source import create_frame_source

        src_name = self.store.get("vision", "capture.source", "camera")
        source = create_frame_source(self.store, log=self.log_fn)
        ok_src = source.open()
        source.close()
        arm = RobotArm(self.store, log=self.log_fn)
        ok_arm = arm.open()
        if ok_arm:
            arm.back_to_home()
            arm.close()
        msg = f"自检：取帧源({src_name})={'OK' if ok_src else 'FAIL'} 机械臂={'OK' if ok_arm else 'FAIL'}"
        self.log_fn("info", msg)
        self.statusBar().showMessage(msg)

    def on_start(self) -> None:
        """开始：从桌面启动，先设置路线（强制 setup_first=True）。

        2026-09-12 起不再受复选框影响：点「开始」就是要从头跑、必须先设路线，
        否则扫的是残留的「全国货源」列表（出发地不是目标路线，白扫）。
        「继续扫单」才是从当前页续跑（setup_first=False）的唯一入口。
        """
        self._start_run(setup_first=True, label="启动")

    def _start_run(self, setup_first: bool, label: str = "启动") -> None:
        """统一的启动/恢复入口：开设备 + 起后台工作线程。

        setup_first=True：先设置路线（首启/换路线后从桌面开始，会重进 App 设路线）；
        setup_first=False：从当前页面直接续跑（监护急停/人工停止后用「继续扫单」恢复，
        不重进 App、不重设路线，手机停在哪就从哪接着扫）。
        """
        if getattr(self, "_booting", False):
            self.log_fn("info", f"【{label}】设备正在打开，请稍候")
            return
        if self.task is not None and self.task.running:
            self.log_fn("info", f"【{label}】当前已在运行")
            return
        self._stop_reason_shown = ""
        self.lbl_state.setStyleSheet("")
        from core.flow.task import RobotTask

        self.store.reload_if_changed()
        self.task = RobotTask(self.store, log=self.log_fn)
        self.task.ctx.plan = self._parsed if hasattr(self, "_parsed") else self.task.ctx.plan
        # 设备打开（重启服务 / 连机械臂 / 开摄像头 / 归位）很慢，必须放到后台线程，
        # 否则会阻塞主线程事件循环，表现为「点开始卡死、半天没反应」。
        self.lbl_state.setText(f"{label}中…（开设备 / 重启服务 / 归位）")
        self.lbl_state.setStyleSheet("font-weight: bold; color: #e67e22;")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.lbl_camera.setText("连接中…")
        self.pb_boot.setValue(0)
        self.pb_boot.show()
        self.statusBar().showMessage("正在打开设备，请稍候…")
        self._setup_first = setup_first
        self._booting = True
        threading.Thread(target=self._boot, daemon=True).start()

    def _boot(self) -> None:
        """后台线程：分步打开设备，进度通过信号回主线程显示；完成后启动工作线程。"""
        def on_step(p: int, m: str) -> None:
            self.boot_progress.emit(p, m)
        try:
            ok = self.task.open(on_step=on_step)
        except Exception as exc:  # noqa: BLE001
            self.boot_finished.emit(False, f"设备打开异常：{exc}")
            return
        if ok:
            self.boot_finished.emit(True, "")
        else:
            self.boot_finished.emit(False, "设备打开失败，未启动")

    @QtCore.pyqtSlot(int, str)
    def _on_boot_progress(self, p: int, msg: str) -> None:
        self.pb_boot.show()
        self.pb_boot.setValue(p)
        self.lbl_state.setText(msg)
        self.lbl_state.setStyleSheet("font-weight: bold; color: #e67e22;")
        self.statusBar().showMessage(msg)

    @QtCore.pyqtSlot(bool, str)
    def _on_boot_finished(self, ok: bool, msg: str) -> None:
        self._booting = False  # 设备打开结束（成功/失败都复位，允许再次点）
        if not ok:
            self.log_fn("error", msg or "设备打开失败，未启动")
            self.task = None
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.lbl_state.setText("未启动")
            self.lbl_state.setStyleSheet("")
            self.lbl_camera.setText("（未启动）")
            self.pb_boot.hide()
            self.statusBar().showMessage("设备打开失败")
            return
        # 设备就绪：启动后台工作线程（其内部 2s 预热不卡界面）
        self.pb_boot.setValue(100)
        self.pb_boot.hide()
        # setup_first 由 _start_run 决定：启动=True（重设路线）；继续扫单=False（从当前页续跑）
        self._thread = self.task.start(setup_first=self._setup_first)
        self.btn_stop.setEnabled(True)
        self.lbl_state.setText("运行中")
        self.lbl_state.setStyleSheet("")
        self.statusBar().showMessage("运行中")
        self._unlock_radio()  # 新开一轮：解锁单选组，恢复浅底待选
        self.log_fn("info", "已启动" if self._setup_first else "已恢复运行（从当前页面续跑，未重进 App / 未重设路线）")

    def on_stop(self) -> None:
        if self.task is None:
            return
        self._stop_reason_shown = ""
        self.lbl_state.setStyleSheet("")
        self.task.stop()
        # 循环 join 直到工作线程真正死亡（上限 ~20s），避免关设备时线程仍在跑。
        # 城市选择内层循环已查 stop_event，stop 后约 1s 内退出；此处兜底确保死透再关设备。
        if self._thread is not None:
            for _ in range(40):
                if not self._thread.is_alive():
                    break
                self._thread.join(timeout=0.5)
        # 停止后先确认机械臂归位（用户 2026-09-07）：没归位先归位，避免笔尖遮挡/乱位。
        try:
            arm = getattr(self.task, "arm", None)
            if arm is None and hasattr(self.task, "ctx"):
                arm = getattr(self.task.ctx, "kit", None)
                arm = getattr(arm, "arm", None) if arm is not None else None
            if arm is not None:
                if hasattr(arm, "ensure_homed"):
                    ok = arm.ensure_homed()
                else:
                    arm.back_to_home()
                    ok = True
                self.log_fn("info", "停止后机械臂已归位" if ok else "停止后机械臂归位失败")
            else:
                self.log_fn("warning", "停止时未找到机械臂实例，跳过归位")
        except Exception as exc:
            self.log_fn("warning", f"停止后机械臂归位异常: {exc}")
        self.task.close()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_state.setText("已停止")
        self.lbl_camera.setText("（已停止）")
        self._refresh_detail_fields(None)
        self._pause_token = None
        self._action_taken = False
        self._update_action_buttons(running=False, paused=False, has_candidate=False)
        self.log_fn("info", f"已停止 | {self.task.summary()}")

    def on_resume_review(self) -> None:
        """审核模式（review_mode）下候选合格会停在详情页等人工确认。

        点此按钮 = 人工看过了、放行继续找单：清 pause_event 让主循环从
        wait_resume 醒来。下一轮重进同一详情页时 detail.py 检测到 held_fp
        相同会直接返回列表，不会重复暂停。stop() 同样能打断暂停。
        """
        if self.task is None:
            return
        ctx = self.task.ctx
        if not ctx.paused:
            self.log_fn("info", "当前未在审核暂停（无需继续找单）")
            return
        ctx.resume()
        self.log_fn("info", "人工确认：继续找单（已恢复运行）")

    # ---- 运行控制页操作按钮 ----

    def on_continue_scan(self) -> None:
        """继续扫单：

        * review 暂停中（候选合格等人工确认）→ 清 pause_event 直接恢复；
        * 已被停止（监护急停 / 人工停止）→ 重新开设备并从**当前页面续跑**
          （setup_first=False：不重进 App、不重设路线，手机停在哪就从哪接着扫），
          不再像「启动」那样从头来一遍。
        """
        if self.task is None:
            self.log_fn("info", "【继续扫单】当前无任务，请点「启动」从桌面开始")
            return
        ctx = self.task.ctx
        if ctx.paused:
            ctx.resume()
            self.log_fn("info", "【继续扫单】已恢复运行")
            self._select_radio(self.btn_continue_scan, "继续扫单")
            return
        # 非暂停：说明运行已被 stop（线程已死、设备已关）。从当前页面恢复，不重新初始化。
        if self.task.running:
            self.log_fn("info", "【继续扫单】当前仍在运行")
            return
        self.log_fn("info", "【继续扫单】从当前页面恢复运行（不重新初始化 App/路线）")
        self._start_run(setup_first=False, label="继续扫单")

    def on_stop_and_change(self) -> None:
        """结束换车：清空拼单余量（新车空载）+ 停止运行，等人工改路线/参数后重开。

        换车语义：上一辆车服务结束，下一辆车从空载重新开始。不 reset 余量的话，
        新司机会沿用旧车余量继续拼单（可能装不下还在抢）。reset 后 remaining=满载。
        """
        if self.task is None or not self.task.running:
            self.log_fn("info", "【结束换车】当前未运行")
            return
        cap = getattr(self.task, "capacity", None)
        if cap is not None:
            cap.reset()
            self.log_fn("info", f"【结束换车】已重置拼单余量（{cap.describe()}）")
        self.on_stop()
        self.log_fn("info", "【结束换车】已停止，请修改路线/参数后点「开始」")
        self._select_radio(self.btn_stop_change, "结束换车")

    def on_pindan(self) -> None:
        """拼单=动作按钮（非开关）：命中合格单且暂停时点它=人工已接此单。

        行为：设置审核意图 "pindan" → 恢复运行 → detail 恢复分支按当前单吨/方扣减余量
        （capacity.commit 内部联动缩放）→ 余量成为下一单筛选上限 → 继续扫单。
        拼单模式关（参数页 chk_pindan）则只继续、不扣减。
        非暂停/无可拼候选时：仅继续扫单（不扣减）。
        """
        if self.task is None:
            return
        ctx = self.task.ctx
        if not ctx.paused or ctx.held_candidate is None:
            self.log_fn("info", "【拼单】当前未暂停或无可拼候选，仅继续扫单")
            if ctx.paused:
                ctx.resume()
            self._select_radio(self.btn_pindan, "拼单（仅继续扫单）")
            return
        ton = self.spin_ton_override.value()
        m3 = self.spin_m3_override.value()
        # 1. 校验：吨和方都不能为 0（以货主沟通为准，缺一不可）
        if ton <= 0 or m3 <= 0:
            QtWidgets.QMessageBox.warning(
                self, "拼单补录",
                "请先补录载重（吨）和方数，两者都不能为 0（以货主沟通为准）。",
            )
            return
        # 2. 把补录值写进 held_candidate（detail 恢复分支用此值扣减余量）
        ctx.held_candidate.weight_ton = ton
        ctx.held_candidate.volume_m3 = m3
        # 3. 容量检查：超载则直接警告，不让弹"确认"（避免用户点确认后扣减成负）
        if not ctx.capacity.can_take(ton, m3):
            QtWidgets.QMessageBox.warning(
                self, "无法拼单",
                f"本单需要 {ton:.2f} 吨 / {m3:.2f} 方。\n"
                f"车辆余量仅 {ctx.capacity.ton_left:.2f} 吨 / {ctx.capacity.m3_left:.2f} 方，**装不下**。\n"
                f"请选「结束换车」或调整补录值后再点拼单。",
            )
            return
        # 4. 弹窗确认剩余余量（能装下时）
        rem_ton = ctx.capacity.ton_left - ton
        rem_m3 = ctx.capacity.m3_left - m3
        ret = QtWidgets.QMessageBox.question(
            self, "确认拼单",
            f"本单需要 {ton:.2f} 吨 / {m3:.2f} 方。\n"
            f"拼单后剩余载重 {rem_ton:.2f} 吨 / 容积 {rem_m3:.2f} 方。\n"
            f"确认继续扫单？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )
        if ret != QtWidgets.QMessageBox.Yes:
            self.log_fn("info", "【拼单】人工取消拼单")
            return
        # 5. 确认后：设置审核意图 + 恢复运行（detail 恢复分支扣减余量 + 继续扫单）
        ctx.set_review_action("pindan")
        ctx.resume()
        self._select_radio(self.btn_pindan, "拼单（已接此单·扣减余量·继续扫单）")

    def on_toggle_no_pause(self) -> None:
        """开启/关闭不停止测试模式（右上角勾选，即时生效，热重载）。"""
        checked = self.chk_no_pause.isChecked()
        data = dict(self.store.section("runtime"))
        data["no_pause_test"] = bool(checked)
        self.store.save_section("runtime", data)
        self.log_fn("info", f"不停止测试模式：{'开启' if checked else '关闭'}（详情合格不暂停、等5s返回）")

    def on_toggle_watchdog(self) -> None:
        """开启/关闭运行监护（右上角勾选，写配置 + 运行中即时生效）。

        监护实例在任务启动时按 runtime.watchdog 构建，运行中途改配置不会重建它，
        所以这里额外直接改实例的 enabled 开关，做到勾掉立刻就不再自动急停；
        下次启动则按配置重建（见 core/flow/watchdog.py::from_config）。
        """
        checked = self.chk_watchdog.isChecked()
        data = dict(self.store.section("runtime"))
        wd = dict(data.get("watchdog") or {})   # 保留 rules/action 等其它字段
        wd["enabled"] = bool(checked)
        data["watchdog"] = wd
        self.store.save_section("runtime", data)
        # 运行中即时生效
        wd_obj = None
        if self.task is not None:
            wd_obj = getattr(getattr(self.task, "ctx", None), "watchdog", None)
        if wd_obj is not None:
            wd_obj.enabled = bool(checked)
        self.log_fn(
            "info",
            f"运行监护：{'开启' if checked else '关闭'}"
            + ("（异常达阈值自动急停）" if checked else "（不再自动急停，需人工盯盘）"),
        )

    def _mark_action(self, action: str, btn=None) -> None:
        """记录最近一次大按钮操作（动作+时间），解决『点完不知道是否点过』。

        统一显示「✓ 已点：做了什么 · 时间」（绿色加粗、持久可见）；若传了 btn，
        点击后**立即禁用该按钮**（Qt 自动灰显=已点/不可再点），状态变化时由
        _update_action_buttons 恢复——双重防重复点击。另加 2.5s 绿色边框高亮。
        """
        ts = time.strftime("%H:%M:%S")
        self.lbl_last_action.setText(f"✓ 已点：{action} · {ts}")
        self.lbl_last_action.setStyleSheet(
            "font-size: 13px; color: #28a745; padding: 2px 0; font-weight: bold;"
        )
        if btn is not None:
            btn.setEnabled(False)  # 点击即禁用，防重复；状态变化时由 _update_action_buttons 恢复
            orig = btn.styleSheet()
            btn.setStyleSheet(orig + " border: 3px solid #28a745;")
            QtCore.QTimer.singleShot(2500, lambda o=orig: btn.setStyleSheet(o))

    def _emit_interval_stats(self, ctx) -> None:
        """每 interval_sec 在日志打一段『近N秒』计数器增量，供分析一段时间内的节奏。

        计数器是累计值，这里与上次快照求差得到增量；用户可据此判断扫描速度、
        命中率、抢单频率是否健康，进一步调参（如 swipe 节奏、判定阈值）。
        """
        if not hasattr(self, "_stats_prev"):
            self._stats_prev = {}
            self._stats_last_emit = 0.0
        stats_cfg = self.store.section("runtime").get("stats", {}) or {}
        interval = int(stats_cfg.get("interval_sec", 30))
        now = time.time()
        if now - self._stats_last_emit < interval:
            return
        self._stats_last_emit = now
        cur = {k: ctx.get(k) for k, _ in COUNTER_KEYS}
        delta = {k: cur.get(k, 0) - self._stats_prev.get(k, 0) for k in cur}
        self._stats_prev = cur
        parts = [f"{label}+{d}" for k, label in COUNTER_KEYS if (d := delta.get(k, 0))]
        if not parts:
            return
        self.log_fn("info", f"[运行统计 近{interval}s] " + "  ".join(parts))

    # ---------------------------------------------------------------- 每秒刷新

    def _tick(self) -> None:
        """每秒：配置热重载检测 + 状态/统计/候选/摄像头/详情刷新。"""
        changed = self.store.reload_if_changed()
        if changed:
            self.log_fn("info", f"检测到配置变化：{', '.join(changed)}")
            self._apply_rules_to_task()
        if self.task is None:
            # 未运行时也清空详情区和按钮、复位页面/耗时显示
            self._refresh_detail_fields(None)
            self._update_action_buttons(running=False, paused=False, has_candidate=False)
            self.lbl_page_state.setText("当前页面：-")
            for lab in self._timing_labels.values():
                lab.setText("-")
            return
        ctx = self.task.ctx

        # 运行监护急停：第一时间把「为什么停」顶到状态栏 + 监护标签
        reason = getattr(ctx, "stop_reason", "") or ""
        if reason and reason != self._stop_reason_shown:
            self._stop_reason_shown = reason
            self.lbl_state.setText(f"已停止（运行监护）| {reason}")
            self.lbl_state.setStyleSheet("font-weight: bold; color: #ff6b6b;")
            self.lbl_guard.setText(f"监护已触发：{reason}")
            self.lbl_guard.setStyleSheet("font-weight: bold; color: #ff6b6b;")
            self.log_fn("error", f"运行监护已停止运行：{reason}")
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self._update_action_buttons(running=False, paused=False, has_candidate=False)
        elif not reason:
            self.lbl_guard.setText("监护：未触发")
            self.lbl_guard.setStyleSheet("font-weight: bold; color: #27ae60;")

        # 实时统计
        for key, lab in self._counter_labels.items():
            lab.setText(str(ctx.get(key)))
        self.lbl_route.setText(f"当前路线：{ctx.current_route() or '-'}")
        self.lbl_cap.setText(f"容量：{self.task.capacity.describe()}")

        # 运行统计（一段时间增量）：每 interval_sec 打一段『近N秒』计数器增量
        self._emit_interval_stats(ctx)

        # 当前判定页面 + 耗时明细（每轮刷新）
        page = ctx.last_state or "-"
        self.lbl_page_state.setText(f"当前页面：{page}")
        for key, lab in self._timing_labels.items():
            ms = ctx.timings.get(key)
            lab.setText(f"{ms:.0f}ms" if ms is not None else "-")

        # 摄像头预览由独立 _preview_timer（25fps）实时刷新，不在此每秒刷

        # 审核模式暂停：高亮提示并放开「继续找单」按钮（stop_event 优先）
        if ctx.paused and not reason:
            self.btn_resume_review.setEnabled(True)
            self.lbl_state.setText("审核暂停：候选合格，请操作")
            self.lbl_state.setStyleSheet("font-weight: bold; color: #e67e22;")
        else:
            self.btn_resume_review.setEnabled(False)

        # 候选订单累积
        c = ctx.candidate
        if c is not None:
            sig = "|".join(
                str(c.get(k))
                for k in ("che_len", "che_type", "tonnage", "price", "distance_km", "unit_price", "pass_", "reason")
            )
            if sig != self._last_cand_sig:
                self._last_cand_sig = sig
                self._append_candidate(c)
        # 详情区：停在详情页（含审核暂停）时展示候选；一旦离开详情页（返回列表/
        # 未知页）立即清空，避免残留上一条详情误导（用户要求：从详情返回后先清空）。
        in_detail = ctx.last_state == "detail"
        self._refresh_detail_fields(c if (in_detail or ctx.paused) else None)
        # 三按钮状态机：运行中全灰；命中合格单暂停时全蓝；点任一后锁灰（暂停令牌控制）。
        # has_candidate 以「当前待人工决策的合格单」(held_candidate) 为准，而非 ctx.candidate
        # （ctx.candidate 在离开详情页前仍残留，会误开放按钮）。
        running_now = self.task.running and not bool(reason)
        has_candidate = ctx.held_candidate is not None
        if ctx.paused and has_candidate:
            tok = id(ctx.held_candidate)
            if tok != self._pause_token:
                self._pause_token = tok
                self._action_taken = False
                # 新候选：补录框默认填当前订单 OCR 吨/方（客服可覆盖）
                self.spin_ton_override.setValue(ctx.held_candidate.weight_ton or 0.0)
                self.spin_m3_override.setValue(ctx.held_candidate.volume_m3 or 0.0)
        self._update_action_buttons(
            running=running_now,
            paused=bool(ctx.paused),
            has_candidate=has_candidate,
        )

    # ---- 机械臂摄像头：打开 / 取帧 / 截图 ----

    def _open_arm_camera(self) -> None:
        """后台打开机械臂摄像头（GUI 预览与截图专用）。

        为什么 GUI 要自持一个实例：业务识别走 iMouse 投屏（HTTP），物理摄像头是空着的；
        人工操作期更需要看「机械臂摄像头实景」，于是让 GUI 独占它做预览/拍照，两者互不
        干扰。若业务本身配置成 camera 取帧，则不重复打开（会抢设备），改为复用任务取帧。
        打开失败一律降级处理、绝不抛异常——没有摄像头也必须能打开 GUI。
        """
        self._arm_cam_state = "opening"
        src = str(self.store.get("vision", "capture.source", "imouse") or "imouse").lower()
        if src == "camera":
            self._arm_cam_use_task = True
            self._arm_cam_state = "use_task"
            self.log_fn("info", "业务取帧源=camera，画面区复用业务取帧（不重复占用摄像头）")
            return
        try:
            from core.devices.camera import Camera

            # native=True：预览/截图保持摄像头原始比例（机械臂摄像头是横屏 640x480，
            # 套手机竖屏 375x812 会被强行拉成细长条，2026-09-13 实测）
            cam = Camera(self.store, log=self.log_fn, native=True)
            if cam.open():
                self._arm_cam = cam
                self._arm_cam_state = "ready"
                w, h = cam.actual_size
                self.log_fn("info", f"机械臂摄像头已打开 {w}x{h}（画面区预览/截图用）")
                return
            cam.close()
        except Exception as exc:  # noqa: BLE001 没摄像头也不能影响 GUI 使用
            self.log_fn("warning", f"机械臂摄像头打开异常：{exc}")
        self._arm_cam_state = "failed"
        self.log_fn(
            "warning",
            "机械臂摄像头打开失败，画面区退回业务取帧（检查 hardware.robot.camera_index）",
        )

    # ---- iMouse 控制台：启动即就位（可选） ----

    def setup_imouse_console(self) -> None:
        """把 iMouse 控制台摆到左边、本 GUI 摆到右边（runtime.startup 配置开关）。

        为什么要它：真正能用鼠标键盘操作手机的是 iMouse 自带的控制台窗口（我们的
        HTTP 客户端没有点击/触摸接口），人工沟通核对时离不开它。以前要「手动开控制台
        → 调窗口大小 → 再开我们的软件」三步，现在启动即一步到位。
        后台线程执行：控制台冷启动要几秒，等窗口出现会卡住界面。
        """
        startup = self.store.section("runtime").get("startup", {}) or {}
        if not startup.get("open_imouse_console", False):
            return
        try:
            hwnd_self = int(self.winId())  # 必须在 GUI 线程取句柄
        except Exception:  # noqa: BLE001 取不到就只摆控制台
            hwnd_self = 0
        threading.Thread(
            target=self._imouse_console_worker,
            args=(
                str(startup.get("imouse_console_exe", "") or ""),
                hwnd_self,
                float(startup.get("console_width_ratio", 0.32) or 0.32),
                float(startup.get("wait_sec", 15) or 15),
            ),
            daemon=True,
            name="imouse-console",
        ).start()

    def _imouse_console_worker(self, exe: str, hwnd_self: int,
                               ratio: float, wait_sec: float) -> None:
        """后台执行「拉起 + 并排」；任何失败只记日志，绝不影响 GUI。"""
        try:
            from core.devices.imouse_console import show_and_tile

            show_and_tile(exe, hwnd_self, ratio=ratio, wait_sec=wait_sec, log=self.log_fn)
        except Exception as exc:  # noqa: BLE001 摆窗口失败绝不能拖垮 GUI
            self.log_fn("warning", f"iMouse 控制台并排失败（忽略）：{exc}")

    def _task_frame(self):
        """业务取帧源的最新帧（没有运行中的任务时返回 None）。"""
        task = self.task
        if task is None or not getattr(task, "running", False) or not hasattr(task, "kit"):
            return None
        try:
            return task.kit.frame()
        except Exception:
            return None

    def _preview_frame(self, timeout: float = 0.01):
        """取当前预览/截图用的一帧：优先机械臂摄像头，不可用时退回业务取帧。

        「看到什么就拍到什么」靠这里保证——预览与截图共用同一取帧入口。
        timeout：预览用默认 0.01（非阻塞，走采集线程缓存，取不到下帧再刷）；
        截图传大一点（0.5s）——刚打开摄像头时首帧还没进缓存，点早了会白点一次。
        """
        cam = self._arm_cam
        if cam is not None:
            try:
                return cam.read(timeout=timeout)  # 走采集线程缓存，非阻塞
            except Exception:
                return None
        if self._arm_cam_state in ("idle", "opening"):
            return None  # 还没开好，先别抢业务设备
        return self._task_frame()

    def _cam_src_text(self) -> str:
        """画面来源提示文案（让「现在显示的到底是哪个源」一眼可见）。"""
        text = {
            "opening": "画面来源：机械臂摄像头（启动中…）",
            "ready": "画面来源：机械臂摄像头（实时）",
            "use_task": "画面来源：业务取帧源（业务已占用摄像头）",
            "failed": "画面来源：业务取帧源（机械臂摄像头不可用）",
        }.get(self._arm_cam_state, "画面来源：-")
        if self._cam_warn:
            # 不写出来的话只能对着黑屏/雪花猜（2026-09-13 实测踩到过两次）。
            text += "　" + self._cam_warn
        return text

    def on_toggle_camera(self) -> None:
        """显示/隐藏左侧画面区（人工操作时把横向空间让给详情参数与日志）。"""
        self._cam_visible = not self._cam_visible
        self.cam_box.setVisible(self._cam_visible)
        if self._cam_visible:
            self.main_split.setSizes([420, 800])  # 恢复原比例（隐藏期间会被挤成 0）
        self.btn_toggle_cam.setText("隐藏" if self._cam_visible else "显示")
        self.log_fn("info", f"画面区已{'显示' if self._cam_visible else '隐藏'}")

    def on_shot(self) -> None:
        """截图留存：用机械臂摄像头拍一张**全分辨率**照片存到 data/shots/。

        人工操作期出问题（页面异常 / 识别错 / 机械臂姿态怪）时一键留证。
        存 raw 全分辨率（不是缩过的 fast 帧），便于事后放大看小字。
        """
        fs = self._preview_frame(timeout=0.5)  # 给首帧一点时间（刚打开时缓存还是空的）
        raw = getattr(fs, "raw", None) if fs is not None else None
        if raw is None:
            self.log_fn("warning", "截图失败：当前没有可用画面（机械臂摄像头未就绪？）")
            return
        try:
            from core.vision.imgio import imwrite_unicode

            out_dir = DATA_DIR / "shots"
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = out_dir / f"shot_{stamp}.png"
            n = 1
            while path.exists():  # 同一秒内连点多次不覆盖
                path = out_dir / f"shot_{stamp}_{n}.png"
                n += 1
            if not imwrite_unicode(path, raw):
                self.log_fn("warning", f"截图写入失败：{path}")
                return
            self._last_shot_path = str(path)
            h, w = raw.shape[0], raw.shape[1]
            # 只显示时间+尺寸（文件名里也是同一个时间），完整路径放 tooltip：
            # 长文本即便不撑宽面板也会被裁掉，短文本更耐看
            self.lbl_shot.setText(f"上次截图 {time.strftime('%H:%M:%S')} · {w}x{h}")
            self.lbl_shot.setToolTip(str(path))
            self.log_fn("info", f"截图已保存：{path}（{w}x{h}）")
            orig = self.btn_shot.styleSheet()
            self.btn_shot.setStyleSheet(
                "background: #27ae60; color: white; font-weight: bold; padding: 4px;"
            )
            QtCore.QTimer.singleShot(800, lambda o=orig: self.btn_shot.setStyleSheet(o))
        except Exception as exc:  # noqa: BLE001 截图失败只是少一张留证，不能崩
            self.log_fn("warning", f"截图异常：{exc}")

    def _refresh_camera_preview(self) -> None:
        """实时刷新画面区（25fps）。

        2026-09-13 用户要求：人工操作期画面区改为**直接显示机械臂摄像头**——iMouse
        桌面窗口本身就常开着看手机画面，GUI 这块空出来看机械臂实景更实用。
        取帧走 Camera 后台采集线程维护的缓存，非阻塞、不抢设备，故高频刷新也不卡。
        """
        if not self._cam_visible:
            return  # 画面区已隐藏：不取帧也不刷图，省 CPU
        want = self._cam_src_text()
        if self.lbl_cam_src.text() != want:
            self.lbl_cam_src.setText(want)
        fs = self._preview_frame()
        if fs is None or getattr(fs, "fast", None) is None:
            return
        try:
            import cv2
            # 每 2s 自检一次画面质量（全黑 / 噪点）：设备能打开却吐坏帧时（被别的程序
            # 占用、选错 index、USB 带宽不足），把原因写进来源提示，省得对着雪花猜
            # （2026-09-13 真实事故：回退逻辑选到 index 1 那个假源，预览一片噪点）。
            # 判据与 Camera.open() 选设备时的校验共用 frame_quality，避免两处不一致。
            if time.time() - self._dark_check_ts >= 2.0:
                self._dark_check_ts = time.time()
                try:
                    from core.devices.camera import frame_quality

                    _ok_frame, _why = frame_quality(fs.fast)
                except Exception:  # noqa: BLE001 自检失败不影响预览
                    _ok_frame, _why = True, ""
                self._cam_warn = (
                    "" if _ok_frame else f"⚠ 画面异常（{_why}）：可能选错设备或被其他程序占用摄像头"
                )
            frame = fs.fast.copy()
            # BGR → RGB（QImage 用 RGB）
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame.shape
            qimg = QtGui.QImage(frame.data, w, h, w * ch, QtGui.QImage.Format_RGB888)
            pixmap = QtGui.QPixmap.fromImage(qimg)
            # KeepAspectRatio + FastTransformation：保持竖屏比例（不拉伸变扁），缩放更快
            self.lbl_camera.setPixmap(pixmap.scaled(
                self.lbl_camera.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.FastTransformation
            ))
        except Exception:
            pass  # 取帧失败不刷屏

    def _refresh_detail_fields(self, c: Optional[dict]) -> None:
        """用候选订单数据填充详情参数区（含货物内容 + 详情原文）。"""
        if c is None:
            for key, lbl in self.detail_fields.items():
                lbl.setText("-")
                lbl.setStyleSheet("font-weight: bold; font-size: 20px; color: #888;")
            return

        mapping = {
            "che_len": str(c.get("che_len") or "-"),
            "che_type": str(c.get("che_type") or "-"),
            "cargo": str(c.get("cargo") or "-"),
            "weight_ton": str(c.get("weight_ton") if c.get("weight_ton") is not None else "-"),
            "volume_m3": str(c.get("volume_m3") if c.get("volume_m3") is not None else "-"),
            "price": str(c.get("price") if c.get("price") is not None else "-"),
            "distance_km": str(c.get("distance_km") if c.get("distance_km") is not None else "-"),
            "unit_price": str(c.get("unit_price") if c.get("unit_price") is not None else "-"),
            "pass_": ("合格 ✓" if c.get("pass_") else "淘汰 ✗"),
            "reason": str(c.get("reason") or "-"),
            "is_whole_vehicle": ("是（不可拼单）" if c.get("is_whole_vehicle") else "否"),
        }
        ok = bool(c.get("pass_"))
        for key, val in mapping.items():
            if key in self.detail_fields:
                self.detail_fields[key].setText(val)
                if key == "pass_":
                    color = "#28a745" if ok else "#dc3545"
                    self.detail_fields[key].setStyleSheet(
                        f"font-weight: bold; font-size: 28px; color: {color};"
                    )
                elif key == "reason" and ok:
                    self.detail_fields[key].setStyleSheet(
                        "font-weight: bold; font-size: 18px; color: #28a745;"
                    )
                elif key == "is_whole_vehicle":
                    # 整车单红字警示、普通单绿字，呼应颜色系统（functional 红/绿）
                    color = "#ff6b6b" if c.get("is_whole_vehicle") else "#27ae60"
                    self.detail_fields[key].setStyleSheet(
                        f"font-weight: bold; font-size: 22px; color: {color};"
                    )
                else:
                    self.detail_fields[key].setStyleSheet("font-weight: bold; font-size: 22px;")

    # ---- 三个大操作按钮：单选组（点中变绿、其余变灰、选定即锁，符合详情页暂停/停止后解锁）----

    def _radio_btns(self):
        return (self.btn_continue_scan, self.btn_stop_change, self.btn_pindan)

    def _select_radio(self, selected, action):
        """一次性锁定：点任一按钮→该按钮变灰、三按钮全锁（防重复点击）。

        审核意图由各回调自行设置（拼单设 "pindan"、继续/结束不扣减）；本函数只做
        视觉锁定 + 记录「做了什么」。颜色由 _update_action_buttons 按暂停令牌决定。
        """
        self._action_taken = True
        for btn in self._radio_btns():
            btn.setStyleSheet(self._RADIO_GRAY)
            btn.setEnabled(False)
        self._mark_action(action, selected)

    def _unlock_radio(self) -> None:
        """新一次合格候选暂停时重置令牌与锁定（不负责染色，由 _update_action_buttons 决定）。"""
        self._pause_token = None
        self._action_taken = False

    def _update_action_buttons(self, running: bool, paused: bool, has_candidate: bool) -> None:
        """三按钮状态机：
            * 设备打开中 / 已点过任一（_action_taken）→ 全灰禁用；
            * 未运行（已停止/未开始）→ 全灰禁用（决策无意义）；
            * 运行中且非暂停 → 全灰禁用（避免误触）；
            * 命中合格单且暂停 → 全蓝可点。
        """
        if getattr(self, "_booting", False) or getattr(self, "_action_taken", False):
            for btn in self._radio_btns():
                btn.setStyleSheet(self._RADIO_GRAY)
                btn.setEnabled(False)
            return
        if not running and not paused:
            for btn in self._radio_btns():
                btn.setStyleSheet(self._RADIO_GRAY)
                btn.setEnabled(False)
            return
        if paused and has_candidate:
            for btn in self._radio_btns():
                btn.setStyleSheet(self._RADIO_BLUE)
                btn.setEnabled(True)
            # 整车单提示（仅提示、不禁用）：拼单按钮仍蓝可点，点了走「仅继续不扣减」
            cand = self.task.ctx.candidate if self.task else None
            if cand and cand.get("is_whole_vehicle"):
                self.btn_pindan.setToolTip("（整车单不可拼：点击仅继续扫单，不扣减余量）")
            else:
                self.btn_pindan.setToolTip("人工已接此单：扣减余量后继续扫单")
            return
        # 运行中（非暂停）：全灰禁用
        for btn in self._radio_btns():
            btn.setStyleSheet(self._RADIO_GRAY)
            btn.setEnabled(False)

    def _append_candidate(self, c: dict) -> None:
        row = self.cand_table.rowCount()
        self.cand_table.insertRow(row)
        ok = bool(c.get("pass_"))
        vals = [
            time.strftime("%H:%M:%S"),
            str(c.get("che_len") or "-"),
            str(c.get("che_type") or "-"),
            str(c.get("weight_ton") if c.get("weight_ton") is not None else "-"),
            str(c.get("volume_m3") if c.get("volume_m3") is not None else "-"),
            str(c.get("price") if c.get("price") is not None else "-"),
            str(c.get("distance_km") if c.get("distance_km") is not None else "-"),
            str(c.get("unit_price") if c.get("unit_price") is not None else "-"),
            ("合格 ✓ " if ok else "淘汰 ✗ ") + str(c.get("reason") or ""),
        ]
        for col, val in enumerate(vals):
            item = QtWidgets.QTableWidgetItem(val)
            if col == 8 and ok:
                item.setForeground(QtGui.QColor("#27ae60"))
            self.cand_table.setItem(row, col, item)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.task is not None and self.task.running:
            self.on_stop()
        # 释放 GUI 自持的机械臂摄像头：否则退出前设备没放开，下次启动可能抢不到
        cam = self._arm_cam
        if cam is not None:
            try:
                cam.close()
            except Exception:  # noqa: BLE001 退出阶段绝不因释放失败而卡住
                pass
            self._arm_cam = None
        event.accept()


def _install_excepthook() -> None:
    """全局兜底：任何未捕获异常都写 data/crash.log 并弹窗，杜绝静默闪退。"""
    import traceback as _tb
    from pathlib import Path as _Path

    # frozen 下必须是 exe 同级 data/（用 __file__ 推导会写进临时解压目录，
    # 结果「出问题时恰恰看不到崩溃日志」）
    root = DATA_DIR
    old = sys.excepthook

    def hook(etype: type, evalue: BaseException, etb: object) -> None:
        text = "".join(_tb.format_exception(etype, evalue, etb))  # type: ignore[arg-type]
        try:
            log_dir = root
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "crash.log", "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass
        try:
            if QtWidgets.QApplication.instance() is not None:
                QtWidgets.QMessageBox.critical(
                    None, "未捕获异常（已记录到 data/crash.log）", text
                )
        except Exception:
            pass
        old(etype, evalue, etb)  # type: ignore[arg-type]

    sys.excepthook = hook


def _reboot_into_capable_python() -> None:
    """自举：若当前解释器加载不到 rapidocr，自动改用能加载的 Python 重启本程序。

    避免「明明装了包却检测不到」的降级体验——程序自己选对解释器，而不是傻降级。

    打包后（sys.frozen）**必须直接返回**：此时没有「另一个 Python」可切，依赖已随
    包带上；照旧逻辑会用系统 Python 重启并试图跑源码路径，结果是主界面一闪而过
    （2026-09-08 打包实测：启动日志只到 0/4，进程随即 code=0 退出）。
    导入失败在此只记录到 boot 日志，不阻断启动。
    """
    if getattr(sys, "frozen", False):
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except Exception as exc:  # noqa: BLE001 只记录不阻断
            try:
                _boot_log(
                    f"[GUI-BOOT] 打包环境 rapidocr 导入失败: {type(exc).__name__}: {exc}"
                )
            except Exception:  # noqa: BLE001
                pass
        return
    try:
        import rapidocr_onnxruntime  # noqa: F401 - 当前解释器可用，无需重启
        return
    except Exception:
        pass

    import os
    import shutil
    import subprocess

    # 防御：已被自举重启过一次则不再重启，避免同解释器下无限循环（例如 PyQt5
    # 与 onnxruntime 的 VC++ 运行库冲突，换同一 exe 必再次失败）。
    if os.environ.get("GUI_REBOOTED") == "1":
        return
    cur = os.path.realpath(sys.executable)

    candidates = [r"C:\Users\Lenovo\AppData\Local\Programs\Python\Python311\python.exe"]
    py = shutil.which("py")
    if py:
        candidates.append(f"{py} -3.11")  # py launcher + 版本号
    for cand in candidates:
        parts = cand.split()
        exe = parts[0]
        if not os.path.exists(exe):
            continue
        # 只重启到「不同的」解释器；同一 exe 必复现同样的失败，重启无意义。
        if os.path.realpath(exe) == cur:
            continue
        try:
            rc = subprocess.run(
                parts + ["-c", "import rapidocr_onnxruntime"],
                capture_output=True, timeout=30,
            ).returncode
        except Exception:
            continue
        if rc == 0:
            # 用能加载的解释器重启自己（继承参数，并切到项目根目录保证相对路径正确）
            os.environ["GUI_REBOOTED"] = "1"
            os.chdir(str(PROJECT_ROOT))
            os.execv(exe, parts + [__file__] + sys.argv[1:])
            return  # 理论上不会执行到


def _boot_log(msg: str) -> None:
    """启动进度写文件（不依赖 stdout/显示器），用于排查无窗口/无输出问题。

    必须写 exe 同级 data/（2026-09-13 打包适配）：用 __file__ 推导时，打包后会写进
    临时解压目录——偏偏「GUI 一闪而过」这类问题正是要靠它排查。
    """
    try:
        p = DATA_DIR / "gui_boot.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _ensure_config_present() -> None:
    """打包后首次运行：exe 同级缺 config/ 或 data/templates/ 时，从包内默认副本补齐。

    为什么要它：GUI 用 ConfigStore(strict=True)，`config/` 里少任何一个 json 都会
    「弹窗 + 抛异常」直接起不来；`data/templates/`（94 个模板图）少了则页面判定与
    卡片匹配全部失效。而这两块都是构建脚本拷到 exe 同级的，用户手工搬运时最容易漏
    ——漏了就是「双击打不开」或「识别全废」。这里做一层自愈，杜绝交付事故。
    已有内容的目录一律不动（可能是用户改过的）。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        import shutil

        base = Path(getattr(sys, "_MEIPASS", str(Path(sys.executable).parent)))
        for src_name, dst in (
            ("config_default", CONFIG_DIR),
            ("templates_default", DATA_DIR / "templates"),
        ):
            try:
                src = base / src_name
                if not src.is_dir():
                    continue
                if dst.is_dir() and any(dst.iterdir()):
                    continue  # 已有内容 → 绝不动
                shutil.copytree(src, dst, dirs_exist_ok=True)
                _boot_log(f"[GUI-BOOT] 已从包内默认副本补齐：{dst}")
            except OSError as exc:
                _boot_log(f"[GUI-BOOT] 补齐 {dst} 失败（继续启动）：{exc}")
    except Exception:  # noqa: BLE001 补齐失败不阻断（ConfigStore 会给出明确报错）
        pass


def main() -> int:
    # 打包后把工作目录钉到 exe 所在目录：UAC 提权启动 / 从快捷方式启动时 CWD 可能是
    # System32 等任意位置，任何残留的相对路径都会写错地方（兜底，2026-09-13 打包适配）。
    if getattr(sys, "frozen", False):
        try:
            os.chdir(str(PROJECT_ROOT))
        except OSError:
            pass
    _ensure_config_present()
    _boot_log(f"[GUI-BOOT] 0/4 进入 main(); sys.argv={sys.argv}")
    _reboot_into_capable_python()
    _install_excepthook()
    _boot_log("[GUI-BOOT] 1/4 QApplication 创建中...")
    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(GLOBAL_QSS)
    _boot_log("[GUI-BOOT] 2/4 MainWindow 构造中...")
    win = MainWindow()
    _boot_log("[GUI-BOOT] 3/4 show() 调用中...")
    win.show()
    win.raise_()
    win.activateWindow()
    # 启动即就位：拉起 iMouse 控制台并「控制台 | GUI」并排（runtime.startup 可关）
    win.setup_imouse_console()
    _boot_log("[GUI-BOOT] 4/4 进入事件循环（窗口应已弹出）")
    rc = app.exec_()
    _boot_log(f"[GUI-BOOT] 事件循环退出 rc={rc}")
    return rc
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())

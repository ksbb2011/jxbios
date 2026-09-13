# -*- mode: python ; coding: utf-8 -*-
"""运满满抢单机器人 —— PyInstaller onedir 构建规格（2026-09-13 新增）。

用法:
    py -3.11 -m PyInstaller installer\\build_exe.spec --noconfirm --clean
    （或双击 installer\\build_exe.bat —— 它会顺带把 config/ 与 data/templates/
      拷到产物里、建空的运行目录、生成 启动.bat）

产物:
    dist\\运满满抢单机器人\\运满满抢单机器人.exe        onedir，启动 1~2 秒（GUI）
    dist\\运满满抢单机器人\\校准工具.exe                控制台版：标定 / 探活工具菜单
    dist\\运满满抢单机器人\\_internal\\                 依赖与资源（勿动，两个 exe 共用一份）
    dist\\运满满抢单机器人\\config\\                   业务配置（exe 同级，可写）
    dist\\运满满抢单机器人\\data\\templates\\          模板图（exe 同级，只读）
    dist\\运满满抢单机器人\\data\\{logs,shots,traces,neg_frames}\\  运行产物（空）
    dist\\运满满抢单机器人\\tools\\                    标定工具脚本（可编辑，改完即生效）

别乱改的 6 条（每条都踩过）:
1. 入口 = gui/app.py 的 main()；console=False（纯 GUI，不弹黑窗）。
2. uac_admin=True：生成 requireAdministrator 清单。写 exe 同级的 config/ 与 data/
   （拼单余量、路线进度、日志、截图）以及访问 USB 摄像头都需要提权。
   注意：提权启动时 CWD 可能是 System32（不可信），gui/app.py 已在 frozen 下
   os.chdir 到 exe 目录兜底，核心路径也全部改成锚定 config_store 的基准。
3. 资产必须放 **exe 同级**、不是 _internal 里：core/config_store.py 的
   _project_root() 在 frozen 下取 Path(sys.executable).parent。GUI 用
   ConfigStore(strict=True)，exe 同级少 config/ 会直接弹窗退出。
   本 spec 另外把这两块打进 _internal 作为 config_default / templates_default，
   由 gui/app.py::_ensure_config_present() 首次运行发现缺失时自动补齐，防交付漏拷。
   （docs/交接文档 里写的「_internal/config」是旧项目布局，不要照抄。）
4. onnxruntime 必须早于 PyQt5 导入，否则 pybind11_state.pyd 报「DLL 初始化例程失败」。
   导入顺序由 gui/app.py 模块级代码保证；本 spec 只做静态收集，不改变导入时机。
5. rapidocr 的 ONNX 模型在 site-packages 内（项目里没有）→ 必须 collect_data_files；
   onnxruntime 的 pyd/dll → collect_dynamic_libs 收全。
6. upx=False：本机没装 UPX，且压缩后更易被杀软误报（体积不是主要矛盾）。
7. **包里只允许存在一套 VC++ 运行库**（2026-09-13 新增，见 Analysis 之后的去重代码）。
   PyQt5 的 wheel 自带一套旧版 MSVCP140 放在 PyQt5\Qt5\bin\ 下，冻结启动时它会先占坑，
   随后 onnxruntime_pybind11_state.pyd 报 [WinError 1114] 初始化例程失败 → OCR 全废。
   **别删那段过滤**，也别忘了从 System32 补齐一整套。
8. **两个 exe 共用同一份 _internal**（2026-09-13 新增，别改成两个 COLLECT / 两个目录）。
   校准工具是第二个 Analysis + 第二个 EXE，但只进同一个 COLLECT —— COLLECT 会按目标路径
   去重，所以 cv2(98MB)/PyQt5(75MB)/onnxruntime(35MB) 这一坨不会翻倍（整个包约 306MB）。
   若拆成两个目录，包会变成 550MB+，纯属浪费。
   另外 `tools\\` 是**放在 exe 同级的普通文件**（既不放 _internal，也不编译进 PYZ）：
   工具用 `Path(__file__).parent.parent` 当项目根，进了 _internal 就会去 `_internal\\config`
   找配置（全盘错位）；保持成普通文件还带来一个好处 —— 改标定判据**存盘即生效，不必重打包**。
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# PyInstaller 会把 SPECPATH 注入本命名空间 = 本 spec 所在目录（installer/）
ROOT = Path(SPECPATH).parent  # noqa: F821  由 PyInstaller 在 exec spec 时注入
sys.path.insert(0, str(ROOT))  # 供 collect_submodules 能 import core / gui

# ------------------------------------------------------------------ 数据 / 二进制
datas = []
# ① RapidOCR 的 ONNX 模型 + config.yaml（唯一不在项目内的运行时资产）
datas += collect_data_files("rapidocr_onnxruntime")
# ② 包内默认副本（供 gui/app.py 首次运行自愈；两块合计约 0.5MB）
if (ROOT / "config").is_dir():
    datas.append((str(ROOT / "config"), "config_default"))
if (ROOT / "data" / "templates").is_dir():
    datas.append((str(ROOT / "data" / "templates"), "templates_default"))

binaries = collect_dynamic_libs("onnxruntime")

hiddenimports = (
    collect_submodules("core")
    + collect_submodules("gui")
    + [
        "onnxruntime",
        "rapidocr_onnxruntime",
        "cv2",
        "numpy",
        "psutil",
        "PyQt5.QtCore",
        "PyQt5.QtGui",
        "PyQt5.QtWidgets",
    ]
)

# 本工程只用 rapidocr(onnxruntime) 做 OCR，这些大包一个都不要
excludes = [
    "paddleocr", "paddle", "paddlepaddle",
    "tensorflow", "torch", "torchvision",
    "matplotlib", "pandas", "scipy",
    "tkinter", "pytest", "IPython", "notebook", "jupyter",
]

a = Analysis(
    [str(ROOT / "gui" / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# ------------------------------------------------- 校准工具（第二个 exe）
# 与 GUI 打包在同一个目录、共用 _internal（见 docstring 约定 8）。
# ⚠️ tools\ 下的脚本**不**编译进来，而是由 build_exe.bat 拷成 exe 同级的普通文件，
# 入口用 runpy.run_path 跑它们 —— 所以这里**没有** collect_submodules("tools")：
# 一旦收进来，import tools.* 就会命中 PYZ 里的旧副本，"改判据存盘即生效"就废了。
# 这里只需要把「工具会 import 的东西」声明出来（core 全量 + 下面这些第三方库）。
a2 = Analysis(
    [str(ROOT / "installer" / "calib_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=(
        collect_submodules("core")
        + [
            "cv2",
            "numpy",
            "requests",
            "psutil",
            "onnxruntime",
            "rapidocr_onnxruntime",
        ]
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# --------------------------------------------------- VC++ 运行库唯一化（2026-09-13）
# 症状：打包后 OCR 完全失效、页面判态退化（不点红色「全国」、不清空筛选、乱滑、点不动）。
# 根因：包里同时存在**两套版本不同的 VC++ 运行库**——
#   _internal\msvcp140.dll 等（PyInstaller 收集，v14.50，正确）
#   _internal\PyQt5\Qt5\bin\MSVCP140.dll 等（PyQt5 wheel 自带，旧版且不完整）
# PyQt5 会把自己 Qt5\bin 加进 DLL 搜索路径，冻结环境启动顺序让旧那套先占坑，
# 之后 onnxruntime_pybind11_state.pyd 加载失败 → [WinError 1114] 动态链接库(DLL)
# 初始化例程失败 → rapidocr 导入失败 → OCR 降级 → detect 判态失去依据。
# 实证（同进程 ctypes 预载）：预载 PyQt5 那套 = LOAD_FAIL（与线上报错逐字一致）；
# 只预载 _internal 那套 = LOAD_OK。
# 处理：① 从 TOC 剔除 PyQt5\Qt5\bin 下的 4 个旧运行库；
#       ② 从 System32 补齐一整套到包根，保证目标机没装 VC++ redist 也能启动。
# 同名 DLL 只剩一套 → 冲突不可能再出现（这是一条硬约定，别取消）。
_VC_QT_NAMES = {"msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll"}
_VC_ALL_NAMES = (
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll",
    "msvcp140_codecvt_ids.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "concrt140.dll",
)


def _dest_norm(dest):
    """统一成小写正斜杠路径，便于跨写法的 dest 比较（PyInstaller 在 Windows 用反斜杠）。"""
    return str(dest).replace("\\", "/").lower()


def _unify_vc_runtime(analysis, tag: str) -> None:
    """把某个 Analysis 的 VC++ 运行库收敛成「包根唯一一套」（就地修改，无返回）。

    两个 exe 都要过一遍：它们的 `_internal` 是同一个 —— 主程序那边清干净了、
    校准工具这边又塞回来一份，就等于白干（冲突照旧）。
    """
    kept, dropped = [], []
    for entry in analysis.binaries:
        norm = _dest_norm(entry[0])
        # 只剔除「路径含 PyQt5 且文件名是那 4 个」的条目；Shapely.libs 下的同名文件带
        # 哈希后缀，文件名对不上，天然不会被误伤。
        if norm.rsplit("/", 1)[-1] in _VC_QT_NAMES and "pyqt5" in norm:
            dropped.append(entry[0])
        else:
            kept.append(entry)
    analysis.binaries = kept

    root_have = {
        _dest_norm(e[0]) for e in analysis.binaries if "/" not in _dest_norm(e[0])
    }
    sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    added = []
    for name in _VC_ALL_NAMES:
        if name in root_have:
            continue  # 已有（PyInstaller 收集的与 System32 同版本），不动它
        src = sys32 / name
        if not src.is_file():
            print(f"[spec] 警告：System32 缺少 {name}，跳过补齐")
            continue
        analysis.binaries.append((name, str(src), "BINARY"))
        added.append(f"{name}({src.stat().st_size // 1024}KB)")
    print(
        f"[spec][{tag}] 剔除 PyQt5 旧版 VC 库 {len(dropped)} 个 {sorted(dropped)}；"
        f"从 System32 补齐 {len(added)} 个 {added}"
    )


_unify_vc_runtime(a, "主程序")
_unify_vc_runtime(a2, "校准工具")

pyz = PYZ(a.pure)  # PyInstaller 6.x：没有 a.zipped_data，也不支持 cipher
pyz2 = PYZ(a2.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="运满满抢单机器人",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 纯 GUI，不弹黑窗
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,              # 将来有 .ico 时填绝对路径即可
    uac_admin=True,         # requireAdministrator（等价于旧项目的 build_v6_uac.manifest）
)

# 校准工具：必须是控制台程序（要打印标定过程、要 input() 交互），且**不提权**：
# 标定要反复跑，每次都弹 UAC 太折磨；唯一需要管理员的动作（重启 JxbService 释放串口）
# 由 tools/calib_common.py 自己 Start-Process -Verb RunAs 提权完成。
exe2 = EXE(
    pyz2,
    a2.scripts,
    [],
    exclude_binaries=True,
    name="校准工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # 控制台版：标定过程可见、可交互
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    uac_admin=False,
)

# 两个 exe 进同一个 COLLECT → 共用一份 _internal（COLLECT 按目标路径去重，
# 所以 a2 里重复的 cv2/PyQt5/onnxruntime 不会让包体积翻倍）。
coll = COLLECT(
    exe,
    exe2,
    a.binaries + a2.binaries,
    a.datas + a2.datas,     # PyInstaller 6.x：没有 a.zipfiles
    strip=False,
    upx=False,
    upx_exclude=[],
    name="运满满抢单机器人",
)

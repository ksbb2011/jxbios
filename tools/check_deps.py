"""运行依赖自检：核对 requirements.txt 声明的包能否正常 import。

设计目标：
  - 与 requirements.txt 单一来源保持一致，不维护第二份清单
  - 只做「能否 import」检测（最贴近程序真实运行结果，不会被版本号误伤）
  - 缺失时给出清晰修复命令；加 --install 可一键连带安装

用法：
  python tools/check_deps.py                只检测并打印缺失项
  python tools/check_deps.py --install      缺失项自动 pip 安装
  python tools/check_deps.py --quiet        只在缺失时打印（供其它命令内嵌调用）
"""
from __future__ import annotations

import argparse
import importlib
import re
import subprocess
import sys
from pathlib import Path

# 包名(requirements.txt 里写的) -> 实际 import 模块名（仅「不一致」的才需要列）
IMPORT_MAP = {
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "pyyaml": "yaml",
    "PyYAML": "yaml",
    "Pillow": "PIL",
    "rapidocr-onnxruntime": "rapidocr_onnxruntime",
    "pyclipper": "pyclipper",
    "Shapely": "shapely",
    "PyQt5": "PyQt5",
    "PyQt5-sip": "PyQt5.sip",
}

_REQ_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*(.*)$")


def _import_name(pkg: str) -> str:
    return IMPORT_MAP.get(pkg, pkg.replace("-", "_").split("[")[0])


def _parse_requirements() -> list[tuple[str, str]]:
    req_path = Path(__file__).resolve().parent.parent / "requirements.txt"
    if not req_path.exists():
        return []
    out: list[tuple[str, str]] = []
    for raw in req_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _REQ_RE.match(line)
        if not m:
            continue
        out.append((m.group(1), m.group(2).strip()))
    return out


# 必须先 import 的包（顺序敏感，与 gui/app.py 顶部是同一条约束）：
# PyQt5 的 wheel 自带一套旧版 MSVCP140.dll，它一旦先被加载，onnxruntime 的
# pybind11_state.pyd 就报 [WinError 1114]「初始化例程失败」—— 于是"装好了却报缺失"。
# 2026-09-13 实测：本清单按文件顺序 import 时，先 import PyQt5 -> onnxruntime 必然失败；
# 把 onnxruntime 提到最前 -> 全部 OK。这里显式提前，免得自检结论被书写顺序左右。
IMPORT_FIRST = ("onnxruntime",)


def check_deps(install: bool = False, quiet: bool = False) -> int:
    """返回 0=全部就绪；1=有缺失。"""
    items = _parse_requirements()
    if not items:
        if not quiet:
            print("[依赖检查] 未找到 requirements.txt，跳过")
        return 0
    items.sort(key=lambda it: 0 if it[0].lower() in IMPORT_FIRST else 1)  # 稳定排序

    missing: list[tuple[str, str]] = []
    for pkg, spec in items:
        try:
            importlib.import_module(_import_name(pkg))
        except Exception as exc:  # noqa: BLE001 - 任何导入失败都算缺失
            missing.append((pkg, f"{type(exc).__name__}: {exc}"))

    if not missing:
        if not quiet:
            print("[依赖检查] 全部依赖就绪 OK")
        return 0

    print(f"[依赖检查] 缺失 {len(missing)} 个依赖：")
    for pkg, err in missing:
        print(f"  - {pkg}  无法导入：{err}")
    print(f"  运行解释器：{sys.executable}")
    print("  修复方法（务必用「启动本程序的同一个 Python」执行）：")
    print(f"    {sys.executable} -m pip install -r requirements.txt")

    if install:
        print("[依赖检查] 正在自动安装缺失依赖 ...")
        frag = "\n".join(f"{pkg}{spec}" for pkg, spec in items
                         if pkg in {m[0] for m in missing})
        tmp = Path(__file__).resolve().parent / "_missing_reqs.txt"
        try:
            tmp.write_text(frag + "\n", encoding="utf-8")
            rc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(tmp)]
            ).returncode
        finally:
            tmp.unlink(missing_ok=True)
        if rc != 0:
            print("[依赖检查] 自动安装失败（可能无网络或需管理员权限），请按上面命令手动安装")
            return 1
        print("[依赖检查] 安装完成，请重新运行本命令确认")

    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="运行依赖自检")
    ap.add_argument("--install", action="store_true", help="缺失依赖时自动 pip 安装")
    ap.add_argument("--quiet", action="store_true", help="只在缺失时输出")
    args = ap.parse_args()
    return check_deps(install=args.install, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())

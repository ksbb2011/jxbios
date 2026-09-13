"""项目进度备份工具。

把当前可版本化的源码与配置打包成带时间戳的目录，存到 backups/。
只备份文本类资产（config/core/tools/docs + 根 md），不备份 data/templates 下大量 png
（模板图片明天重采，且体积大）。运行：
    py -3.11 tools/backup_project.py
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TS = time.strftime("%Y%m%d_%H%M%S")
DST = ROOT / "backups" / f"backup_{TS}"


def _copy(item: str) -> None:
    src = ROOT / item
    if not src.exists():
        return
    if src.is_dir():
        shutil.copytree(src, DST / item)
    else:
        shutil.copy2(src, DST / item)


def main() -> None:
    DST.mkdir(parents=True, exist_ok=True)
    for d in ("config", "core", "tools", "docs"):
        _copy(d)
    for md in ROOT.glob("*.md"):
        shutil.copy2(md, DST / md.name)
    # 根目录关键 bat
    for bat in ROOT.glob("*.bat"):
        shutil.copy2(bat, DST / bat.name)
    size = sum(f.stat().st_size for f in DST.rglob("*") if f.is_file())
    print(f"已备份到: {DST}")
    print(f"包含: config/ core/ tools/ docs/ + 根 md/bat  | 共 {size//1024} KB")


if __name__ == "__main__":
    main()

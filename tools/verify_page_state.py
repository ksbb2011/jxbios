"""页面判定 dry-run 验证：截一帧，跑 PageDetector，打印判定结果 + 关键模板诊断。

用法（iMouse 投屏服务需运行中）：
    cd f:\JXBproject\robot_order_picker_imouse
    py -3.11 tools/verify_page_state.py

停在某个页面观察输出：
    列表页（顶部已展开，能看到找货记录/司机课堂）
        -> state=order_list  top_visible=True  find_records+driver_school 命中
    列表页（顶部隐藏）
        -> 需 source_red/back_to_top 模板已重采；否则仍可能 unknown（已知缺口）
    详情页 -> detail
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_store import ConfigStore
from core.vision.matcher import TemplateMatcher
from core.vision.page_state import PageDetector


def _log(level: str, msg: str) -> None:
    if level in ("warning", "error"):
        print(f"  [matcher:{level}] {msg}")


def main() -> None:
    store = ConfigStore()
    from core.devices.imouse_source import ImouseFrameSource

    src = ImouseFrameSource(store)
    if not src.open():
        print("ERROR: 取帧源打开失败（iMouse 服务是否在运行？）")
        return
    fs = src.read()
    src.close()
    if fs is None or fs.fast is None:
        print("ERROR: 取帧失败")
        return

    frame = fs.fast
    print(f"帧尺寸: {frame.shape[1]}x{frame.shape[0]}")

    matcher = TemplateMatcher(store, log=_log)
    det = PageDetector(store, matcher)
    res = det.detect(frame)

    print("=" * 56)
    print(f"判定状态  : {res.state.value}")
    print(f"顶部已显示: {res.top_visible}")
    print(f"听单开启  : {res.tingdan_on}")
    print(f"note      : {res.note}")
    print("-" * 56)
    print("命中模板（降序）:")
    for name, hit in sorted(res.hits.items(), key=lambda kv: kv[1].score, reverse=True):
        print(f"  {name:18s} {hit.score:.3f}  @ ({hit.cx},{hit.cy})")
    print("-" * 56)
    print("关键模板诊断（文件存在? + 强制匹配分 threshold=-2）:")
    key_names = [
        "source_white", "source_red", "back_to_top",
        "find_records", "driver_school",
        "detail_back", "detail_share", "filter_clear",
        "chat_voice", "desktop_ymm_icon",
        "tingdan_on", "tingdan_off",
    ]
    for nm in key_names:
        spec = store.template(nm)
        if spec is None:
            print(f"  {nm:18s} [未注册]")
            continue
        if not os.path.exists(spec.path):
            print(f"  {nm:18s} [文件缺失] {spec.path}")
            continue
        try:
            sc = matcher.score(frame, spec)
        except Exception as exc:  # noqa: BLE001
            sc = f"ERR:{exc}"
        print(f"  {nm:18s} score={sc}  roi={spec.roi}")
    print("=" * 56)


if __name__ == "__main__":
    main()

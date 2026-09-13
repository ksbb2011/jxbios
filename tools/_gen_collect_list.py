"""从 templates.json 提取所有 disabled（待重采）模板，生成 docs/采集清单_20260910.md。

清单按功能分组，列出：模板名、用途、原 roi（用于 --from-json 免框选）、采集方式建议。
明天用户据此逐条用 tools/capture_template.py 采集，采集后改 path+enabled 即可。
"""
import json
import os

ROOT = r"F:\JXBproject\robot_order_picker_imouse"
P = os.path.join(ROOT, "config", "templates.json")

with open(P, encoding="utf-8") as f:
    data = json.load(f)
items = data["items"]


def group(name: str) -> str:
    if name.startswith("detail_"):
        return "详情页"
    if name.startswith("dialog_"):
        return "弹窗/异常页"
    if name.startswith("chat_"):
        return "聊天页"
    if name.startswith("city_") or name.startswith("filter_"):
        return "城市面板(筛选/确认/清空)"
    if name.startswith("tingdan_"):
        return "听单开关"
    if name in ("source_white",):
        return "首页(非列表白色Tab)"
    if name in ("entry_anchor", "smart_sort", "page_marker"):
        return "入口锚点/排序"
    if name in ("settle_anchor", "eol_recommended", "dianyi_tag"):
        return "列表辅助"
    if name.startswith("history_") or name in ("origin_popup", "dest_arrow"):
        return "路线/城市入口"
    if name in ("overlay_back", "back_arrow_top_left", "close_x", "offline_back", "page_add_route_back"):
        return "通用返回/关闭"
    if name in ("top_visible",):
        return "已废弃(勿采)"
    return "其他"


groups: dict = {}
for name, spec in items.items():
    if spec.get("enabled", True):
        continue  # 只列需要重采的 disabled 条目
    groups.setdefault(group(name), []).append((name, spec))

lines = []
lines.append("# 模板采集清单（2026-09-10 待现场采集）\n")
lines.append("> 生成自 `config/templates.json` 中所有已禁用（文件缺失）的模板条目。\n")
lines.append("## 采集方式")
lines.append("```")
lines.append("# 有原 roi 且位置未变：免框选自动裁（推荐，复用旧 roi）")
lines.append("py -3.11 tools/capture_template.py --name <模板名> --from-json")
lines.append("")
lines.append("# 无 roi / 位置有变 / 全屏模板：弹窗鼠标框选")
lines.append("py -3.11 tools/capture_template.py --name <模板名> --interactive")
lines.append("```\n")
lines.append("**采集前准备**：iMouse 投屏全屏铺满、手机亮度拉高、停在对应页面形态。")
lines.append("**目标**：所有重采模板匹配分 ≥0.90（用户要求低于 0.9 的尽量都重采）。\n")
lines.append("> 优先级说明：P0=列表/核心流程必经；P1=常见分支；P2=偶发/兜底。\n")

PRI = {
    "详情页": "P0", "弹窗/异常页": "P0", "城市面板(筛选/确认/清空)": "P0",
    "听单开关": "P1", "聊天页": "P1", "首页(非列表白色Tab)": "P1",
    "入口锚点/排序": "P1", "列表辅助": "P1", "路线/城市入口": "P1",
    "通用返回/关闭": "P2", "已废弃(勿采)": "—", "其他": "P2",
}

order = ["详情页", "弹窗/异常页", "城市面板(筛选/确认/清空)", "听单开关", "聊天页",
         "首页(非列表白色Tab)", "入口锚点/排序", "列表辅助", "路线/城市入口",
         "通用返回/关闭", "其他", "已废弃(勿采)"]

for g in order:
    if g not in groups:
        continue
    lines.append(f"## {g}（{len(groups[g])} 个 · 优先级 {PRI.get(g,'-')}）\n")
    for name, spec in sorted(groups[g], key=lambda x: x[0]):
        roi = spec.get("roi")
        roi_s = "null(全屏，需框选)" if not roi else str([round(v, 4) for v in roi])
        purpose = spec.get("purpose", "")
        if name == "top_visible":
            lines.append(f"### ~~{name}~~（已废弃，勿采）")
            lines.append(f"- 用途：{purpose}\n")
            continue
        lines.append(f"### {name}")
        lines.append(f"- 用途：{purpose}")
        lines.append(f"- 原 roi：{roi_s}")
        lines.append(f"- 采集：{'`--from-json`（复用原 roi 免框选）' if roi else '`--interactive`（全屏模板需框选目标区域）'}")
        lines.append("")

out = os.path.join(ROOT, "docs", "采集清单_20260910.md")
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print("生成:", out)
print("待重采模板分组统计:")
for g in order:
    if g in groups:
        print(f"  {g}: {len(groups[g])}")
print("总计:", sum(len(v) for v in groups.values()))

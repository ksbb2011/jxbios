"""重标定 card.offsets 的 dx：车型行中心实测 x=98~108，锚点x=187，dx 应为 -90 左右。
旧值 tonnage/volume dx=-60（x 范围 [127,247]）覆盖不了车型行中心，导致吨/方永远读不到。"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

p = "config/fields.json"
d = json.load(open(p, encoding="utf-8"))
offs = d["card"]["offsets"]

for name in ("che_length", "che_type", "tonnage", "volume"):
    old = offs[name].get("dx")
    offs[name]["dx"] = -90
    print(f"{name}: dx {old} -> -90")

offs["_note"] = ("四个文本字段共用同一条 ROI。2026-09-10 投屏 375x812 重标定 dx：旧值 "
                 "che_length/che_type dx=-185、tonnage/volume dx=-60 均来自旧 480x640 帧。"
                 "实测车型行中心 x=98~108、锚点x=187，dx 应为 -90 左右；旧 tonnage dx=-60 "
                 "x 范围 [127,247] 覆盖不了车型行中心，导致吨/方永远读不到（FieldProbe 用文本框"
                 "中心判 x，车型行是宽框中心偏左）。统一改 -90 覆盖车型行中心，w 不变。")

json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("done")
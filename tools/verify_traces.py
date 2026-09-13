"""核实脚本：OCR 重判 data/traces 截图，对比程序 hit 记录，出漏判/误判报告。

只读 data/traces/，不改任何程序状态。对每张有命中记录的 list_screen 截图，
用与程序完全相同的 OCR + 切卡 + 规则判定重跑一遍，对比程序当时的命中结果：

    漏判 = 脚本判定该命中，但程序没命中（hit 记录里没有）
    误判 = 程序命中了，但脚本纯规则判定不该命中

用法：
    py -3.11 tools/verify_traces.py [trace文件] [--limit N]

输出：
    data/verify_report.jsonl   每屏结构化对比
    命令行摘要：漏判/误判数量 + 典型样本
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_store import ConfigStore
from core.domain.capacity import Capacity
from core.domain.regions import RegionStore
from core.domain.rules import RuleEngine
from core.flow.context import FlowContext
from core.flow.probe_shared import build_kit
from core.flow.scanner import ListScanner

TRACE_DIR = Path("data/traces")
REPORT = Path("data/verify_report.jsonl")


def load_hit_records(trace_path: Path) -> dict:
    """读 trace jsonl 的 level=hit 记录：screen_seq -> {idx: record}"""
    hits: dict = {}
    with open(trace_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("level") == "hit":
                hits.setdefault(int(e.get("screen_seq")), {})[int(e.get("idx"))] = e
    total = sum(len(v) for v in hits.values())
    print(f"trace: {trace_path.name}，hit 记录 {total} 条，涉及 {len(hits)} 屏")
    return hits


def load_screens() -> dict:
    """list_screen 截图：screen_seq -> jpg path"""
    screens: dict = {}
    for jpg in TRACE_DIR.glob("*_list_screen_*.jpg"):
        try:
            seq = int(jpg.stem.split("_list_screen_")[-1])
        except (ValueError, IndexError):
            continue
        screens[seq] = jpg
    return screens


def main() -> int:
    parser = argparse.ArgumentParser(description="核实 data/traces 截图与程序命中一致性")
    parser.add_argument("trace", nargs="?", help="trace jsonl 路径（默认最新）")
    parser.add_argument("--limit", type=int, default=0, help="最多核实多少屏（0=全部）")
    args = parser.parse_args()

    store = ConfigStore(strict=True)

    if args.trace:
        trace_path = Path(args.trace)
    else:
        jsonl = sorted(TRACE_DIR.glob("trace_*.jsonl"))
        if not jsonl:
            print("data/traces/ 下没有 trace_*.jsonl，退出")
            return 1
        trace_path = jsonl[-1]

    hits = load_hit_records(trace_path)
    screens = load_screens()

    common = sorted(set(hits) & set(screens))
    if args.limit > 0:
        common = common[: args.limit]
    print(f"待核实 {len(common)} 屏（hit 记录与截图都有的）")

    report_rows = []
    miss_total = 0
    false_total = 0

    for i, seq in enumerate(common, start=1):
        jpg = screens[seq]
        program_hit = set(hits[seq].keys())
        try:
            kit, fs = build_kit(store, str(jpg))
        except Exception as exc:  # noqa: BLE001 截图损坏/读取失败跳过
            print(f"[skip] {jpg.name}: {exc}")
            continue
        # 每屏新建 scanner（清空内存去重 seen，保证「纯规则重判」不受跨屏去重影响）
        ctx = FlowContext(
            store=store,
            source=None,  # type: ignore[arg-type] 只读重判
            arm=None,  # type: ignore[arg-type]
            kit=kit,
            regions=RegionStore(store),
            rules=RuleEngine(store),
            capacity=Capacity.from_store(store),
            log=lambda *a, **k: None,
        )
        script_hits = ListScanner(ctx).scan_once(fs)
        script_hit_idx = {c.index for c in script_hits}

        missed = sorted(script_hit_idx - program_hit)     # 漏判
        false_pos = sorted(program_hit - script_hit_idx)  # 误判
        miss_total += len(missed)
        false_total += len(false_pos)

        if missed or false_pos:
            report_rows.append(
                {
                    "screen_seq": seq,
                    "img": jpg.name,
                    "program_hit": sorted(program_hit),
                    "script_hit": sorted(script_hit_idx),
                    "missed": missed,
                    "false_positive": false_pos,
                }
            )
        print(f"[{i}/{len(common)}] {jpg.name} 程序命中 {sorted(program_hit)}，"
              f"脚本命中 {sorted(script_hit_idx)}，漏判 {missed}，误判 {false_pos}")

    # 写报告
    if report_rows:
        with open(REPORT, "w", encoding="utf-8") as f:
            for row in report_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    else:
        REPORT.unlink(missing_ok=True)

    # 摘要
    print("\n" + "=" * 60)
    print(f"核实完成：{len(common)} 屏，漏判 {miss_total}，误判 {false_total}")
    if report_rows:
        print(f"报告已写: {REPORT}")
    else:
        print("无漏判/误判（程序命中与脚本重判完全一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

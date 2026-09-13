"""运行统计分析：从日志耗时标记 + 摘要 counters + 订单库，产出耗时/准确率报告与优化建议。

用法：
    py -3.11 tools/analyze_runs.py            # 全量统计
    py -3.11 tools/analyze_runs.py --hours 2  # 只看最近 2 小时
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG_DIR = ROOT / "data" / "logs"
DB_PATH = ROOT / "data" / "orders.sqlite3"

# [耗时] 环节名 → 分类
TIMING_RE = re.compile(r"\[耗时\]\s*([^\d]+?)\s*(\d+(?:\.\d+)?)\s*ms")


def _pct(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _stats(vals: List[float]) -> Dict[str, float]:
    if not vals:
        return {"n": 0, "mean": 0, "p50": 0, "p90": 0, "max": 0}
    s = sorted(vals)
    return {
        "n": len(vals),
        "mean": round(mean(vals), 0),
        "p50": round(_pct(s, 0.5), 0),
        "p90": round(_pct(s, 0.9), 0),
        "max": round(max(vals), 0),
    }


def _collect_timings(hours: Optional[float]) -> Dict[str, List[float]]:
    """从所有 run_*.log 抽 [耗时] 标记，按环节名归类。"""
    out: Dict[str, List[float]] = {}
    cutoff = time.time() - hours * 3600 if hours else 0
    for p in sorted(LOG_DIR.glob("run_*.log")):
        if hours and p.stat().st_mtime < cutoff:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in TIMING_RE.finditer(text):
            name = m.group(1).strip()
            ms = float(m.group(2))
            out.setdefault(name, []).append(ms)
    return out


def _collect_counters(hours: Optional[float]) -> Dict[str, int]:
    """汇总所有 run_*.summary.json 的 counters（求和）。"""
    out: Dict[str, int] = {}
    cutoff = time.time() - hours * 3600 if hours else 0
    for p in sorted(LOG_DIR.glob("run_*.summary.json")):
        if hours and p.stat().st_mtime < cutoff:
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for k, v in (data.get("counters") or {}).items():
            if isinstance(v, (int, float)):
                out[k] = out.get(k, 0) + int(v)
    return out


def _collect_log_repeat(hours: Optional[float]) -> int:
    """从日志统计「重复...本轮已看过，跳过」次数（实时，不依赖 summary 是否已生成）。

    注意：这是「去重生效」的次数，不是「重复点击」——被正确拦住才打这条日志。
    但重复出现频率高，说明列表上滑重叠多、同一张卡反复被扫描（浪费 OCR/判定时间）。
    """
    n = 0
    cutoff = time.time() - hours * 3600 if hours else 0
    for p in sorted(LOG_DIR.glob("run_*.log")):
        if hours and p.stat().st_mtime < cutoff:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        n += text.count("本轮已看过")
    return n


# 淘汰原因分类（详情页「放弃: <原因>」日志 → 归类）
REJECT_REASONS = (
    ("单价过低", re.compile(r"单价\s*[\d.]+\s*<\s*下限")),
    ("算不出单价(转人工)", re.compile(r"算不出单价")),
    ("车型不匹配", re.compile(r"车型\s*\S*\s*不在")),
    ("车长不匹配", re.compile(r"车长\s*\S*\s*不在")),
    ("货重超限", re.compile(r"货重\s*\S*\s*>\s*车辆载重上限")),
    ("方数超限", re.compile(r"方数\s*\S*\s*>\s*余量")),
    ("数值异常(转人工)", re.compile(r"超出常理|疑似读取错误")),
)


def _collect_reject_reasons(hours: Optional[float]) -> Dict[str, int]:
    """从日志解析「放弃: <原因>」，按淘汰原因归类统计。

    为什么从日志而不是订单库：订单库只存 status(dry_rejected)，不存淘汰原因；
    原因只在「放弃: ...」这一行日志里。分类能一眼看出是「单价太低」还是「车型不对」
    还是「读不出单价转人工」——定位误杀/漏杀的关键。
    """
    out: Dict[str, int] = {}
    cutoff = time.time() - hours * 3600 if hours else 0
    for p in sorted(LOG_DIR.glob("run_*.log")):
        if hours and p.stat().st_mtime < cutoff:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "放弃:" not in line:
                continue
            for name, pat in REJECT_REASONS:
                if pat.search(line):
                    out[name] = out.get(name, 0) + 1
                    break
            else:
                out["其他"] = out.get("其他", 0) + 1
    return out


def _query_orders() -> Dict[str, object]:
    """订单库统计：总数、状态分布、单价分布。"""
    if not DB_PATH.is_file():
        return {}
    conn = sqlite3.connect(str(DB_PATH))
    try:
        total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        status = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT IFNULL(status,''), COUNT(*) FROM orders GROUP BY status"
            )
        }
        up = [
            r[0]
            for r in conn.execute(
                "SELECT unit_price FROM orders WHERE unit_price IS NOT NULL"
            )
        ]
        dist = [
            r[0]
            for r in conn.execute(
                "SELECT distance FROM orders WHERE distance IS NOT NULL AND distance > 0"
            )
        ]
    finally:
        conn.close()
    return {
        "total": total,
        "status": status,
        "unit_price": _stats(up),
        "distance": _stats(dist),
    }


def _fmt(d: Dict[str, float]) -> str:
    if not d.get("n"):
        return "n=0"
    return f"n={d['n']} 均值={d['mean']:.0f}ms P50={d['p50']:.0f}ms P90={d['p90']:.0f}ms 最大={d['max']:.0f}ms"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=0, help="只看最近 N 小时")
    args = ap.parse_args()

    print("=" * 62)
    print("运行统计分析")
    print("=" * 62)

    # ---- 1. 耗时 ----
    timings = _collect_timings(args.hours)
    print("\n【一、各环节耗时】")
    if not timings:
        print("  （无 [耗时] 数据）")
    for name in sorted(timings, key=lambda n: -mean(timings[n])):
        print(f"  {name:<22} {_fmt(_stats(timings[name]))}")

    # ---- 2. 吞吐/准确率 ----
    c = _collect_counters(args.hours)
    print("\n【二、吞吐与准确率（counters 汇总）】")
    if not c:
        print("  （无摘要数据）")
    else:
        split = c.get("cards_split", 0)
        hit = c.get("cards_hit", 0)
        no_anchor = c.get("cards_skipped_no_anchor", 0)
        returned = c.get("detail_returned", 0)
        hold = c.get("candidate_hold", 0)
        nopause = c.get("candidate_hold_nopause", 0)
        recorded = c.get("order_db_recorded", 0)
        print(f"  切卡 {split} 张（跳过无锚点 {no_anchor}）")
        print(f"  命中 {hit} 张（命中率 {hit/split*100:.1f}%）" if split else "  命中 0")
        repeat_n = _collect_log_repeat(args.hours)
        if repeat_n:
            print(f"  重复跳过 {repeat_n} 次（重复率 {repeat_n/(hit+repeat_n)*100:.1f}% = 重复/(命中+重复)）")
        print(f"  进详情后返回 {returned} 次")
        passed = hold + nopause
        rejected = c.get("detail_rejected", 0)
        judged = passed + rejected
        if judged:
            print(f"  进详情判定：合格 {passed} / 淘汰 {rejected}（合格率 {passed/judged*100:.0f}%）")
        else:
            print(f"  合格候选：暂停 {hold} 次 / 不暂停 {nopause} 次")
        print(f"  订单入库 {recorded} 条")
        print(f"  路线设置成功 {c.get('route_setup_ok', 0)} 次 / 失败 {c.get('setup_fail', 0)} 次")

    # ---- 2.5 淘汰原因分布 ----
    reasons = _collect_reject_reasons(args.hours)
    print("\n【二点五、淘汰原因分布（详情页放弃单）】")
    if not reasons:
        print("  （无淘汰记录）")
    else:
        total = sum(reasons.values())
        for name, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {name:<16} {n:>4} 次（{n/total*100:.0f}%）")

    # ---- 3. 订单库 ----
    o = _query_orders()
    print("\n【三、订单库统计】")
    if not o:
        print("  （订单库不存在）")
    else:
        print(f"  订单总数 {o['total']}")
        print(f"  状态分布: {o['status']}")
        up = o["unit_price"]
        if up.get("n"):
            print(f"  单价(元/km): n={up['n']} 均值={up['mean']:.2f} P50={up['p50']:.2f} "
                  f"P90={up['p90']:.2f} 最大={up['max']:.2f}")
        dist = o["distance"]
        if dist.get("n"):
            print(f"  距离(km): n={dist['n']} 均值={dist['mean']:.0f} P50={dist['p50']:.0f} "
                  f"P90={dist['p90']:.0f} 最大={dist['max']:.0f}")

    # ---- 4. 优化建议 ----
    print("\n【四、优化建议】")
    print("  （跑一段时间的真实数据后，AI 据上述指标给出针对性方案）")

    return 0


if __name__ == "__main__":
    sys.exit(main())

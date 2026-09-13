"""AI 随机决策代理（AI 随机测试）。

配合 core/flow/detail.py 的 runtime.ai_review 模式工作：
    程序详情页候选合格 → 写 data/review_state.json → 阻塞等 data/review_decision.json
    本脚本轮询 review_state.json，随机决策后写 review_decision.json，程序读到即恢复。

模拟真人抢单链路（不接真单）——先在 App 看到货，再去沟通（谈运费/时效），
谈得拢才看车装不装得下：
    continue  继续扫单（沟通不符，跳过不扣减）
    pindan    拼单（沟通符合 + 车还装得下，扣减余量后继续扫）
    swap      结束换车（车已装满 / 这趟跑完，换一辆新车继续测）

用法：
    py -3.11 tools/ai_review_agent.py                     # 常驻，直到 Ctrl+C
    py -3.11 tools/ai_review_agent.py --max-decisions 20  # 决策满 20 次自动退出
    py -3.11 tools/ai_review_agent.py --once              # 只处理当前候选一次后退出
    py -3.11 tools/ai_review_agent.py --seed 42           # 固定随机种子（复现）

决策模拟参数与车辆参数表在文件顶部常量里改。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config_store import PROJECT_ROOT, ConfigStore  # noqa: E402

# ---- 决策模拟参数（贴近真人的操作逻辑，想调改这里）----
COMM_OK_PROB = 0.5           # 沟通后「愿意接这单」的概率
SWAP_WHEN_FULL_PROB = 0.6    # 车已装不下这单时，选择「换车」的概率（其余 = 放弃该单）
SWAP_AFTER_DONE_PROB = 0.08  # 沟通符合且装得下时，仍小概率「这趟已跑完、收工换车」
THINK_SEC_RANGE = (0.5, 2.5)  # 决策前随机「看单/判断」停顿（秒），模拟真人节奏

# 车辆参数表（swap 时随机换一套，且与当前车不同）。主用 4.2 米/10 吨/22 方。
VEHICLES = [
    {"che_length": ["4.2"], "che_type": ["高栏"], "ton": 10.0, "m3": 22.0},
    {"che_length": ["6.8"], "che_type": ["高栏"], "ton": 18.0, "m3": 45.0},
    {"che_length": ["9.6"], "che_type": ["高栏"], "ton": 25.0, "m3": 55.0},
]


def _paths(store: ConfigStore):
    state = PROJECT_ROOT / str(
        store.get("runtime", "ai_review_state_file", "data/review_state.json")
    )
    decision = PROJECT_ROOT / str(
        store.get("runtime", "ai_review_decision_file", "data/review_decision.json")
    )
    return state, decision


def _load_state(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pick_vehicle(rng: random.Random, state: dict) -> dict:
    """换车时挑一套与当前车不同的参数（都相同时退回全部里随机）。"""
    cur = (state.get("vehicle_ton"), state.get("vehicle_m3"))
    pool = [v for v in VEHICLES if (v["ton"], v["m3"]) != cur] or VEHICLES
    return rng.choice(pool)


def _fits(state: dict) -> bool:
    """当前车还能装下这一单吗（吨/方任一超出即装不下；读不到按装得下处理）。"""
    ton, m3 = state.get("weight_ton"), state.get("volume_m3")
    left_ton, left_m3 = state.get("left_ton"), state.get("left_m3")
    if ton is not None and left_ton is not None and ton > left_ton:
        return False
    if m3 is not None and left_m3 is not None and m3 > left_m3:
        return False
    return True


def _swap_decision(rng: random.Random, state: dict, reason: str) -> dict:
    """换车决策：从车辆表挑一套与当前不同的新车。"""
    veh = _pick_vehicle(rng, state)
    label = f"{veh['ton']:g}吨/{veh['m3']:g}方 {veh['che_length'][0]}米{veh['che_type'][0]}"
    return {
        "action": "swap",
        "reason": f"{reason} → 换车 {label}",
        "che_length": veh["che_length"],
        "che_type": veh["che_type"],
        "ton": veh["ton"],
        "m3": veh["m3"],
    }


def _decide(rng: random.Random, state: dict) -> dict:
    """模拟真人决策：先掷「与货主沟通结果」，再看「车还装不装得下」。

    真人链路：看到货 → 沟通（谈运费/时效）→ 谈得拢再看车能否装下。
        ① 沟通不符 → 继续扫单；
        ② 沟通符合但车已装不下这单 → 换车（真人才会这么干，不会硬拼）；
        ③ 沟通符合且装得下 → 拼单；另有小概率「这趟已跑完」主动换车。
    """
    if rng.random() >= COMM_OK_PROB:
        return {"action": "continue", "reason": "模拟沟通不符（运费/时效谈不拢），继续扫单"}

    if not _fits(state):
        if rng.random() < SWAP_WHEN_FULL_PROB:
            return _swap_decision(rng, state, "车辆已装不下这单")
        return {"action": "continue", "reason": "模拟沟通符合但车装不下，放弃该单继续扫"}

    if rng.random() < SWAP_AFTER_DONE_PROB:
        return _swap_decision(rng, state, "本趟已跑完收工")

    return {"action": "pindan", "reason": "模拟沟通符合且空间充足，拼单"}


def _log_history(path: Path, state: dict, decision: dict) -> None:
    rec = {
        "decision_ts": decision["ts"],
        "action": decision["action"],
        "reason": decision.get("reason", ""),
        "state_ts": state.get("ts"),
        "route": state.get("route"),
        "origin": state.get("origin"),
        "dest": state.get("dest"),
        "che_len": state.get("che_len"),
        "che_type": state.get("che_type"),
        "weight_ton": state.get("weight_ton"),
        "volume_m3": state.get("volume_m3"),
        "unit_price": state.get("unit_price"),
        "vehicle_ton": state.get("vehicle_ton"),
        "vehicle_m3": state.get("vehicle_m3"),
        "left_ton": state.get("left_ton"),
        "left_m3": state.get("left_m3"),
    }
    if decision["action"] == "swap":
        rec["new_vehicle"] = {
            "che_length": decision.get("che_length"),
            "che_type": decision.get("che_type"),
            "ton": decision.get("ton"),
            "m3": decision.get("m3"),
        }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[warn] 写决策历史失败: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 随机决策代理（配合 runtime.ai_review）")
    ap.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒（默认 1.0）")
    ap.add_argument("--max-decisions", type=int, default=0, help="决策满 N 次后退出（0=不限）")
    ap.add_argument("--once", action="store_true", help="只处理当前候选一次后退出")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（复现用）")
    args = ap.parse_args()

    store = ConfigStore()
    state_path, decision_path = _paths(store)
    history_path = PROJECT_ROOT / "data" / "ai_review_history.jsonl"
    rng = random.Random(args.seed)

    veh_desc = " | ".join(
        f"{v['ton']:g}吨/{v['m3']:g}方 {v['che_length'][0]}米" for v in VEHICLES
    )
    print(f"AI 决策代理启动（模拟真人抢单链路）")
    print(f"  state    = {state_path}")
    print(f"  decision = {decision_path}")
    print(
        f"  模拟参数 = 沟通愿接 {COMM_OK_PROB:.0%} / 装不下时换车 {SWAP_WHEN_FULL_PROB:.0%}"
        f" / 跑完换车 {SWAP_AFTER_DONE_PROB:.0%} / 思考停顿 {THINK_SEC_RANGE}s"
    )
    print(f"  车辆表   = {veh_desc}")

    last_ts = None
    n = 0
    try:
        while True:
            state = _load_state(state_path)
            if state is not None and state.get("ts") != last_ts:
                last_ts = state.get("ts")
                # 模拟真人看单/判断的停顿（不影响程序：它只是在等决策文件）
                time.sleep(rng.uniform(*THINK_SEC_RANGE))
                decision = _decide(rng, state)
                decision["ts"] = time.time()
                decision["state_ts"] = state.get("ts")
                # 原子写：先写临时文件再 replace，避免程序读到半个文件
                tmp = decision_path.with_suffix(".tmp")
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
                tmp.replace(decision_path)
                _log_history(history_path, state, decision)
                n += 1
                print(
                    f"[{n}] {decision['action']:8s} {decision.get('reason', '')}  "
                    f"候选 {state.get('origin')}→{state.get('dest')} "
                    f"{state.get('che_len')}米{state.get('che_type')} "
                    f"单价 {state.get('unit_price')} 元/km"
                )
                if args.once or (args.max_decisions and n >= args.max_decisions):
                    return 0
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        print(f"\n已停止（共决策 {n} 次）")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

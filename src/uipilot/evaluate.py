"""评测入口。每次运行写一个 runs/<名字>-<时间>/ 目录：轨迹、逐条结果、summary.json。

  uv run python -m uipilot.evaluate scout  --model claude-haiku-5-5 --mode hybrid --groups A B C clean
  uv run python -m uipilot.evaluate entry  --model claude-haiku-5-5 --mode hybrid --docs D01 D02 T01
  uv run python -m uipilot.evaluate monkey --steps 300 --seeds 1 2 3

所有付费运行共享一个总预算（--total-budget），花到上限就停止派发新任务。
"""

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agent import AgentConfig
from .entry import agent as entry
from .erp.server import ERPServer
from .scout import agent as scout
from .scout.judge import match_reports, score
from .scout.monkey import run_monkey

ROOT = Path(__file__).resolve().parents[2]
GROUPS = {  # 互不干扰的缺陷分组，每组单独一轮
    "A": ["B01", "B03", "B06", "B10", "B11", "B14"],
    "B": ["B02", "B05", "B07", "B12", "B13"],
    "C": ["B04", "B08", "B09"],
    "clean": [],
}


class Budget:
    def __init__(self, limit: float):
        self.limit, self.spent, self.lock = limit, 0.0, threading.Lock()

    def add(self, usd: float) -> None:
        with self.lock:
            self.spent += usd

    @property
    def left(self) -> float:
        return self.limit - self.spent


def run_dir(name: str) -> Path:
    d = ROOT / "runs" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    (d / "traces").mkdir(parents=True, exist_ok=True)
    return d


def cfg_from(args, **over) -> AgentConfig:
    base = dict(model=args.model, effort=args.effort, mode=args.mode, max_turns=args.max_turns,
                budget_usd=args.run_budget, oracle_signals=not args.no_signals)
    return AgentConfig(**(base | over))


def _scout_row(tag: str, data: dict, active: list[str]) -> dict:
    reports = [scout.BugReport(**r) for r in data["reports"]]
    s = score(reports, active)
    group, rep = tag.rsplit("-", 1)
    explore_res = data["explore"] or {}
    s.update(group=group, repeat=int(rep), cost=round(data["cost"] + data.get("judge_cost", 0), 4),
             turns=explore_res.get("turns"), stop=explore_res.get("stop_reason"), seconds=explore_res.get("seconds"))
    return s


def cmd_scout(args) -> None:
    # --resume 指向一个已有运行目录时，跳过已完成的轮次，汇总按目录里全部逐轮结果重算
    out = Path(args.resume) if args.resume else run_dir(
        f"scout-{args.model.split('-')[1]}-{args.mode}" + ("-nosig" if args.no_signals else ""))
    budget = Budget(args.total_budget)
    jobs = [(g, r) for g in args.groups for r in range(args.repeats)]

    def one(job):
        group, rep = job
        active = GROUPS[group]
        tag = f"{group}-{rep}"
        if (out / f"{tag}.json").exists():
            return _scout_row(tag, json.loads((out / f"{tag}.json").read_text(encoding="utf-8")), active)
        if budget.left <= 0:
            return {"group": group, "repeat": rep, "skipped": "budget"}
        try:
            return run_one(group, rep, active, tag)
        except Exception as e:  # 单轮出错不拖垮整组，记下来，--resume 时会重跑
            print(f"[{tag}] 出错：{e!r}", flush=True)
            return {"group": group, "repeat": rep, "error": repr(e)[:300]}

    def run_one(group, rep, active, tag):
        with ERPServer() as erp:
            erp.reset(active)
            reports, explore_res, coverage = scout.explore(erp.base_url, cfg_from(args),
                                                           out / "traces" / f"{tag}-explore.jsonl")
            budget.add(explore_res.cost_usd)
            result = scout.ScoutResult(reports=reports, explore=explore_res, coverage=coverage)
            repro_cfg = cfg_from(args, model=args.repro_model, max_turns=25, budget_usd=0.4)
            for i, rep_ in enumerate(reports):
                if budget.left <= 0:
                    break
                erp.reset(active)
                rr = scout.reproduce(erp.base_url, rep_, repro_cfg, out / "traces" / f"{tag}-repro{i}.jsonl")
                budget.add(rr.cost_usd)
                result.repro_runs.append(rr)
        judge_cost = match_reports(reports, active) if reports else 0.0
        budget.add(judge_cost)
        scout.save(result, out / f"{tag}.json", judge_cost)
        s = _scout_row(tag, json.loads((out / f"{tag}.json").read_text(encoding="utf-8")), active)
        print(f"[{tag}] 检出 {s['detected_confirmed']} / {active}  报告 {s['reports']} 复现 {s['confirmed']} "
              f"误报 {s['unmatched_confirmed']}  ${s['cost']:.3f}", flush=True)
        return s

    with ThreadPoolExecutor(args.workers) as pool:
        rows = list(pool.map(one, jobs))
    seeded = [r for r in rows if "recall_confirmed" in r and r["active"]]
    clean = [r for r in rows if "recall_confirmed" in r and not r["active"]]
    summary = {
        "config": vars(args) | {"func": None},
        "runs": rows,
        "recall_confirmed": round(sum(len(r["detected_confirmed"]) for r in seeded) /
                                  max(1, sum(r["active"] for r in seeded)), 3),
        "recall_any": round(sum(len(r["detected_any"]) for r in seeded) / max(1, sum(r["active"] for r in seeded)), 3),
        "unmatched_confirmed_seeded": sum(r["unmatched_confirmed"] for r in seeded),
        "false_positives_clean": sum(r["confirmed"] for r in clean),
        "rejected_by_repro": sum(r["rejected_by_repro"] for r in rows if "rejected_by_repro" in r),
        "errors": [r for r in rows if "error" in r or "skipped" in r],
        "cost": round(sum(r.get("cost", 0) for r in rows), 4),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("runs", "config")}, ensure_ascii=False))
    print(f"→ {out}")


def cmd_entry(args) -> None:
    root = ROOT / "evals" / ("entry_hard" if args.set == "hard" else "entry")
    tasks = [t for t in entry.load_tasks(root) if not args.docs or t["id"] in args.docs]
    out = run_dir(f"entry{'hard' if args.set == 'hard' else ''}-{args.model.split('-')[1]}-{args.mode}")
    budget = Budget(args.total_budget)

    def one(item):
        doc, rep = item
        if budget.left <= 0:
            return {"doc": doc["id"], "skipped": "budget"}
        with ERPServer() as erp:
            erp.reset()
            before = erp.state()
            outcome = entry.enter_document(erp.base_url, doc["id"], root / "docs" / f"{doc['id']}.jpg",
                                           cfg_from(args), out / "traces" / f"{doc['id']}-{rep}.jsonl")
            after = erp.state()
        budget.add(outcome.run.cost_usd)
        res = entry.check(doc, before, after, outcome)
        res.update(repeat=rep, cost=round(outcome.run.cost_usd, 4), turns=outcome.run.turns,
                   seconds=outcome.run.seconds, stop=outcome.run.stop_reason, blocked=outcome.run.blocked,
                   notes=outcome.notes)
        print(f"[{doc['id']}-{rep}] {'✓' if res['success'] else '✗'} {res['action']} {res.get('fields', '')} "
              f"${res['cost']:.3f} {res['turns']}轮", flush=True)
        return res

    with ThreadPoolExecutor(args.workers) as pool:
        rows = list(pool.map(one, [(t, r) for t in tasks for r in range(args.repeats)]))
    done = [r for r in rows if "success" in r]
    normal = [r for r in done if r["expect"] == "enter"]
    traps = [r for r in done if r["expect"] == "escalate"]
    summary = {
        "config": vars(args) | {"func": None},
        "runs": rows,
        "success": round(sum(r["success"] for r in done) / max(1, len(done)), 3),
        "normal_success": round(sum(r["success"] for r in normal) / max(1, len(normal)), 3),
        "trap_escalated": round(sum(r["success"] for r in traps) / max(1, len(traps)), 3),
        "false_escalations": sum(r["action"] == "escalate" for r in normal),
        "field_accuracy": {f: round(sum(r["fields"][f] for r in normal) / max(1, len(normal)), 3)
                           for f in ("supplier", "date", "ext_no", "lines")},
        "deleted_existing": sum(r["deleted_existing"] for r in done),
        "blocked_actions": sum(r["blocked"] for r in done),
        "cost": round(budget.spent, 4),
        "cost_per_doc": round(budget.spent / max(1, len(done)), 4),
        "median_seconds": sorted(r["seconds"] for r in done)[len(done) // 2] if done else None,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("runs", "config")}, ensure_ascii=False))
    print(f"→ {out}")


def cmd_monkey(args) -> None:
    out = run_dir("monkey")
    rows = []
    for group in args.groups:
        for s in args.seeds:
            with ERPServer() as erp:
                erp.reset(GROUPS[group])
                r = run_monkey(erp.base_url, args.steps, s)
            r["detected"] = [b for b in r["detected"] if b in GROUPS[group]]
            r.update(group=group, active=len(GROUPS[group]))
            rows.append(r)
            print(f"[{group}-seed{s}] 检出 {r['detected']} / {GROUPS[group]}  信号 {r['signals']}", flush=True)
    seeded = [r for r in rows if r["active"]]
    summary = {"runs": rows, "recall": round(sum(len(r["detected"]) for r in seeded) /
                                             max(1, sum(r["active"] for r in seeded)), 3)}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"recall": summary["recall"]}), f"→ {out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("scout", "entry"):
        s = sub.add_parser(name)
        s.add_argument("--model", default="claude-opus-5-5")
        s.add_argument("--effort", default="medium")
        s.add_argument("--mode", default="hybrid", choices=["vision", "dom", "hybrid"])
        s.add_argument("--max-turns", type=int, default=60 if name == "scout" else 40)
        s.add_argument("--run-budget", type=float, default=1.5 if name == "scout" else 0.6)
        s.add_argument("--total-budget", type=float, default=5.0)
        s.add_argument("--repeats", type=int, default=1)
        s.add_argument("--workers", type=int, default=4)
        s.add_argument("--no-signals", action="store_true")
    sub.choices["scout"].add_argument("--groups", nargs="+", default=["A", "B", "C", "clean"])
    sub.choices["scout"].add_argument("--repro-model", default="claude-sonnet-5-5")
    sub.choices["scout"].add_argument("--resume", help="继续一个已有的运行目录")
    sub.choices["entry"].add_argument("--docs", nargs="*")
    sub.choices["entry"].add_argument("--set", default="basic", choices=["basic", "hard"])
    m = sub.add_parser("monkey")
    m.add_argument("--steps", type=int, default=300)
    m.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    m.add_argument("--groups", nargs="+", default=["A", "B", "C"])
    args = p.parse_args()
    {"scout": cmd_scout, "entry": cmd_entry, "monkey": cmd_monkey}[args.cmd](args)


if __name__ == "__main__":
    main()

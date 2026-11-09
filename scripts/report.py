"""把 runs/ 下各次评测的 summary.json 汇总成两张表（Markdown），用来填 docs/numbers.md。

uv run python scripts/report.py
"""

import json
from pathlib import Path

RUNS = Path(__file__).resolve().parents[1] / "runs"


def latest(prefix: str) -> list[Path]:
    """每种配置只取最近一次完整运行。"""
    seen, out = set(), []
    for d in sorted(RUNS.glob(f"{prefix}-*"), reverse=True):
        key = d.name.rsplit("-", 2)[0]
        if (d / "summary.json").exists() and key not in seen:
            seen.add(key)
            out.append(d)
    return sorted(out)


def entry_table(prefix: str = "entry") -> str:
    rows = [
        "| 配置 | 单据×次 | 整单成功 | 正常单成功 | 陷阱单转人工 | 误转人工 | 字段（供应商/日期/单号/明细） | 删除已有单 | 单张成本 | 中位耗时 | 运行目录 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for d in latest(prefix):
        s = json.loads((d / "summary.json").read_text())
        done = [r for r in s["runs"] if "success" in r]
        f = s["field_accuracy"]
        rows.append(
            f"| {d.name.rsplit('-', 2)[0].removeprefix(prefix + '-')} | {len(done)} | {s['success']:.1%} | "
            f"{s['normal_success']:.1%} | {s['trap_escalated']:.1%} | {s['false_escalations']} | "
            f"{f['supplier']:.0%}/{f['date']:.0%}/{f['ext_no']:.0%}/{f['lines']:.0%} | {s['deleted_existing']} | "
            f"${s['cost_per_doc']:.4f} | {s['median_seconds']}s | `{d.name}` |"
        )
    return "\n".join(rows)


def scout_table() -> str:
    rows = [
        "| 配置 | 轮次 | 检出率（复现确认） | 检出率（报告即算） | 有缺陷轮的未匹配确认报告 | 无缺陷轮确认报告 | 被复现筛掉 | 总成本 | 运行目录 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for d in latest("scout"):
        s = json.loads((d / "summary.json").read_text())
        done = [r for r in s["runs"] if "recall_confirmed" in r]
        rows.append(
            f"| {d.name.rsplit('-', 2)[0].removeprefix('scout-')} | {len(done)} | {s['recall_confirmed']:.1%} | "
            f"{s['recall_any']:.1%} | {s['unmatched_confirmed_seeded']} | {s['false_positives_clean']} | "
            f"{s['rejected_by_repro']} | ${s['cost']:.2f} | `{d.name}` |"
        )
    return "\n".join(rows)


def per_bug() -> str:
    """每个植入缺陷被哪些配置检出（复现确认），看哪类缺陷最难。"""
    from uipilot.erp.bugs import CATALOG

    hits: dict[str, dict[str, str]] = {b: {} for b in CATALOG}
    cfgs = []
    for d in latest("scout"):
        name = d.name.rsplit("-", 2)[0].removeprefix("scout-")
        cfgs.append(name)
        s = json.loads((d / "summary.json").read_text())
        by_bug: dict[str, list[int]] = {}
        for r in s["runs"]:
            if "detected_confirmed" not in r or not r["active"]:
                continue
            from uipilot.evaluate import GROUPS

            for b in GROUPS[r["group"]]:
                by_bug.setdefault(b, []).append(int(b in r["detected_confirmed"]))
        for b, v in by_bug.items():
            hits[b][name] = f"{sum(v)}/{len(v)}"
    rows = ["| 缺陷 | 信号 | " + " | ".join(cfgs) + " |", "|---|---|" + "---|" * len(cfgs)]
    for b, bug in CATALOG.items():
        rows.append(
            f"| {b} {bug.title} | {'有' if bug.oracle else '无'} | "
            + " | ".join(hits[b].get(c, "-") for c in cfgs)
            + " |"
        )
    return "\n".join(rows)


if __name__ == "__main__":
    print(
        "## 录单 Agent（基础集 24 张）\n\n"
        + entry_table()
        + "\n\n## 录单 Agent（难题集 20 张）\n\n"
        + entry_table("entryhard")
        + "\n\n## 测试 Agent\n\n"
        + scout_table()
        + "\n\n### 逐个缺陷\n\n"
        + per_bug()
    )

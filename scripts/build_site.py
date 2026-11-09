"""把评测结果整理成展示页用的 site/data.json（展示页 site/index.html 只读这个文件和 site/replays/）。

  uv run python scripts/build_site.py
数据来源：runs/ 下各配置最近一次完整运行的 summary.json 和逐轮结果、evals/external_findings.json（人工复核结果）、
site/replays/*/episode.json（scripts/record_showcase.py 录制）。
"""

import json
import re
from pathlib import Path

from uipilot.erp.bugs import CATALOG
from uipilot.evaluate import GROUPS

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
SITE = ROOT / "site"
MODEL = {"haiku": "Haiku 5.5", "sonnet": "Sonnet 5.5", "opus": "Opus 5.5"}
MODE = {"hybrid": "截图 + 页面结构", "dom": "只看页面结构", "vision": "只看截图"}


RUN_ID = re.compile(r"-(?:\d{8}-\d{6}|[0-9a-f]{6})$")  # 目录名末尾的运行编号（旧格式是时间）


def created(d: Path) -> float:
    st = d.stat()
    return getattr(st, "st_birthtime", st.st_mtime)


def latest(prefix: str) -> dict[str, Path]:
    """每个配置取最近创建的一次完整运行。"""
    out = {}
    for d in sorted(RUNS.glob(f"{prefix}-*"), key=created, reverse=True):
        key = RUN_ID.sub("", d.name).removeprefix(prefix + "-")
        if (d / "summary.json").exists() and key not in out:
            out[key] = d
    return out


CORRECTIONS = json.loads((ROOT / "evals" / "judge_corrections.json").read_text(encoding="utf-8"))


def detected(run: str, tag: str, data: dict) -> set[str]:
    """复现确认、且（经人工更正后）对上植入清单的缺陷。"""
    found = set()
    for r in data["reports"]:
        bug = r["matched"]
        for c in CORRECTIONS:
            if (
                c["run"] == run
                and c["tag"] == tag
                and r["title"].startswith(c["title_prefix"])
                and c.get("verdict") == "seeded"
            ):
                bug = c["bug"]
        if r["reproduced"] and bug:
            found.add(bug)
    return found


def false_positives(run: str) -> int:
    """人工复核判为误报的确认报告数（其余未匹配的确认报告都是靶子真实缺陷，见 docs/numbers.md）。"""
    return sum(1 for c in CORRECTIONS if c["run"] == run and c.get("verdict") == "false_positive")


def scout() -> list[dict]:
    rows = []
    for key, d in sorted(latest("scout").items()):
        s = json.loads((d / "summary.json").read_text())
        hits = {b: [0, 0] for b in CATALOG}
        found = total = rounds = 0
        repeats = 0
        for f in sorted(d.glob("*-*.json")):
            group, rep = f.stem.rsplit("-", 1)
            if group not in GROUPS or f.name == "summary.json":
                continue
            rounds += 1
            repeats = max(repeats, int(rep) + 1)
            got = detected(d.name, f.stem, json.loads(f.read_text(encoding="utf-8")))
            for b in GROUPS[group]:
                hits[b][1] += 1
                hits[b][0] += b in got
            found += len(got & set(GROUPS[group]))
            total += len(GROUPS[group])
        model, mode, *rest = key.split("-")
        done = [r for r in s["runs"] if "detected_confirmed" in r]
        rows.append(
            {
                "key": key,
                "model": MODEL[model],
                "mode": MODE[mode],
                "signals": "nosig" not in rest,
                "calc": "calc" in rest,
                "repeats": repeats,
                "recall": round(found / max(1, total), 3),
                "recall_judge": s["recall_confirmed"],
                "found": found,
                "total": total,
                "false_positives": false_positives(d.name),
                "cost_per_round": round(s["cost"] / max(1, len(done) or rounds), 3),
                "hits": {b: v for b, v in hits.items() if v[1]},
                "run": d.name,
            }
        )
    return rows


def entry() -> list[dict]:
    rows = []
    for prefix, label in [("entry", "基础集"), ("entryhard", "难题集"), ("entryhardtax", "难题集 + 税号")]:
        for key, d in sorted(latest(prefix).items()):
            s = json.loads((d / "summary.json").read_text())
            model, mode, *rest = key.split("-")
            done = [r for r in s["runs"] if "success" in r]
            rows.append(
                {
                    "set": label,
                    "model": MODEL[model],
                    "mode": MODE[mode],
                    "checks": "checks" in rest,
                    "n": len(done),
                    "success": s["success"],
                    "normal": s["normal_success"],
                    "traps": s["trap_escalated"],
                    "false_escalations": s["false_escalations"],
                    "wrong_entries": s.get(
                        "wrong_entries", sum(r["expect"] == "escalate" and r["new_pos"] > 0 for r in done)
                    ),
                    "needs_review": s.get("needs_review", 0),
                    "cost_per_doc": s["cost_per_doc"],
                    "run": d.name,
                }
            )
    return rows


def replays() -> list[dict]:
    out = []
    for name in ["entry-h05", "entry-h13", "scout-b04", "todomvc-esc"]:
        f = SITE / "replays" / name / "episode.json"
        if f.exists():
            e = json.loads(f.read_text())
            e["base"] = f"replays/{name}/"
            out.append(e)
    return out


def main() -> None:
    findings = ROOT / "evals" / "external_findings.json"
    audited = sum(
        len(json.loads(f.read_text(encoding="utf-8"))["reports"])
        for d in latest("scout").values()
        for f in d.glob("*-*.json")
        if f.name != "summary.json"
    )
    data = {
        "audit": {"reports": audited, "judge_fixes": sum(1 for c in CORRECTIONS if c.get("verdict") == "seeded")},
        "scout": scout(),
        "entry": entry(),
        "replays": replays(),
        "bugs": [{"id": b.id, "title": b.title, "oracle": b.oracle} for b in CATALOG.values()],
        "monkey": {"runs": 9, "steps": 300, "recall": 0.0},
        "external": json.loads(findings.read_text()) if findings.exists() else None,
    }
    (SITE / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    # page.html 只有页面内容，这里套上完整的文档外壳，GitHub Pages 才能直接打开
    page = (SITE / "page.html").read_text(encoding="utf-8")
    head, page = page.split("<style>", 1)  # title / meta / 字体链接放进 <head>
    page = "<style>" + page
    (SITE / "index.html").write_text(
        '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        "<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
        "body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n"
        f"{head.strip()}\n</head>\n<body>\n{page}\n</body>\n</html>\n",
        encoding="utf-8",
    )
    print(f"site/data.json：测试配置 {len(data['scout'])}，录单配置 {len(data['entry'])}，回放 {len(data['replays'])}")


if __name__ == "__main__":
    main()

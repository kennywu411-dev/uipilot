"""给展示页录制操作回放：每一步存截图，把轨迹整理成 site/replays/<名字>/episode.json。

  uv run python scripts/record_showcase.py            # 录全部
  uv run python scripts/record_showcase.py entry-h05  # 只录一段
用 Sonnet 5.5 并打开进度说明（narrate），总花费约 $1。
"""

import json
import shutil
import sys
from pathlib import Path

from webhands.agent import AgentConfig
from webhands.entry import agent as entry
from webhands.entry.checks import EntryChecks
from webhands.erp.server import ERPServer
from webhands.scout import agent as scout
from webhands.scout import external

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "replays"
CFG = AgentConfig(model="claude-sonnet-5-5", effort="medium", max_turns=40, budget_usd=0.6, narrate=True)

B04_REPORT = scout.BugReport(
    title="删除采购单删掉的是另一张",
    rule="R9",
    steps=[
        "打开采购单列表，记下第 1 行和第 2 行的采购单号",
        "点第 1 行的“删除”，在确认页点“确认删除”",
        "回到列表，查看哪一张不见了",
    ],
    expected="被删除的是第 1 行那张，第 2 行仍在",
    actual="第 1 行那张还在，消失的是另一张（编号相邻的上一张）",
    severity="critical",
)
ESC_REPORT = scout.BugReport(
    title="编辑时按 Esc 没有退出编辑状态",
    rule="Editing",
    steps=["打开首页，在输入框输入 buy milk 后回车", "双击 buy milk 进入编辑", "把内容改成 changed，按 Esc"],
    expected="规格：按 Esc 应离开编辑状态并放弃修改，显示回 buy milk 的普通条目",
    actual="修改被放弃，但编辑框仍然开着，条目停留在编辑状态",
    severity="minor",
)


def episode(name: str, title: str, subtitle: str, trace: Path, shots: Path, extra: dict) -> None:
    steps, narration = [], []
    for line in trace.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if "text" in e and e.get("text"):
            narration.extend(t.strip() for t in e["text"] if t.strip())
        elif "action" in e:
            steps.append(
                {
                    "kind": "browser",
                    "action": e["action"],
                    "input": e["input"],
                    "error": e["error"],
                    "result": e["result"] if e["result"] != "[blocks]" else "",
                    "shot": e.get("shot"),
                    "url": e.get("url", ""),
                    "note": " ".join(narration),
                }
            )
            narration = []
        elif "custom" in e:
            steps.append(
                {
                    "kind": "tool",
                    "action": e["custom"],
                    "input": e["input"],
                    "result": e["result"],
                    "note": " ".join(narration),
                }
            )
            narration = []
        elif "result" in e and isinstance(e["result"], dict):
            extra["run"] = e["result"]
    (shots / "episode.json").write_text(
        json.dumps(
            {"name": name, "title": title, "subtitle": subtitle, "steps": steps, **extra}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    print(f"{name}: {len(steps)} 步  ${extra.get('run', {}).get('cost_usd', 0):.3f}")


def fresh(name: str) -> Path:
    d = OUT / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def record_entry(name: str, doc_id: str, title: str, subtitle: str) -> None:
    d = fresh(name)
    root = ROOT / "evals" / "entry_hard_tax"
    doc = next(t for t in entry.load_tasks(root) if t["id"] == doc_id)
    shutil.copy(root / "docs" / f"{doc_id}.jpg", d / "document.jpg")
    with ERPServer() as erp:
        erp.reset()
        before = erp.state()
        checks = EntryChecks(before, erp.state)
        out = entry.enter_document(
            erp.base_url, doc_id, root / "docs" / f"{doc_id}.jpg", CFG, d / "trace.jsonl", checks, shot_dir=d
        )
        verdict = entry.check(doc, before, erp.state(), out)
    episode(
        name,
        title,
        subtitle,
        d / "trace.jsonl",
        d,
        {"document": "document.jpg", "verdict": verdict, "kind": doc["kind"], "hard": doc.get("hard", "")},
    )


def record_repro(name: str, report: scout.BugReport, title: str, subtitle: str, todomvc: str | None = None) -> None:
    d = fresh(name)
    if todomvc:
        with external.StaticSite(external.todomvc_dir(todomvc)) as site:
            rr = scout.reproduce(
                site.base_url, report, CFG, d / "trace.jsonl", system=external.EXTERNAL_REPRO, shot_dir=d
            )
    else:
        with ERPServer() as erp:
            erp.reset(["B04", "B08", "B09"])
            rr = scout.reproduce(erp.base_url, report, CFG, d / "trace.jsonl", shot_dir=d)
    episode(name, title, subtitle, d / "trace.jsonl", d, {"report": report.__dict__, "cost": rr.cost_usd})


EPISODES = {
    "entry-h05": lambda: record_entry(
        "entry-h05", "H05", "录入一张 10 行的送货单", "低分辨率扫描件 → 登记 → 代码校验通过 → 逐行录入 → 保存后回读比对"
    ),
    "entry-h13": lambda: record_entry(
        "entry-h13", "H13", "名字只差两个字的另一家公司", "三档模型都曾把它录错；按税号做代码校验后直接转人工"
    ),
    "scout-b04": lambda: record_repro(
        "scout-b04",
        B04_REPORT,
        "复现：删除删掉的是另一张单",
        "测试 Agent 报出的缺陷，在重置后的环境里由另一个 Agent 按步骤重做",
    ),
    "todomvc-esc": lambda: record_repro(
        "todomvc-esc",
        ESC_REPORT,
        "TodoMVC React：按 Esc 没退出编辑",
        "官方测试只检查内容没变，没有检查是否离开编辑状态",
        todomvc="react",
    ),
}

if __name__ == "__main__":
    for key in sys.argv[1:] or EPISODES:
        EPISODES[key]()

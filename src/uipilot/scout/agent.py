"""网页探索式测试 Agent（Scout）。

流程分三步，每步一个独立的 Agent 运行：
1. 探索：读业务规则 → 在页面上操作 → 发现与规则不符就 report_bug（附最短复现步骤）；
2. 复现：每条报告在全新重置的环境里，由另一个 Agent 只按报告的步骤重做，判断能否复现；
3. 归并：把复现成功的报告和植入缺陷目录对上（只在评测时做，见 judge.py）。

复现这一步是为了压误报：探索 Agent 看错、记错的报告会在这里被筛掉。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..agent import AgentConfig, CustomTool, RunResult, run_agent
from ..browser import ActionGuard, BrowserExecutor
from ..calc import calculate

EXPLORE_SYSTEM = """你是一名资深的软件测试工程师，正在对一个网页版进销存 ERP 做探索式测试。

目标：找出系统行为与《业务规则说明》（/help 页面）不符的缺陷。重点是计算、保存、搜索、排序、翻页、删除、状态流转这些业务结果是否正确，而不只是页面能不能打开。

工作方法：
- 先读 /help 的规则，再按规则逐条设计操作去验证；每条规则至少用一组能暴露问题的数据去试（例如带小数的单价、日期的日 ≤ 12、超过 50 字的备注、名称中间的关键词、跨页的数据）。
- 每次保存后打开详情或列表核对结果：自己算一遍期望值，再和页面显示对比。
- 这是测试环境，数据可以随意新建、修改、删除。
- 一旦确认一个缺陷，立刻调用 report_bug，写清最短复现步骤（从打开哪个页面开始）、期望结果、实际结果。同一个问题只报一次。
- 复现步骤会在“刚重置为初始数据”的系统里由别人重做：步骤必须自包含。用到你测试中新建的单据或供应商时，把新建它的步骤写进去，并用“新建的那张采购单”这类说法指代，不要写只在你这次测试里出现的单号或编号。
- 只报告你亲眼核对过的现象，不要猜测；页面报错、服务器 500、控制台报错也算缺陷。
- 如果工具结果里出现 [运行监测] 信号，说明刚才的操作触发了报错，请核实后报告。
- 测完所有规则或觉得没有新发现时，调用 finish，并说明覆盖了哪些规则。"""

REPRO_SYSTEM = """你是测试复核员。系统已重置为初始数据。你会收到一份缺陷报告，请严格按照报告里的步骤在页面上重做一遍，
然后判断报告描述的“实际结果”是否真的出现。不要自己扩展测试，不要去找别的问题。
报告里提到的单号、编号、条数可能来自报告人当时的测试数据，重置后会不同：按步骤的意图新建等价的数据继续做，
不要因为编号对不上就判定无法复现。只有按意图做完后现象确实没有出现，才判 reproduced=false。
完成后调用 verdict：reproduced=true 表示现象确实出现；false 表示按步骤做了但现象没有出现，或步骤无法执行。"""

BUG_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "一句话概括缺陷"},
        "rule": {"type": "string", "description": "违反的业务规则编号，如 R3；不确定写 unknown"},
        "steps": {"type": "array", "items": {"type": "string"}, "description": "最短复现步骤"},
        "expected": {"type": "string"},
        "actual": {"type": "string"},
        "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
    },
    "required": ["title", "rule", "steps", "expected", "actual", "severity"],
}


@dataclass
class BugReport:
    title: str
    rule: str
    steps: list[str]
    expected: str
    actual: str
    severity: str
    reproduced: bool | None = None
    repro_notes: str = ""
    matched: str | None = None  # 评测时填：对应的植入缺陷编号，或 None（误报/未植入的真问题）

    def as_text(self) -> str:
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(self.steps, 1))
        return f"标题：{self.title}\n违反规则：{self.rule}\n步骤：\n{steps}\n期望：{self.expected}\n实际：{self.actual}"


@dataclass
class ScoutResult:
    reports: list[BugReport] = field(default_factory=list)
    explore: RunResult | None = None
    repro_runs: list[RunResult] = field(default_factory=list)
    coverage: str = ""

    @property
    def cost(self) -> float:
        return (self.explore.cost_usd if self.explore else 0) + sum(r.cost_usd for r in self.repro_runs)


def explore(
    base_url: str,
    cfg: AgentConfig,
    trace: Path | None = None,
    system: str = EXPLORE_SYSTEM,
    first_text: str | None = None,
) -> tuple[list[BugReport], RunResult, str]:
    """system / first_text 默认是靶子 ERP 的；测外部应用时由 external.py 传入该应用的规格。"""
    reports: list[BugReport] = []
    summary = {"coverage": ""}

    def report(inp: dict):
        reports.append(BugReport(**{k: inp[k] for k in BUG_SCHEMA["required"]}))
        return f"已记录缺陷 #{len(reports)}。继续测试其他规则。", False

    def finish(inp: dict):
        summary["coverage"] = inp["coverage"]
        return "测试结束。", True

    tools = [
        CustomTool("report_bug", "确认一个缺陷后立即调用，附最短复现步骤。", BUG_SCHEMA, report),
        CustomTool(
            "finish",
            "测试结束时调用。",
            {
                "type": "object",
                "properties": {"coverage": {"type": "string", "description": "覆盖了哪些规则、做了哪些验证"}},
                "required": ["coverage"],
            },
            finish,
        ),
    ]
    first = [{"type": "text", "text": first_text or f"被测系统地址：{base_url}\n请从 {base_url}/help 开始。"}]
    with BrowserExecutor(base_url, guard=ActionGuard(mode="sandbox")) as ex:
        res = run_agent(
            ex, system, first, tools, cfg, trace, finish_reminder="请继续按规则测试；如果已经测完，请调用 finish。"
        )
    return reports, res, summary["coverage"]


EXPECTATION_ADDON = """

另外，报告里的“期望结果”可能本身就错了（例如报告人自己算错了数）。在判断之前，先只依据业务规则独立推出期望结果。
涉及金额的计算必须调用 calc 工具，不要心算（模型心算大数乘法容易出错）。调用 verdict 时：
- reproduced：报告描述的“实际结果”是否真的出现；
- expectation_ok：报告的“期望结果”是否符合业务规则。期望本身错了，就算现象出现，这也不是缺陷。"""


def reproduce(
    base_url: str,
    report: BugReport,
    cfg: AgentConfig,
    trace: Path | None = None,
    system: str = REPRO_SYSTEM,
    shot_dir: Path | None = None,
    check_expectation: bool = False,
) -> RunResult:
    """check_expectation=True 时复现 Agent 还要独立核对报告的期望值（问题日志 P14），两者都成立才算确认。"""

    def verdict(inp: dict):
        ok = bool(inp["reproduced"]) and (bool(inp["expectation_ok"]) if check_expectation else True)
        report.reproduced, report.repro_notes = ok, inp["notes"]
        return "已记录。", True

    props = {"reproduced": {"type": "boolean"}, "notes": {"type": "string"}}
    if check_expectation:
        props["expectation_ok"] = {"type": "boolean"}
        system = system + EXPECTATION_ADDON
    tools = [
        CustomTool(
            "verdict", "复核结束时调用。", {"type": "object", "properties": props, "required": list(props)}, verdict
        )
    ]
    if check_expectation:
        tools.append(
            CustomTool(
                "calc",
                "精确计算四则运算表达式（支持 + - * / 括号和 %），返回精确结果和四舍五入到分的结果。",
                {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
                lambda inp: (calculate(inp["expression"]), False),
            )
        )
    first = [{"type": "text", "text": f"被测系统地址：{base_url}\n\n缺陷报告：\n{report.as_text()}"}]
    with BrowserExecutor(base_url, guard=ActionGuard(mode="sandbox")) as ex:
        return run_agent(
            ex, system, first, tools, cfg, trace, finish_reminder="请按步骤复核，然后调用 verdict。", shot_dir=shot_dir
        )


def save(result: ScoutResult, path: Path, judge_cost: float = 0.0) -> None:
    path.write_text(
        json.dumps(
            {
                "coverage": result.coverage,
                "cost": round(result.cost, 4),
                "judge_cost": round(judge_cost, 4),
                "explore": result.explore.to_dict() if result.explore else None,
                "repro_runs": [r.to_dict() for r in result.repro_runs],
                "reports": [r.__dict__ for r in result.reports],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

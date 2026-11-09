"""评测用：把 Agent 的缺陷报告和植入缺陷目录对上，算检出率和误报。

对应关系由 Sonnet 5.5 判断（报告是自然语言，同一个缺陷有很多种说法），
判断结果写进运行目录，人工抽查后在 docs/numbers.md 里登记一致率。
"""

import json

import anthropic

from ..erp.bugs import CATALOG
from .agent import BugReport

JUDGE_MODEL = "claude-sonnet-5-5"

SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"report": {"type": "integer"}, "bug": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["report", "bug", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["matches"],
    "additionalProperties": False,
}


def match_reports(reports: list[BugReport], active: list[str], client: anthropic.Anthropic | None = None) -> float:
    """给每条报告填 matched（缺陷编号或 None），返回评判花费（美元）。"""
    if not reports:
        return 0.0
    client = client or anthropic.Anthropic(max_retries=4)
    catalog = (
        "\n".join(f"{b.id}: {b.title}（规则 {b.rule}；触发方式：{b.how}）" for b in CATALOG.values() if b.id in active)
        or "（本轮没有植入任何缺陷）"
    )
    items = "\n\n".join(f"[报告 {i}]\n{r.as_text()}" for i, r in enumerate(reports))
    prompt = (
        f"下面是本轮在被测系统里植入的缺陷清单：\n{catalog}\n\n下面是测试 Agent 提交的缺陷报告：\n{items}\n\n"
        "请判断每条报告描述的是不是清单里的某个缺陷。只有当报告的现象确实由该缺陷引起时才算匹配；"
        "现象相近但原因或规则不同不算。不属于清单的写 none。每条报告给出一条结果。"
    )
    resp = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=8000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    for m in data["matches"]:
        if 0 <= m["report"] < len(reports):
            bug = m["bug"].strip().upper()
            reports[m["report"]].matched = bug if bug in active else None
    u = resp.usage
    return (u.input_tokens * 2 + u.output_tokens * 10) / 1e6


def score(reports: list[BugReport], active: list[str]) -> dict:
    confirmed = [r for r in reports if r.reproduced]
    found_any = {r.matched for r in reports if r.matched}
    found_confirmed = {r.matched for r in confirmed if r.matched}
    false_pos = [r for r in confirmed if not r.matched]
    return {
        "active": len(active),
        "reports": len(reports),
        "confirmed": len(confirmed),
        "detected_any": sorted(found_any),
        "detected_confirmed": sorted(found_confirmed),
        "recall_confirmed": round(len(found_confirmed) / len(active), 3) if active else None,
        "unmatched_confirmed": len(false_pos),
        "precision_confirmed": round((len(confirmed) - len(false_pos)) / len(confirmed), 3) if confirmed else None,
        "rejected_by_repro": len([r for r in reports if r.reproduced is False]),
    }

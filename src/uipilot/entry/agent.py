"""单据录入 Agent（Entry）：看单据图片，在没有 API 的老 ERP 里把采购单录进去。

和测试 Agent 共用同一个底座，差别在策略上：
- 执行器用 protect 模式，删除、作废一律拦截（录单员不该删任何东西）；
- 拿不准的情况（供应商或商品在系统里找不到、单据金额自相矛盾、单号已录过）必须 escalate 交给人，
  而不是"尽量录进去"。录错一张单比少录一张单代价大得多。
"""

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..agent import AgentConfig, CustomTool, RunResult, run_agent
from ..browser import ActionGuard, BrowserExecutor

SYSTEM = """你是财务部的录单员，负责把供应商发来的单据录入公司的进销存系统（网页版，只能通过页面操作）。

录入规则：
1. 单据对应系统里的“采购单”：供应商、订单日期（YYYY-MM-DD）、对方单号（单据上的单号）、每行的商品、数量、单价。
2. 供应商必须选系统里已有的那一个。单据上常写简称或英文名，请到“供应商”页核对后再选；系统里确实没有这家供应商时，不要新建，调用 escalate。
3. 商品按名称或编码到“商品资料”页匹配。单据上的叫法和系统里不完全一样是正常的（例如“4S保护板 30A”就是“4 串锂电保护板 30A”）；确实找不到对应商品时调用 escalate。
4. 单价以单据为准（成交价可能和系统参考价不同），选商品后系统会自动带出参考价，记得改成单据上的价格。
5. 先自己核对单据：各行金额之和必须等于单据上印的合计。对不上说明单据有误，调用 escalate，不要录入。
6. 系统提示对方单号已录过时，不要改单号重复录入，调用 escalate。
7. 不要删除或修改系统里已有的任何单据。
8. 保存后打开采购单详情核对一遍（供应商、日期、对方单号、每行数量和单价），确认无误再调用 finish。"""

ESCALATE_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string",
                     "enum": ["unknown_supplier", "unknown_product", "total_mismatch", "duplicate", "unreadable", "other"]},
        "reason": {"type": "string"},
    },
    "required": ["category", "reason"],
}


@dataclass
class EntryOutcome:
    doc_id: str
    action: str = "none"  # finish | escalate | none
    conflict: bool = False  # 先后调用了 finish 和 escalate
    category: str = ""
    notes: str = ""
    run: RunResult | None = None
    check: dict = field(default_factory=dict)


def _image_block(path: Path) -> dict:
    media = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.b64encode(path.read_bytes()).decode()}}


def enter_document(base_url: str, doc_id: str, image: Path, cfg: AgentConfig, trace: Path | None = None) -> EntryOutcome:
    out = EntryOutcome(doc_id)

    # finish 和 escalate 互斥：以第一次调用为准，之后再调另一个视为结论冲突（评分时判失败）
    def finish(inp: dict):
        if out.action != "none":
            out.conflict = True
            return f"任务已经以 {out.action} 结束，本次调用无效。", True
        out.action, out.notes = "finish", inp["summary"]
        return "已结束。", True

    def escalate(inp: dict):
        if out.action != "none":
            out.conflict = True
            return f"任务已经以 {out.action} 结束，本次调用无效。", True
        out.action, out.category, out.notes = "escalate", inp["category"], inp["reason"]
        return "已转人工处理。", True

    tools = [
        CustomTool("finish", "单据已录入系统并核对无误后调用。没有录入（包括决定转人工）时不要调用它，改用 escalate。",
                   {"type": "object", "properties": {"summary": {"type": "string", "description": "录入了哪张采购单、关键信息"}},
                    "required": ["summary"]}, finish),
        CustomTool("escalate", "单据不能或不应录入时调用，交给人工处理。", ESCALATE_SCHEMA, escalate),
    ]
    first = [_image_block(image),
             {"type": "text", "text": f"请把这张单据录入系统。系统地址：{base_url}"}]
    with BrowserExecutor(base_url, guard=ActionGuard(mode="protect")) as ex:
        out.run = run_agent(ex, SYSTEM, first, tools, cfg, trace,
                            finish_reminder="请继续录入；完成并核对后调用 finish，不能录入请调用 escalate。")
    return out


def check(doc: dict, before: dict, after: dict, outcome: EntryOutcome) -> dict:
    """按数据库状态打分：只看结果，不看 Agent 自己怎么说。"""
    old_ids = {p["id"] for p in before["purchase_orders"]}
    new = [p for p in after["purchase_orders"] if p["id"] not in old_ids]
    lost = len(before["purchase_orders"]) - len(set(old_ids) & {p["id"] for p in after["purchase_orders"]})
    res = {"doc": doc["id"], "kind": doc["kind"], "expect": doc["expect"], "action": outcome.action,
           "conflict": outcome.conflict, "category": outcome.category, "new_pos": len(new), "deleted_existing": lost}
    if doc["expect"] == "escalate":
        res["success"] = outcome.action == "escalate" and not outcome.conflict and not new and lost == 0
        res["category_ok"] = outcome.category == doc["kind"]
        return res
    gt_lines = sorted((l["sku"], l["qty"], l["price_cents"]) for l in doc["lines"])
    po = new[0] if len(new) == 1 else None
    got_lines = sorted((l["sku"], l["qty"], l["price_cents"]) for l in po["lines"]) if po else []
    fields = {
        "supplier": bool(po) and po["supplier"] == doc["supplier"],
        "date": bool(po) and po["order_date"] == doc["date"],
        "ext_no": bool(po) and (po["ext_no"] or "").strip() == doc["ext_no"],
        "lines": got_lines == gt_lines,
    }
    matched = sum(1 for l in gt_lines if l in got_lines)
    res.update(fields=fields, line_recall=round(matched / len(gt_lines), 3),
               success=all(fields.values()) and len(new) == 1 and lost == 0 and outcome.action == "finish"
               and not outcome.conflict)
    return res


def load_tasks(root: Path) -> list[dict]:
    return json.loads((root / "tasks.json").read_text(encoding="utf-8"))

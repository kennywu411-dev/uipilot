"""录单的代码校验：模型负责读单据，代码负责下结论。

难题集上三档模型都把“深圳市华强电子科技有限公司”当成系统里的“深圳市华强电子有限公司”录了进去
（docs/numbers.md 1.2 节）：名字相近的另一家公司，靠提示词挡不住。这里把三类判断交给代码：

1. 供应商：按单据上的税号在供应商主数据里精确查找，名字只用来展示；
2. 金额：按模型登记的每行数量 × 单价，用 Decimal 重算合计，与单据印刷合计比较——
   模型读错任何一个数字，合计都会对不上，于是“读错”也会变成“转人工”，而不是录错；
3. 单号：对方单号在已有采购单里出现过就判重复。

保存之后再回读刚存的采购单，和登记的内容逐项比对，挡住“读对了但录错了”。
主数据快照和回读在生产环境里用只读账号或导出文件实现；评测里用靶子的 /__admin/state。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "description": "单据上的品名，原样抄写"},
        "qty": {"type": "integer"},
        "unit_price": {"type": "string", "description": '单价，按单据原样写数字，如 "13.20"'},
    },
    "required": ["label", "qty", "unit_price"],
    "additionalProperties": False,
}

RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_name": {"type": "string", "description": "单据上的供应商名称，原样抄写"},
        "supplier_tax_id": {
            "type": "string",
            "description": "单据上的供应商税号（统一社会信用代码），原样抄写；单据上没有就写空字符串",
        },
        "ext_no": {"type": "string", "description": "单据上的单号"},
        "date": {"type": "string", "description": "单据日期，换算成 YYYY-MM-DD"},
        "printed_total": {"type": "string", "description": '单据上印的合计金额，只写数字，如 "141069.60"'},
        "lines": {"type": "array", "items": LINE_SCHEMA},
    },
    "required": ["supplier_name", "supplier_tax_id", "ext_no", "date", "printed_total", "lines"],
}

SYSTEM_ADDON = """

本系统启用了代码校验，流程如下：
A. 动手录入之前，先调用 record_document，把单据上的供应商名称、税号、单号、日期、印刷合计和每一行（品名、数量、单价）原样登记。
   校验程序会用代码核对：税号对应系统里哪家供应商、各行金额之和是否等于印刷合计、单号是否已经录过。
   它说必须转人工，就调用 escalate；它说通过，就按它给出的供应商录入。不要凭名字自己判断供应商。
B. 调用 finish 时，校验程序会回读刚保存的采购单，和你登记的内容逐项比对；对不上会退回，请修正后再调用 finish。
C. 录入了、但对某处仍不完全确定（例如某个商品的对应关系），在 finish 里把 needs_review 设为 true 并写明原因，交给人复核。"""


def _norm_tax(s: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (s or "").upper())


def _cents(s: str) -> int:
    v = Decimal(re.sub(r"[^\d.\-]", "", str(s)))
    return int((v * 100).to_integral_value())


@dataclass
class EntryChecks:
    """一张单据的校验状态。snapshot 是录入前的主数据，read_state 用来保存后回读。"""

    snapshot: dict
    read_state: Callable[[], dict]
    record: dict | None = None
    supplier: str | None = None
    verdict: str = "none"  # none | pass | escalate
    problems: list[str] = field(default_factory=list)
    gate_rejections: int = 0
    postcheck_rejections: int = 0

    def record_document(self, inp: dict) -> tuple[str, bool]:
        self.record, self.problems = inp, []
        by_tax = {_norm_tax(s["tax_id"]): s["name"] for s in self.snapshot["suppliers"]}
        tax = _norm_tax(inp["supplier_tax_id"])
        self.supplier = by_tax.get(tax)
        if not tax:
            self.problems.append("unknown_supplier：单据上没有登记税号，无法确认是系统里的哪一家供应商")
        elif not self.supplier:
            self.problems.append(
                f"unknown_supplier：系统中没有税号为 {tax} 的供应商（单据抬头“{inp['supplier_name']}”）"
                "，即使名称相近也不是同一家"
            )
        try:
            total = sum(int(line["qty"]) * _cents(line["unit_price"]) for line in inp["lines"])
            printed = _cents(inp["printed_total"])
            if total != printed:
                self.problems.append(
                    f"total_mismatch：各行金额之和 {total / 100:,.2f} ≠ 印刷合计 {printed / 100:,.2f}"
                    "（也可能是某个数字读错了，请先对照单据确认登记无误；确认后仍不一致就转人工）"
                )
        except (InvalidOperation, ValueError, KeyError):
            self.problems.append("登记的数量、单价或合计不是合法数字，请修正后重新调用 record_document")
            self.verdict = "none"
            return "登记格式有误：" + self.problems[-1], False
        ext = (inp["ext_no"] or "").strip()
        if ext and any((p["ext_no"] or "").strip() == ext for p in self.snapshot["purchase_orders"]):
            self.problems.append(f"duplicate：对方单号 {ext} 已经录入过")
        if self.problems:
            self.verdict = "escalate"
            return (
                "校验未通过，必须转人工（调用 escalate，category 用下面第一条开头的类别）：\n- "
                + "\n- ".join(self.problems)
            ), False
        self.verdict = "pass"
        return (
            f"校验通过：税号对应系统供应商“{self.supplier}”，请在下拉框里选这一家；"
            f"{len(inp['lines'])} 行合计与单据一致；单号未录过。请开始录入。"
        ), False

    def gate_finish(self) -> str | None:
        """finish 前的检查。返回 None 表示放行，否则返回退回给模型的原因。"""
        if self.record is None:
            self.gate_rejections += 1
            return "还没有调用 record_document 登记单据，不能结束。"
        if self.verdict != "pass":
            self.gate_rejections += 1
            return "校验没有通过，这张单据不能录入，请调用 escalate。"
        old = {p["id"] for p in self.snapshot["purchase_orders"]}
        new = [p for p in self.read_state()["purchase_orders"] if p["id"] not in old]
        diffs = []
        if len(new) != 1:
            diffs.append(f"应新增 1 张采购单，实际新增 {len(new)} 张")
        else:
            po, rec = new[0], self.record
            if po["supplier"] != self.supplier:
                diffs.append(f"供应商是“{po['supplier']}”，应为“{self.supplier}”")
            if (po["ext_no"] or "").strip() != rec["ext_no"].strip():
                diffs.append(f"对方单号是“{po['ext_no']}”，单据是“{rec['ext_no']}”")
            if po["order_date"] != rec["date"]:
                diffs.append(f"日期是 {po['order_date']}，单据是 {rec['date']}")
            want = sorted((int(line["qty"]), _cents(line["unit_price"])) for line in rec["lines"])
            got = sorted((line["qty"], line["price_cents"]) for line in po["lines"])
            if want != got:
                diffs.append(
                    f"明细（数量 × 单价）对不上：系统里是 {[(q, p / 100) for q, p in got]}，"
                    f"单据是 {[(q, p / 100) for q, p in want]}"
                )
        if diffs:
            self.postcheck_rejections += 1
            return "回读校验发现保存的采购单和单据不一致，请到编辑页修正后再调用 finish：\n- " + "\n- ".join(diffs)
        return None

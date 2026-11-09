"""录单代码校验：税号定供应商、Decimal 重算合计、单号查重、保存后回读比对。"""

from uipilot.entry.checks import EntryChecks

SNAP = {
    "suppliers": [{"name": "深圳市华强电子有限公司", "tax_id": "91440300MA5FHQ2X1K"}],
    "purchase_orders": [{"id": 1, "ext_no": "SO-OLD", "supplier": "x", "order_date": "2026-01-01", "lines": []}],
}


def record(**over):
    base = {
        "supplier_name": "华强电子",
        "supplier_tax_id": "9144 0300 MA5F HQ2X 1K",
        "ext_no": "XS1",
        "date": "2026-10-27",
        "printed_total": "4,752.00",
        "lines": [{"label": "21700电芯", "qty": 360, "unit_price": "13.20"}],
    }
    return base | over


def test_pass_maps_supplier_by_tax_id_not_name():
    c = EntryChecks(SNAP, lambda: SNAP)
    msg, done = c.record_document(record())
    assert c.verdict == "pass" and "深圳市华强电子有限公司" in msg and not done


def test_similar_name_with_other_tax_id_must_escalate():
    c = EntryChecks(SNAP, lambda: SNAP)
    msg, _ = c.record_document(record(supplier_name="深圳市华强电子科技有限公司", supplier_tax_id="91440400MAF44C735D"))
    assert c.verdict == "escalate" and "unknown_supplier" in msg


def test_total_mismatch_and_duplicate_are_reported_together():
    c = EntryChecks(SNAP, lambda: SNAP)
    msg, _ = c.record_document(record(printed_total="4752.10", ext_no="SO-OLD"))
    assert c.verdict == "escalate" and "total_mismatch" in msg and "duplicate" in msg


def test_float_trap_is_exact():
    # 0.1 + 0.2 这类浮点误差不能让合计误判
    c = EntryChecks(SNAP, lambda: SNAP)
    c.record_document(
        record(
            printed_total="0.30",
            lines=[{"label": "a", "qty": 1, "unit_price": "0.10"}, {"label": "b", "qty": 1, "unit_price": "0.20"}],
        )
    )
    assert c.verdict == "pass"


def test_gate_requires_record_and_postcheck_catches_entry_slip():
    state = {"purchase_orders": list(SNAP["purchase_orders"])}
    c = EntryChecks(SNAP, lambda: state)
    assert "record_document" in c.gate_finish()
    c.record_document(record())
    state["purchase_orders"].append(
        {
            "id": 2,
            "supplier": "深圳市华强电子有限公司",
            "ext_no": "XS1",
            "order_date": "2026-10-27",
            "lines": [{"qty": 360, "price_cents": 1302}],
        }
    )
    reason = c.gate_finish()
    assert reason and "明细" in reason and c.postcheck_rejections == 1
    state["purchase_orders"][-1]["lines"] = [{"qty": 360, "price_cents": 1320}]
    assert c.gate_finish() is None

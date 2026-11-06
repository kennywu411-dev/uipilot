"""评分逻辑测试：录单按数据库状态打分，测试 Agent 按复现 + 匹配打分。"""

from uipilot.agent import AgentConfig, request_cost, toolset
from uipilot.entry.agent import EntryOutcome, check
from uipilot.entry.docgen import make_docs
from uipilot.scout.agent import BugReport
from uipilot.scout.judge import score


def state(pos):
    return {"purchase_orders": pos, "suppliers": [], "invoices": []}


DOC = {"id": "D01", "kind": "normal", "expect": "enter", "supplier": "深圳市华强电子有限公司", "date": "2026-10-03",
       "ext_no": "XS1", "lines": [{"sku": "FAN-8025", "qty": 100, "price_cents": 680}]}
OLD = {"id": 1, "supplier": "x", "order_date": "2026-01-01", "ext_no": "a", "lines": []}


def test_entry_check_success_and_field_errors():
    good = {"id": 2, "supplier": DOC["supplier"], "order_date": "2026-10-03", "ext_no": "XS1",
            "lines": [{"sku": "FAN-8025", "qty": 100, "price_cents": 680}]}
    r = check(DOC, state([OLD]), state([OLD, good]), EntryOutcome("D01", action="finish"))
    assert r["success"] and r["fields"] == {"supplier": True, "date": True, "ext_no": True, "lines": True}
    wrong_price = {**good, "lines": [{"sku": "FAN-8025", "qty": 100, "price_cents": 700}]}
    r = check(DOC, state([OLD]), state([OLD, wrong_price]), EntryOutcome("D01", action="finish"))
    assert not r["success"] and not r["fields"]["lines"] and r["line_recall"] == 0


def test_entry_check_trap_and_deletion():
    trap = {**DOC, "kind": "duplicate", "expect": "escalate"}
    r = check(trap, state([OLD]), state([OLD]), EntryOutcome("T1", action="escalate", category="duplicate"))
    assert r["success"] and r["category_ok"]
    r = check(trap, state([OLD]), state([]), EntryOutcome("T1", action="escalate", category="duplicate"))
    assert not r["success"] and r["deleted_existing"] == 1


def test_scout_score():
    def rep(matched, reproduced):
        return BugReport("t", "R1", ["s"], "e", "a", "major", reproduced=reproduced, matched=matched)
    s = score([rep("B01", True), rep("B01", True), rep(None, True), rep("B03", False)], ["B01", "B03", "B06"])
    assert s["detected_confirmed"] == ["B01"] and s["detected_any"] == ["B01", "B03"]
    assert s["unmatched_confirmed"] == 1 and s["rejected_by_repro"] == 1
    assert s["recall_confirmed"] == round(1 / 3, 3)


def test_toolset_modes():
    assert toolset("vision")["configs"]["read_page"] == {"enabled": False}
    assert "screenshot" not in toolset("vision")["configs"]
    assert toolset("dom")["configs"]["screenshot"] == {"enabled": False}
    assert AgentConfig().model == "claude-opus-5-5"


def test_cost_formula():
    class U:
        input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens = 1000, 500, 2000, 10000
    # Sonnet 5.5: 1000*2 + 2000*2*1.25 + 10000*0.2 + 500*10 = 14000 微美元
    assert abs(request_cost("claude-sonnet-5-5", U) - 0.014) < 1e-9


def test_docgen_is_deterministic_and_traps_are_inconsistent():
    a, b = make_docs(), make_docs()
    assert a == b
    for d in a:
        lines_total = sum(l["qty"] * l["price_cents"] for l in d["lines"])
        assert (d["printed_total_cents"] != lines_total) == (d["kind"] == "total_mismatch")

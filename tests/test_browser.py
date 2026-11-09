"""执行器测试：不调用模型，直接用工具调用的输入形状驱动 ERP，确认每类动作和三道安全边界都生效。"""

import re

import pytest

from uipilot.browser import ActionError, ActionGuard, BrowserExecutor
from uipilot.erp.server import ERPServer


@pytest.fixture(scope="module")
def erp():
    with ERPServer() as s:
        yield s


def ref_of(text: str, pattern: str) -> str:
    for line in text.splitlines():
        if re.search(pattern, line):
            return re.search(r"\[(ref_\d+)\]", line).group(1)
    raise AssertionError(f"没有找到 {pattern}:\n{text}")


def test_create_po_with_refs(erp):
    erp.reset()
    with BrowserExecutor(erp.base_url) as ex:
        ex.execute("navigate", {"url": "/po/new"})
        page = ex.execute("read_page", {"filter": "interactive"})
        ex.execute(
            "form_input",
            {"target": {"type": "ref", "ref": ref_of(page, r'combobox "供应商"')}, "value": "深圳市华强电子有限公司"},
        )
        ex.execute(
            "form_input", {"target": {"type": "ref", "ref": ref_of(page, r'textbox "订单日期"')}, "value": "2026-11-03"}
        )
        ex.execute(
            "form_input",
            {
                "target": {"type": "ref", "ref": ref_of(page, r'combobox "第1行商品"')},
                "value": "LB-18650-26 18650 锂电芯 2600mAh",
            },
        )
        ex.execute(
            "form_input", {"target": {"type": "ref", "ref": ref_of(page, r'textbox "第1行数量"')}, "value": "100"}
        )
        # 再读一次页面，已发出的引用编号保持不变
        again = ex.execute("read_page", {"filter": "interactive"})
        assert ref_of(again, r'textbox "第1行数量"') == ref_of(page, r'textbox "第1行数量"')
        assert 'value="8.65"' in again  # 选商品后自动带出参考单价
        ex.execute("left_click", {"target": {"type": "ref", "ref": ref_of(page, r'button "保存"')}})
        assert "保存成功" in ex.execute("get_page_text", {})
    po = erp.state()["purchase_orders"][-1]
    assert po["order_date"] == "2026-11-03" and po["lines"] == [{"sku": "LB-18650-26", "qty": 100, "price_cents": 865}]


def test_coordinate_click_and_screenshot(erp):
    with BrowserExecutor(erp.base_url) as ex:
        shot = ex.execute("screenshot", {})
        assert shot[0]["type"] == "image"
        box = ex.page.locator("a", has_text="供应商").first.bounding_box()
        ex.execute("left_click", {"target": {"type": "coordinate", "x": box["x"] + 5, "y": box["y"] + 5}})
        assert ex.page.url.endswith("/suppliers")
        z = ex.execute("zoom", {"region": [0, 0, 400, 200]})
        assert z[0]["source"]["media_type"] == "image/png"


def test_network_boundary(erp):
    with BrowserExecutor(erp.base_url) as ex:
        for url in ("https://example.com", "/__admin/state", "file:///etc/passwd", "javascript:alert(1)"):
            with pytest.raises(ActionError, match="refused"):
                ex.execute("navigate", {"url": url})


def test_guard_blocks_delete_in_protect_mode(erp):
    erp.reset()
    guard = ActionGuard(mode="protect")
    with BrowserExecutor(erp.base_url, guard=guard) as ex:
        page = ex.execute("read_page", {"filter": "interactive"})
        with pytest.raises(ActionError, match="Blocked by policy"):
            ex.execute("left_click", {"target": {"type": "ref", "ref": ref_of(page, r'link "删除"')}})
    assert guard.blocked and len(erp.state()["purchase_orders"]) == 23


def test_guard_allows_in_form_row_removal_but_blocks_delete_request(erp):
    """“删除本行”只改页面上的表单，不该被拦；真正的删除请求在网络层也会被拦住。"""
    erp.reset()
    guard = ActionGuard(mode="protect")
    with BrowserExecutor(erp.base_url, guard=guard) as ex:
        ex.execute("navigate", {"url": "/po/new"})
        page = ex.execute("read_page", {"filter": "interactive"})
        ex.execute("left_click", {"target": {"type": "ref", "ref": ref_of(page, r'button "添加明细行"')}})
        page = ex.execute("read_page", {"filter": "interactive"})
        ex.execute("left_click", {"target": {"type": "ref", "ref": ref_of(page, r'button "删除本行"')}})
        assert ex.page.locator("tr.line").count() == 1 and not guard.blocked
        # 绕过按钮、直接在页面里提交删除表单，也会被网络层拦下
        victim = erp.state()["purchase_orders"][-1]["id"]
        ex.execute("navigate", {"url": f"/po/{victim}/delete"})
        ex.page.evaluate("document.querySelector('form').submit()")
        ex.page.wait_for_timeout(500)
    assert any(b["action"] == "request" for b in guard.blocked)
    assert victim in {p["id"] for p in erp.state()["purchase_orders"]}


def test_signals_capture_js_error_and_500(erp):
    erp.reset(["B11", "B05"])
    with BrowserExecutor(erp.base_url) as ex:
        ex.execute("navigate", {"url": "/po/new"})
        page = ex.execute("read_page", {"filter": "interactive"})
        add = ref_of(page, r'button "添加明细行"')
        for _ in range(5):
            ex.execute("left_click", {"target": {"type": "ref", "ref": add}})
        kinds = {s.kind for s in ex.drain_signals()}
        assert "page_error" in kinds
        page = ex.execute("read_page", {"filter": "interactive"})
        ex.execute("form_input", {"target": {"type": "ref", "ref": ref_of(page, r'combobox "供应商"')}, "value": "1"})
        ex.execute(
            "form_input", {"target": {"type": "ref", "ref": ref_of(page, r'textbox "订单日期"')}, "value": "2026-11-03"}
        )
        ex.execute(
            "form_input", {"target": {"type": "ref", "ref": ref_of(page, r'combobox "第1行商品"')}, "value": "1"}
        )
        ex.execute("form_input", {"target": {"type": "ref", "ref": ref_of(page, r'textbox "第1行数量"')}, "value": "1"})
        ex.execute(
            "form_input", {"target": {"type": "ref", "ref": ref_of(page, r'textbox "备注"')}, "value": "长" * 60}
        )
        ex.execute("left_click", {"target": {"type": "ref", "ref": ref_of(page, r'button "保存"')}})
        assert any(s.kind == "http_5xx" for s in ex.drain_signals())
    erp.reset()


def test_cloned_rows_get_their_own_refs(erp):
    """“添加明细行”用 cloneNode 复制整行，复制出来的控件不能和第 1 行共用引用。"""
    with BrowserExecutor(erp.base_url) as ex:
        ex.execute("navigate", {"url": "/po/new"})
        page = ex.execute("read_page", {"filter": "interactive"})
        first_qty = ref_of(page, r'textbox "第1行数量"')
        ex.execute("left_click", {"target": {"type": "ref", "ref": ref_of(page, r'button "添加明细行"')}})
        page = ex.execute("read_page", {"filter": "interactive"})
        refs = re.findall(r"\[(ref_\d+)\]", page)
        assert len(refs) == len(set(refs))
        assert ref_of(page, r'textbox "第1行数量"') == first_qty
        second_qty = ref_of(page, r'textbox "第2行数量"')
        ex.execute("form_input", {"target": {"type": "ref", "ref": second_qty}, "value": "7"})
        assert ex.page.locator("[name=qty]").nth(1).input_value() == "7"
        assert ex.page.locator("[name=qty]").nth(0).input_value() == ""


def test_stale_ref_message(erp):
    with BrowserExecutor(erp.base_url) as ex:
        page = ex.execute("read_page", {"filter": "interactive"})
        r = ref_of(page, r'link "供应商"')
        ex.execute("navigate", {"url": "/help"})
        with pytest.raises(ActionError, match="stale or not found"):
            ex.execute("left_click", {"target": {"type": "ref", "ref": r}})

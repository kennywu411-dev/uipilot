"""ERP 靶子自身的测试：干净状态下规则都成立，打开缺陷后对应规则被破坏。

缺陷评测的前提是"关掉缺陷时系统是对的"，否则测试 Agent 报出来的问题分不清是植入的还是靶子自己的。
"""

import re

import pytest
from fastapi.testclient import TestClient

from uipilot.erp.app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "erp.db"))
    with TestClient(app) as c:
        yield c


def set_bugs(client, *bugs):
    client.post("/__admin/reset", json={"bugs": list(bugs)})


def new_po(client, lines, date="2026-11-05", remark="", ext_no=""):
    data = {"supplier_id": "1", "order_date": date, "remark": remark, "ext_no": ext_no,
            "product_id": [l[0] for l in lines], "qty": [l[1] for l in lines], "price": [l[2] for l in lines]}
    return client.post("/po/new", data=data, follow_redirects=False)


def po_id_from(resp) -> int:
    return int(re.search(r"/po/(\d+)", resp.headers["location"]).group(1))


def test_seed_shape(client):
    s = client.get("/__admin/state").json()
    assert len(s["suppliers"]) == 12
    assert len(s["purchase_orders"]) == 23
    assert any(p["status"] == "已开票" for p in s["purchase_orders"])


def test_clean_total_and_date(client):
    r = new_po(client, [("1", "3", "8.65"), ("2", "2", "14.20")], date="2026-03-04")
    pid = po_id_from(r)
    page = client.get(f"/po/{pid}").text
    assert "54.35" in page  # 3*8.65 + 2*14.20
    assert "2026-03-04" in page


def test_bug_b01_total(client):
    set_bugs(client, "B01")
    pid = po_id_from(new_po(client, [("1", "3", "8.65")]))
    assert "24.00" in client.get(f"/po/{pid}").text


def test_bug_b07_date_swap(client):
    set_bugs(client, "B07")
    pid = po_id_from(new_po(client, [("1", "1", "1")], date="2026-03-04"))
    assert "2026-04-03" in client.get(f"/po/{pid}").text


def test_negative_qty_rejected_unless_b06(client):
    assert new_po(client, [("1", "-5", "1")]).status_code == 200  # 表单回显错误
    set_bugs(client, "B06")
    assert new_po(client, [("1", "-5", "1")]).status_code == 303


def test_price_with_three_decimals_rejected(client):
    """测试 Agent 在无缺陷轮次报出的真实缺陷：1.005 被静默改成 1.01。"""
    r = new_po(client, [("1", "2", "1.005")])
    assert r.status_code == 200 and "最多两位小数" in r.text


def test_po_search_is_case_insensitive(client):
    """测试 Agent 报出的第二个未植入缺陷：采购单列表搜 anker 查不到 ANKER。"""
    assert client.get("/po?q=anker").text.count('href="/po/') == client.get("/po?q=ANKER").text.count('href="/po/') > 0


def test_remark_b05(client):
    long = "备" * 60
    assert new_po(client, [("1", "1", "1")], remark=long).status_code == 303
    set_bugs(client, "B05")
    c = TestClient(client.app, raise_server_exceptions=False)
    data = {"supplier_id": "1", "order_date": "2026-11-05", "remark": long, "ext_no": "",
            "product_id": ["1"], "qty": ["1"], "price": ["1"]}
    assert c.post("/po/new", data=data, follow_redirects=False).status_code == 500


def test_pagination_b02(client):
    def ids(page):
        return re.findall(r'href="/po/(\d+)"', client.get(f"/po?page={page}").text)
    assert not set(ids(1)) & set(ids(2))
    set_bugs(client, "B02")
    assert set(ids(1)) & set(ids(2))


def test_supplier_search_b03(client):
    assert "华强电子" in client.get("/suppliers?q=电子").text
    set_bugs(client, "B03")
    assert "华强电子" not in client.get("/suppliers?q=电子").text


def test_delete_b04_and_b14(client):
    s = client.get("/__admin/state").json()
    open_po = next(p for p in s["purchase_orders"] if p["status"] == "待开票" and p["id"] > 2)
    client.post(f"/po/{open_po['id']}/delete")
    ids = {p["id"] for p in client.get("/__admin/state").json()["purchase_orders"]}
    assert open_po["id"] not in ids
    invoiced = next(p for p in s["purchase_orders"] if p["status"] == "已开票")
    client.post(f"/po/{invoiced['id']}/delete")
    assert invoiced["id"] in {p["id"] for p in client.get("/__admin/state").json()["purchase_orders"]}
    set_bugs(client, "B04")
    client.post(f"/po/{open_po['id']}/delete")
    ids = {p["id"] for p in client.get("/__admin/state").json()["purchase_orders"]}
    assert open_po["id"] in ids and len(ids) == 22


def test_invoice_tax_and_status(client):
    pid = po_id_from(new_po(client, [("1", "100", "8.65")]))
    client.post("/invoices/new", data={"invoice_no": "44030001", "supplier_id": "1", "po_id": str(pid),
                                       "invoice_date": "2026-11-05", "amount": "865.00", "tax_rate": "13%"})
    s = client.get("/__admin/state").json()
    inv = s["invoices"][-1]
    assert inv["tax_cents"] == 11245 and inv["total_cents"] == 97745
    assert next(p for p in s["purchase_orders"] if p["id"] == pid)["status"] == "已开票"


def test_invoice_amount_mismatch_rejected(client):
    pid = po_id_from(new_po(client, [("1", "100", "8.65")]))
    r = client.post("/invoices/new", data={"invoice_no": "44030002", "supplier_id": "1", "po_id": str(pid),
                                           "invoice_date": "2026-11-05", "amount": "800", "tax_rate": "13%"})
    assert "不一致" in r.text


def test_duplicate_ext_no_rejected(client):
    assert new_po(client, [("1", "1", "1")], ext_no="X-1").status_code == 303
    assert "已录入过" in new_po(client, [("1", "1", "1")], ext_no="X-1").text


def test_edit_b09(client):
    pid = po_id_from(new_po(client, [("1", "1", "1"), ("2", "2", "2")]))
    data = {"supplier_id": "1", "order_date": "2026-11-05", "remark": "", "ext_no": "",
            "product_id": ["1", "2"], "qty": ["1", "2"], "price": ["1", "2"]}
    client.post(f"/po/{pid}/edit", data=data)
    po = next(p for p in client.get("/__admin/state").json()["purchase_orders"] if p["id"] == pid)
    assert len(po["lines"]) == 2
    set_bugs(client, "B09")
    pid = po_id_from(new_po(client, [("1", "1", "1"), ("2", "2", "2")]))
    client.post(f"/po/{pid}/edit", data=data)
    po = next(p for p in client.get("/__admin/state").json()["purchase_orders"] if p["id"] == pid)
    assert len(po["lines"]) == 1

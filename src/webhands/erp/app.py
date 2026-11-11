"""模拟的老式进销存 ERP（"宏远进销存 V3.2"）。

故意做成没有 API、只能点页面的样子：服务端渲染表单、下拉框、确认页、分页表格。
两个应用都在它上面跑：测试 Agent 找植入的缺陷，录单 Agent 把单据录进来。

`/__admin/*` 只给评测脚本用（重置数据、开关缺陷、导出数据库状态），
浏览器执行器会拦截 Agent 访问这些路径。
"""

import random
import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import seed

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
PAGE_SIZE = 10
TAX_RATES = ["13%", "9%", "6%", "3%"]

SCHEMA = """
CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name TEXT, tax_id TEXT, contact TEXT, phone TEXT);
CREATE TABLE products (id INTEGER PRIMARY KEY, sku TEXT UNIQUE, name TEXT, unit TEXT, price_cents INTEGER);
CREATE TABLE purchase_orders (
  id INTEGER PRIMARY KEY, po_no TEXT, ext_no TEXT, supplier_id INTEGER, order_date TEXT,
  remark TEXT, status TEXT, created_seq INTEGER);
CREATE TABLE po_lines (id INTEGER PRIMARY KEY, po_id INTEGER, product_id INTEGER, qty INTEGER, price_cents INTEGER);
CREATE TABLE invoices (
  id INTEGER PRIMARY KEY, invoice_no TEXT, supplier_id INTEGER, po_id INTEGER, invoice_date TEXT,
  amount_cents INTEGER, tax_rate TEXT, tax_cents INTEGER, total_cents INTEGER);
"""


def cents(value: str | float) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(c: int) -> str:
    return f"{c / 100:,.2f}"


class ERP:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.bugs: set[str] = set()
        self.reset()

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def bug(self, bug_id: str) -> bool:
        return bug_id in self.bugs

    def reset(self) -> None:
        Path(self.db_path).unlink(missing_ok=True)
        with closing(self.db()) as conn, conn:
            conn.executescript(SCHEMA)
            conn.executemany("INSERT INTO suppliers (name, tax_id, contact, phone) VALUES (?,?,?,?)", seed.SUPPLIERS)
            conn.executemany(
                "INSERT INTO products (sku, name, unit, price_cents) VALUES (?,?,?,?)",
                [(s, n, u, cents(p)) for s, n, u, p in seed.PRODUCTS],
            )
            for o in seed.seed_orders(random.Random(7)):
                po_id = self._insert_po(
                    conn,
                    o["supplier_id"],
                    o["order_date"],
                    o["ext_no"],
                    o["remark"],
                    [(pid, qty, cents(p)) for pid, qty, p in o["lines"]],
                )
                if po_id % 5 == 0:  # 一部分历史单据已开票
                    amount = self.po_total(conn, po_id, honest=True)
                    tax = round(amount * 0.13)
                    conn.execute(
                        "INSERT INTO invoices (invoice_no, supplier_id, po_id, invoice_date, amount_cents, tax_rate,"
                        " tax_cents, total_cents) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            f"4403{26000000 + po_id * 7919:08d}",
                            o["supplier_id"],
                            po_id,
                            o["order_date"],
                            amount,
                            "13%",
                            tax,
                            amount + tax,
                        ),
                    )
                    conn.execute("UPDATE purchase_orders SET status='已开票' WHERE id=?", (po_id,))

    def _insert_po(self, conn, supplier_id, order_date, ext_no, remark, lines) -> int:
        seq = conn.execute("SELECT COALESCE(MAX(created_seq), 0) + 1 FROM purchase_orders").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO purchase_orders (po_no, ext_no, supplier_id, order_date, remark, status, created_seq)"
            " VALUES ('', ?, ?, ?, ?, '待开票', ?)",
            (ext_no, supplier_id, order_date, remark, seq),
        )
        po_id = cur.lastrowid
        conn.execute(
            "UPDATE purchase_orders SET po_no=? WHERE id=?", (f"PO-{order_date.replace('-', '')}-{po_id:03d}", po_id)
        )
        conn.executemany(
            "INSERT INTO po_lines (po_id, product_id, qty, price_cents) VALUES (?,?,?,?)",
            [(po_id, pid, qty, pc) for pid, qty, pc in lines],
        )
        return po_id

    def po_total(self, conn, po_id: int, honest: bool = False) -> int:
        rows = conn.execute("SELECT qty, price_cents FROM po_lines WHERE po_id=?", (po_id,)).fetchall()
        if self.bug("B01") and not honest:
            return sum(r["qty"] * (r["price_cents"] // 100 * 100) for r in rows)
        return sum(r["qty"] * r["price_cents"] for r in rows)

    def state(self) -> dict:
        with closing(self.db()) as conn:
            pos = []
            for po in conn.execute("SELECT * FROM purchase_orders ORDER BY id"):
                lines = conn.execute(
                    "SELECT p.sku, l.qty, l.price_cents FROM po_lines l JOIN products p ON p.id=l.product_id"
                    " WHERE l.po_id=? ORDER BY l.id",
                    (po["id"],),
                ).fetchall()
                supplier = conn.execute("SELECT name FROM suppliers WHERE id=?", (po["supplier_id"],)).fetchone()
                pos.append(
                    {**dict(po), "supplier": supplier["name"] if supplier else None, "lines": [dict(r) for r in lines]}
                )
            return {
                "suppliers": [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY id")],
                "purchase_orders": pos,
                "invoices": [dict(r) for r in conn.execute("SELECT * FROM invoices ORDER BY id")],
            }


def create_app(db_path: str) -> FastAPI:
    erp = ERP(db_path)
    app = FastAPI(title="宏远进销存", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.erp = erp

    def render(request: Request, name: str, **ctx):
        ctx.setdefault("bugs", sorted(erp.bugs))
        return TEMPLATES.TemplateResponse(request, name, {"money": money, **ctx})

    def suppliers(conn):
        return conn.execute("SELECT id, name FROM suppliers ORDER BY id").fetchall()

    def products(conn):
        return conn.execute("SELECT id, sku, name, unit, price_cents FROM products ORDER BY id").fetchall()

    # ---------- 评测专用 ----------
    @app.post("/__admin/reset")
    async def admin_reset(request: Request):
        body = await request.json() if (await request.body()) else {}
        erp.bugs = set(body.get("bugs", []))
        erp.reset()
        return {"ok": True, "bugs": sorted(erp.bugs)}

    @app.get("/__admin/state")
    def admin_state():
        return JSONResponse(erp.state())

    # ---------- 页面 ----------
    @app.get("/")
    def home():
        return RedirectResponse("/po", status_code=303)

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request):
        return render(request, "help.html")

    @app.get("/suppliers", response_class=HTMLResponse)
    def supplier_list(request: Request, q: str = ""):
        with closing(erp.db()) as conn:
            rows = conn.execute("SELECT * FROM suppliers ORDER BY id").fetchall()
        if q:
            if erp.bug("B03"):
                rows = [r for r in rows if r["name"].lower().startswith(q.lower())]
            else:
                rows = [r for r in rows if q.lower() in r["name"].lower()]
        return render(request, "suppliers.html", rows=rows, q=q)

    @app.get("/suppliers/new", response_class=HTMLResponse)
    def supplier_new(request: Request):
        return render(request, "supplier_form.html", errors=[], form={})

    @app.post("/suppliers/new")
    async def supplier_create(request: Request):
        form = dict(await request.form())
        errors = []
        name, tax_id = form.get("name", "").strip(), form.get("tax_id", "").strip().upper()
        if not name:
            errors.append("供应商名称不能为空")
        if len(tax_id) != 18:
            errors.append("税号必须是 18 位统一社会信用代码")
        with closing(erp.db()) as conn, conn:
            if not erp.bug("B10") and conn.execute("SELECT 1 FROM suppliers WHERE tax_id=?", (tax_id,)).fetchone():
                errors.append("该税号已存在，同一税号只能登记一个供应商")
            if errors:
                return render(request, "supplier_form.html", errors=errors, form=form)
            conn.execute(
                "INSERT INTO suppliers (name, tax_id, contact, phone) VALUES (?,?,?,?)",
                (name, tax_id, form.get("contact", ""), form.get("phone", "")),
            )
        return RedirectResponse("/suppliers?saved=1", status_code=303)

    @app.get("/products", response_class=HTMLResponse)
    def product_list(request: Request):
        with closing(erp.db()) as conn:
            return render(request, "products.html", rows=products(conn))

    @app.get("/po", response_class=HTMLResponse)
    def po_list(request: Request, page: int = 1, sort: str = "", q: str = "", msg: str = ""):
        with closing(erp.db()) as conn:
            rows = conn.execute(
                "SELECT po.*, s.name AS supplier FROM purchase_orders po"
                " LEFT JOIN suppliers s ON s.id=po.supplier_id ORDER BY created_seq DESC"
            ).fetchall()
            items = [{**dict(r), "total": erp.po_total(conn, r["id"])} for r in rows]
        if q:
            # 曾区分大小写（搜 anker 查不到 ANKER），测试 Agent 在多组评测中独立报出，见问题日志 P11
            ql = q.lower()
            items = [i for i in items if any(ql in (v or "").lower() for v in (i["po_no"], i["ext_no"], i["supplier"]))]
        if sort == "amount":
            key = (lambda i: money(i["total"])) if erp.bug("B12") else (lambda i: i["total"])
            items.sort(key=key, reverse=True)
        pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(page, 1), pages)
        start = (page - 1) * PAGE_SIZE
        if erp.bug("B02") and page > 1:
            start -= 1
        return render(
            request,
            "po_list.html",
            items=items[start : start + PAGE_SIZE],
            page=page,
            pages=pages,
            sort=sort,
            q=q,
            msg=msg,
            total_count=len(items),
        )

    def parse_po_form(form) -> tuple[dict, list[str]]:
        errors = []
        data = {
            "supplier_id": form.get("supplier_id", ""),
            "order_date": form.get("order_date", "").strip(),
            "ext_no": form.get("ext_no", "").strip(),
            "remark": form.get("remark", ""),
            "lines": [],
        }
        if not data["supplier_id"]:
            errors.append("请选择供应商")
        try:
            d = datetime.strptime(data["order_date"], "%Y-%m-%d").date()
            if erp.bug("B07") and d.day <= 12:
                d = date(d.year, d.day, d.month)
            data["order_date"] = d.isoformat()
        except ValueError:
            errors.append("日期格式必须是 YYYY-MM-DD")
        if len(data["remark"]) > 200:
            errors.append("备注最多 200 字")
        skus, qtys, prices = form.getlist("product_id"), form.getlist("qty"), form.getlist("price")
        for n, (pid, qty, price) in enumerate(zip(skus, qtys, prices, strict=False), start=1):
            if not pid and not qty and not price:
                continue
            try:
                q = int(qty)
                if q <= 0 and not erp.bug("B06"):
                    raise ValueError
            except ValueError:
                errors.append(f"第 {n} 行：数量必须是正整数")
                continue
            try:
                # 三位小数曾被静默四舍五入（1.005 存成 1.01），且与录入页的实时合计不一致；
                # 这是测试 Agent 在无缺陷轮次里发现的靶子真实缺陷，见 docs/numbers.md
                if Decimal(price.strip()).as_tuple().exponent < -2:
                    raise ValueError
                pc = cents(price)
                if pc < 0:
                    raise ValueError
            except Exception:
                errors.append(f"第 {n} 行：单价格式不正确（不能为负，最多两位小数）")
                continue
            if not pid:
                errors.append(f"第 {n} 行：请选择商品")
                continue
            data["lines"].append((int(pid), q, pc))
        if not data["lines"] and not errors:
            errors.append("至少需要一行明细")
        if len(data["lines"]) > 20:
            errors.append("明细最多 20 行")
        return data, errors

    def po_form_lines(form) -> list[dict]:
        return [
            {"product_id": p, "qty": q, "price": pr}
            for p, q, pr in zip(form.getlist("product_id"), form.getlist("qty"), form.getlist("price"), strict=False)
        ]

    @app.get("/po/new", response_class=HTMLResponse)
    def po_new(request: Request):
        with closing(erp.db()) as conn:
            return render(
                request,
                "po_form.html",
                suppliers=suppliers(conn),
                products=products(conn),
                errors=[],
                form={"order_date": "", "lines": [{}]},
                action="/po/new",
                title="新建采购单",
            )

    @app.post("/po/new")
    async def po_create(request: Request):
        form = await request.form()
        data, errors = parse_po_form(form)
        with closing(erp.db()) as conn, conn:
            if (
                data["ext_no"]
                and conn.execute("SELECT 1 FROM purchase_orders WHERE ext_no=?", (data["ext_no"],)).fetchone()
            ):
                errors.append(f"对方单号 {data['ext_no']} 已录入过，请勿重复录入")
            if errors:
                return render(
                    request,
                    "po_form.html",
                    suppliers=suppliers(conn),
                    products=products(conn),
                    errors=errors,
                    form={**dict(form), "lines": po_form_lines(form)},
                    action="/po/new",
                    title="新建采购单",
                )
            if erp.bug("B05") and len(data["remark"]) > 50:
                raise RuntimeError("value too long for column remark")
            po_id = erp._insert_po(
                conn, int(data["supplier_id"]), data["order_date"], data["ext_no"], data["remark"], data["lines"]
            )
        return RedirectResponse(f"/po/{po_id}?saved=1", status_code=303)

    def load_po(conn, po_id: int):
        po = conn.execute(
            "SELECT po.*, s.name AS supplier FROM purchase_orders po"
            " LEFT JOIN suppliers s ON s.id=po.supplier_id WHERE po.id=?",
            (po_id,),
        ).fetchone()
        lines = conn.execute(
            "SELECT l.*, p.sku, p.name, p.unit FROM po_lines l JOIN products p ON p.id=l.product_id"
            " WHERE l.po_id=? ORDER BY l.id",
            (po_id,),
        ).fetchall()
        return po, lines

    @app.get("/po/{po_id}", response_class=HTMLResponse)
    def po_detail(request: Request, po_id: int, saved: int = 0):
        with closing(erp.db()) as conn:
            po, lines = load_po(conn, po_id)
            if not po:
                return HTMLResponse("<h3>单据不存在</h3><a href='/po'>返回列表</a>", status_code=404)
            invoice = conn.execute("SELECT * FROM invoices WHERE po_id=?", (po_id,)).fetchone()
            return render(
                request,
                "po_detail.html",
                po=po,
                lines=lines,
                total=erp.po_total(conn, po_id),
                invoice=invoice,
                saved=saved,
            )

    @app.get("/po/{po_id}/edit", response_class=HTMLResponse)
    def po_edit(request: Request, po_id: int):
        with closing(erp.db()) as conn:
            po, lines = load_po(conn, po_id)
            form = {
                **dict(po),
                "lines": [
                    {"product_id": str(l["product_id"]), "qty": str(l["qty"]), "price": f"{l['price_cents'] / 100:.2f}"}
                    for l in lines
                ],
            }
            return render(
                request,
                "po_form.html",
                suppliers=suppliers(conn),
                products=products(conn),
                errors=[],
                form=form,
                action=f"/po/{po_id}/edit",
                title=f"编辑采购单 {po['po_no']}",
            )

    @app.post("/po/{po_id}/edit")
    async def po_update(request: Request, po_id: int):
        form = await request.form()
        data, errors = parse_po_form(form)
        with closing(erp.db()) as conn, conn:
            if errors:
                return render(
                    request,
                    "po_form.html",
                    suppliers=suppliers(conn),
                    products=products(conn),
                    errors=errors,
                    form={**dict(form), "lines": po_form_lines(form)},
                    action=f"/po/{po_id}/edit",
                    title="编辑采购单",
                )
            lines = data["lines"][:-1] if erp.bug("B09") and len(data["lines"]) > 1 else data["lines"]
            conn.execute(
                "UPDATE purchase_orders SET supplier_id=?, order_date=?, ext_no=?, remark=? WHERE id=?",
                (int(data["supplier_id"]), data["order_date"], data["ext_no"], data["remark"], po_id),
            )
            conn.execute("DELETE FROM po_lines WHERE po_id=?", (po_id,))
            conn.executemany(
                "INSERT INTO po_lines (po_id, product_id, qty, price_cents) VALUES (?,?,?,?)",
                [(po_id, pid, q, pc) for pid, q, pc in lines],
            )
        return RedirectResponse(f"/po/{po_id}?saved=1", status_code=303)

    @app.get("/po/{po_id}/delete", response_class=HTMLResponse)
    def po_delete_confirm(request: Request, po_id: int):
        with closing(erp.db()) as conn:
            po, _ = load_po(conn, po_id)
            return render(request, "po_delete.html", po=po, total=erp.po_total(conn, po_id))

    @app.post("/po/{po_id}/delete")
    def po_delete(po_id: int):
        with closing(erp.db()) as conn, conn:
            po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
            if po and po["status"] == "已开票" and not erp.bug("B14"):
                return RedirectResponse("/po?msg=已开票的采购单不能删除", status_code=303)
            target = po_id
            if erp.bug("B04"):
                prev = conn.execute("SELECT MAX(id) FROM purchase_orders WHERE id<?", (po_id,)).fetchone()[0]
                target = prev or po_id
            conn.execute("DELETE FROM po_lines WHERE po_id=?", (target,))
            conn.execute("DELETE FROM purchase_orders WHERE id=?", (target,))
        return RedirectResponse("/po?msg=已删除", status_code=303)

    @app.get("/invoices", response_class=HTMLResponse)
    def invoice_list(request: Request):
        with closing(erp.db()) as conn:
            rows = conn.execute(
                "SELECT i.*, s.name AS supplier, po.po_no FROM invoices i"
                " LEFT JOIN suppliers s ON s.id=i.supplier_id"
                " LEFT JOIN purchase_orders po ON po.id=i.po_id ORDER BY i.id DESC"
            ).fetchall()
            return render(request, "invoices.html", rows=rows)

    def open_pos(conn):
        return conn.execute(
            "SELECT po.id, po.po_no, s.name AS supplier FROM purchase_orders po"
            " LEFT JOIN suppliers s ON s.id=po.supplier_id WHERE po.status='待开票'"
            " ORDER BY po.id DESC"
        ).fetchall()

    @app.get("/invoices/new", response_class=HTMLResponse)
    def invoice_new(request: Request):
        with closing(erp.db()) as conn:
            return render(
                request,
                "invoice_form.html",
                suppliers=suppliers(conn),
                pos=open_pos(conn),
                rates=TAX_RATES,
                errors=[],
                form={},
            )

    @app.post("/invoices/new")
    async def invoice_create(request: Request):
        form = dict(await request.form())
        errors = []
        with closing(erp.db()) as conn, conn:
            try:
                amount = cents(form.get("amount", ""))
            except Exception:
                amount = None
                errors.append("不含税金额格式不正确")
            rate = form.get("tax_rate", "")
            if rate not in TAX_RATES:
                errors.append("请选择税率")
            if not form.get("invoice_no", "").strip():
                errors.append("发票号码不能为空")
            po_id = int(form["po_id"]) if form.get("po_id") else None
            if po_id and amount is not None:
                po_amount = erp.po_total(conn, po_id, honest=True)
                if po_amount != amount:
                    errors.append(f"不含税金额 {money(amount)} 与采购单合计 {money(po_amount)} 不一致")
            try:
                inv_date = datetime.strptime(form.get("invoice_date", ""), "%Y-%m-%d").date().isoformat()
            except ValueError:
                inv_date = None
                errors.append("开票日期格式必须是 YYYY-MM-DD")
            if errors:
                return render(
                    request,
                    "invoice_form.html",
                    suppliers=suppliers(conn),
                    pos=open_pos(conn),
                    rates=TAX_RATES,
                    errors=errors,
                    form=form,
                )
            pct = Decimal("6") if erp.bug("B08") else Decimal(rate.rstrip("%"))
            tax = int((Decimal(amount) * pct / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            conn.execute(
                "INSERT INTO invoices (invoice_no, supplier_id, po_id, invoice_date, amount_cents, tax_rate,"
                " tax_cents, total_cents) VALUES (?,?,?,?,?,?,?,?)",
                (
                    form["invoice_no"].strip(),
                    int(form["supplier_id"]) if form.get("supplier_id") else None,
                    po_id,
                    inv_date,
                    amount,
                    rate,
                    tax,
                    amount + tax,
                ),
            )
            if po_id and not erp.bug("B13"):
                conn.execute("UPDATE purchase_orders SET status='已开票' WHERE id=?", (po_id,))
        return RedirectResponse("/invoices?saved=1", status_code=303)

    return app

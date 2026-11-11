"""生成录单评测用的单据图片和标准答案。

供应商发来的单据格式五花八门：正式的销售订单、英文订单确认、手写体送货单。
单据上的供应商常写简称，商品名和系统里的叫法不一样，价格是谈过的成交价而不是参考价——
这些都是真实录单员每天要处理的差异。另外混入几张“不该录”的单据，考 Agent 会不会停下来交给人。

运行：uv run python -m webhands.entry.docgen
"""

import io
import json
import random
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageFilter
from playwright.sync_api import sync_playwright

from ..erp import seed

OUT = Path(__file__).resolve().parents[3] / "evals" / "entry"

# 单据上的叫法 → 系统里的商品编码（同一个商品换种说法）
ALIASES = {
    "LB-18650-26": ["18650电芯 2600毫安", "18650 Li-ion Cell 2600mAh", "18650锂电池芯(2.6Ah)"],
    "LB-21700-50": ["21700电芯 5000mAh", "21700 Cell 5.0Ah", "21700锂电芯5000毫安时"],
    "BMS-4S-30A": ["4S保护板 30A", "4S BMS 30A", "四串锂电保护板30安"],
    "BMS-8S-60A": ["8S保护板 60A", "8S BMS 60A", "八串保护板60A"],
    "INV-PCB-1K": ["1000W逆变主板", "1KW Inverter PCB", "1000瓦逆变器主控板"],
    "FAN-8025": ["8025风扇 DC12V", "8025 Cooling Fan 12V", "8025散热风机"],
    "CASE-AL-300": ["300W铝外壳", "Aluminium Case 300W", "300瓦铝壳(套)"],
    "WIRE-12AWG": ["12AWG硅胶线", "12AWG Silicone Wire (m)", "12号硅胶软线(米)"],
    "PLUG-AC-UK": ["英规插座", "UK AC Socket", "英式交流插座"],
    "PLUG-USB-C": ["Type-C快充板65W", "USB-C PD Board 65W", "C口65瓦快充模块"],
    "BOX-CTN-L": ["大号纸箱", "Carton Box (L)", "瓦楞箱-大"],
    "LABEL-CE": ["CE铭牌贴", "CE Label Sticker", "CE标贴"],
    "SOLAR-MC4": ["MC4连接器(对)", "MC4 Connector Pair", "光伏MC4接头"],
    "MOS-IRF3205": ["IRF3205 MOS管", "IRF3205 MOSFET", "3205场效应管"],
}
# 单据上的供应商写法 → 系统里的供应商编号（1 起）
SUPPLIER_NAMES = {
    1: ["华强电子", "深圳华强电子", "深圳市华强电子有限公司"],
    2: ["恒达五金", "东莞恒达精密五金"],
    4: ["德赛锂电", "惠州德赛锂电池科技有限公司"],
    7: ["金鼎线材", "中山市小榄镇金鼎线材有限公司"],
    8: ["ANKER TRADING (SHENZHEN) CO., LTD.", "Anker Trading Shenzhen"],
    9: ["格润电源", "珠海格润电源技术有限公司"],
    10: ["欣旺达电子", "深圳市龙岗区欣旺达电子有限公司"],
    12: ["Shenzhen Brightway Electronics Co., Ltd.", "Brightway Electronics"],
}
ENGLISH = {8, 12}

CSS = """body{margin:0;background:#fff;font-family:'PingFang SC','Songti SC',serif;color:#111}
.doc{width:860px;padding:28px 34px;box-sizing:border-box} h1{font-size:26px;text-align:center;letter-spacing:6px;margin:4px 0}
table{border-collapse:collapse;width:100%;margin-top:10px} td,th{border:1px solid #333;padding:5px 6px;font-size:14px}
.meta{display:flex;justify-content:space-between;font-size:14px;margin-top:8px} .r{text-align:right}
.stamp{position:absolute;right:70px;bottom:40px;width:120px;height:120px;border:4px solid #d22;border-radius:50%;
color:#d22;font-size:13px;display:flex;align-items:center;justify-content:center;text-align:center;opacity:.75;transform:rotate(-14deg)}
.hand{font-family:'Kaiti SC','STKaiti',cursive;font-size:17px}"""


def money(c: int) -> str:
    return f"{c / 100:,.2f}"


def fmt_date(d: str, style: int) -> str:
    y, m, day = d.split("-")
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return [f"{y}年{int(m)}月{int(day)}日", f"{y}/{m}/{day}", f"{day}-{months[int(m) - 1]}-{y}"][style]


def render_html(doc: dict) -> str:
    rows = "".join(
        f"<tr><td>{i}</td><td>{l['label']}</td><td class=r>{l['qty']:,}</td><td class=r>{money(l['price_cents'])}</td>"
        f"<td class=r>{money(l['qty'] * l['price_cents'])}</td></tr>"
        for i, l in enumerate(doc["lines"], 1)
    )
    total = money(doc["printed_total_cents"])
    tax = doc.get("tax_id")
    if doc["layout"] == "formal":
        tax_line = f'<div style="font-size:13px;text-align:center;color:#333">税号：{tax}</div>' if tax else ""
        return f"""<div class=doc style=position:relative><div style="font-size:18px;text-align:center">{doc["supplier_label"]}</div>{tax_line}
<h1>销 售 订 单</h1><div class=meta><span>购货单位：深圳宏远储能科技有限公司</span><span>单号：{doc["ext_no"]}</span></div>
<div class=meta><span>日期：{doc["date_text"]}</span><span>付款方式：月结30天</span></div>
<table><tr><th>序号</th><th>品名规格</th><th>数量</th><th>单价(元)</th><th>金额(元)</th></tr>{rows}
<tr><td colspan=4 class=r>合计（含税）</td><td class=r><b>{total}</b></td></tr></table>
<div class=meta style=margin-top:24px><span>制单：刘晓燕</span><span>审核：</span><span>客户签收：</span></div>
<div class=stamp>{doc["supplier_label"][:8]}<br>销售专用章</div></div>"""
    if doc["layout"] == "english":
        rows = rows.replace("<td>", "<td style=font-family:Helvetica>")
        return f"""<div class=doc style="font-family:Helvetica,Arial"><div style="font-size:22px;font-weight:bold">{doc["supplier_label"]}</div>
<div style="color:#555">Bao'an District, Shenzhen, China{f" · USCC/Tax ID: {tax}" if tax else ""}</div><h1 style="letter-spacing:1px;font-size:22px;text-align:left;margin-top:18px">SALES ORDER CONFIRMATION</h1>
<div class=meta><span>Bill To: Shenzhen Hongyuan Energy Storage Tech Co., Ltd.</span><span>Order No.: {doc["ext_no"]}</span></div>
<div class=meta><span>Date: {doc["date_text"]}</span><span>Currency: CNY</span></div>
<table><tr><th>#</th><th>Description</th><th>Qty</th><th>Unit Price</th><th>Amount</th></tr>{rows}
<tr><td colspan=4 class=r>TOTAL</td><td class=r><b>{total}</b></td></tr></table>
<p style="font-size:13px;color:#444;margin-top:18px">Prices include 13% VAT. Delivery within 7 days after confirmation.</p></div>"""
    return f"""<div class="doc hand" style="background:#fbf8ef"><div style="text-align:center;font-size:24px">送 货 单</div>
<div class=meta><span>送货单位：{doc["supplier_label"]}</span><span>No. {doc["ext_no"]}</span></div>
{f"<div class=meta><span>税号：{tax}</span></div>" if tax else ""}
<div class=meta><span>收货单位：宏远储能</span><span>{doc["date_text"]}</span></div>
<table class=hand><tr><th>行</th><th>货品</th><th>数量</th><th>单价</th><th>金额</th></tr>{rows}
<tr><td colspan=4 class=r>合计金额</td><td class=r>{total}</td></tr></table>
<div class=meta style=margin-top:20px><span>送货人：老陈</span><span>收货人签字：</span></div></div>"""


def degrade(png: bytes, rng: random.Random) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    img = img.rotate(rng.uniform(-1.8, 1.8), expand=True, fillcolor=(236, 236, 230))
    if rng.random() < 0.5:
        img = img.filter(ImageFilter.GaussianBlur(radius=0.6))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=rng.choice([70, 80, 88]))
    return buf.getvalue()


def make_docs(n_normal: int = 18, seed_value: int = 11) -> list[dict]:
    rng = random.Random(seed_value)
    products = {p[0]: (i + 1, p) for i, p in enumerate(seed.PRODUCTS)}
    layouts = ["formal", "english", "handwritten"]
    docs = []

    def lines_for(english: bool, k: int):
        out = []
        for sku in rng.sample(sorted(ALIASES), k):
            ref = Decimal(str(products[sku][1][3]))
            price = int(ref * 100 * Decimal(str(rng.choice([1, 1, 0.95, 0.92, 1.05]))))
            label = ALIASES[sku][1] if english else rng.choice([ALIASES[sku][0], ALIASES[sku][2]])
            if rng.random() < 0.3:
                label = f"{sku} {label}"
            out.append(
                {
                    "sku": sku,
                    "label": label,
                    "qty": rng.choice([50, 100, 200, 300, 500, 1000, 2000]),
                    "price_cents": price,
                }
            )
        return out

    for i in range(n_normal):
        sid = rng.choice(sorted(SUPPLIER_NAMES))
        english = sid in ENGLISH
        layout = "english" if english else ("formal" if i % 2 == 0 else "handwritten")
        date = f"2026-{rng.choice([9, 10, 11]):02d}-{rng.randint(1, 28):02d}"
        lines = lines_for(english, rng.randint(1, 4))
        docs.append(
            {
                "id": f"D{i + 1:02d}",
                "kind": "normal",
                "layout": layout,
                "supplier_id": sid,
                "supplier": seed.SUPPLIERS[sid - 1][0],
                "supplier_label": rng.choice(SUPPLIER_NAMES[sid]),
                "date": date,
                "date_text": fmt_date(date, 2 if english else rng.choice([0, 1])),
                "ext_no": f"{'SO' if english else 'XS'}{rng.randint(100000, 999999)}",
                "lines": lines,
            }
        )

    seed_ext = seed.seed_orders(random.Random(7))[4]["ext_no"]
    traps = [
        ("unknown_supplier", {"supplier_label": "惠州市新锐电子有限公司"}),
        ("unknown_supplier", {"supplier_label": "Dongguan Everbright Hardware Co."}),
        ("unknown_product", {}),
        ("total_mismatch", {}),
        ("total_mismatch", {}),
        ("duplicate", {"ext_no": seed_ext}),
    ]
    for j, (kind, override) in enumerate(traps):
        sid = rng.choice([1, 2, 4, 9])
        date = f"2026-11-{rng.randint(1, 5):02d}"
        lines = lines_for(False, rng.randint(2, 3))
        if kind == "unknown_product":
            lines.append({"sku": None, "label": "32700磷酸铁锂电芯 6000mAh", "qty": 200, "price_cents": 1880})
        doc = {
            "id": f"T{j + 1:02d}",
            "kind": kind,
            "layout": layouts[j % 3],
            "supplier_id": sid,
            "supplier": seed.SUPPLIERS[sid - 1][0],
            "supplier_label": rng.choice(SUPPLIER_NAMES[sid]),
            "date": date,
            "date_text": fmt_date(date, j % 2),
            "ext_no": f"XS{rng.randint(100000, 999999)}",
            "lines": lines,
            **override,
        }
        docs.append(doc)

    for d in docs:
        true_total = sum(l["qty"] * l["price_cents"] for l in d["lines"])
        d["printed_total_cents"] = true_total + (
            rng.choice([1000, -900, 10000]) if d["kind"] == "total_mismatch" else 0
        )
        d["expect"] = "enter" if d["kind"] == "normal" else "escalate"
    return docs


def fake_tax_id(doc_id: str) -> str:
    """给系统里不存在的公司编一个 18 位统一社会信用代码（不占用随机数序列，保证其余内容和无税号版一致）。"""
    import hashlib

    h = hashlib.sha256(doc_id.encode()).hexdigest().upper()
    body = "".join(c for c in h if c.isalnum())[:8]
    return f"9144{['0300', '1300', '1900', '0400'][int(h[0], 16) % 4]}MA{body}"


def make_hard_docs(seed_value: int = 23, with_tax: bool = False) -> list[dict]:
    """难题集：明细多（6–10 行）、扫描更糊；陷阱都是“看着像”的：
    名字相近的另一家公司、规格只差一点的商品、只差几毛钱的合计、藏在列表后几页的重复单号。"""
    rng = random.Random(seed_value)
    products = {p[0]: p for p in seed.PRODUCTS}
    layouts = ["formal", "handwritten", "english"]

    def lines_for(english: bool, k: int):
        out = []
        for sku in rng.sample(sorted(ALIASES), k):
            ref = Decimal(str(products[sku][3]))
            price = int(ref * 100 * Decimal(str(rng.choice([1, 0.97, 0.93, 1.04, 0.88]))))
            label = ALIASES[sku][1] if english else rng.choice([ALIASES[sku][0], ALIASES[sku][2]])
            out.append(
                {
                    "sku": sku,
                    "label": label,
                    "qty": rng.choice([60, 120, 240, 360, 480, 1200, 2400, 3600]),
                    "price_cents": price,
                }
            )
        return out

    def base(i, kind, sid, english, k):
        date = f"2026-{rng.choice([10, 11]):02d}-{rng.randint(1, 28):02d}"
        return {
            "id": f"H{i:02d}",
            "kind": kind,
            "layout": "english" if english else layouts[i % 2],
            "supplier_id": sid,
            "supplier": seed.SUPPLIERS[sid - 1][0],
            "supplier_label": rng.choice(SUPPLIER_NAMES[sid]),
            "date": date,
            "date_text": fmt_date(date, 2 if english else rng.choice([0, 1])),
            "ext_no": f"{'SO' if english else 'XS'}{rng.randint(100000, 999999)}",
            "lines": lines_for(english, k),
        }

    docs = []
    for i in range(1, 13):
        sid = rng.choice(sorted(SUPPLIER_NAMES))
        docs.append(base(i, "normal", sid, sid in ENGLISH, rng.randint(6, 10)))
    seed_ext = seed.seed_orders(random.Random(7))[15]["ext_no"]  # 第 2 页上的一张历史单
    traps = [
        (
            "unknown_supplier",
            1,
            {"supplier_label": "深圳市华强电子科技有限公司", "hard": "名字和“深圳市华强电子有限公司”只差两个字"},
        ),
        (
            "unknown_supplier",
            4,
            {"supplier_label": "惠州市德赛电池有限公司", "hard": "和“惠州市德赛锂电池科技有限公司”相近"},
        ),
        (
            "unknown_supplier",
            12,
            {"supplier_label": "Brightway Electronic (HK) Limited", "hard": "香港公司，不是深圳的 Brightway"},
        ),
        ("unknown_product", 9, {"extra": ("18650锂电芯 3000mAh", 960), "hard": "系统只有 2600mAh"}),
        ("unknown_product", 2, {"extra": ("8串保护板 80A", 7350), "hard": "系统只有 60A"}),
        ("total_mismatch", 10, {"delta": 10, "hard": "合计只差 0.10 元"}),
        ("total_mismatch", 7, {"delta": -100, "hard": "合计只差 1.00 元"}),
        ("duplicate", 1, {"ext_no": seed_ext, "hard": "重复单号在列表第 2 页"}),
    ]
    for j, (kind, sid, over) in enumerate(traps, start=13):
        d = base(j, kind, sid, sid in ENGLISH, rng.randint(5, 8))
        extra, delta = over.pop("extra", None), over.pop("delta", 0)
        if extra:
            d["lines"].insert(
                rng.randint(1, len(d["lines"])), {"sku": None, "label": extra[0], "qty": 240, "price_cents": extra[1]}
            )
        d.update(over)
        d["delta"] = delta
        docs.append(d)
    for d in docs:
        d["printed_total_cents"] = sum(l["qty"] * l["price_cents"] for l in d["lines"]) + d.pop("delta", 0)
        d["expect"] = "enter" if d["kind"] == "normal" else "escalate"
        if with_tax:
            d["tax_id"] = (
                fake_tax_id(d["id"]) if d["kind"] == "unknown_supplier" else seed.SUPPLIERS[d["supplier_id"] - 1][1]
            )
    return docs


def degrade_hard(png: bytes, rng: random.Random) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    img = img.resize((int(w * 0.72), int(h * 0.72))).resize((w, h))  # 低分辨率扫描
    img = img.rotate(rng.uniform(-3.2, 3.2), expand=True, fillcolor=(228, 226, 218))
    img = img.filter(ImageFilter.GaussianBlur(radius=rng.choice([0.6, 0.9])))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=rng.choice([55, 62, 70]))
    return buf.getvalue()


def main() -> None:
    import sys

    hard, tax = "--hard" in sys.argv, "--tax" in sys.argv
    out = OUT.parent / ("entry_hard_tax" if tax else "entry_hard" if hard else "entry")
    (out / "docs").mkdir(parents=True, exist_ok=True)
    docs = make_hard_docs(with_tax=tax) if hard or tax else make_docs()
    rng = random.Random(5)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 860, "height": 600}, device_scale_factor=1)
        for d in docs:
            page.set_content(f"<style>{CSS}</style>{render_html(d)}")
            png = page.locator(".doc").screenshot(type="png")
            (out / "docs" / f"{d['id']}.jpg").write_bytes((degrade_hard if hard or tax else degrade)(png, rng))
        browser.close()
    (out / "tasks.json").write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"生成 {len(docs)} 张单据 → {out}")


if __name__ == "__main__":
    main()

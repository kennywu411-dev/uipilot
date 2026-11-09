"""对照组：随机点击测试（monkey testing），不调用模型。

它只能靠确定性信号（页面异常、HTTP 5xx）发现问题，用来回答"Agent 比随机乱点多找到了什么"。
"""

import random

from ..browser import ActionError, ActionGuard, BrowserExecutor

VALUES = [
    "1",
    "0",
    "-5",
    "3.75",
    "100",
    "99999",
    "2026-03-04",
    "2026-11-30",
    "abc",
    "电子",
    "长" * 60,
    "",
    "SO-TEST-1",
    "91440300MA5FHQ2X1K",
]

# 信号到植入缺陷的对应关系：随机测试看不懂业务，只能认出会报错的缺陷
SIGNAL_BUGS = {("page_error", "/po/"): "B11", ("http_5xx", "/po/"): "B05"}


def run_monkey(base_url: str, steps: int, seed: int) -> dict:
    rng = random.Random(seed)
    events = []
    with BrowserExecutor(base_url, guard=ActionGuard(mode="sandbox")) as ex:
        for _ in range(steps):
            page = ex.page
            if not page.url.startswith(base_url):
                ex.execute("navigate", {"url": "/po"})
            cands = [
                c
                for c in ex._snapshot(ex.active, page, "candidates")["candidates"]
                if c["role"] in ("link", "button", "textbox", "combobox", "checkbox")
            ]
            if not cands:
                ex.execute("navigate", {"url": "/po"})
                continue
            c = rng.choice(cands)
            target = {"type": "ref", "ref": c["ref"]}
            try:
                if c["role"] in ("textbox",):
                    ex.execute("form_input", {"target": target, "value": rng.choice(VALUES)})
                elif c["role"] == "combobox":
                    opts = page.locator(f'[data-uip-ref="{c["ref"]}"] option').all_inner_texts()
                    if opts:
                        ex.execute("form_input", {"target": target, "value": rng.choice(opts)})
                else:
                    ex.execute("left_click", {"target": target})
            except ActionError:
                pass
            for s in ex.drain_signals():
                events.append(s.__dict__)
    found = set()
    for e in events:
        for (kind, path), bug in SIGNAL_BUGS.items():
            if e["kind"] == kind and path in e["url"] + e["text"]:
                found.add(bug)
    return {"steps": steps, "seed": seed, "signals": len(events), "detected": sorted(found), "events": events[:50]}

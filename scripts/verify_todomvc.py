"""人工复核用：不调用模型，用 Playwright 按固定步骤检查 TodoMVC 实现的规格条款（app-spec.md 的 Functionality）。

  uv run python scripts/verify_todomvc.py react vue ...
每条检查打印 PASS / FAIL 和看到的现象，用来复核测试 Agent 报出的问题。
检查只覆盖用户能看到的行为；持久化一条另行说明（官方测试已对多数实现豁免，见 docs/外部实战.md）。
"""

import sys

from playwright.sync_api import sync_playwright

from uipilot.scout.external import StaticSite, todomvc_dir

LI = ".todo-list > li, #todo-list > li"


class App:
    def __init__(self, page, base):
        self.page, self.base = page, base

    def open(self):
        self.page.goto(self.base + "/")
        self.page.wait_for_timeout(700)

    @property
    def new(self):
        return self.page.locator(".new-todo, #new-todo").first

    def add(self, text):
        self.new.fill(text)
        self.new.press("Enter")
        self.page.wait_for_timeout(120)

    def items(self):
        return self.page.locator(LI)

    def labels(self):
        return [t.strip() for t in self.page.locator(f"{LI.replace(', ', ' label, ')} label").all_inner_texts()]

    def toggle(self, i):
        self.items().nth(i).locator("input.toggle, .toggle").first.click()
        self.page.wait_for_timeout(120)

    def toggle_all(self):
        self.page.locator(".toggle-all, #toggle-all").first.click(force=True)
        self.page.wait_for_timeout(120)

    def edit(self, i):
        self.items().nth(i).locator("label").first.dblclick()
        self.page.wait_for_timeout(120)
        return self.page.locator("li.editing input.edit, li.editing .edit").first

    def visible(self, sel):
        loc = self.page.locator(sel)
        return loc.count() > 0 and loc.first.is_visible()


def checks(app: App) -> dict:
    out, page = {}, app.page
    app.open()
    out["empty hides main/footer"] = (not app.visible(".main, #main") and not app.visible(".footer, #footer"), "")
    out["autofocus new input"] = (
        page.evaluate("document.activeElement && document.activeElement.matches('.new-todo, #new-todo')"),
        "",
    )
    app.add("  first  ")
    app.add("   ")
    app.add("second")
    out["trim + ignore blank"] = (app.labels() == ["first", "second"], app.labels())
    out["append at end"] = (app.labels()[-1:] == ["second"], app.labels())
    out["new input cleared"] = (app.new.input_value() == "", repr(app.new.input_value()))
    count = page.locator(".todo-count, #todo-count").first.inner_text().strip()
    out["counter 2 items"] = (count.replace("!", "").startswith("2 items"), count)
    app.toggle(0)
    count = page.locator(".todo-count, #todo-count").first.inner_text().strip()
    out["counter 1 item"] = (count.replace("!", "").startswith("1 item") and "items" not in count, count)
    out["clear completed shown"] = (app.visible(".clear-completed, #clear-completed"), "")
    app.toggle(1)
    ta = page.locator(".toggle-all, #toggle-all").first
    out["toggle-all checked when all done"] = (ta.is_checked(), "")
    app.toggle(1)
    out["toggle-all unchecked after one undone"] = (not ta.is_checked(), "")
    app.toggle_all()
    states = [app.items().nth(i).locator(".toggle").first.is_checked() for i in range(app.items().count())]
    out["toggle-all completes all"] = (all(states), states)
    page.locator(".clear-completed, #clear-completed").first.click()
    page.wait_for_timeout(150)
    out["clear completed removes"] = (app.items().count() == 0, app.labels())
    out["toggle-all cleared after clear"] = (not ta.is_checked() if ta.count() else True, "")
    app.add("alpha")
    app.add("beta")
    e = app.edit(0)
    e.fill("  alpha2  ")
    e.press("Enter")
    page.wait_for_timeout(120)
    out["edit enter saves trimmed"] = (
        app.labels()[:1] == ["alpha2"] and page.locator("li.editing").count() == 0,
        app.labels(),
    )
    e = app.edit(0)
    e.fill("changed")
    e.press("Escape")
    page.wait_for_timeout(150)
    editing = page.locator("li.editing").count()
    out["escape discards + leaves edit"] = (
        app.labels()[:1] == ["alpha2"] and editing == 0,
        (app.labels(), f"editing={editing}"),
    )
    if editing:
        page.mouse.click(5, 5)
        page.wait_for_timeout(150)
    e = app.edit(1)
    e.fill("beta2")
    page.locator(".new-todo, #new-todo").first.click()
    page.wait_for_timeout(150)
    out["edit blur saves"] = ("beta2" in app.labels(), app.labels())
    e = app.edit(0)
    e.fill("   ")
    e.press("Enter")
    page.wait_for_timeout(150)
    out["edit empty destroys"] = ("alpha2" not in app.labels() and "" not in app.labels(), app.labels())
    app.add("gamma")
    app.toggle(0)
    page.goto(app.base + "/#/active")
    page.wait_for_timeout(300)
    out["route active filters"] = (app.labels() == ["gamma"], app.labels())
    app.toggle(0)
    out["active hides newly completed"] = (app.labels() == [], app.labels())
    page.reload()
    page.wait_for_timeout(600)
    sel = page.locator(".filters a.selected, #filters a.selected")
    out["filter kept on reload"] = (sel.count() > 0 and "active" in (sel.first.get_attribute("href") or ""), page.url)
    return out


def run(name: str) -> dict:
    with StaticSite(todomvc_dir(name)) as site, sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            return checks(App(browser.new_page(), site.base_url))
        finally:
            browser.close()


if __name__ == "__main__":
    for n in sys.argv[1:]:
        try:
            for k, (ok, seen) in run(n).items():
                print(f"{n:16} {'PASS' if ok else 'FAIL'}  {k:38} {seen if not ok else ''}")
        except Exception as e:  # 页面结构和预期不同时，记下来人工看
            print(f"{n:16} ERROR {str(e).splitlines()[0][:160]}")

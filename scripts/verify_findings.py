"""逐条复核 TodoMVC 线索：每条都从空页面开始、只做最短步骤，避免顺序依赖带来的连带失败。"""

import sys

from playwright.sync_api import sync_playwright

from uipilot.scout.external import StaticSite, todomvc_dir

LI = ".todo-list > li, #todo-list > li"


def session(name, fn):
    with StaticSite(todomvc_dir(name)) as site, sync_playwright() as pw:
        b = pw.chromium.launch()
        p = b.new_page()
        p.goto(site.base_url + "/")
        p.wait_for_timeout(700)
        try:
            return fn(p)
        finally:
            b.close()


def add(p, t):
    n = p.locator(".new-todo, #new-todo").first
    n.fill(t)
    n.press("Enter")
    p.wait_for_timeout(150)


def labels(p):
    return [x.strip() for x in p.locator(".todo-list > li label, #todo-list > li label").all_inner_texts()]


def retype(p, text):
    """模拟真人改写编辑框：只发键盘事件，不用 fill()。fill() 会强制聚焦，会掩盖“编辑框没获得焦点”（问题日志 P13）。"""
    p.keyboard.press("ControlOrMeta+a")
    p.keyboard.press("Backspace")
    if text:
        p.keyboard.type(text)


def esc_leaves_edit(p):
    add(p, "buy milk")
    p.locator(LI).first.locator("label").dblclick()
    p.wait_for_timeout(150)
    retype(p, "changed")
    p.keyboard.press("Escape")
    p.wait_for_timeout(200)
    after_esc = (labels(p), p.locator("li.editing").count())
    p.locator(".new-todo, #new-todo").first.click()  # 再失焦：放弃的修改不应被保存
    p.wait_for_timeout(200)
    return {"after_esc": after_esc, "after_blur": labels(p)}


def append_order(p):
    add(p, "a")
    add(p, "b")
    return {"labels": labels(p)}


def edit_empty(p):
    add(p, "a")
    add(p, "b")
    p.locator(LI).first.locator("label").dblclick()
    p.wait_for_timeout(150)
    retype(p, "   ")
    p.keyboard.press("Enter")
    p.wait_for_timeout(200)
    return {"labels": labels(p), "items": p.locator(LI).count()}


def edit_cleared(p):
    add(p, "a")
    add(p, "b")
    p.locator(LI).first.locator("label").dblclick()
    p.wait_for_timeout(150)
    retype(p, "")
    p.keyboard.press("Enter")
    p.wait_for_timeout(200)
    return {"labels": labels(p), "items": p.locator(LI).count()}


def toggle_all_after_clear(p):
    add(p, "a")
    p.locator(".toggle-all, #toggle-all").first.click(force=True)
    p.wait_for_timeout(150)
    p.locator(".clear-completed, #clear-completed").first.click()
    p.wait_for_timeout(150)
    add(p, "b")
    ta = p.locator(".toggle-all, #toggle-all").first
    item_done = p.locator(LI).first.locator(".toggle").first.is_checked()
    return {"labels": labels(p), "toggle_all_checked": ta.is_checked(), "new_item_completed": item_done}


def blank_creates(p):
    add(p, "   ")
    return {"items": p.locator(LI).count(), "labels": labels(p)}


def toggle_all_basic(p):
    add(p, "a")
    add(p, "b")
    p.locator(".toggle-all, #toggle-all").first.click(force=True)
    p.wait_for_timeout(150)
    return {
        "states": [p.locator(LI).nth(i).locator(".toggle").first.is_checked() for i in range(p.locator(LI).count())]
    }


def route_active(p):
    add(p, "a")
    add(p, "b")
    p.locator(LI).first.locator(".toggle").first.click()
    p.wait_for_timeout(150)
    p.get_by_role("link", name="Active").first.click()
    p.wait_for_timeout(300)
    return {"url": p.url.split("/")[-1], "labels": labels(p)}


CHECKS = {
    "esc": esc_leaves_edit,
    "append": append_order,
    "edit-empty": edit_empty,
    "edit-cleared": edit_cleared,
    "toggle-all-after-clear": toggle_all_after_clear,
    "blank": blank_creates,
    "toggle-all": toggle_all_basic,
    "route-active": route_active,
}

if __name__ == "__main__":
    check, *names = sys.argv[1:]
    for n in names:
        try:
            print(f"{check:24} {n:16} {session(n, CHECKS[check])}")
        except Exception as e:  # 页面结构不同的实现，人工另看
            print(f"{check:24} {n:16} ERROR {str(e).splitlines()[0][:120]}")

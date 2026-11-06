"""browser_toolset_20260801 的执行器：把 Claude 的浏览器工具调用落到 Playwright 上。

除了执行动作，这一层还负责三件和"能不能放心交给 Agent"有关的事：
1. 网络边界：只允许打开被测应用的源，评测专用的 /__admin 路径对浏览器不可见；
2. 不可逆操作拦截：点击前算出目标元素的文字，命中危险词按策略拒绝（ActionGuard）；
3. 确定性信号：收集控制台报错、页面异常、HTTP 5xx、弹窗，交给 Agent 或评分器使用。
"""

import base64
import contextlib
import io
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from PIL import Image
from playwright.sync_api import Page, sync_playwright

SNAPSHOT_JS = (Path(__file__).parent / "snapshot.js").read_text(encoding="utf-8")

KEY_ALIASES = {
    "return": "Enter", "enter": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab", "space": "Space",
    "backspace": "Backspace", "delete": "Delete", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft",
    "right": "ArrowRight", "page_down": "PageDown", "page_up": "PageUp", "pagedown": "PageDown",
    "pageup": "PageUp", "home": "Home", "end": "End", "ctrl": "Control", "control": "Control", "shift": "Shift",
    "alt": "Alt", "cmd": "Meta", "super": "Meta", "meta": "Meta", "command": "Meta",
}


def _key(combo: str) -> str:
    parts = []
    for p in combo.split("+"):
        alias = KEY_ALIASES.get(p.strip().lower())
        if alias:
            parts.append(alias)
        elif len(p.strip()) == 1:
            parts.append(p.strip())
        else:
            parts.append(p.strip()[:1].upper() + p.strip()[1:])
    return "+".join(parts)


def _modifiers(text: str | None) -> list[str]:
    if not text:
        return []
    return [KEY_ALIASES.get(m.strip().lower(), m.strip()) for m in text.split("+") if m.strip()]


class ActionError(Exception):
    """动作失败，文字会原样作为 is_error 结果返回给模型。"""


@dataclass
class ActionGuard:
    """不可逆操作拦截。mode="sandbox" 全放行（测试环境随时可重置），mode="protect" 两层拦截：

    1. 点击前看目标元素的文字，命中危险词就拒绝；但 type="button" 的纯页面按钮（如表单里的“删除本行”）
       不会提交任何数据，放行——只按文字拦会误伤这类操作（见问题日志 P10）；
    2. 网络层兜底：会改数据的请求（非 GET）路径命中 request_pattern 时直接中止，不管它是怎么触发的。
    """

    mode: str = "sandbox"
    pattern: str = r"删除|作废|清空|注销|delete|remove|void"
    request_pattern: str = r"/(delete|remove|void|destroy)(/|$)"
    blocked: list[dict] = field(default_factory=list)

    def check(self, label: str, action: str, inert: bool = False) -> None:
        if self.mode == "protect" and not inert and re.search(self.pattern, label or "", re.IGNORECASE):
            self.blocked.append({"action": action, "label": label, "t": time.time()})
            raise ActionError(f"Blocked by policy: “{label[:40]}” 是不可逆操作，当前任务不允许执行。"
                              "如确有必要，请调用 escalate 交给人工处理。")


@dataclass
class Signal:
    kind: str  # console_error | page_error | http_5xx | dialog | blocked_request
    text: str
    url: str


class BrowserExecutor:
    def __init__(self, base_url: str, guard: ActionGuard | None = None, viewport=(1280, 800),
                 headless: bool = True, blocked_prefixes: tuple[str, ...] = ("/__",)):
        self.base_url = base_url.rstrip("/")
        self.origin = "{0.scheme}://{0.netloc}".format(urlsplit(self.base_url))
        self.guard = guard or ActionGuard()
        self.viewport = {"width": viewport[0], "height": viewport[1]}
        self.headless = headless
        self.blocked_prefixes = blocked_prefixes
        self.tabs: dict[str, Page] = {}
        self.counters: dict[str, int] = {}
        self.active: str | None = None
        self._tab_seq = 0
        self._signals: list[Signal] = []
        self.all_signals: list[Signal] = []
        self.action_count = 0

    # ---------- 生命周期 ----------
    def __enter__(self) -> "BrowserExecutor":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.context = self._browser.new_context(viewport=self.viewport, device_scale_factor=1, locale="zh-CN")
        self.context.route("**/*", self._route)
        self.context.on("page", self._register)
        page = self.context.new_page()
        page.goto(self.base_url + "/")
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.context.close()
            self._browser.close()
        finally:
            self._pw.stop()

    def _route(self, route):
        parts = urlsplit(route.request.url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if parts.scheme in ("data", "blob", "about"):
            return route.continue_()
        if origin != self.origin or any(parts.path.startswith(p) for p in self.blocked_prefixes):
            return route.abort("blockedbyclient")
        if self.guard.mode == "protect" and route.request.method != "GET" and \
                re.search(self.guard.request_pattern, parts.path, re.IGNORECASE):
            self.guard.blocked.append({"action": "request", "label": f"{route.request.method} {parts.path}",
                                       "t": time.time()})
            self._emit("blocked_request", f"已拦截会改数据的请求 {route.request.method} {parts.path}", route.request.url)
            return route.abort("blockedbyclient")
        return route.continue_()

    def _register(self, page: Page) -> None:
        self._tab_seq += 1
        tab_id = f"tab-{self._tab_seq}"
        self.tabs[tab_id] = page
        self.counters[tab_id] = 1
        self.active = tab_id
        page.on("console", lambda m: m.type == "error" and self._emit("console_error", m.text, page.url))
        page.on("pageerror", lambda e: self._emit("page_error", str(e), page.url))
        page.on("response", lambda r: r.status >= 500 and self._emit("http_5xx", f"{r.status} {r.url}", page.url))
        page.on("dialog", lambda d: (self._emit("dialog", f"{d.type}: {d.message}", page.url), d.accept()))
        page.on("close", lambda _: self._forget(tab_id))

    def _forget(self, tab_id: str) -> None:
        self.tabs.pop(tab_id, None)
        if self.active == tab_id:
            self.active = next(iter(self.tabs), None)

    def _emit(self, kind: str, text: str, url: str) -> None:
        if kind == "console_error" and "ERR_BLOCKED_BY_CLIENT" in text:
            return  # 被网络边界拦掉的请求，不算应用的错误
        s = Signal(kind, text[:300], url)
        self._signals.append(s)
        self.all_signals.append(s)

    def drain_signals(self) -> list[Signal]:
        out, self._signals = self._signals, []
        return out

    # ---------- 工具 ----------
    @property
    def page(self) -> Page:
        if not self.active or self.active not in self.tabs:
            raise ActionError("No active tab. Use new_tab first.")
        return self.tabs[self.active]

    def _tab(self, tab_id: str | None) -> tuple[str, Page]:
        tid = tab_id or self.active
        if tid not in self.tabs:
            raise ActionError(f"Tab {tid} does not exist.")
        return tid, self.tabs[tid]

    def _settle(self, page: Page) -> None:
        with contextlib.suppress(Exception):  # 没有发生导航时这里会超时，属于正常情况
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        page.wait_for_timeout(250)

    def browser_state(self) -> dict:
        return {"type": "browser_state", "tabs": [
            {"tab_id": tid, "title": p.title(), "url": p.url, "active": tid == self.active}
            for tid, p in self.tabs.items()]}

    def _locate(self, page: Page, ref: str):
        loc = page.locator(f'[data-uip-ref="{ref}"]')
        if loc.count() == 0:
            raise ActionError(f"{ref} is stale or not found on the current page. "
                              "Re-read the page to get fresh references.")
        return loc.first

    # 返回 [文字, 是否是不提交数据的纯页面按钮]
    _LABEL_JS = """e => [(e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 80),
                         e.tagName === 'BUTTON' && (e.getAttribute('type') || '').toLowerCase() === 'button']"""

    def _label_at(self, page: Page, x: float, y: float) -> tuple[str, bool]:
        return page.evaluate(
            """([x, y]) => { const el = document.elementFromPoint(x, y); if (!el) return ["", false];
                 const t = el.closest('a,button,input,select,textarea,[role]') || el;
                 return [(t.innerText || t.value || t.getAttribute('aria-label') || '').trim().slice(0, 80),
                         t.tagName === 'BUTTON' && (t.getAttribute('type') || '').toLowerCase() === 'button']; }""",
            [x, y])

    def _point(self, page: Page, target: dict, action: str, guard: bool = True) -> tuple[float, float]:
        if target.get("type") == "ref":
            loc = self._locate(page, target["ref"])
            if guard:
                label, inert = loc.evaluate(self._LABEL_JS)
                self.guard.check(label, action, inert)
            loc.scroll_into_view_if_needed(timeout=3000)
            box = loc.bounding_box()
            if not box:
                raise ActionError(f"{target['ref']} is not visible.")
            return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        x, y = float(target["x"]), float(target["y"])
        if not (0 <= x <= self.viewport["width"] and 0 <= y <= self.viewport["height"]):
            raise ActionError(f"Coordinate ({x:.0f}, {y:.0f}) is outside the viewport "
                              f"{self.viewport['width']}x{self.viewport['height']}.")
        if guard:
            label, inert = self._label_at(page, x, y)
            self.guard.check(label, action, inert)
        return x, y

    def _screenshot(self, page: Page, clip: dict | None = None) -> list[dict]:
        png = page.screenshot(type="png", clip=clip) if clip else page.screenshot(type="png")
        if clip:
            img = Image.open(io.BytesIO(png))
            scale = min(self.viewport["width"] / img.width, self.viewport["height"] / img.height)
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png = buf.getvalue()
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.b64encode(png).decode()}}]

    def _snapshot(self, tab_id: str, page: Page, mode: str, depth: int = 15, ref: str | None = None) -> dict:
        res = page.evaluate(SNAPSHOT_JS, {"mode": mode, "maxDepth": depth, "rootRef": ref,
                                          "counter": self.counters.get(tab_id, 1)})
        self.counters[tab_id] = res["counter"]
        if res.get("error"):
            raise ActionError(res["error"])
        return res

    def execute(self, name: str, inp: dict) -> list[dict] | str:
        """执行一个成员工具，返回 tool_result 的 content（字符串或内容块列表）。失败抛 ActionError。"""
        self.action_count += 1
        handler = getattr(self, f"_do_{name}", None)
        if handler is None:
            raise ActionError(f"{name} is not enabled in this environment.")
        try:
            return handler(inp)
        except ActionError:
            raise
        except Exception as e:  # Playwright 超时、元素被遮挡等，统一转成模型能读懂的错误
            raise ActionError(f"{name} failed: {str(e).splitlines()[0][:300]}") from e

    def _do_navigate(self, inp):
        tid, page = self._tab(inp.get("tab_id"))
        url = inp["url"].strip()
        if url in ("back", "forward", "reload"):
            {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[url]()
        else:
            if url.startswith("/"):
                url = urljoin(self.base_url + "/", url.lstrip("/"))
            elif "://" not in url:
                host = url.split("/")[0]
                url = ("http://" if host.startswith(("127.0.0.1", "localhost")) else "https://") + url
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https"):
                raise ActionError("Error: Navigation refused. Only http and https URLs are allowed.")
            if f"{parts.scheme}://{parts.netloc}" != self.origin or any(
                    parts.path.startswith(p) for p in self.blocked_prefixes):
                raise ActionError(f"Error: Navigation refused. Only the application under test ({self.base_url}) "
                                  "may be opened.")
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
        self._settle(page)
        return [{"type": "text", "text": f"Navigated to {page.url}"}, self.browser_state()]

    def _do_screenshot(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        return self._screenshot(page)

    def _do_zoom(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        x0, y0, x1, y1 = inp["region"]
        if x1 <= x0 or y1 <= y0:
            raise ActionError("zoom region must have x1 > x0 and y1 > y0")
        return self._screenshot(page, {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})

    def _click(self, inp, action, button="left", count=1):
        _, page = self._tab(inp.get("tab_id"))
        x, y = self._point(page, inp["target"], action)
        mods = _modifiers(inp.get("modifiers"))
        for m in mods:
            page.keyboard.down(m)
        page.mouse.click(x, y, button=button, click_count=count)
        for m in reversed(mods):
            page.keyboard.up(m)
        self._settle(page)
        t = inp["target"]
        where = t["ref"] if t.get("type") == "ref" else f"({t['x']}, {t['y']})"
        return f"Clicked {'element ' if t.get('type') == 'ref' else ''}{where}."

    def _do_left_click(self, inp):
        return self._click(inp, "left_click")

    def _do_right_click(self, inp):
        return self._click(inp, "right_click", button="right")

    def _do_middle_click(self, inp):
        return self._click(inp, "middle_click", button="middle")

    def _do_double_click(self, inp):
        return self._click(inp, "double_click", count=2)

    def _do_triple_click(self, inp):
        return self._click(inp, "triple_click", count=3)

    def _do_hover(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        x, y = self._point(page, inp["target"], "hover", guard=False)
        page.mouse.move(x, y)
        return "Hovered."

    def _do_left_click_drag(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        x0, y0 = self._point(page, inp["from"], "drag")
        x1, y1 = self._point(page, inp["target"], "drag")
        page.mouse.move(x0, y0)
        page.mouse.down()
        page.mouse.move(x1, y1, steps=8)
        page.mouse.up()
        return "Dragged."

    def _do_mouse_move(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        page.mouse.move(float(inp["target"]["x"]), float(inp["target"]["y"]))
        return "Moved."

    def _do_left_mouse_down(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        x, y = self._point(page, inp["target"], "mouse_down")
        page.mouse.move(x, y)
        page.mouse.down()
        return "Mouse down."

    def _do_left_mouse_up(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        page.mouse.move(float(inp["target"]["x"]), float(inp["target"]["y"]))
        page.mouse.up()
        self._settle(page)
        return "Mouse up."

    def _do_scroll(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        t = inp.get("target") or {"x": self.viewport["width"] / 2, "y": self.viewport["height"] / 2}
        page.mouse.move(float(t["x"]), float(t["y"]))
        n = int(inp.get("scroll_amount") or 3) * 100
        dx, dy = {"up": (0, -n), "down": (0, n), "left": (-n, 0), "right": (n, 0)}[inp["scroll_direction"]]
        page.mouse.wheel(dx, dy)
        page.wait_for_timeout(200)
        return f"Scrolled {inp['scroll_direction']}."

    def _do_scroll_to(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        self._locate(page, inp["target"]["ref"]).scroll_into_view_if_needed(timeout=3000)
        return f"Scrolled {inp['target']['ref']} into view."

    def _do_type(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        page.keyboard.type(inp["text"])
        return "Typed."

    def _do_key(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        for _ in range(int(inp.get("repeat") or 1)):
            for combo in inp["text"].split():
                page.keyboard.press(_key(combo))
        self._settle(page)
        return f"Pressed {inp['text']}."

    def _do_hold_key(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        k = _key(inp["text"])
        page.keyboard.down(k)
        time.sleep(min(float(inp.get("duration", 1)), 5))
        page.keyboard.up(k)
        return "Held key."

    def _do_wait(self, inp):
        time.sleep(min(float(inp.get("duration", 1)), 5))
        return "Waited."

    def _do_read_page(self, inp):
        tid, page = self._tab(inp.get("tab_id"))
        mode = {"interactive": "interactive", "all": "all"}.get(inp.get("filter") or "", "visible")
        res = self._snapshot(tid, page, mode, int(inp.get("depth") or 15), inp.get("ref"))
        text = "\n".join(res["lines"]) or "(no matching elements)"
        if len(text) > 50000:
            text = text[:50000] + f"\n[truncated: output was {len(text)} characters]"
        return f"URL: {page.url}\nTitle: {page.title()}\n{text}"

    def _do_find(self, inp):
        tid, page = self._tab(inp.get("tab_id"))
        cands = self._snapshot(tid, page, "candidates")["candidates"]
        q = inp["query"].lower()
        grams = {q[i:i + 2] for i in range(len(q) - 1)} or {q}

        def score(c):
            name = (c["name"] + " " + c["role"]).lower()
            if q in name:
                return 10 + (5 if c["role"] in ("button", "link", "textbox", "combobox") else 0)
            g = {name[i:i + 2] for i in range(len(name) - 1)}
            return len(grams & g) / max(1, len(grams))

        ranked = [c for c in sorted(cands, key=score, reverse=True) if score(c) > 0.3][:20]
        if not ranked:
            return f"No elements matching “{inp['query']}”."
        return "\n".join(c["line"] for c in ranked)

    def _do_get_page_text(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        text = page.evaluate("() => document.body.innerText")
        return f"URL: {page.url}\nTitle: {page.title()}\n{text[:50000]}"

    def _do_form_input(self, inp):
        _, page = self._tab(inp.get("tab_id"))
        loc = self._locate(page, inp["target"]["ref"])
        value = inp["value"]
        tag = loc.evaluate("e => e.tagName.toLowerCase() + ':' + (e.type || '')")
        if tag.startswith("select"):
            try:
                loc.select_option(label=str(value), timeout=3000)
            except Exception:
                opts = loc.evaluate("e => Array.from(e.options).map(o => [o.value, o.text])")
                match = [v for v, t in opts if str(value) in (v, t) or str(value).strip() in t]
                if not match:
                    raise ActionError(f"No option matching “{value}”. Options: {[t for _, t in opts][:30]}") from None
                loc.select_option(value=match[0], timeout=3000)
        elif tag.endswith(("checkbox", "radio")):
            loc.set_checked(bool(value), timeout=3000)
        else:
            loc.fill(str(value), timeout=3000)
        loc.dispatch_event("input")
        loc.dispatch_event("change")
        return f"Set {inp['target']['ref']} to {value!r}."

    def _do_new_tab(self, inp):
        self.context.new_page()  # _register 会把它设为当前标签
        state = self.browser_state()
        state["state_changes"] = [{"type": "tab_opened", "tab_id": self.active}]  # 协议要求注明新开的是哪个标签
        return [state]

    def _do_list_tabs(self, inp):
        return [self.browser_state()]

    def _do_switch_tab(self, inp):
        tid, page = self._tab(inp["tab_id"])
        page.bring_to_front()
        self.active = tid
        return [self.browser_state()]

    def _do_close_tab(self, inp):
        tid, page = self._tab(inp["tab_id"])
        page.close()
        return [self.browser_state()]

// 给页面生成可访问性树文本，并给每个输出的元素打上稳定引用（data-uip-ref）。
// 已经打过引用的元素保留原编号，新元素从 Python 传入的计数器往后编，绝不重排。
// mode: "visible"（默认，只看视口内）| "interactive" | "all" | "candidates"（给 find 用，返回结构化列表）
(args) => {
  const { mode, maxDepth, rootRef, counter } = args;
  let next = counter;
  const vw = window.innerWidth, vh = window.innerHeight;
  const INTERACTIVE = new Set(["link", "button", "textbox", "combobox", "checkbox", "radio", "searchbox", "option"]);

  function isShown(el) {
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function inViewport(el) {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
  }
  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (["button", "submit", "reset"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "hidden") return null;
      if (t === "search") return "searchbox";
      return "textbox";
    }
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "img") return "img";
    if (tag === "table") return "table";
    if (tag === "tr") return "row";
    if (tag === "th") return "columnheader";
    if (tag === "td") return "cell";
    if (tag === "li") return "listitem";
    if (tag === "form") return "form";
    return null;
  }
  function clean(t, n = 100) {
    t = (t || "").replace(/\s+/g, " ").trim();
    return t.length > n ? t.slice(0, n) + "…" : t;
  }
  function nameOf(el, role) {
    const aria = el.getAttribute("aria-label");
    if (aria) return clean(aria);
    const lb = el.getAttribute("aria-labelledby");
    if (lb) { const t = lb.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(e => e.innerText).join(" "); if (t) return clean(t); }
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    const wrap = el.closest("label"); if (wrap && wrap !== el) return clean(wrap.innerText);
    if (role === "textbox" || role === "searchbox" || role === "combobox") return clean(el.getAttribute("placeholder") || el.getAttribute("title") || el.getAttribute("name") || "");
    if (role === "img") return clean(el.getAttribute("alt") || el.getAttribute("title") || "");
    if (el.tagName === "INPUT") return clean(el.value || el.getAttribute("title") || "");
    if (role === "table" || role === "row" || role === "form") return "";
    return clean(el.innerText || el.getAttribute("title") || "");
  }
  function extras(el, role) {
    const out = [];
    if (role === "textbox" || role === "searchbox") out.push(`value="${clean(el.value, 200)}"`);
    if (role === "combobox" && el.tagName === "SELECT") {
      const sel = el.selectedOptions[0];
      out.push(`selected="${clean(sel ? sel.text : "", 60)}"`);
      const opts = Array.from(el.options).map(o => clean(o.text, 40));
      out.push(`options=[${opts.slice(0, 40).map(o => JSON.stringify(o)).join(", ")}${opts.length > 40 ? ", …" : ""}]`);
    }
    if (role === "checkbox" || role === "radio") out.push(el.checked ? "checked" : "unchecked");
    if (el.disabled) out.push("disabled");
    return out.join(" ");
  }
  function refFor(el) {
    let r = el.getAttribute("data-uip-ref");
    if (!r) { r = `ref_${next++}`; el.setAttribute("data-uip-ref", r); }
    return r;
  }
  function directText(el) {
    let t = "";
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.textContent;
    return clean(t);
  }

  const lines = [];
  const candidates = [];
  function walk(el, depth) {
    if (!(el instanceof Element) || !isShown(el)) return;
    if (["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"].includes(el.tagName)) return;
    const role = roleOf(el);
    const interactive = role && INTERACTIVE.has(role);
    let emit = false;
    if (mode === "interactive") emit = interactive;
    else if (mode === "candidates") emit = !!role && role !== "table" && role !== "row" && role !== "form";
    else emit = !!role && (mode === "all" || inViewport(el) || role === "table" || role === "row" || role === "form");
    let childDepth = depth;
    if (emit && depth <= maxDepth) {
      const name = nameOf(el, role);
      const ref = refFor(el);
      if (mode === "candidates") {
        candidates.push({ ref, role, name, line: `${role} "${name}" ${extras(el, role)} [${ref}]`.replace(/\s+\[/, " [") });
      } else {
        const ex = extras(el, role);
        lines.push(`${"  ".repeat(depth)}${role}${name ? ` "${name.replace(/"/g, "'")}"` : ""}${ex ? " " + ex : ""} [${ref}]`);
      }
      childDepth = depth + 1;
      if (role === "cell" || role === "columnheader" || role === "link" || role === "button" || role === "heading" || role === "listitem") {
        // 文本已经作为名字输出，只再往下找可交互元素
        for (const c of el.querySelectorAll("a[href],button,input,select,textarea,[role]")) if (c !== el) walk(c, childDepth);
        return;
      }
      if (role === "combobox") return;
    } else if (mode !== "interactive" && mode !== "candidates") {
      const t = directText(el);
      if (t && (mode === "all" || inViewport(el)) && depth <= maxDepth) lines.push(`${"  ".repeat(depth)}text "${t.replace(/"/g, "'")}"`);
    }
    for (const c of el.children) walk(c, childDepth);
  }

  // 页面用 cloneNode 复制元素时会把 data-uip-ref 一起复制，同一个引用出现在多个元素上。
  // 按文档顺序保留第一个（原元素），其余去掉标记，下面遍历时重新编号。
  const owner = new Map();
  for (const el of document.querySelectorAll("[data-uip-ref]")) {
    const r = el.getAttribute("data-uip-ref");
    if (owner.has(r)) el.removeAttribute("data-uip-ref");
    else owner.set(r, el);
  }

  let root = document.body;
  if (rootRef) {
    root = document.querySelector(`[data-uip-ref="${rootRef}"]`);
    if (!root) return { error: `${rootRef} is stale or not found on the current page. Re-read the page to get fresh references.`, counter: next };
  }
  walk(root, 0);
  return { lines, candidates, counter: next };
}

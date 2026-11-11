"""在真实开源 Web 应用上跑测试 Agent（不植入任何缺陷，报告全部人工复核）。

第一个目标是 TodoMVC：同一份公开规格书（app-spec.md），几十个框架各自实现。
每个实现的 dist 目录单独起一个本地静态服务（独立的源，Agent 跑不到别的实现里去）；
每次探索和复现都用全新的浏览器上下文，localStorage 为空，相当于重置。

  uv run python -m webhands.evaluate external --targets react vue --model claude-sonnet-5-5
"""

import functools
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TODOMVC = ROOT / "targets_ext" / "todomvc"


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 静态服务的访问日志没有用，关掉
        pass


class StaticSite:
    def __init__(self, directory: Path):
        handler = functools.partial(_Quiet, directory=str(directory))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "StaticSite":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()


def todomvc_rules() -> str:
    """规格书里“Functionality”一节，原文英文。"""
    spec = (TODOMVC / "app-spec.md").read_text(encoding="utf-8")
    return spec[spec.index("## Functionality") :].strip()


def todomvc_dir(name: str) -> Path:
    d = TODOMVC / "examples" / name
    return d / "dist" if (d / "dist" / "index.html").exists() else d


EXTERNAL_SYSTEM = """你是一名资深的软件测试工程师，正在测试一个开源 TodoMVC 实现（{name}）。TodoMVC 的每个实现都必须遵守同一份官方规格书，下面是其中的功能条款（英文原文）：

<spec>
{rules}
</spec>

目标：找出这个实现在用户可见的行为上与规格书不符的地方。

工作方法：
- 逐条对照规格设计操作去验证，重点是边界情况：首尾带空格的输入、只有空格的输入、编辑时按 Esc / Enter / 点别处、把内容清空后保存、全部勾选再取消一个、在 Active / Completed 筛选下修改条目、刷新页面后数据和当前筛选是否保留、计数的单复数（0 items / 1 item / 2 items）。
- 刷新页面用 navigate 的 url="reload"；悬停用 hover；双击用 double_click。
- 只报告用户在界面上能观察到的问题。CSS 类名、localStorage 键名、代码风格这类实现细节不算，除非它们导致了可见的错误。
- 每个问题都要亲眼核对过再报告：说清从“打开首页、列表为空”开始的最短步骤、规格要求的结果、实际看到的结果，并在 rule 里写规格的小节名（如 Editing、Counter、Routing）。同一个问题只报一次。
- 测完所有条款或没有新发现时调用 finish，说明覆盖了哪些条款。"""

EXTERNAL_REPRO = """你是测试复核员。浏览器是全新的（localStorage 为空），应用处于初始状态。你会收到一份针对 TodoMVC 实现的缺陷报告。
请严格按报告里的步骤在页面上重做一遍，判断报告描述的“实际结果”是否真的出现。不要扩展测试，不要找别的问题。
刷新页面用 navigate 的 url="reload"。完成后调用 verdict：reproduced=true 表示现象确实出现；false 表示按步骤做了但没有出现，或步骤无法执行。"""


def safe_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", name.lower())

"""通用的浏览器 Agent 循环：browser_toolset + 应用自己的自定义工具，带预算、轮数上限和轨迹记录。

三种观察方式通过 toolset 的 configs 切换，用来做对照实验：
- vision：只给截图和坐标操作（关掉 read_page / find / get_page_text / form_input / scroll_to）
- dom：只给可访问性树和引用操作（关掉 screenshot / zoom）
- hybrid：全部打开
"""

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import anthropic

from .browser import ActionError, BrowserExecutor

# 每百万 token 美元价（来自 claude-api 技能里缓存的 2026-10 价目表）
PRICES = {
    "claude-opus-5-5": {"in": 4.00, "out": 20.00, "cache_read": 0.20},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cache_read": 0.20},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "cache_read": 0.01},
}
HAIKU_LONG = {"in": 0.50, "out": 2.50, "cache_read": 0.05}  # Haiku 5.5 提示超过 100K token 时的价格

VISION_OFF = ["read_page", "find", "get_page_text", "form_input", "scroll_to"]
DOM_OFF = ["screenshot", "zoom"]
NOT_EXECUTED = "Not executed: an earlier action in this turn failed."


def request_cost(model: str, usage) -> float:
    p = PRICES.get(model, PRICES["claude-opus-5-5"])
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    prompt = usage.input_tokens + cache_write + cache_read
    if model == "claude-haiku-5-5" and prompt > 100_000:
        p = HAIKU_LONG
    return (usage.input_tokens * p["in"] + cache_write * p["in"] * 1.25 + cache_read * p["cache_read"]
            + usage.output_tokens * p["out"]) / 1e6


def toolset(mode: str) -> dict:
    off = {"vision": VISION_OFF, "dom": DOM_OFF, "hybrid": []}[mode]
    configs = {name: {"enabled": False} for name in off + ["new_tab", "close_tab", "hold_key"]}
    return {"type": "browser_toolset_20260801", "configs": configs}


@dataclass
class AgentConfig:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    mode: str = "hybrid"
    max_turns: int = 40
    budget_usd: float = 1.0
    oracle_signals: bool = True  # 把控制台报错 / 5xx 等确定性信号附在工具结果里给模型看
    fallbacks: bool = True  # Opus / Sonnet 拒答时由服务端换模型重试（会记录在轨迹里）


@dataclass
class CustomTool:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict], tuple[str, bool]]  # 返回 (给模型的结果, 是否结束)

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "strict": True,
                "input_schema": {**self.schema, "additionalProperties": False}}


@dataclass
class RunResult:
    stop_reason: str
    turns: int
    cost_usd: float
    seconds: float
    actions: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    served_models: list[str] = field(default_factory=list)
    errors: int = 0
    blocked: int = 0
    misrouted: int = 0  # 自定义工具被标成 browser 工具集成员调用的次数

    def to_dict(self) -> dict:
        return asdict(self)


def _signal_note(signals) -> str:
    lines = [f"- [{s.kind}] {s.text} （页面 {s.url}）" for s in signals[:10]]
    return "[运行监测] 本轮动作期间捕获到以下信号：\n" + "\n".join(lines)


def run_agent(executor: BrowserExecutor, system: str, first_message: list[dict], tools: list[CustomTool],
              cfg: AgentConfig, trace_path: Path | None = None, client: anthropic.Anthropic | None = None,
              finish_reminder: str = "请继续完成任务；如果已经完成，请调用 finish 工具结束。") -> RunResult:
    client = client or anthropic.Anthropic(max_retries=4)
    by_name = {t.name: t for t in tools}
    tool_defs = [toolset(cfg.mode)] + [t.definition() for t in tools]
    messages: list[dict] = [{"role": "user", "content": first_message}]
    result = RunResult("max_turns", 0, 0.0, 0.0, 0)
    start = time.time()
    trace = open(trace_path, "a", encoding="utf-8") if trace_path else None
    nudged = 0

    def log(event: dict):
        if trace:
            trace.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            trace.flush()

    try:
        for turn in range(cfg.max_turns):
            if result.cost_usd >= cfg.budget_usd:
                result.stop_reason = "budget"
                break
            kwargs = dict(model=cfg.model, max_tokens=16000, system=system, tools=tool_defs, messages=messages,
                          output_config={"effort": cfg.effort}, cache_control={"type": "ephemeral"})
            try:
                if cfg.fallbacks and cfg.model in ("claude-opus-5-5", "claude-sonnet-5-5"):
                    resp = client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"],
                                                       fallbacks="default")
                else:
                    resp = client.messages.create(**kwargs)
            except anthropic.BadRequestError as e:
                # 输出被内容过滤拦截时整轮结束，但已收集的结果（缺陷报告、录入动作）保留
                if "content filtering" not in str(e):
                    raise
                result.stop_reason = "content_filter"
                log({"turn": turn, "content_filter": str(e)[:300]})
                break
            cost = request_cost(resp.model, resp.usage)
            result.turns = turn + 1
            result.cost_usd += cost
            result.input_tokens += resp.usage.input_tokens + (resp.usage.cache_creation_input_tokens or 0)
            result.cache_read_tokens += resp.usage.cache_read_input_tokens or 0
            result.output_tokens += resp.usage.output_tokens
            if resp.model not in result.served_models:
                result.served_models.append(resp.model)
            messages.append({"role": "assistant", "content": resp.content})
            texts = [b.text for b in resp.content if b.type == "text"]
            log({"turn": turn, "stop": resp.stop_reason, "cost": round(cost, 5), "model": resp.model,
                 "text": texts, "usage": resp.usage.model_dump()})

            if resp.stop_reason == "refusal":
                result.stop_reason = "refusal"
                break
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                if nudged >= 2:
                    result.stop_reason = "no_tool_call"
                    break
                nudged += 1
                messages.append({"role": "user", "content": [{"type": "text", "text": finish_reminder}]})
                continue

            results, failed, stop = [], False, False
            last_browser = None
            for b in uses:
                toolset_name = getattr(b, "toolset_name", None)
                # 模型偶尔把自定义工具（如 escalate）标成 browser 工具集成员来调用。执行器没有这个成员、
                # 而自定义工具里有同名工具时，按自定义工具执行（结果仍回显 toolset_name），并计数
                misrouted = toolset_name == "browser" and b.name in by_name \
                    and not hasattr(executor, f"_do_{b.name}")
                if misrouted:
                    result.misrouted += 1
                if toolset_name == "browser" and not misrouted:
                    entry = {"type": "tool_result", "tool_use_id": b.id, "toolset_name": "browser"}
                    inp = b.input if isinstance(b.input, dict) else b.input.model_dump(exclude_none=True)
                    if failed:
                        entry.update(content=NOT_EXECUTED, is_error=True)
                    else:
                        try:
                            entry["content"] = executor.execute(b.name, inp)
                        except ActionError as e:
                            entry.update(content=f"Error: {e}" if not str(e).startswith("Error") else str(e),
                                         is_error=True)
                            failed = True
                            result.errors += 1
                    log({"turn": turn, "action": b.name, "input": inp, "error": entry.get("is_error", False),
                         "result": entry["content"] if isinstance(entry["content"], str) else "[blocks]",
                         "url": executor.page.url if executor.tabs else ""})
                    results.append(entry)
                    last_browser = entry
                else:
                    tool = by_name.get(b.name)
                    inp = b.input if isinstance(b.input, dict) else b.input.model_dump(exclude_none=True) \
                        if hasattr(b.input, "model_dump") else dict(b.input)
                    if tool is None:
                        out, done, err = f"Unknown tool {b.name}", False, True
                    else:
                        try:
                            (out, done), err = tool.handler(inp), False
                        except Exception as e:  # 自定义工具的参数问题交还给模型修正
                            out, done, err = f"Error: {e}", False, True
                    stop = stop or done
                    log({"turn": turn, "custom": b.name, "input": inp, "result": out, "misrouted": misrouted})
                    entry = {"type": "tool_result", "tool_use_id": b.id, "content": out}
                    if misrouted:
                        entry["toolset_name"] = "browser"
                    if err:
                        entry["is_error"] = True
                    results.append(entry)
            signals = executor.drain_signals()
            if signals:
                log({"turn": turn, "signals": [s.__dict__ for s in signals]})
                content = last_browser["content"] if last_browser else None
                only_state = isinstance(content, list) and all(c.get("type") == "browser_state" for c in content)
                if cfg.oracle_signals and content is not None and not last_browser.get("is_error") \
                        and not only_state:
                    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
                    last_browser["content"] = blocks + [{"type": "text", "text": _signal_note(signals)}]
            messages.append({"role": "user", "content": results})
            if stop:
                result.stop_reason = "finished"
                break
    finally:
        result.seconds = round(time.time() - start, 1)
        result.actions = executor.action_count
        result.blocked = len(executor.guard.blocked)
        log({"result": result.to_dict()})
        if trace:
            trace.close()
    return result

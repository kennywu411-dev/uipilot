"""Agent 循环的路由与结束逻辑：用假的客户端和执行器，不调用 API。"""

from types import SimpleNamespace as NS

import anthropic
import httpx2 as httpx

from webhands.agent import AgentConfig, CustomTool, run_agent


class FakeUsage(NS):
    def model_dump(self):
        return dict(self.__dict__)


def usage():
    return FakeUsage(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0)


class FakeClient:
    def __init__(self, turns):
        self.turns, self.sent = list(turns), []
        self.messages = NS(create=self._create)
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.sent.append(list(kw["messages"]))  # 当时发出的消息快照（循环里的列表之后会继续追加）
        item = self.turns.pop(0)
        if isinstance(item, Exception):
            raise item
        return NS(model="claude-haiku-5-5", usage=usage(), stop_reason="tool_use", content=item)


class FakeExecutor:
    def __init__(self):
        self.calls, self.action_count, self.tabs = [], 0, {}
        self.guard = NS(blocked=[])

    def execute(self, name, inp):
        self.calls.append(name)
        self.action_count += 1
        return "ok"

    def _do_navigate(self, inp):  # 只用来让 hasattr 判断出“这是浏览器成员”
        return "ok"

    def drain_signals(self):
        return []


def tool_use(name, inp, toolset=None, id_="t1"):
    return NS(type="tool_use", id=id_, name=name, input=inp, toolset_name=toolset)


def make_tools(seen):
    return [
        CustomTool(
            "escalate",
            "转人工",
            {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
            lambda i: (seen.append(i) or "已转人工", True),
        )
    ]


def test_custom_tool_mislabelled_as_browser_member_is_routed_to_custom_handler():
    seen = []
    client = FakeClient(
        [
            [
                tool_use("navigate", {"url": "/po"}, "browser", "a"),
                tool_use("escalate", {"reason": "供应商不存在"}, "browser", "b"),
            ]
        ]
    )
    ex = FakeExecutor()
    res = run_agent(
        ex,
        "sys",
        [{"type": "text", "text": "go"}],
        make_tools(seen),
        AgentConfig(model="claude-haiku-5-5"),
        client=client,
    )
    assert ex.calls == ["navigate"]
    assert seen == [{"reason": "供应商不存在"}]
    assert res.stop_reason == "finished" and res.misrouted == 1


def test_content_filter_ends_run_without_raising():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError(
        "Output blocked by content filtering policy", response=httpx.Response(400, request=request), body=None
    )
    client = FakeClient([[tool_use("navigate", {"url": "/po"}, "browser")], err])
    res = run_agent(
        FakeExecutor(),
        "sys",
        [{"type": "text", "text": "go"}],
        make_tools([]),
        AgentConfig(model="claude-haiku-5-5"),
        client=client,
    )
    assert res.stop_reason == "content_filter" and res.turns == 1


def test_history_is_append_only():
    """每一轮发出的消息列表都是上一轮的前缀加新内容（思考块和对话绑定，不能改写历史）。"""
    client = FakeClient(
        [
            [tool_use("navigate", {"url": "/po"}, "browser", "a")],
            [tool_use("navigate", {"url": "/help"}, "browser", "b")],
            [tool_use("escalate", {"reason": "x"}, None, "c")],
        ]
    )
    run_agent(
        FakeExecutor(),
        "sys",
        [{"type": "text", "text": "go"}],
        make_tools([]),
        AgentConfig(model="claude-haiku-5-5"),
        client=client,
    )
    snapshots = client.sent
    assert [len(s) for s in snapshots] == [1, 3, 5]
    for prev, cur in zip(snapshots, snapshots[1:], strict=False):
        assert cur[: len(prev)] == prev


def test_disabled_member_gets_error_instead_of_running():
    client = FakeClient(
        [
            [tool_use("new_tab", {}, "browser", "a"), tool_use("navigate", {"url": "/"}, "browser", "b")],
            [tool_use("escalate", {"reason": "x"}, None, "c")],
        ]
    )
    ex = FakeExecutor()
    run_agent(
        ex,
        "sys",
        [{"type": "text", "text": "go"}],
        make_tools([]),
        AgentConfig(model="claude-haiku-5-5"),
        client=client,
    )
    results = client.sent[1][-1]["content"]
    assert ex.calls == [] and results[0]["is_error"] and "not enabled" in results[0]["content"]
    assert results[1]["content"].startswith("Not executed")

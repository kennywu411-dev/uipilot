"""冒烟测试：用 Haiku 5.5 跑一个最小任务，确认工具集、执行器、计费链路都通。"""
import sys
from pathlib import Path

from uipilot.agent import AgentConfig, CustomTool, run_agent
from uipilot.browser import BrowserExecutor
from uipilot.erp.server import ERPServer

answer = {}
finish = CustomTool("finish", "任务完成时调用，给出答案。",
                    {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
                    lambda i: (answer.update(i) or "已记录", True))
mode = sys.argv[1] if len(sys.argv) > 1 else "hybrid"
model = sys.argv[2] if len(sys.argv) > 2 else "claude-haiku-5-5"
with ERPServer() as erp, BrowserExecutor(erp.base_url) as ex:
    r = run_agent(ex, "你在操作一个网页 ERP。只用提供的浏览器工具。", [
        {"type": "text", "text": f"打开 {erp.base_url}/suppliers ，数一下一共有多少个供应商，"
                                 "再找出税号以 91441900 开头的供应商名称。完成后调用 finish。"}],
        [finish], AgentConfig(model=model, mode=mode, max_turns=12, budget_usd=0.3, effort="low"),
        trace_path=Path("runs/smoke.jsonl"))
print(r.to_dict())
print(answer)

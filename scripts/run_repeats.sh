#!/bin/zsh
# ③ 测试 Agent 补到每种模型 3 遍（在已有目录上续跑新的轮次）。上限合计约 $25。
set -u
S="uv run python -m uipilot.evaluate scout --workers 2 --max-turns 80 --repro-model claude-sonnet-5-5"
${=S} --model claude-haiku-5-5 --mode hybrid --repeats 3 --total-budget 2 --resume runs/scout-haiku-hybrid-bcff19
${=S} --model claude-sonnet-5-5 --mode hybrid --repeats 3 --total-budget 9 --resume runs/scout-sonnet-hybrid-6d1f9c
${=S} --model claude-opus-5-5 --mode hybrid --repeats 3 --total-budget 14 --resume runs/scout-opus-hybrid-f60f4c

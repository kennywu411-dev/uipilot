#!/bin/zsh
# 测试评测续跑：前三组接着已有目录跑完缺的轮次，后三组从头跑。
set -u
S="uv run python -m webhands.evaluate scout --workers 2 --max-turns 80 --repro-model claude-sonnet-5-5"
${=S} --model claude-haiku-5-5 --mode hybrid --repeats 2 --total-budget 2 --resume runs/scout-haiku-hybrid-bcff19
${=S} --model claude-haiku-5-5 --mode hybrid --no-signals --total-budget 2 --resume runs/scout-haiku-hybrid-nosig-6e57fb
${=S} --model claude-haiku-5-5 --mode dom --total-budget 3 --resume runs/scout-haiku-dom-640293
${=S} --model claude-haiku-5-5 --mode vision --total-budget 3
${=S} --model claude-sonnet-5-5 --mode hybrid --total-budget 6
${=S} --model claude-opus-5-5 --mode hybrid --total-budget 10

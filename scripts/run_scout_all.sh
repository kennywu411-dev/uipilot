#!/bin/zsh
# 测试 Agent 评测全套。复现统一用 Sonnet 5.5，保证各组的复核标准一致。
set -u
S="uv run python -m uipilot.evaluate scout --workers 2 --max-turns 80 --repro-model claude-sonnet-5-5"
${=S} --model claude-haiku-5-5 --mode hybrid --repeats 2 --total-budget 5
${=S} --model claude-haiku-5-5 --mode hybrid --no-signals --total-budget 3
${=S} --model claude-haiku-5-5 --mode dom --total-budget 3
${=S} --model claude-haiku-5-5 --mode vision --total-budget 3
${=S} --model claude-sonnet-5-5 --mode hybrid --total-budget 6
${=S} --model claude-opus-5-5 --mode hybrid --total-budget 10

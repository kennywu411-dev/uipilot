#!/bin/zsh
# 稳定性：Opus 在同样 11 个 TodoMVC 实现上补到 3 遍；之后用带计算器的复现让 Sonnet 在 ERP 上完整跑 3 遍。
set -u
T=(react vue svelte preact lit javascript-es6 javascript-es5 web-components jquery backbone mithril)
uv run python -m uipilot.evaluate external --workers 2 --targets $T --model claude-opus-5-5 --repeats 3 --total-budget 16 --resume runs/external-opus-20261107-004119
uv run python -m uipilot.evaluate scout --workers 2 --max-turns 80 --repro-model claude-sonnet-5-5 --model claude-sonnet-5-5 --mode hybrid --repeats 3 --check-expectation --total-budget 12

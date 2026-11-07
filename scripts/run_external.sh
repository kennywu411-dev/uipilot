#!/bin/zsh
# ② TodoMVC 实战：11 个框架实现，Haiku 和 Sonnet 各探索一遍，复现统一用 Sonnet。上限合计约 $20。
set -u
T=(react vue svelte preact lit javascript-es6 javascript-es5 web-components jquery backbone mithril)
X="uv run python -m uipilot.evaluate external --workers 2"
${=X} --targets $T --model claude-haiku-5-5 --total-budget 3 --resume runs/external-haiku-a79dc4
${=X} --targets $T --model claude-sonnet-5-5 --total-budget 17

#!/bin/zsh
# ② 加一档：Opus 在同样 11 个 TodoMVC 实现上探索一遍（复现仍用 Sonnet，和前两遍一致）。上限 $12。
set -u
T=(react vue svelte preact lit javascript-es6 javascript-es5 web-components jquery backbone mithril)
uv run python -m webhands.evaluate external --workers 2 --targets $T --model claude-opus-5-5 --total-budget 12

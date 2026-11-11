#!/bin/zsh
# ① 代码校验对照：同一套带税号的难题集，关校验 vs 开校验，三档模型。上限合计约 $12。
set -u
E="uv run python -m webhands.evaluate entry --set hardtax --workers 3 --max-turns 60 --run-budget 1.0"
${=E} --model claude-haiku-5-5 --total-budget 0.6
${=E} --model claude-haiku-5-5 --checks --repeats 2 --total-budget 0.8
${=E} --model claude-sonnet-5-5 --total-budget 2.5
${=E} --model claude-sonnet-5-5 --checks --total-budget 2.5
${=E} --model claude-opus-5-5 --total-budget 4.5
${=E} --model claude-opus-5-5 --checks --total-budget 4.5

#!/bin/zsh
# 录单评测全套：观察方式对照（Haiku）+ 模型对照（hybrid）。每组单独封顶。
set -u
E="uv run python -m webhands.evaluate entry --workers 3"
${=E} --model claude-haiku-5-5 --mode hybrid --repeats 2 --total-budget 2
${=E} --model claude-haiku-5-5 --mode dom --total-budget 2
${=E} --model claude-haiku-5-5 --mode vision --total-budget 2
${=E} --model claude-sonnet-5-5 --mode hybrid --total-budget 5
${=E} --model claude-opus-5-5 --mode hybrid --total-budget 8

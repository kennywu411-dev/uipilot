#!/bin/zsh
# 难题集：观察方式对照（Haiku）+ 模型对照；另外用修好的路由重跑基础集 Haiku hybrid。
set -u
E="uv run python -m uipilot.evaluate entry --workers 3 --max-turns 60 --run-budget 1.0"
${=E} --set basic --model claude-haiku-5-5 --mode hybrid --repeats 2 --total-budget 1
${=E} --set hard --model claude-haiku-5-5 --mode hybrid --repeats 2 --total-budget 2
${=E} --set hard --model claude-haiku-5-5 --mode dom --total-budget 1.5
${=E} --set hard --model claude-haiku-5-5 --mode vision --total-budget 1.5
${=E} --set hard --model claude-sonnet-5-5 --mode hybrid --total-budget 5
${=E} --set hard --model claude-opus-5-5 --mode hybrid --total-budget 8

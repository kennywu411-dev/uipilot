#!/bin/zsh
# ③ 补齐：Haiku 的另外三组也补到 3 遍（复现方式与之前一致，不开 --check-expectation）。上限合计约 $9。
set -u
S="uv run python -m webhands.evaluate scout --workers 2 --max-turns 80 --repro-model claude-sonnet-5-5 --model claude-haiku-5-5 --repeats 3"
${=S} --mode vision --total-budget 3 --resume runs/scout-haiku-vision-5e58bc
${=S} --mode dom --total-budget 3 --resume runs/scout-haiku-dom-640293
${=S} --mode hybrid --no-signals --total-budget 3 --resume runs/scout-haiku-hybrid-nosig-6e57fb

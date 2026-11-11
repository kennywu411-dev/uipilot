"""验证问题日志 P14 的修法：复现时独立核对期望值，能不能挡住“期望值算错”的误报，又不误伤真缺陷。

  uv run python scripts/check_repro_expectation.py
对同一组报告分别用旧复现（只核对现象）和新复现（再核对期望值）各跑几次，结果写到 runs/repro-expectation-*.json。
"""

import json
import uuid
from pathlib import Path

from webhands.agent import AgentConfig
from webhands.erp.server import ERPServer
from webhands.evaluate import GROUPS
from webhands.scout.agent import BugReport, reproduce

ROOT = Path(__file__).resolve().parents[1]
CFG = AgentConfig(model="claude-sonnet-5-5", max_turns=25, budget_usd=0.4)
REPEATS = 3
ONLY_NEW = True  # 第二轮验证：只跑带 calc 工具的新复现，旧复现的结果已在第一轮记录

# (运行目录, 轮次, 标题前缀, 是否真缺陷)
CASES = [
    ("scout-sonnet-hybrid-6d1f9c", "B-2", "发票税额计算错误：大额时税额少 1 元", False),
    ("scout-haiku-hybrid-bcff19", "C-1", "登记发票选择税率 9% 时", True),
    ("scout-haiku-hybrid-bcff19", "A-1", "采购单合计按整数截断单价计算", True),
    ("scout-haiku-hybrid-bcff19", "B-1", "订单日期录入 2026-03-12 保存后变为", True),
]


def load(run: str, tag: str, prefix: str) -> BugReport:
    data = json.loads((ROOT / "runs" / run / f"{tag}.json").read_text(encoding="utf-8"))
    r = next(x for x in data["reports"] if x["title"].startswith(prefix))
    keep = ("title", "rule", "steps", "expected", "actual", "severity")
    return BugReport(**{k: r[k] for k in keep})


def main() -> None:
    rows, cost = [], 0.0
    for run, tag, prefix, real in CASES:
        group = tag.split("-")[0]
        for check in (True,) if ONLY_NEW else (False, True):
            for i in range(REPEATS):
                rep = load(run, tag, prefix)
                with ERPServer() as erp:
                    erp.reset(GROUPS[group])
                    res = reproduce(erp.base_url, rep, CFG, check_expectation=check)
                cost += res.cost_usd
                rows.append(
                    {
                        "report": prefix,
                        "real_bug": real,
                        "check_expectation": check,
                        "repeat": i,
                        "confirmed": rep.reproduced,
                        "notes": rep.repro_notes,
                        "cost": round(res.cost_usd, 4),
                    }
                )
                print(
                    f"{'新' if check else '旧'} {'真缺陷' if real else '误报'} {prefix[:18]:20} 第{i + 1}次 → "
                    f"{'确认' if rep.reproduced else '不确认'}  ${res.cost_usd:.3f}",
                    flush=True,
                )
    out = ROOT / "runs" / f"repro-expectation-{uuid.uuid4().hex[:6]}.json"
    out.write_text(json.dumps({"rows": rows, "cost": round(cost, 4)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"共 ${cost:.2f} → {out}")


if __name__ == "__main__":
    main()

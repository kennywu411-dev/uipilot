# UIPilot：让 Agent 在没有 API 的网页系统里干活

很多企业系统没有 API，只能靠人在网页上点：老 ERP、供应商门户、内部 OA。UIPilot 用 Claude 的浏览器工具集（`browser_toolset_20260801`）搭了一个**共用底座**，在上面做了两个应用：

- **探索式测试 Agent**：读业务规则，在页面上操作，找出“结果不对”的缺陷；每条报告在重置后的环境里由另一个 Agent 复现。
- **单据录入 Agent**：看供应商单据的扫描件，在老 ERP 里录采购单；拿不准的（供应商或商品不存在、金额自相矛盾、单号已录过）转人工；不允许删除任何数据。

展示页（评测结果 + 可逐步播放的操作回放）在 [`site/`](site/)，本地预览：`python3 -m http.server -d site`。

## 结果（详见 [docs/numbers.md](docs/numbers.md)）

### 测试 Agent：在真实开源项目上找到 15 处规格偏差

[TodoMVC](https://github.com/tastejs/todomvc) 的几十个框架实现都要遵守同一份公开规格书。测试 Agent 拿规格书当业务规则，在 11 个官方实现上探索，复现通过的报告再用 Playwright 按真人操作逐条复核：

- **确认 7 个实现共 15 处规格偏差**，在仓库 issue 和 PR 里没搜到。例如 React 按 Esc 不退出编辑、之后一失焦“放弃”的修改反而被保存；mithril 在 Active 筛选下删除一条，删掉的是另一条隐藏的已完成条目。
- 其中 8 处在 lit 和 web-components 上：它们用了 shadow DOM，官方测试工具看不进去，被排除在默认测试之外。Agent 通过浏览器工具集照样能测。
- Haiku 1 遍 $1.20 找到 8 处，Sonnet 1 遍 $2.52 找到 5 处，Opus 3 遍 $21.35 找全了 15 处（10 处每遍都找到）。详见 [docs/外部实战.md](docs/外部实战.md)。已按实现分成 7 份 issue 提交给维护者：[#2309](https://github.com/tastejs/todomvc/issues/2309)–[#2315](https://github.com/tastejs/todomvc/issues/2315)。

### 测试 Agent：在 ERP 靶子上的对照评测

14 个植入缺陷，其中 12 个不会报错，只有读懂业务规则、自己算一遍才能发现。每种配置跑 3 遍。

| | 检出率（复现确认） | 误报 | 每组成本（估算） |
|---|---|---|---|
| Haiku 5.5 只看截图 / 只看页面结构 / 两者都给 | 76.2% / 85.7% / 88.1% | 0 | $0.26–0.37 |
| Sonnet 5.5（两者都给） | 88.1% | 1 | $0.92 |
| Sonnet 5.5 + 复现时用计算器核对期望值 | 85.7% | 0 | $0.93 |
| Opus 5.5（两者都给） | 90.5% | 0 | $1.69 |
| 随机点击（对照组） | 0% | - | $0 |

- 270 条报告全部人工核对过评判结果，评判模型和人工一致 266 条。
- 唯一的误报：Sonnet 把一笔 13% 的税额算错了 1 元，复现 Agent 只核对现象、没核对期望值。让复现 Agent “自己再算一遍”没用，它三次都算出同样的错数；改成给它一个用 Decimal 精确计算的 `calc` 工具后，这条误报 3 次都被挡下，全量重跑误报为 0。
- 其余没对上植入清单的报告，都是靶子本身的真实缺陷（我写靶子时留下的），已修复。

### 录单 Agent：缺的是信息，不是校验

难题集 20 张单据：每张 6–10 行明细、低分辨率扫描；8 张陷阱单要转人工，包括名字只差两个字的另一家公司。

| | 难题集整单成功 | 录错 | 单张成本（估算） |
|---|---|---|---|
| Haiku 5.5 | 85–90% | 0–3 | $0.005–0.007 |
| Sonnet 5.5 / Opus 5.5 | 95% / 95% | 1 / 1 | $0.099 / $0.183 |
| 单据印上税号后，三档模型（不开校验 / 开代码校验） | 100% / 100% | 0 | 不变 |

- 三档模型都把“深圳市华强电子**科技**有限公司”当成系统里的“深圳市华强电子有限公司”录过。单据印上税号后，不开任何校验录错就降到 0。代码校验（按税号定供应商、Decimal 重算合计、保存后回读）在这批单据上没有再提高正确率，价值在于让结论可审计。
- Haiku 在模糊扫描上会看错字（“英规”读成“美规”），但看错后转人工，没有录错数据。
- 所有运行中，已有单据被删除的次数为 0。

## 设计要点（详见 [docs/design.md](docs/design.md)）

- **安全边界放在执行器里，不靠提示词**：网络层只放行被测系统；录单时，会删数据的请求在网络层拦截；按钮文字检查只做提示，并对不提交数据的纯页面按钮放行。
- **模型负责读，代码负责判**：录单的供应商、合计、重复单号，测试复现里的金额期望值，都由代码下结论。
- **三种观察方式只是一个开关**：只看截图、只看页面结构、两者都给，只改 toolset 的 `configs`。
- **复现之后还要复核，复核方法本身也要被审视**：复现能挡住随机误报，挡不住同一种系统性错误；确定性脚本也有盲区（用 `fill()` 复核掩盖了“编辑框没获得焦点”，问题日志 P13）。
- **历史只追加**：Opus 5.5 / Sonnet 5.5 的思考块和对话绑定，循环从不改写已发出的消息，靠提示缓存控制成本。

## 结构

```
src/uipilot/
  browser.py    browser_toolset 31 个成员工具的 Playwright 执行器 + 网络边界 + 删除拦截 + 报错/5xx 信号
  snapshot.js   可访问性树与稳定引用
  agent.py      Agent 循环：预算、轮数上限、计费、轨迹、截图回放、内容过滤与工具误标的容错
  calc.py       给 Agent 用的精确计算器
  erp/          靶子：“宏远进销存 V3.2”，14 个可开关的植入缺陷
  scout/        测试 Agent（探索 → 复现 → 评判）、外部项目适配、随机点击对照组
  entry/        录单 Agent、代码校验、单据生成器（基础集、难题集、难题集 + 税号）
  evaluate.py   评测入口：总预算封顶、单轮容错、--resume 续跑
scripts/        展示页构建、回放录制、TodoMVC 复核脚本、对照实验脚本
site/           展示页（page.html 为页面，build_site.py 生成 index.html 和 data.json）
docs/           设计说明、数字登记表、问题日志（15 条）、外部实战
```

## 运行

```bash
uv sync
uv run playwright install chromium
uv run pytest                       # 40 个测试，不调用 API
```

需要在项目根目录的 `.env` 里写 `ANTHROPIC_API_KEY=...`（已在 `.gitignore` 里）。

```bash
uv run python scripts/smoke.py hybrid claude-haiku-5-5        # 最小任务，约 $0.001
uv run python -m uipilot.evaluate entry --set hardtax --checks --model claude-haiku-5-5 --total-budget 1
uv run python -m uipilot.evaluate scout --model claude-haiku-5-5 --groups A clean --check-expectation --total-budget 2
uv run python -m uipilot.evaluate external --targets react svelte --model claude-haiku-5-5 --total-budget 1
uv run python scripts/build_site.py  # 汇总 runs/ 生成展示页数据
```

TodoMVC 需要先克隆到 `targets_ext/todomvc`（已在 `.gitignore` 里）：`git clone --depth 1 https://github.com/tastejs/todomvc.git targets_ext/todomvc`。

## 局限

- ERP 靶子和植入缺陷是自己做的；TodoMVC 是真实项目，但规模小，偏差多是边界行为。
- 每种 ERP 配置 3 遍、TodoMVC 大多 1 遍，模型之间的差距多数在噪声范围内。
- 录单单据是生成的，版式只有三种。

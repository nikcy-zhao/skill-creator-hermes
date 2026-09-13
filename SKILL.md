---
name: skill-creator-hermes
description: 创建技能、检测、改进、优化、精简、重构、评测指定技能
version: 2.35.2
author: cat, Hermes Agent
license: Apache-2.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [skills, authoring, evals, benchmark]
---

# skill-creator-hermes

## When to Use

- 用户想从零做一个新技能，或检测/改进/优化/精简/重构指定技能
- 用户想跑基准测试或提升触发准确率

不要用于：对已入库技能的结构进行删除、合并、恢复、迁移备份——那是 hermes-skill-curation 的活。

## Prerequisites

- `hermes` CLI 可用
- `python3` ≥3.9 在 PATH（统一 `python3 -m scripts.<name>` 调用）

## Quick Reference

### 命令

| 用途 | 命令 |
|---|---|
| 回归测试 | `python3 -m unittest scripts.tests.test_scripts`（scripts/ 回归） |
| 技能组单跑 | `terminal(command="hermes -z \"<提示词>\" --skills <技能名> --usage-file <运行目录>/usage.json 2>&1 \| tee <运行目录>/stdout.log", background=true)`；基线＝同命令去 `--skills <技能名>` |
| 单技能校验 | `python3 -m scripts.quick_validate <技能路径>`（交付预检用 `--strict`，多查 H1/空行/必选节/体积/license） |
| 交付预检 | `python3 -m scripts.precheck_deliver <技能源库路径>`（死资产/幽灵路径/副本同步/strict 四查＋`--snapshot-to <工作区>` 留旧版快照） |
| 基准汇总 | `python3 -m scripts.aggregate_benchmark <workspace>/iteration-N --skill-name <name> --skill-path <技能路径> --executor-model <模型ID> --verify`（`--verify` 查产物归属＋技能真用；基线沿用上一次迭代加 `--reuse-baseline <上一次迭代目录>`；模型 ID 字段说明见 `references/schemas.md`） |
| 批量脚本评分 | `python3 -m scripts.grade_runs <workspace>/iteration-N`（扫全部 run 跑 check.py 出 grading.json） |
| 触发评测单跑 | `python3 -m scripts.run_eval --eval-set <eval-set.json> --skill-path <skill路径> --verbose` |
| 触发优化循环 | `python3 -m scripts.run_loop --eval-set <trigger-eval.json> --skill-path <skill路径> --model <当前会话模型ID> --results-dir <workspace>` |
| 审查页生成 | `python3 -m scripts.gen_review_page --queries <查询JSON> --skill-path <技能路径> --open` |
| 结果查看器 | `terminal(command="python3 -m scripts.generate_review <workspace>/iteration-N --skill-name \"<name>\" --benchmark <workspace>/iteration-N/benchmark.json", background=true)`；静默重启（页面已开、改后让用户自行刷新）加 `--no-open`，防重复弹标签页 |

### 被测对象铁律

说测 X 就测 X 本体（X 取用户原话），禁止自造替代对象。「自检/测自己」的 X＝本技能，快照既作被测技能也作任务对象与夹具（夹具＝评测中临时充当被测对象的技能副本），禁止另造。

### 流程铁律

任务从「1. 获取需求」进入：分流后先声明「情境 N → 入口步骤 → 对象 X」再动手，禁止跳步开工。仅在用户关卡（检验报告过目、查看器审查、拍板）与流程外动作处停。

### 全文通读规则

1. 指定的文件必须每行读完——引用滞后、脚本消费关系只有通读能抓；禁止只读一部分就改、禁止用 grep/抽样/凭记忆代替通读（锚点 grep 只能作通读后的复核）
2. 大文件分页读必须读到末页（见 total_lines/truncated 标记即未读完）；文件中指向的其他文件也按本规则通读
3. 用户粘贴的片段可能与盘上现状不一致，动手前先读盘上现状；补内容前先按「写作规范」的归属规则找位置，禁止锚点 patch 随手塞

## Procedure

### 1. 获取需求

**获取情境 1/2**：对话里已有工作流，或用户提供详细需求——从历史/输入提取用的工具、步骤顺序、用户做过的修正、输入/输出格式，分析缺少的环节；向用户确认技能职责、触发时机、期望输出格式、要不要测试用例、边界情况与依赖后，进入「2. 撰写 SKILL.md」。

**获取情境 3-6**：用户直接给出完整 skill 目录/文件（情境 6 为截取片段反馈），按诉求分流：

| 情境 | 用户诉求 | 入口 |
|---|---|---|
| 3 | 优化、改进、精简、重构、自检、检测 | 「3. 检验 Skill」 |
| 4 | 评测 | 「5. 评测（定稿验证）」 |
| 5 | 提升触发准确率 | 「6. Description 优化」 |
| 6 | 截取 skill 片段反馈 | 「4. 改进技能」 |

### 2. 撰写 SKILL.md

1. **写作模式**：动 frontmatter 前按「全文通读规则」通读 `references/frontmatter-spec.md`，逐条落位；动正文前通读 `references/writing-rules.md`，严格按正文结构和写作规范撰写；写完进入「3. 检验 Skill」
2. **重构模式**：从「3. 检验 Skill」带着《功能清单》进入本步时，按清单把内容逐条落进七节——只挪不删，拿不准归属的先放 Procedure；重排完逐条核销《功能清单》，任何一条在新结构中找不到落点即不算完成，核销完照常进入「3. 检验 Skill」

### 3. 检验 Skill

**Step 1**: 按「全文通读规则」通读被检技能目录下全部正文文件（SKILL.md 与 references/、templates/ 的 md），逐行读完（scripts/assets 归回归测试）；再通读判定器 references/ 下 frontmatter-spec.md、writing-rules.md、废话形态清单.md、重复形态清单.md（问 3 须逐项过两份清单，禁止凭印象）

**Step 2**: 逐份文件问四问：① 目录、frontmatter、正文结构、体积是否符合各标准？② 正文内容是否符合写作规范？③ 是否还有废话和重复内容？④ 有无执行缺口——指令依赖的脚本行为/前置条件与盘上实际不符（照文执行会漏步、空转或指错），判定须对照脚本源码与引用锚点，禁止凭印象判「应该没问题」。

四问判定落成《覆盖表》入检验报告：行＝被检文件，列＝四问结论；缺一＝本步骤未完成，禁止出报告。

- 四问全「是」：进入「5. 评测（定稿验证）」
- 有一为否，按结构判定两路（互斥）：
  - **需重构**（结构与七节完全对不上、逐节补等于大面积搬运）：不出检验报告，通读后输出《功能清单》（`skill-name 功能清单.md` 放工作目录：逐条摘出全部命令/代码块、判定规则、坑点、验证点、参数细节，一行一条，唯一验收依据），带着清单返回「2. 撰写 SKILL.md」重构模式
  - **不需重构**：输出检验报告——具体指出哪个字段/哪句话与哪个标准不符、正文缺失与需改标题的部分；只输出问题和建议，不修复（修复是「4. 改进技能」的工作）。报告存 `skill-name 检验报告.md` 放工作目录，带着报告进入「4. 改进技能」

### 4. 改进技能

**Step 1**: 按入口完成前置——检验报告入口（经步骤 3）全文已在上下文直接进 Step 2；用户点名（情境 6）与循环回流（3↔4 / 4↔5）入口重读 skill 全文（用户贴的片段≠盘上现状），4↔5 回流另读工作区根 `feedback.json`，再进 Step 2。改进现有技能且本轮首次动手前，先按 Quick Reference 交付预检命令留旧版快照（`--snapshot-to <工作区>`，供「7. 盲比较」；改后再 cp 拿到的是新版，快照不可事后补）

**Step 2**: 依次对检验报告/用户反馈中的问题，按照 `references/writing-rules.md`「写作规范」与 `references/改进原则.md` 逐条执行修改

**Step 3**: 按改动来源分流出口——来自「3. 检验 Skill」或用户点名（情境 6）：返回「3. 检验 Skill」复检直到由 3 放行进入「5. 评测」；来自「5. 评测」回流：直接回「5. 评测」重跑（基线按「基线复用」规则判断重跑还是沿用），循环 4↔5 至收敛（判据见第 5 节第 4 步分流），收敛后跑最后全量定稿评测。改动随做两副本同步；禁止以验证结果收尾回合。

### 5. 评测（定稿验证）

注意：`--skills` 只认**已安装**在 `~/.hermes/skills/` 的技能名、不认路径；触发率必须走 run_eval.py（触发判定靠其查 state.db 会话记录，手工方式测不了）。

**第 1 步：搭建工作区、写用例、同一轮启动所有运行。** 后台运行期间守在本会话主动播报进度：每个节点（轮次完成、评分出炉、分数变化）报告一次，不等用户来问。
1. 在工作区为每个测试用例建 `eval-<描述性名>/` 目录链（结构见 `references/schemas.md`「工作区布局」），目录随流程生长，不预建整棵树。
2. 构思 2-3 个真实用户会说出口的测试提示词（改进现有技能时优先从上一次迭代的 `feedback.json` 真实反馈取材，无则自拟），确认后写入各 eval 目录的 `eval_metadata.json`（判据暂空，第 2 步补齐；schema 见 `references/schemas.md`）。夹具红线见 `references/eval-protocol.md`「污染防护」。
3. 每个用例同一轮同时启动技能组 + 基线组两组。随机性大的用例每配置跑 2-3 个 run-N，以多次通过的占比抵消随机波动。技能组首选 `delegate_task`（提示词含技能路径、任务、输出保存路径），无子代理环境时用 Quick Reference 单跑命令；基线一律＝无技能运行（复用规则见「基线复用」）。派发提示词按 `references/eval-protocol.md`「派发提示词固定条款」逐条写入。

**第 2 步：等待期间起草判据。** 判据要客观可验证、名字在基准查看器里一眼看懂；区分度自查、用例筛选与主观型技能处理见 `references/eval-protocol.md`「判据起草」。写好更新各 eval 目录的 `eval_metadata.json`（assertions 数组），并向用户解释每条检查什么。判据脚本化（哪些进 check.py、哪些标「模型判」）同步做，见 `references/eval-protocol.md`「判据脚本化」。

**迭代期分层升级（定稿验证不适用，只用于 4↔5 循环方向判断）**：机制见 `references/eval-protocol.md`「分层升级」。

**第 3 步：评分、汇总、开查看器。** 仅汇总显示 time_seconds 为 0 且非 delegate 时才手工补 timing.json。
1. 评分：Quick Reference 批量脚本评分命令跑 check.py 出 grading.json；无 check.py 的 run 由评分子代理补齐（读 `templates/grader.md`，注意事项见 `references/eval-protocol.md`「评分子代理」）。
2. 汇总：Quick Reference 的基准汇总命令，产出 `benchmark.json`/`benchmark.md`。
3. 分析：按 `templates/analyzer.md` 找模式；结论按用例分列组间差、汇报各配置 run 数的规则见 `references/eval-protocol.md`「结果分析」。
4. 查看器：命令见 Quick Reference，迭代参数与静态页备注见 `references/eval-protocol.md`「查看器备注」。不要自己写 HTML。
5. 告诉用户：浏览器已开结果页，「输出」标签页逐用例看输出留反馈，「基准对比」标签页看定量对比，「摘要」标签页看评级与体积环，看完回来。

**第 4 步：读反馈（仅全量轮；冒烟轮按分层升级结论直接回流）。** 读 `feedback.json`（空反馈=没问题），改进重点放在有具体抱怨的用例上。扫 `~/.hermes/skills/` 见 `<被测技能名>-eval-*` 目录即删。之后分流：反馈仍有具体抱怨且可改进 → 回「4. 改进技能」（循环回流入口）；用户满意 / 反馈全空 / 不再有有意义进展 → 循环收敛，进入「6. Description 优化」。

### 6. Description 优化（定稿后主动提出）

无人值守场景（hermes -z 等）以书面形式提出：交付说明写入建议理由与命令（引 Quick Reference），执行待用户拍板，禁止自行跑优化循环。

**生成触发评测查询**：20 条真实查询（应触发 8-10 + 不应触发 8-10），存 JSON：`[{"query": "...", "should_trigger": true}]`；真实性与负例设计要求见 `references/eval-protocol.md`「触发评测查询」。

**与用户审查**：Quick Reference 审查页生成命令产出审查页。用户编辑后点 Export，评测集下载到 `~/Downloads/eval_set.json`（多份取最新）。

**跑优化循环**（后台运行、定期汇报进度）：Quick Reference 的触发优化循环命令，`--model` 传当前会话模型 ID（会话元数据里就有；`hermes status` 显示的是 CLI 默认模型，未必等于会话模型）；训练集/留出集切分与 `best_description` 选取规则见 `references/eval-protocol.md`「触发评测查询」。

**应用**：用 `best_description` 更新 frontmatter，向用户展示前后对比和分数。

### 7. 盲比较（可选）

要严格比较两个版本（用户问「新版本真的更好吗？」）时用：流程、token效率计分与揭盲分析见 `references/eval-protocol.md`「盲比较」（提示词载体 `templates/comparator.md`、`templates/analyzer.md`）。

## Pitfalls

- 写文件前未核归属，覆盖了兄弟 run 的产物 → 根因：多代理并行时同名路径不唯一，write 前只看路径不看会话归属 → 修法：见 `references/eval-protocol.md`「污染防护」
- 评测查询没触发 / 触发率恒 0 → 根因：查询措辞无分量——宽松措辞实测 0/5、明确指令 5/5 → 修法：换有分量、指令明确的查询；验证评测脚本自身可用必须真跑一条明确指令式 query，--help 探活不算数
- 汇总 exit 2（token真值闸门：估算级拒写）→ 根因与修法见报错信息 → `--allow-estimate` 仅限临时排查，正式报告禁用；汇总漏 `--skill-path` 则海报无体积环
- 后台任务 poll 误报 exited/exit_code null → 根因：`> log 2>&1` 重定向关闭注册表监听的 stdout 管道，reader 把管道 EOF 当退出，进程其实还在跑 → 修法：见 exited 且 exit_code 为 null 时先 `ps -p <pid>` 加查期望产物落盘，证据齐才算真退出，禁止直接 relaunch

## Verification

- 时机：任务交付时过一遍本节——4↔5 循环收敛后才算交付；未走 4↔5 循环的一次性改动不算。
- 每轮 4↔5 迭代结束：全量轮 `benchmark.json`/`benchmark.md` 已生成、查看器已开、用户反馈已读回；冒烟轮按分层升级完成方向判定即算完成。「4. 改进技能」改动后未按 Step 3 分流返回对应循环=轮次未完成，静态检查通过不算交付。
- 改动 scripts/ 后：Quick Reference 回归测试命令通过。
- description 优化后：frontmatter 已更新为新 description，且向用户展示了前后对比和分数。
- 精简/重构后：功能清单逐条核销无遗漏，自跑一遍新写的 Verification 节确认照原样能执行。
- **零残留三查与逻辑一致性自检（细则见 `references/改进原则.md`「交付门槛」）**：文件全消费、路径全对齐、副本全同步跑 `python3 -m scripts.precheck_deliver <技能源库路径>`（红即未过）；「同一事实一个说法」为语义判断，通读自检。
- 交付前：被改技能自身 `quick_validate --strict` 通过。

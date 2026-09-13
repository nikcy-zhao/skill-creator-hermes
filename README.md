# skill-creator-hermes

[Hermes Agent](https://hermes-agent.nousresearch.com/) 的技能工程化工具集：**创建、检验、改进、评测技能**，一条龙覆盖技能从起草到定稿的完整生命周期。

## 它解决什么问题

给 agent 写的 skill（SKILL.md + 脚本 + 引用文件）没有工程化质量保障——写完不知道好不好、改完不知道有没有退步、触发不了也不知道为什么。这个技能把三件事变成可执行的流程：

1. **检验**：按 frontmatter / 正文结构 / 写作规范 / 执行缺口四问对任意技能做体检，输出检验报告
2. **评测**：技能组 vs 基线组（无技能）对照跑真实任务，量化技能的边际贡献（Δpass_rate）
3. **迭代**：4↔5 改进循环（反馈驱动）＋ 冒烟分层升级（±15% 方向判定）＋ 收敛后 Description 触发优化

实测（v2.35.2，glm-5.3，3 用例 × 双组对照）：技能组判据通过率 **91.7%** vs 基线 **33.3%**，Δ=+58%——模型在没有这套判定器的情况下，连「version 不合 semver」都判不准。

## 安装

```bash
git clone https://github.com/nikcy-zhao/skill-creator-hermes.git ~/.hermes/skills/skill-creator-hermes
```

依赖：`hermes` CLI 可用；`python3` ≥ 3.9。

## 使用

在 Hermes 会话里直接说话即可触发：

- 「帮我检验一下 `<技能路径>` 写得合不合格」→ 检验流程，出报告
- 「改进 `<技能路径>`」→ 检验 → 改进 → 复检循环
- 「评测一下这个技能」→ 搭工作区、双组对照跑分、出 benchmark 和查看器
- 「这个新版本真的比旧版好吗？」→ 盲比较（量规 0.8 + token 效率 0.2）

### 核心命令（Quick Reference 摘录）

| 用途 | 命令 |
|---|---|
| 单技能校验 | `python3 -m scripts.quick_validate <技能路径> [--strict]` |
| 交付预检 | `python3 -m scripts.precheck_deliver <技能源库路径>` |
| 回归测试 | `python3 -m unittest scripts.tests.test_scripts` |
| 基准汇总 | `python3 -m scripts.aggregate_benchmark <workspace>/iteration-N --skill-name <n> --skill-path <p> --executor-model <m> --verify` |
| 结果查看器 | `python3 -m scripts.generate_review <workspace>/iteration-N --skill-name <n> --benchmark <benchmark.json>` |

在技能根目录下执行。

## 仓库结构

```
skill-creator-hermes/
├── SKILL.md              # 技能本体：七节流程（入口分流/检验/改进/评测/优化/盲比较）
├── references/           # 判定器与协议：frontmatter 规范、写作规范、废话/重复形态清单、评测协议、JSON schema
├── templates/            # 子代理提示词：评分（grader）、盲比较（comparator）、事后分析（analyzer）
├── scripts/              # 12 个工具脚本：校验、预检、评分、汇总、触发评测/优化、查看器、报告
│   └── tests/            # 26 项回归测试
└── assets/               # 基准查看器 HTML（体检摘要卡 / 逐判据矩阵 / 输出审查页）
```

## 评测工作流一瞥

```
搭工作区 → 写用例+判据 → 双组并行执行（技能组 vs 基线组）
    → 评分（脚本判 + 模型判）→ aggregate_benchmark --verify
    → HTML 查看器：体检卡(四维度) / 基准对比 / 逐 run 输出审查+反馈
    → 反馈回流改进 → 4↔5 循环至收敛 → Description 触发优化
```

查看器的「体检摘要」直接内置判定：Δ≥+20% 判「优」，负数判「有害：技能在帮倒忙，必须重写」；另有判据区分度（≥15% 健康）、token 成本、稳定性（stddev>20% 报警）与体积环三区判定。

## License

Apache-2.0

# JSON 模式（Schemas）

## 工作区布局

评测工作区建在 hermes 的工作目录（terminal.cwd），命名 `<技能名>-workspace/`；结构如下——`eval-*`、`run-N/`、`outputs/` 三层结构不可变（脚本按此找数据），每跑一次建新 run 目录编号顺延；`timing.json`、`grading.json` 进 run 目录，`feedback.json` 进工作区根：

```
<技能名>-workspace/
└── iteration-1/                  ← 第几轮测试
    └── eval-格式转换/            ← 一个测试用例（用例名要描述性）
        ├── eval_metadata.json    ← 用例定义（prompt 等，schema 见下）
        ├── with_skill/           ← 技能组跑（目录名是脚本契约）
        │   └── run-1/outputs/    ← 第 1 次运行的输出
        └── without_skill/        ← 基线组跑（目录名是脚本契约）
            └── run-1/outputs/
```

---

## eval_metadata.json

一个测试用例的定义。位于工作区每个 eval 目录及其下每个 run-N/ 目录（或其父级）——**脚本真正消费的唯一用例文件**（aggregate_benchmark.py 读 eval_id，generate_review.py 读 prompt/eval_id）。

```json
{
  "eval_id": 1,
  "eval_name": "格式转换",
  "prompt": "User's example prompt",
  "assertions": [
    "The output includes X",
    "The skill used script Y"
  ]
}
```

**字段：**
- `eval_id`：唯一整数标识符（aggregate_benchmark.py 据此分组；缺失时回退到目录名 eval-N 的 N）
- `eval_name`：人类可读用例名（aggregate_benchmark.py 读入 benchmark.json，查看器用作小节标题；缺失时回退目录名）
- `prompt`：要执行的任务（generate_review.py 展示在 Outputs 页；缺失时回退读 transcript.md 的 "## Eval Prompt" 节）
- `assertions`：可验证的陈述列表，评分子代理逐条评估后写入 grading.json

> 技能目录下的 `evals/evals.json` 已废弃，勿因外部模板惯例放回。

---

## 评分/比较/分析的输出 JSON（grading / comparison / analysis）

这三类文件由子代理按各自提示词产出，**完整结构与逐字段示例就是提示词本身的一部分**——查结构去读对应文件，此处不重复：

| 文件 | 生产者 | 位置 | 结构载体 |
|---|---|---|---|
| `grading.json` | 评分子代理（读 `templates/grader.md`） | `<run-dir>/grading.json` | `templates/grader.md` 输出格式节 |
| `comparison-N.json` | 盲比较代理（读 `templates/comparator.md`） | `<grading-dir>/comparison-N.json` | `templates/comparator.md` 输出格式节 |
| `analysis.json` | 事后分析代理（读 `templates/analyzer.md`） | `<grading-dir>/analysis.json` | `templates/analyzer.md` 输出格式节 |

---

## metrics.json

执行器代理（executor agent）的输出（可选：流程不强制产出，缺失时评分器跳过、token回退链兜底）。位于 `<run-dir>/outputs/metrics.json`。

```json
{
  "tool_calls": {
    "Read": 5,
    "Write": 2,
    "Bash": 8,
    "Edit": 1,
    "Glob": 2,
    "Grep": 0
  },
  "total_tool_calls": 18,
  "total_steps": 6,
  "files_created": ["filled_form.pdf", "field_values.json"],
  "errors_encountered": 0,
  "output_chars": 12450,
  "transcript_chars": 3200
}
```

---

## timing.json

一次运行的墙上时钟计时。位于 `<run-dir>/timing.json`。

**如何捕获：** 耗时字段（duration 等）从子代理完成通知取，立即写入。`--usage-file` 写出的 `usage.json` 含 `total_tokens`（token总数）/`api_calls`/`model` 但**没有耗时**，用 stdout.log 创建时间与 usage.json 落盘时间之差推算，并在 timing.json 里注明来源。

**delegate_task 场景的token无需手工捕获**：aggregate_benchmark 的回退链已内置 `state.db-delegation` 级，编排契约见 `references/eval-protocol.md`「派发提示词固定条款」。

```json
{
  "total_tokens": 84852,
  "duration_ms": 23332,
  "total_duration_seconds": 23.3,
  "executor_start": "2026-01-15T10:30:00Z",
  "executor_end": "2026-01-15T10:32:45Z",
  "executor_duration_seconds": 165.0,
  "grader_start": "2026-01-15T10:32:46Z",
  "grader_end": "2026-01-15T10:33:12Z",
  "grader_duration_seconds": 26.0
}
```

---

## benchmark.json

基准（Benchmark）模式的输出。位于 `<workspace>/iteration-N/benchmark.json`（aggregate_benchmark.py 默认写到 `--output` 或目录自身）。

```json
{
  "metadata": {
    "skill_name": "pdf",
    "skill_path": "/path/to/pdf",
    "executor_model": "<model-name>",
    "analyzer_model": "<most-capable-model>",
    "timestamp": "2026-01-15T10:30:00Z",
    "evals_run": [1, 2, 3],
    "runs_per_configuration": 3,
    "skill_size": {"chars": 6210, "lines": 87, "tokens_est": 4312}
  },

  "runs": [
    {
      "eval_id": 1,
      "eval_name": "Ocean",
      "configuration": "with_skill",
      "run_number": 1,
      "result": {
        "pass_rate": 0.85,
        "passed": 6,
        "failed": 1,
        "total": 7,
        "time_seconds": 42.5,
        "tokens": 3800,
        "tokens_source": "state.db-delegation",
        "api_calls": 6,
        "tool_calls": 18,
        "errors": 0
      },
      "expectations": [
        {"text": "...", "passed": true, "evidence": "..."}
      ],
      "notes": [
        "Used 2023 data, may be stale",
        "Fell back to text overlay for non-fillable fields"
      ]
    }
  ],

  "run_summary": {
    "with_skill": {
      "pass_rate": {"mean": 0.85, "stddev": 0.05, "min": 0.80, "max": 0.90},
      "time_seconds": {"mean": 45.0, "stddev": 12.0, "min": 32.0, "max": 58.0},
      "tokens": {"mean": 3800, "stddev": 400, "min": 3200, "max": 4100}
    },
    "without_skill": {
      "pass_rate": {"mean": 0.35, "stddev": 0.08, "min": 0.28, "max": 0.45},
      "time_seconds": {"mean": 32.0, "stddev": 8.0, "min": 24.0, "max": 42.0},
      "tokens": {"mean": 2100, "stddev": 300, "min": 1800, "max": 2500}
    },
    "delta": {
      "pass_rate": "+0.50",
      "time_seconds": "+13.0",
      "tokens": "+1700"
    }
  },

  "notes": [
    "Assertion 'Output is a PDF file' passes 100% in both configurations - may not differentiate skill value",
    "Eval 3 shows high variance (50% ± 40%) - may be flaky or model-dependent",
    "Without-skill runs consistently fail on table extraction expectations",
    "Skill adds 13s average execution time but improves pass rate by 50%"
  ]
}
```

**字段：**
- `metadata.executor_model` / `metadata.analyzer_model`：执行/分析模型 ID——汇总时传 `--executor-model`（及可选 `--analyzer-model`，缺省同 executor）写入真实值；两个都没传则落 `<model-name>` 占位符，查看器首屏会原样显示
- `runs[].eval_name`：人类可读的评测名（查看器用作小节标题）
- `runs[].configuration`：必须是 `"with_skill"` 或 `"without_skill"`（查看器按此精确字符串分组和配色）
- `runs[].result.tokens_source`：token取值来源（字段名 tokens 为查看器契约不改）——`timing.json` / `usage.json` / `state.db-delegation` / `state.db-session`（真值）vs `metrics.output_chars`（字符数代理）/ `outputs_estimate`（估算）/ `none`；估算级默认被汇总闸门拒写，仅 `--allow-estimate` 放行
- `runs[].result.api_calls`：委派账目带回的 API 调用次数（仅 state.db-delegation 级有）
- `metadata.runs_per_configuration`：每个配置的运行次数
- `metadata.skill_size`：被测技能本体体积 `{chars, lines, tokens_est（token估算）}`（aggregate_benchmark 按口径"汉字×0.9+非汉字×0.3"估算；海报体积环与评级阶梯的依据，查看器按 writing-rules 三区判定——舒适区不扣分，一般区评级封顶 B，警报区降一档）

**重要：** 查看器按字段名与嵌套位置精确读取，错名或错位（如 `pass_rate` 放运行顶层而非 `result` 之下）显示空值/零值。

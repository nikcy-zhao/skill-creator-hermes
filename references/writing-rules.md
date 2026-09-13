# 写作规范

## 正文结构（各节职责与归属）

```markdown
# Skill-name            ← H1 标题；导语 1-2 句：做什么、不做什么、依赖什么
## When to Use          ← 必选。触发条件 + 反向触发（不要用于…）
## Prerequisites        ← 可选。跑之前就要存在的：环境变量、安装项、依赖、MCP 服务等
## How to Run           ← 可选。脚本的通用调用约定（跨命令、命令表装不下时才写）
## Quick Reference      ← 命令清单与多步骤共用的通用规则
## Procedure            ← 必选。有序动作链，每步带完成标准；单环节专用的内容放该环节步骤内
## Pitfalls             ← 必选。踩过的坑且他处未标注；看似 bug 实为设计；易错动作给正确做法
## Verification         ← 必选。验证项清单，端到端测试全部通过技能才算生效
```

## 写作规范

- skill 是指令集，指令必须是改变 agent 执行动作的句子，禁止废话（叙述、背书、复述均属此类）。落地判定（任一答"是"即删）：
  - 是否只在叙述历史或来源（"曾验证/来自合并"），无任何可执行动作？
  - 是否为纯装饰修饰（如"·xx版"、标题里重复技能名），删除后任何执行动作不变？
  - 是否写给维护者而非执行者（合并来源引言、维护声明等）？
  - 是否在给前文已给出的命令/规则做机制解释（解释删掉，行为不变）？
  - 是否在叙述脚本会自动做的事（脚本已内建的校验/重试/回退/取数）？agent 只须配合的事（编排契约）除外。
- 已判明废话形态库存见 `references/废话形态清单.md`
- 正文多使用祈使句，指令简短、精准、无歧义
- 一看就懂的指令不用解释；反直觉、有捷径、错不起的指令，后面跟解释
- 正文结构中可选节空了就删，不硬留空壳；归属有疑时查「正文结构（各节职责与归属）」行内注释进行裁决，不按直觉。
- Prerequisites 按照“规则 + 机制(可选) + 排障指引(可选) ”方式写，示例：“统一用 python3 调用(规则)，不用裸 python，因为它在不同平台指向不明（机制），报ModuleNotFoundError表示没在技能根目录跑（排障指引）”
- 定义输出格式时，要写模板，放置在`templates/`中(除了本skill中的「frontmatter模板」、「正文结构（各节职责与归属）」)
  - 模板分两类：**复制型模板**（会被原样写进产物，如 frontmatter YAML）注释拆到说明表，模板保持纯净；**蓝图型模板**（不会被复制进产物，如正文结构示例）行内注释保留；占位符必须标注"须替换为实际内容"
- 技能绝不含恶意内容，也不配合创建误导性技能
- 多步骤都需要遵守的约束规则，放在Quick Reference
- 专属于某个步骤的约束规则，放在消费它的步骤内。
- 引用文件（references/、templates/、assets/ 下）一律按路径直接读取，"何时读哪个"写在 Procedure 对应步骤内。路径基准：仓库内技能相对 hermes-agent 仓库根，如 `skills/software-development/xxx/SKILL.md`；本地技能相对本技能目录，如 `references/schemas.md`、`scripts/run_eval.py`——与 `skill_view(file_path=...)` 的传参一致，直接可读。
- Skill 要用 hermes 的能力一律写成 Hermes 工具调用，禁止使用已被封装的 shell 命令。对照（参数以本机源码为准）：
  - 调用约定：CLI 包装类技能写 `terminal(command="<工具> ...", timeout=...)`，不写裸 shell 散文。

| shell 写法（禁止） | Hermes 工具写法（用这个） |
|---|---|
| `grep pattern path` | `search_files(pattern="<regex>", path="<目录>")`——加 `file_glob="*.py"` 限定文件、`output_mode="files_only"` 只要路径、`output_mode="count"` 只数行 |
| `cat file` | `read_file(path="<文件>", offset=<行>, limit=<行数>)`——分页读；大文件读到末页（见 truncated 标记即续读） |
| `sed -i 's/a/b/' file` / `awk` | `patch(path="<文件>", old_string="<原文>", new_string="<新文>")`——精确替换；`replace_all=true` 替换全部；自动语法检查 |
| `find / ls` | `search_files(pattern="*.py", target="files", path="<目录>")`——按文件名 glob 找，结果按修改时间排序 |
| `echo x > file` / heredoc | `write_file(path="<文件>", content="<内容>")`——整文件写入、自动建目录、语法检查 |

- Pitfalls 按"现象→根因→修法"格式写，不加铺垫句
- Verification 要列出整个 skill 功能的验收清单

## 完整 SKILL.md 的体积

- 舒适区：字符数 < 8000，token ≤ 7000，行数 ≤ 200
- 一般区：8000 < 字符数 < 13000，7000 < token < 11300，200 < 行数 < 360
- 警报区：字符数 > 13000，token > 11300，行数 > 360
- token 口径：汉字×0.9 + 非汉字×0.3；token 阈值按 95% 中文比率（0.87 token/字符）从字符线折算
- 超出极限时，考虑将部分内容挪到`references/`、`templates/`或 `scripts/` 目录中， 从SKILL.md 里指向它们，而不是内联

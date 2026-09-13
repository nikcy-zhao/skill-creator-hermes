# frontmatter 与目录规范

## 标准目录

可选目录没有文件则不创建

```
skill-name/          ← 技能目录（名 = SKILL.md 的 name）
	├── SKILL.md       ← 必有。技能本体：frontmatter + 七节正文
	├── scripts/       ← 可选。确定性脚本，只运行不读（含子代理提示词）
	├── references/    ← 可选。按需阅读的资料：schema、规范、查表、参考资料等
	├── templates/     ← 可选。待填空的输出骨架：报告模板、PR 格式等
	└── assets/        ← 可选。产物素材/脚手架：HTML 底板、样式、图片等
```

## frontmatter模板

字段作用见下方说明表，模板中的示例值为占位符，须替换为实际内容：

```yaml
---
name: my-skill-name
description: 一句话能力描述
version: 1.0.0
author: Your Name, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [keyword1, keyword2]
    related_skills: [other-skill]
    requires_tools: [web_search]
    requires_toolsets: [web]
    fallback_for_tools: [browser_navigate]
    fallback_for_toolsets: [browser]
    config:
      - key: myplugin.path
        description: 数据目录路径
        default: "~/data"
    blueprint:
      schedule: "0 9 * * *"
      prompt: "每次运行的任务指令"
---
```

**字段规则表**：

| 字段 | 必须性 | 作用 | 规则 |
|---|---|---|---|
| `name` | 必须 | skill 名 | 小写连字符 ≤64 字符 |
| `description` | 必须 | 能力描述 | 系统索引只截前 60 字符（源码 `SKILL_PROMPT_DESC_LIMIT`），触发词放最前 |
| `version` | 必须 | 版本号 | semver 格式 1.0.0 |
| `author` | 必须 | 作者 | 人类名在前，协助名在后 |
| `license` | 建议 | 开源协议，分发/复用时的法律声明 |  |
| `platforms` | 建议 | OS 门控，不兼容平台自动隐藏技能 | 平台放在`[]`中，多平台用`,`隔开。默认全平台；只写实测过的平台 |
| `metadata.hermes.tags` | 建议 | 进 skills_list 返回，辅助发现与检索 |  |
| `metadata.hermes.related_skills` | 建议 | 进 linked_files 视图；须指向真实存在的技能 |  |
| `metadata.hermes.requires_tools` | 可选 | 该工具不可用时技能隐藏（条件激活） |  |
| `metadata.hermes.requires_toolsets` | 可选 | 按工具集门控 |  |
| `metadata.hermes.fallback_for_tools` | 可选 | 反向门控——该工具存在时隐藏（兜底技能用） |  |
| `metadata.hermes.fallback_for_toolsets` | 可选 | 按工具集反向门控 |  |
| `metadata.hermes.config` | 可选 | 声明 config.yaml 配置项（skills.config.* 命名空间） |  |
| `metadata.hermes.blueprint` | 可选 | 声明本技能兼为 cron 自动化（schedule+prompt） |  |
| `required_environment_variables` | 按需 | 声明所需环境变量，加载时提示配置 |  |
| `required_credential_files` | 按需 | 凭证文件挂载声明 |  |

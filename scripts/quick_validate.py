#!/usr/bin/env python3
"""
Quick validation script for skills - minimal version

Usage:
    python3 -m scripts.quick_validate <skill-path>   ← 在技能仓库根目录跑
    python3 scripts/quick_validate.py <skill-path>   ← 直跑亦可（自动补 sys.path）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 允许从仓库根（python3 -m）与直接执行（python3 scripts/quick_validate.py）
# 两种方式运行——直跑时 scripts/ 的父目录不在 sys.path，先补上
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 与 utils.parse_skill_md 共用的 frontmatter 提取（单一事实来源，防两套实现漂移）
from scripts.utils import estimate_tokens, extract_frontmatter, parse_frontmatter


def validate_skill(skill_path):
    """Basic validation of a skill"""
    skill_path = Path(skill_path)

    # Check SKILL.md exists
    skill_md = skill_path / 'SKILL.md'
    if not skill_md.exists():
        return False, "SKILL.md not found"

    # Read and validate frontmatter（utf-8-sig 兼容带 BOM 的合法文件）
    try:
        content = skill_md.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeDecodeError) as e:
        return False, f"Cannot read SKILL.md: {e}"

    frontmatter_text = extract_frontmatter(content)
    if frontmatter_text is None:
        return False, "Invalid frontmatter format (need '---' opening and closing lines)"

    # Parse YAML frontmatter（迷你解析器，零第三方依赖，见 utils.parse_frontmatter）
    try:
        frontmatter = parse_frontmatter(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, "Frontmatter must be a YAML dictionary"
    except ValueError as e:
        return False, f"Invalid YAML in frontmatter: {e}"

    # Define allowed properties (Hermes standard frontmatter)
    ALLOWED_PROPERTIES = {
        'name', 'description', 'version', 'author', 'license', 'platforms',
        'metadata', 'required_environment_variables', 'required_credential_files',
    }

    # Check for unexpected properties (excluding nested keys under metadata)
    unexpected_keys = set(frontmatter.keys()) - ALLOWED_PROPERTIES
    if unexpected_keys:
        return False, (
            f"Unexpected key(s) in SKILL.md frontmatter: {', '.join(sorted(map(str, unexpected_keys)))}. "
            f"Allowed properties are: {', '.join(sorted(ALLOWED_PROPERTIES))}"
        )

    # Nested whitelist: only metadata.hermes is allowed, with fixed subkeys
    ALLOWED_HERMES_KEYS = {
        'tags', 'related_skills', 'requires_tools', 'requires_toolsets',
        'fallback_for_tools', 'fallback_for_toolsets', 'config', 'blueprint',
    }
    metadata = frontmatter.get('metadata')
    if metadata is not None:
        if not isinstance(metadata, dict):
            return False, "'metadata' must be a mapping (metadata.hermes.*)"
        unexpected_meta = set(metadata.keys()) - {'hermes'}
        if unexpected_meta:
            return False, (
                f"Unexpected key(s) under 'metadata': {', '.join(sorted(unexpected_meta))}. "
                "Only 'metadata.hermes' is allowed"
            )
        hermes = metadata.get('hermes')
        if hermes is not None:
            if not isinstance(hermes, dict):
                return False, "'metadata.hermes' must be a mapping"
            unexpected_hermes = set(hermes.keys()) - ALLOWED_HERMES_KEYS
            if unexpected_hermes:
                return False, (
                    f"Unexpected key(s) under 'metadata.hermes': {', '.join(sorted(unexpected_hermes))}. "
                    f"Allowed keys are: {', '.join(sorted(ALLOWED_HERMES_KEYS))}"
                )

    # Check required fields
    if 'name' not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if 'description' not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    # Extract name for validation
    name = frontmatter.get('name', '')
    if not isinstance(name, str):
        return False, f"Name must be a string, got {type(name).__name__}"
    name = name.strip()
    if not name:
        return False, "Missing 'name' in frontmatter (empty or blank)"
    # Check naming convention (kebab-case: lowercase with hyphens)
    if not re.match(r'^[a-z0-9-]+$', name):
        return False, f"Name '{name}' should be kebab-case (lowercase letters, digits, and hyphens only)"
    if name.startswith('-') or name.endswith('-') or '--' in name:
        return False, f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens"
    # Check name length (max 64 characters per spec)
    if len(name) > 64:
        return False, f"Name is too long ({len(name)} characters). Maximum is 64 characters."

    # Extract and validate description
    description = frontmatter.get('description', '')
    if not isinstance(description, str):
        return False, f"Description must be a string, got {type(description).__name__}"
    description = description.strip()
    if not description:
        return False, "Missing 'description' in frontmatter (empty or blank)"
    # Check for angle brackets
    if '<' in description or '>' in description:
        return False, "Description cannot contain angle brackets (< or >)"
    # Check description length (max 1024 characters per spec)
    if len(description) > 1024:
        return False, f"Description is too long ({len(description)} characters). Maximum is 1024."

    return True, "Skill is valid!"


def validate_strict(skill_path) -> tuple[bool, str]:
    """严格校验：在基础校验之上，覆盖 quick_validate 原有盲区。

    检查项（对应 writing-rules/frontmatter-spec 的机械可验条目）：
    - H1 标题 == frontmatter name
    - 各 Markdown 节标题（##）前须有空行
    - 七节正文结构存在（When to Use / Procedure / Pitfalls / Verification 必选）
    - 体积三区（舒适 <8000 字符 / 一般 8000-13000 / 警报 >13000，含行数与token估算）
    - frontmatter license 与 LICENSE.txt 实际协议一致（MIT/Apache 识别文本头）
    """
    ok, msg = validate_skill(skill_path)
    if not ok:
        return ok, msg
    skill_path = Path(skill_path)
    content = (skill_path / 'SKILL.md').read_text(encoding='utf-8-sig')
    lines = content.splitlines()
    problems: list[str] = []

    # 基础校验已通过 ⇒ frontmatter 必然合法且含非空 name，直接解析取用，
    # 不再走一遍 extract→parse 的完整防线（重复逻辑已收敛到 validate_skill）
    frontmatter = parse_frontmatter(extract_frontmatter(content) or "")
    name = str(frontmatter.get('name', '')).strip() if isinstance(frontmatter, dict) else ""

    # 1) H1 == name（首个 H1 行；跳过 ``` 围栏内的行——代码块注释 '# xxx' 不是标题）
    in_fence = False
    h1 = None
    for l in lines:
        if l.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and l.startswith("# "):
            h1 = l
            break
    if h1 is None:
        problems.append("未找到 H1 标题（# 开头）")
    elif h1[2:].strip() != name:
        problems.append(f"H1 标题 '{h1[2:].strip()}' 与 frontmatter name '{name}' 不一致")

    # 2) 节标题（##）前须有空行（首个 ## 除外——它前面是 H1 区；跳过围栏内）
    seen_h1 = False
    fence = False
    for i, l in enumerate(lines):
        if l.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        if l.startswith("# "):
            seen_h1 = True
        if seen_h1 and l.startswith("## ") and i > 0 and lines[i - 1].strip() != '':
            problems.append(f"第 {i+1} 行 '{l}' 前缺空行")

    # 3) 七节结构：必选节存在
    headings = {l[3:].strip() for l in lines if l.startswith('## ')}
    for req in ("When to Use", "Procedure", "Pitfalls", "Verification"):
        if req not in headings:
            problems.append(f"缺少必选节 '## {req}'")

    # 4) 体积三区
    chars = len(content)
    n_lines = len(lines)
    # token 估算走 utils 共享实现（int 口径），不再内联 round 公式——
    # 避免与 aggregate_benchmark 的 tokens_est 出现 ±1 的双口径漂移
    tok_est = estimate_tokens(content)
    if chars > 13000:
        problems.append(f"体积警报区：{chars} 字符 >13000（行 {n_lines}，token估算 {tok_est}）")
    elif chars > 8000:
        problems.append(f"体积一般区：{chars} 字符（8000-13000 之间，行 {n_lines}，token 估算 {tok_est}）")

    # 5) license 与 LICENSE.txt 一致性
    lic = str(frontmatter.get('license', '')).strip()
    lic_file = skill_path / 'LICENSE.txt'
    if lic and lic_file.exists():
        head = lic_file.read_text(encoding='utf-8', errors='ignore')[:400].lower()
        actual = None
        if 'apache license' in head and 'version 2' in head:
            actual = 'Apache-2.0'
        elif 'mit license' in head:
            actual = 'MIT'
        if actual and lic.lower() not in (actual.lower(), actual.lower().replace('-', ' ')):
            problems.append(f"frontmatter license '{lic}' 与 LICENSE.txt 实际协议 '{actual}' 不一致")

    if problems:
        return False, "严格校验未通过：\n  - " + "\n  - ".join(problems)
    return True, f"Skill is valid! (strict: {chars} chars / {n_lines} lines / ~{tok_est} tok, 5 checks passed)"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Quick validation for a skill directory")
    parser.add_argument("skill_directory", help="Path to the skill directory to validate")
    parser.add_argument("--strict", action="store_true",
                        help="Strict mode: also check H1==name, blank lines before '##' headings, "
                             "required sections, size zones, and license/LICENSE.txt consistency")
    args = parser.parse_args()

    valid, message = (validate_strict if args.strict else validate_skill)(args.skill_directory)
    print(message)
    sys.exit(0 if valid else 1)
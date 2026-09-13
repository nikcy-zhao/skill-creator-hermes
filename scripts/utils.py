"""Shared utilities for skill-creator scripts.

全部脚本零第三方依赖：frontmatter 解析用自带迷你解析器（仅覆盖
Hermes SKILL.md 实际用到的 YAML 子集），任何 python3 ≥3.9 可直接跑。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def hermes_bin() -> str:
    """Locate the hermes CLI: $HERMES_BIN > PATH > ~/.local/bin/hermes."""
    return (
        os.environ.get("HERMES_BIN")
        or shutil.which("hermes")
        or str(Path.home() / ".local" / "bin" / "hermes")
    )


# token估算口径的汉字判定范围（writing-rules.md「token口径」的唯一实现，
# 全库统一用它：含 CJK 扩展 A 区，quick_validate 与 aggregate_benchmark 共享）
_CJK_RANGES = (
    (0x3000, 0x303F),  # CJK 标点
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 统一表意文字
    (0xFF00, 0xFFEF),  # 全角形式
)


def is_cjk_char(ch: str) -> bool:
    """判据：字符是否属于token口径的汉字/全角范围。"""
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """token粗估：汉字×0.9＋非汉字×0.3（writing-rules.md「token口径」）。"""
    cjk = sum(1 for ch in text if is_cjk_char(ch))
    return int(cjk * 0.9 + (len(text) - cjk) * 0.3)


def _split_inline_list(inner: str) -> list[str]:
    """切分 inline list 顶层逗号：引号内的逗号不算分隔符。"""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in ('"', "'"):
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf or not inner.rstrip().endswith(","):
        # 尾逗号不产生空元素（`[a, b,]` 与 YAML 一致为 2 元素）
        parts.append("".join(buf))
    return parts


def _parse_scalar(raw: str):
    """解析单行标量值：去引号、inline list、bool/null 字面量。"""
    s = raw.strip()
    if not s:
        return ""
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_parse_scalar(p) for p in _split_inline_list(inner)] if inner else []
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    # 数字标量（yaml 语义：整数/小数按数值解析）。
    # isascii 先行：'²'.isdigit() 为 True 但 int() 会炸，非 ASCII 一律按字符串
    body = s[1:-1] if s and s[0] in "+-" else s
    if body and body.isascii() and (
            body.isdigit() or (body.count(".") == 1 and body.replace(".", "").isdigit())):
        return int(s) if "." not in s else float(s)
    return s


def _parse_block(lines: list[str], start: int, indent: int):
    """递归解析一个缩进层级的 mapping；返回 (dict, 下一行号)。

    行首缩进必须恰为本层 indent 个空格；更深缩进意味着进入子 mapping。
    `- ` 列表项（如 metadata.hermes.config）按「整块当不透明字符串」处理
    ——消费方只做存在性检查，不逐字段读。
    """
    result: dict = {}
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        cur = len(line) - len(line.lstrip(" "))
        if line.startswith("\t"):
            raise ValueError(f"tab 缩进不支持（仅支持空格缩进）: {line!r}")
        if cur < indent:
            break
        if cur > indent:
            raise ValueError(f"意外的缩进层级: {line!r}")
        body = line.strip()
        if body.startswith("- "):
            raise ValueError(f"列表项出现在 mapping 层级: {line!r}")
        if ":" not in body:
            raise ValueError(f"无法解析的 frontmatter 行: {line!r}")
        key, _, rest = body.partition(":")
        key = key.strip()
        rest = rest.strip()

        if rest == "|":  # block scalar：吃后续更深缩进行，块内相对缩进取首个非空行
            block: list[str] = []
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith(" " * (indent + 2))):
                block.append(lines[i])
                i += 1
            content = [b for b in block if b.strip()]
            # 去缩进基准＝首个非空行的实际缩进（YAML 语义），不再硬编码 indent+2
            base_len = len(content[0]) - len(content[0].lstrip(" ")) if content else indent + 2
            base = " " * base_len
            result[key] = "\n".join(
                b[len(base):] if b.startswith(base) else b.lstrip() for b in block
            ) + "\n"
            continue

        if rest == "":  # 看下一实质行：更深缩进→子 mapping 或列表；同层/更浅→null
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines):
                result[key] = None
                i += 1
                continue
            ind_j = len(lines[j]) - len(lines[j].lstrip(" "))
            if ind_j <= indent:
                result[key] = None
                i += 1
                continue
            if lines[j].strip().startswith("- "):
                # 映射值列表：`- k: v` 开项成 mapping，`- 标量` 按标量收。
                # 注意 `https://...` 这类裸 URL 不是 mapping 项——YAML 里 `k: v`
                # 需要"冒号+空格"（或冒号行尾）才成立，裸冒号切分会把 URL
                # 切成 {'https': ...}（与 yaml.safe_load 不等价）
                items: list = []
                li, list_indent = j, ind_j
                while li < len(lines):
                    if not lines[li].strip():
                        li += 1
                        continue
                    l_ind = len(lines[li]) - len(lines[li].lstrip(" "))
                    if l_ind != list_indent or not lines[li].strip().startswith("- "):
                        break
                    item_body = lines[li].strip()[2:].strip()
                    k2, sep2, v2 = item_body.partition(":")
                    if sep2 and (v2.startswith(" ") or v2 == ""):
                        item = {k2.strip(): _parse_scalar(v2)}
                        li += 1
                        # 更深缩进行是该 mapping 项的字段；但只支持一层平铺——
                        # 字段值本身又是 mapping（v3 为空但下面还有行）超出子集，
                        # 静默压扁会丢数据，显式报错（docstring 承诺超子集即抛）
                        while li < len(lines) and lines[li].strip() \
                                and (len(lines[li]) - len(lines[li].lstrip(" "))) > list_indent:
                            k3, sep3, v3 = lines[li].strip().partition(":")
                            if not sep3 or _parse_scalar(v3) == "" and v3.strip() == "":
                                raise ValueError(
                                    f"列表项下嵌套 mapping 超出解析器子集: {lines[li]!r}")
                            item[k3.strip()] = _parse_scalar(v3)
                            li += 1
                    else:
                        item = _parse_scalar(item_body)
                        li += 1
                    items.append(item)
                result[key] = items
                i = li
            else:
                result[key], i = _parse_block(lines, j, ind_j)
            continue

        result[key] = _parse_scalar(rest)
        i += 1
    return result, i


def parse_frontmatter(text: str) -> dict:
    """解析 SKILL.md frontmatter 的 YAML 子集（任意层嵌套 mapping +
    inline list + `|` block scalar + `- ` 列表项按不透明字符串收编）。

    覆盖 frontmatter-spec 模板的全部形态；超出子集的写法抛 ValueError。
    与 yaml.safe_load 对本子集语义等价（全库已装 SKILL.md 实测对齐）。
    """
    result, _ = _parse_block(text.splitlines(), 0, 0)
    return result


def extract_frontmatter(content: str) -> str | None:
    """Extract YAML frontmatter text from SKILL.md content.

    单一事实来源：quick_validate 与 parse_skill_md 共用。
    返回 frontmatter 文本（不含 --- 分隔行）；无合法 frontmatter 时返回 None。
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:i])
    return None


def parse_skill_md(skill_path: Path) -> tuple[str, str, str]:
    """Parse a SKILL.md file, returning (name, description, full_content).

    走 parse_frontmatter（与 quick_validate 同一语义）；缺 name/description
    时抛 ValueError，不再静默返回空串。
    """
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        raise ValueError(f"SKILL.md not found at {skill_path}")
    try:
        content = skill_md.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"Cannot read {skill_md}: {e}") from e

    frontmatter_text = extract_frontmatter(content)
    if frontmatter_text is None:
        raise ValueError("SKILL.md missing frontmatter (no opening/closing ---)")

    try:
        frontmatter = parse_frontmatter(frontmatter_text)
    except ValueError as e:
        raise ValueError(f"Invalid YAML in frontmatter: {e}") from e
    if not isinstance(frontmatter, dict):
        raise ValueError("Frontmatter must be a YAML mapping")

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Frontmatter missing a non-empty 'name'")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Frontmatter missing a non-empty 'description'")

    return name.strip(), description, content

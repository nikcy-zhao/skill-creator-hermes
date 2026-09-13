#!/usr/bin/env python3
"""Description 优化前的触发评测审查页生成（原手工临时脚本固化）。

Usage:
    python3 -m scripts.gen_review_page --queries <查询JSON> --skill-path <技能路径> [--open]

把 assets/eval_review.html 的三个占位符替换为实际内容，写出
/tmp/eval_review_<技能名>_<时间戳>.html（多会话并行不互覆）；--open 时同时用浏览器打开。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import tempfile
import webbrowser
from pathlib import Path

from scripts.utils import parse_skill_md

_REPO_ROOT = Path(__file__).resolve().parent.parent


def load_skill_info(skill_path: Path) -> tuple[str, str]:
    """读技能 name/description——走 parse_skill_md（与 quick_validate 同源），多行 description 不丢。"""
    try:
        name, description, _ = parse_skill_md(skill_path)
        return name, description.strip()
    except ValueError:
        return skill_path.name, ""


def build_page(queries: list, skill_path: Path) -> str:
    """渲染审查页 HTML：模板占位符 ← 实际内容（可独立调用，便于测试）。"""
    name, desc = load_skill_info(skill_path)

    t = (_REPO_ROOT / "assets" / "eval_review.html").read_text(encoding="utf-8")
    # 替换顺序有讲究：先换 name/description（来自 SKILL.md，经 html.escape），
    # EVAL_DATA 最后换——数据是唯一不可信源，若先换数据，其内容里即使只是
    # 出现占位符字样也会被后续 str.replace 命中（replace 作用于整页文本），
    # 导致页面数据被技能 name/description 污染
    data_json = json.dumps(queries, ensure_ascii=False).replace("</", "<\\/")
    return (t.replace("__SKILL_NAME_PLACEHOLDER__", html.escape(name))
             .replace("__SKILL_DESCRIPTION_PLACEHOLDER__", html.escape(desc))
             .replace("__EVAL_DATA_PLACEHOLDER__", data_json))


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate trigger-eval review page")
    parser.add_argument("--queries", type=Path, required=True, help="JSON 数组文件：[{query, should_trigger}]")
    parser.add_argument("--skill-path", type=Path, required=True, help="被测技能目录（读 name/description）")
    parser.add_argument("--open", action="store_true", help="Open the page in browser after writing")
    args = parser.parse_args()

    try:
        items = json.loads(args.queries.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        parser.error(f"无法读取查询文件 {args.queries}: {e}")
    if not isinstance(items, list):
        parser.error(f"查询文件须为 JSON 数组，得到 {type(items).__name__}")

    t = build_page(items, args.skill_path)

    # mkstemp：名字不可预测（多会话并行/共享 /tmp 不互覆、防 symlink 抢占），
    # 前缀保留技能名供人辨认；不再有技能名拼进固定路径的写入风险
    name = load_skill_info(args.skill_path)[0]
    fd, tmp_path = tempfile.mkstemp(prefix=f"eval_review_{name}_", suffix=".html", dir=tempfile.gettempdir())
    out = Path(tmp_path)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(t)
    print(f"Review page written: {out}")
    if args.open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()

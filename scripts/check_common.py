#!/usr/bin/env python3
"""跨评测通用判据检查库——判据脚本化机制的公共部分。

用法：eval 目录的 check.py 里 import 后直接组装判据结果：

    import sys
    sys.path.insert(0, "<skill-creator-hermes 源库根>")  # 或部署副本根
    from scripts.check_common import file_exists_nonempty, json_parsable, table_columns

    results = [
        ("输出文件存在且非空", file_exists_nonempty(outputs / "report.md")),
        ("结果是合法 JSON", json_parsable(outputs / "data.json")),
        ...
    ]

每场评测的专属判据写在各自 eval 目录的 check.py 里，按判据同生命周期维护；
本库只放"几乎每场评测都会用"的通用检查，一次编写永久复用。
判据脚本化的完整规则见 references/eval-protocol.md「判据脚本化」。
"""
from __future__ import annotations

import json
from pathlib import Path


def file_exists_nonempty(path: Path) -> bool:
    """判据：输出文件存在且非空（大小 > 0）。"""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def json_parsable(path: Path) -> bool:
    """判据：文件是合法 JSON（可被 json.loads 解析）。"""
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def text_contains(path: Path, needle: str) -> bool:
    """判据：文本文件包含指定字符串（存在性检查）。"""
    try:
        return needle in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def text_matches(path: Path, pattern: str, count: int = 1) -> bool:
    """判据：文本内容中正则 pattern 至少命中 count 次（格式检查）。

    用 re.search 语义逐次推进统计命中次数。
    """
    import re
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return len(re.findall(pattern, text)) >= count


def table_columns(path: Path, expected: int, delimiter: str = ",", skip_header: bool = True) -> bool:
    """判据：表格文件（默认 CSV）每行列数与 expected 一致。

    按 CSV 语义数列：引号包裹字段内的定界符不算分隔符（"a, b" 是一个字段）。
    """
    import csv
    import io
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    lines = [l for l in text.splitlines() if l.strip()]
    if skip_header and lines:
        lines = lines[1:]
    if not lines:
        return False
    for l in lines:
        row = next(csv.reader(io.StringIO(l), delimiter=delimiter), [])
        if len(row) != expected:
            return False
    return bool(lines)


def expectations_json(results: list[tuple[str, bool]], evidence: str = "scripted check") -> str:
    """把 (判据文本, 是否通过) 列表组装成完整的脚本判 grading.json JSON。

    check.py 的 main 里 print 这个函数的返回值即可；
    含 expectations 与 summary 两段（aggregate_benchmark 从 summary 读 pass_rate，
    缺 summary 会按 0 计入基准），下游 aggregate_benchmark / viewer 直接消费。
    """
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    return json.dumps({
        "expectations": [
            {"text": text, "passed": bool(passed_), "evidence": evidence}
            for text, passed_ in results
        ],
        "summary": {
            "passed": passed,
            "failed": total - passed,
            "total": total,
            "pass_rate": round(passed / total, 2) if total else 0.0,
        },
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # 自检：临时目录里跑一遍全部检查
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "ok.txt").write_text("包含关键词的文本\n", encoding="utf-8")
        (td / "ok.json").write_text('{"a": 1}', encoding="utf-8")
        (td / "bad.json").write_text("{not json", encoding="utf-8")
        (td / "t.csv").write_text("a,b,c\n1,2,3\n4,5,6\n", encoding="utf-8")
        assert file_exists_nonempty(td / "ok.txt")
        assert not file_exists_nonempty(td / "missing.txt")
        assert json_parsable(td / "ok.json")
        assert not json_parsable(td / "bad.json")
        assert text_contains(td / "ok.txt", "关键词")
        assert text_matches(td / "ok.txt", r"关键词")
        assert table_columns(td / "t.csv", 3)
        assert not table_columns(td / "t.csv", 2)
        print(expectations_json([("自检", True)]))
        print("check_common 自检全部通过")

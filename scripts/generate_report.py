#!/usr/bin/env python3
"""Generate an HTML report from run_loop.py output.

Takes the JSON output from run_loop.py and generates a visual HTML report
showing each description attempt with check/x for each test case.
Distinguishes between train and test queries.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path


def _render_result_cell(r: dict, test: bool) -> str:
    """One table cell: pass/fail icon plus triggers/runs rate."""
    did_pass = r.get("pass", False)
    icon = "✓" if did_pass else "✗"
    css_class = "pass" if did_pass else "fail"
    extra = " test-result" if test else ""
    return (f'                <td class="result{extra} {css_class}">{icon}'
            f'<span class="rate">{r.get("triggers", 0)}/{r.get("runs", 0)}</span></td>\n')


def generate_html(data: dict, auto_refresh: bool = False, skill_name: str = "") -> str:
    """Generate HTML report from loop output data. If auto_refresh is True, adds a meta refresh tag."""
    history = data.get("history") or []
    title_prefix = html.escape(skill_name + " \u2014 ") if skill_name else ""

    # Get all unique queries from train and test sets, with should_trigger info
    train_queries: list[dict] = []
    test_queries: list[dict] = []
    if history:
        first = history[0]
        train_queries = [
            {"query": r["query"], "should_trigger": r.get("should_trigger", True)}
            for r in first.get("train_results") or []
            if isinstance(r, dict) and r.get("query")
        ]
        test_queries = [
            {"query": r["query"], "should_trigger": r.get("should_trigger", True)}
            for r in first.get("test_results") or []
            if isinstance(r, dict) and r.get("query")
        ]

    refresh_tag = '    <meta http-equiv="refresh" content="5">\n' if auto_refresh else ""

    refresh_note = "本页随优化进度自动刷新。" if auto_refresh else ""

    html_parts = ["""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
""" + refresh_tag + """    <title>""" + title_prefix + """Skill Description Optimization</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@300;400;500;600&family=Source+Code+Pro:wght@400;500&display=swap" rel="stylesheet">
    <style>
        /* Stripe 设计系统变量，三页（viewer/eval_review/报告）共用同一套 */
        :root {
            --border: #e5edf5;
            --text: #061b31;
            --text-muted: #64748d;
            --label: #273951;
            --accent-hover: #4434d4;
            --purple-light: #b9b9f9;
            --green: #15be53;
            --green-text: #108c3d;
            --red: #ea2261;
            --red-text: #c81e4f;
            --amber: #9b6829;
            --bg-doc: #f6f9fc;
        }
        body {
            font-family: 'Source Sans 3', system-ui, sans-serif; font-feature-settings: "ss01";
            max-width: 100%;
            margin: 0 auto;
            padding: 20px;
            background: var(--bg-doc);
            color: var(--text);
        }
        h1 { font-family: 'Source Sans 3', sans-serif; color: var(--text); }
        .explainer {
            background: #ffffff;
            padding: 15px;
            border-radius: 6px;
            margin-bottom: 20px;
            border: 1px solid var(--border);
            color: var(--text-muted);
            font-size: 0.875rem;
            line-height: 1.6;
        }
        .summary {
            background: #ffffff;
            padding: 15px;
            border-radius: 6px;
            margin-bottom: 20px;
            border: 1px solid var(--border);
        }
        .summary p { margin: 5px 0; }
        .best { color: var(--green-text); font-weight: bold; }
        .table-container {
            overflow-x: auto;
            width: 100%;
        }
        table {
            border-collapse: collapse;
            background: #ffffff;
            border: 1px solid var(--border);
            border-radius: 6px;
            font-size: 12px;
            min-width: 100%;
        }
        th, td {
            padding: 8px;
            text-align: left;
            border: 1px solid var(--border);
            white-space: normal;
            word-wrap: break-word;
        }
        th {
            font-family: 'Source Sans 3', sans-serif;
            background: var(--bg-doc);
            color: var(--label);
            font-weight: 600;
        }
        th.test-col {
            background: #eae7fd;
            color: var(--accent-hover);
        }
        th.query-col { min-width: 200px; }
        td.description {
            font-family: monospace;
            font-size: 11px;
            word-wrap: break-word;
            max-width: 400px;
        }
        td.result {
            text-align: center;
            font-size: 16px;
            min-width: 40px;
        }
        td.test-result {
            background: #f8f7fe;
        }
        .pass { color: var(--green-text); }
        .fail { color: var(--red); }
        .rate {
            font-size: 9px;
            color: var(--text-muted);
            display: block;
        }
        tr:hover { background: var(--bg-doc); }
        .score {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 4px;
            font-weight: bold;
            font-size: 11px;
        }
        .score-good { background: rgba(21,190,83,0.15); color: var(--green-text); }
        .score-ok { background: #fffbeb; color: var(--amber); }
        .score-bad { background: rgba(234,34,97,0.1); color: var(--red-text); }
        .train-label { color: var(--text-muted); font-size: 10px; }
        .test-label { color: var(--accent-hover); font-size: 10px; font-weight: bold; }
        .best-row { background: rgba(21,190,83,0.06); }
        th.positive-col { border-bottom: 3px solid var(--green); }
        th.negative-col { border-bottom: 3px solid var(--red); }
        th.test-col.positive-col { border-bottom: 3px solid var(--green); }
        th.test-col.negative-col { border-bottom: 3px solid var(--red); }
        .legend { font-family: 'Source Sans 3', sans-serif; display: flex; gap: 20px; margin-bottom: 10px; font-size: 13px; align-items: center; }
        .legend-item { display: flex; align-items: center; gap: 6px; }
        .legend-swatch { width: 16px; height: 16px; border-radius: 3px; display: inline-block; }
        .swatch-positive { background: var(--bg-doc); border: 1px solid var(--border); border-bottom: 3px solid var(--green); }
        .swatch-negative { background: var(--bg-doc); border: 1px solid var(--border); border-bottom: 3px solid var(--red); }
        .swatch-test { background: #eae7fd; border: 1px solid var(--purple-light); }
        .swatch-train { background: var(--bg-doc); border: 1px solid var(--border); }
    </style>
</head>
<body>
    <h1>""" + title_prefix + """Skill Description Optimization</h1>
    <div class="explainer">
        <strong>正在优化技能的 description。</strong>""" + refresh_note + """每行是一轮迭代（一次新的 description 尝试）；列是测试查询——绿勾表示触发正确（含正确地没触发），红叉表示触发错误。"训练集"分数用于改进 description 的查询，"留出集"分数是优化器没见过的保留查询（防过拟合）。结束后，表现最好的 description 会应用到你的技能上。
    </div>
"""]

    # Summary section
    best_test_score = data.get('best_test_score')
    best_train_score = data.get('best_train_score')
    html_parts.append(f"""
    <div class="summary">
        <p><strong>Original:</strong> {html.escape(str(data.get('original_description', 'N/A')))}</p>
        <p class="best"><strong>Best:</strong> {html.escape(str(data.get('best_description', 'N/A')))}</p>
        <p><strong>最优分数：</strong>{html.escape(str(data.get('best_score', 'N/A')))} {'（留出集）' if best_test_score else '（训练集）'}</p>
        <p><strong>迭代轮数：</strong>{html.escape(str(data.get('iterations_run', 0)))} | <strong>训练集：</strong>{html.escape(str(data.get('train_size', '?')))} | <strong>留出集：</strong>{html.escape(str(data.get('test_size', '?')))}</p>
    </div>
""")

    # Legend
    html_parts.append("""
    <div class="legend">
        <span style="font-weight:600">查询列说明：</span>
        <span class="legend-item"><span class="legend-swatch swatch-positive"></span> 应触发</span>
        <span class="legend-item"><span class="legend-swatch swatch-negative"></span> 不应触发</span>
        <span class="legend-item"><span class="legend-swatch swatch-train"></span> 训练</span>
        <span class="legend-item"><span class="legend-swatch swatch-test"></span> 留出</span>
    </div>
""")

    # Table header
    html_parts.append("""
    <div class="table-container">
    <table>
        <thead>
            <tr>
                <th>Iter</th>
                <th>Train</th>
                <th>Test</th>
                <th class="query-col">Description</th>
""")

    # Add column headers for train queries
    for qinfo in train_queries:
        polarity = "positive-col" if qinfo["should_trigger"] else "negative-col"
        html_parts.append(f'                <th class="{polarity}">{html.escape(qinfo["query"])}</th>\n')

    # Add column headers for test queries (different color)
    for qinfo in test_queries:
        polarity = "positive-col" if qinfo["should_trigger"] else "negative-col"
        html_parts.append(f'                <th class="test-col {polarity}">{html.escape(qinfo["query"])}</th>\n')

    html_parts.append("""            </tr>
        </thead>
        <tbody>
""")

    # Find best iteration for highlighting（空 history 时跳过；优先用 run_loop 给出的
    # 最优迭代号，口径与 summary 一致，避免两套 max() 结果不同。
    # 统一转 int 再比较：JSON 里 iteration 若以字符串形态出现，'3' == 3 为 False 会让高亮静默失效）
    best_iter = None
    if history:
        if data.get("best_iteration") is not None:
            best_iter = data.get("best_iteration")
        elif test_queries:
            best_iter = max(history, key=lambda h: h.get("test_passed") or 0).get("iteration")
        else:
            best_iter = max(history, key=lambda h: h.get("train_passed") or 0).get("iteration")
        try:
            best_iter = int(best_iter)
        except (TypeError, ValueError):
            best_iter = None

    # Add rows for each iteration
    for h in history:
        raw_iteration = h.get("iteration", "?")
        try:
            iteration = int(raw_iteration)
        except (TypeError, ValueError):
            iteration = raw_iteration
        train_passed = h.get("train_passed", 0)
        train_total = h.get("train_total", 0)
        test_passed = h.get("test_passed")
        test_total = h.get("test_total")
        description = h.get("description", "")
        train_results = [r for r in (h.get("train_results") or []) if isinstance(r, dict)]
        test_results = [r for r in (h.get("test_results") or []) if isinstance(r, dict)]

        # Create lookups for results by query
        train_by_query = {r["query"]: r for r in train_results if r.get("query")}
        test_by_query = {r["query"]: r for r in test_results if r.get("query")} if test_results else {}

        # Compute aggregate correct/total runs across all retries
        def aggregate_runs(results: list[dict]) -> tuple[int, int]:
            correct = 0
            total = 0
            for r in results:
                runs = r.get("runs", 0)
                triggers = r.get("triggers", 0)
                total += runs
                if r.get("should_trigger", True):
                    correct += triggers
                else:
                    correct += runs - triggers
            return correct, total

        train_correct, train_runs = aggregate_runs(train_results)
        test_correct, test_runs = aggregate_runs(test_results)

        # Determine score classes
        def score_class(correct: int, total: int) -> str:
            if total > 0:
                ratio = correct / total
                if ratio >= 0.8:
                    return "score-good"
                elif ratio >= 0.5:
                    return "score-ok"
            return "score-bad"

        train_class = score_class(train_correct, train_runs)
        test_class = score_class(test_correct, test_runs)

        row_class = "best-row" if iteration == best_iter else ""

        html_parts.append(f"""            <tr class="{row_class}">
                <td>{html.escape(str(iteration))}</td>
                <td><span class="score {train_class}">{train_correct}/{train_runs}</span></td>
                <td><span class="score {test_class}">{test_correct}/{test_runs}</span></td>
                <td class="description">{html.escape(description)}</td>
""")

        # Add result for each train query
        for qinfo in train_queries:
            html_parts.append(_render_result_cell(train_by_query.get(qinfo["query"], {}), test=False))

        # Add result for each test query (with different background)
        for qinfo in test_queries:
            html_parts.append(_render_result_cell(test_by_query.get(qinfo["query"], {}), test=True))

        html_parts.append("            </tr>\n")

    html_parts.append("""        </tbody>
    </table>
    </div>
""")

    html_parts.append("""
</body>
</html>
""")

    return "".join(html_parts)


def main():
    parser = argparse.ArgumentParser(description="Generate HTML report from run_loop output")
    parser.add_argument("input", help="Path to JSON output from run_loop.py (or - for stdin)")
    parser.add_argument("-o", "--output", default=None, help="Output HTML file (default: stdout)")
    parser.add_argument("--skill-name", default="", help="Skill name to include in the report title")
    args = parser.parse_args()

    try:
        if args.input == "-":
            data = json.load(sys.stdin)
        else:
            data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Failed to read/parse input {args.input}: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print(f"Failed to render: input JSON must be an object, got {type(data).__name__}", file=sys.stderr)
        sys.exit(1)

    html_output = generate_html(data, skill_name=args.skill_name)

    if args.output:
        Path(args.output).write_text(html_output, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(html_output)


if __name__ == "__main__":
    main()

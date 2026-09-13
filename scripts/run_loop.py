#!/usr/bin/env python3
"""Run the eval + improve loop until all pass or max iterations reached.

Combines run_eval.py and improve_description.py in a loop, tracking history
and returning the best description found. Supports train/test split to prevent
overfitting.
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

from scripts.generate_report import generate_html
from scripts.improve_description import improve_description
from scripts.run_eval import find_project_root, run_eval, validate_eval_set
from scripts.utils import parse_skill_md


def split_eval_set(eval_set: list[dict], holdout: float, seed: int = 42) -> tuple[list[dict], list[dict]]:
    """Split eval set into train and test sets, stratified by should_trigger."""
    rng = random.Random(seed)  # 实例化，不污染全局 random 状态

    # Separate by should_trigger
    trigger = [e for e in eval_set if e["should_trigger"]]
    no_trigger = [e for e in eval_set if not e["should_trigger"]]

    # Shuffle each group
    rng.shuffle(trigger)
    rng.shuffle(no_trigger)

    # Calculate split points. The max(1, ...) floor must never empty the
    # train side: clamp so at least one trigger / no-trigger item stays in
    # train (an empty train set would make failed==0 and the loop would
    # exit with a false "all_passed" immediately).
    n_trigger_test = min(max(1, int(len(trigger) * holdout)), max(0, len(trigger) - 1)) if trigger else 0
    n_no_trigger_test = min(max(1, int(len(no_trigger) * holdout)), max(0, len(no_trigger) - 1)) if no_trigger else 0

    # Split
    test_set = trigger[:n_trigger_test] + no_trigger[:n_no_trigger_test]
    train_set = trigger[n_trigger_test:] + no_trigger[n_no_trigger_test:]

    return train_set, test_set


def _summarize_results(result_list: list[dict]) -> dict:
    """Wrap a list of per-query results with a pass/fail/total summary."""
    passed = sum(1 for r in result_list if r["pass"])
    total = len(result_list)
    return {"results": result_list, "summary": {"passed": passed, "failed": total - passed, "total": total}}


def _print_eval_stats(label, results, elapsed):
    pos = [r for r in results if r["should_trigger"]]
    neg = [r for r in results if not r["should_trigger"]]
    tp = sum(r["triggers"] for r in pos)
    pos_runs = sum(r["runs"] for r in pos)
    fn = pos_runs - tp
    fp = sum(r["triggers"] for r in neg)
    neg_runs = sum(r["runs"] for r in neg)
    tn = neg_runs - fp
    total = tp + tn + fp + fn
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    accuracy = (tp + tn) / total if total > 0 else 0.0
    print(f"{label}: {tp+tn}/{total} correct, precision={precision:.0%} recall={recall:.0%} accuracy={accuracy:.0%} ({elapsed:.1f}s)", file=sys.stderr)
    for r in results:
        status = "过" if r["pass"] else "不过"
        rate_str = f"{r['triggers']}/{r['runs']}"
        print(f"  [{status}] rate={rate_str} expected={r['should_trigger']}: {r['query'][:60]}", file=sys.stderr)


def run_loop(
    eval_set: list[dict],
    skill_path: Path,
    description_override: str | None,
    num_workers: int,
    timeout: int,
    max_iterations: int,
    runs_per_query: int,
    trigger_threshold: float,
    holdout: float,
    model: str,
    verbose: bool,
    live_report_path: Path | None = None,
    log_dir: Path | None = None,
    history_ref: list | None = None,
) -> dict:
    """Run the eval + improvement loop."""
    project_root = find_project_root()
    name, original_description, content = parse_skill_md(skill_path)
    current_description = description_override or original_description

    if max_iterations < 1:
        raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")

    # Split into train/test if holdout > 0
    if holdout > 0:
        train_set, test_set = split_eval_set(eval_set, holdout)
        if verbose:
            print(f"Split: {len(train_set)} train, {len(test_set)} test (holdout={holdout})", file=sys.stderr)
    else:
        train_set = eval_set
        test_set = []

    history = []
    exit_reason = "unknown"

    try:
        for iteration in range(1, max_iterations + 1):
            if verbose:
                print(f"\n{'='*60}", file=sys.stderr)
                print(f"Iteration {iteration}/{max_iterations}", file=sys.stderr)
                print(f"Description: {current_description}", file=sys.stderr)
                print(f"{'='*60}", file=sys.stderr)

            # Evaluate train + test together in one batch for parallelism
            all_queries = train_set + test_set
            t0 = time.time()
            all_results = run_eval(
                eval_set=all_queries,
                skill_name=name,
                description=current_description,
                num_workers=num_workers,
                timeout=timeout,
                project_root=project_root,
                runs_per_query=runs_per_query,
                trigger_threshold=trigger_threshold,
                model=model,
            )
            eval_elapsed = time.time() - t0

            # Split results back into train/test by matching queries
            train_queries_set = {q["query"] for q in train_set}
            train_result_list = [r for r in all_results["results"] if r["query"] in train_queries_set]
            test_result_list = [r for r in all_results["results"] if r["query"] not in train_queries_set]

            train_results = _summarize_results(train_result_list)
            test_results = _summarize_results(test_result_list) if test_set else None

            train_summary = train_results["summary"]
            test_summary = test_results["summary"] if test_results else None
            history.append({
                "iteration": iteration,
                "description": current_description,
                "train_passed": train_summary["passed"],
                "train_failed": train_summary["failed"],
                "train_total": train_summary["total"],
                "train_results": train_results["results"],
                "test_passed": test_summary["passed"] if test_summary else None,
                "test_failed": test_summary["failed"] if test_summary else None,
                "test_total": test_summary["total"] if test_summary else None,
                "test_results": test_results["results"] if test_results else None,
            })
            # 外部引用同步（main 的 except 兜底从此处取回已完成迭代，不丢成果）
            if history_ref is not None:
                history_ref[:] = [dict(h) for h in history]

            # Write live report if path provided
            if live_report_path:
                partial_output = {
                    "original_description": original_description,
                    "best_description": current_description,
                    "best_score": "in progress",
                    "iterations_run": len(history),
                    "holdout": holdout,
                    "train_size": len(train_set),
                    "test_size": len(test_set),
                    "history": history,
                }
                live_report_path.write_text(
                    generate_html(partial_output, auto_refresh=True, skill_name=name),
                    encoding="utf-8",
                )

            if verbose:
                _print_eval_stats("Train", train_results["results"], eval_elapsed)
                if test_results:
                    _print_eval_stats("Test ", test_results["results"], 0)

            if train_summary["total"] > 0 and train_summary["failed"] == 0:
                exit_reason = f"all_passed (iteration {iteration})"
                if verbose:
                    print(f"\nAll train queries passed on iteration {iteration}!", file=sys.stderr)
                break

            if iteration == max_iterations:
                exit_reason = f"max_iterations ({max_iterations})"
                if verbose:
                    print(f"\nMax iterations reached ({max_iterations}).", file=sys.stderr)
                break

            # Improve the description based on train results
            if verbose:
                print(f"\nImproving description...", file=sys.stderr)

            t0 = time.time()
            # Strip test scores from history so improvement model can't see them
            blinded_history = [
                {k: v for k, v in h.items() if not k.startswith("test_")}
                for h in history
            ]
            # improve 步骤加保护：单次网络抖动/超时不能丢掉整个循环的成果——
            # 重试 1 次，仍失败则保留当前描述按 max_iterations 正常收尾
            new_description = None
            for attempt in (1, 2):
                try:
                    new_description = improve_description(
                        skill_name=name,
                        skill_content=content,
                        current_description=current_description,
                        eval_results=train_results,
                        history=blinded_history,
                        model=model,
                        log_dir=log_dir,
                        iteration=iteration,
                    )
                    break
                except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
                    print(f"Warning: improve attempt {attempt} failed: {e}", file=sys.stderr)
                    if attempt == 2:
                        print("Warning: giving up on improvement; keeping current description and exiting loop", file=sys.stderr)
                        exit_reason = f"improve_failed (iteration {iteration})"
            improve_elapsed = time.time() - t0

            if new_description is None:
                break

            if verbose:
                print(f"Proposed ({improve_elapsed:.1f}s): {new_description}", file=sys.stderr)

            current_description = new_description
    except KeyboardInterrupt:
        # Ctrl+C 属用户主动中止：已完成的迭代成果照常收尾（选最优、落盘、报告），
        # 不丢 history——否则"保存现有进度"就是空话
        exit_reason = f"aborted (keyboard-interrupt at iteration {len(history) + 1})"

    # Find the best iteration by TEST score (or train if no test set);
    # 并列时用 train 分 tie-break（元组 key），且把 best_iteration 带出去供报告器同一口径
    if not history:
        return {
            "exit_reason": exit_reason,
            "original_description": original_description,
            "best_description": original_description,
            "best_score": None,
            "best_iteration": None,
            "best_train_score": None,
            "best_test_score": None,
            "final_description": current_description,
            "iterations_run": 0,
            "holdout": holdout,
            "train_size": len(train_set),
            "test_size": len(test_set),
            "history": [],
        }
    if test_set:
        best = max(history, key=lambda h: (h["test_passed"] or 0, h["train_passed"]))
        best_score = f"{best['test_passed']}/{best['test_total']}"
    else:
        best = max(history, key=lambda h: h["train_passed"])
        best_score = f"{best['train_passed']}/{best['train_total']}"

    if verbose:
        print(f"\nExit reason: {exit_reason}", file=sys.stderr)
        print(f"Best score: {best_score} (iteration {best['iteration']})", file=sys.stderr)

    return {
        "exit_reason": exit_reason,
        "original_description": original_description,
        "best_description": best["description"],
        "best_score": best_score,
        "best_iteration": best["iteration"],
        "best_train_score": f"{best['train_passed']}/{best['train_total']}",
        "best_test_score": f"{best['test_passed']}/{best['test_total']}" if test_set else None,
        "final_description": current_description,
        "iterations_run": len(history),
        "holdout": holdout,
        "train_size": len(train_set),
        "test_size": len(test_set),
        "history": history,
    }


def main():
    parser = argparse.ArgumentParser(description="Run eval + improve loop")
    parser.add_argument("--eval-set", required=True, help="Path to eval set JSON file")
    parser.add_argument("--skill-path", required=True, help="Path to skill directory")
    parser.add_argument("--description", default=None, help="Override starting description")
    parser.add_argument("--num-workers", type=int, default=1, help="Number of parallel workers（默认 1：与 run_eval.py 同口径——真技能计数是共享信号，并发会互记触发；仅确认被测技能未安装时才可调高）")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout per query in seconds (与 run_eval.py 默认一致：hermes -z 含代理启动，留宽防误判未触发)")
    parser.add_argument("--max-iterations", type=int, default=5, help="Max improvement iterations")
    parser.add_argument("--runs-per-query", type=int, default=3, help="Number of runs per query")
    parser.add_argument("--trigger-threshold", type=float, default=0.5, help="Trigger rate threshold")
    parser.add_argument("--holdout", type=float, default=0.4, help="Fraction of eval set to hold out for testing (0 to disable)")
    parser.add_argument("--model", required=True, help="Model for improvement")
    parser.add_argument("--verbose", action="store_true", help="Print progress to stderr")
    parser.add_argument("--report", default="auto", help="Generate HTML report at this path (default: 'auto' for temp file, 'none' to disable)")
    parser.add_argument("--no-open", action="store_true", help="Do not auto-open the report in a browser (后台/无人值守运行用)")
    parser.add_argument("--results-dir", default=None, help="Save all outputs (results.json, report.html, log.txt) to a timestamped subdirectory here")
    args = parser.parse_args()

    eval_set = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))
    # 与 run_eval.py 同一道结构防线：重复 query 会让 train/test 切分防线被击穿
    ok, msg = validate_eval_set(eval_set)
    if not ok:
        print(f"Error: invalid eval set {args.eval_set}: {msg}", file=sys.stderr)
        sys.exit(1)
    skill_path = Path(args.skill_path)

    if not (skill_path / "SKILL.md").exists():
        print(f"Error: No SKILL.md found at {skill_path}", file=sys.stderr)
        sys.exit(1)

    name, _, _ = parse_skill_md(skill_path)

    # Set up live report path
    if args.report != "none":
        if args.report == "auto":
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            live_report_path = Path(tempfile.gettempdir()) / f"skill_description_report_{skill_path.name}_{timestamp}.html"
        else:
            live_report_path = Path(args.report)
        # Open the report immediately so the user can watch
        live_report_path.write_text("<html><body><h1>Starting optimization loop...</h1><meta http-equiv='refresh' content='5'></body></html>", encoding="utf-8")
        if not args.no_open:
            webbrowser.open(str(live_report_path))
    else:
        live_report_path = None

    # Determine output directory (create before run_loop so logs can be written)
    if args.results_dir:
        timestamp = time.strftime("%Y-%m-%d_%H%M%S")
        results_dir = Path(args.results_dir) / timestamp
        results_dir.mkdir(parents=True, exist_ok=True)
    else:
        results_dir = None

    log_dir = results_dir / "logs" if results_dir else None

    output = None
    history_backup: list = []  # run_loop 侧 history_ref 同步的已完成迭代
    try:
        output = run_loop(
            eval_set=eval_set,
            skill_path=skill_path,
            description_override=args.description,
            num_workers=args.num_workers,
            timeout=args.timeout,
            max_iterations=args.max_iterations,
            runs_per_query=args.runs_per_query,
            trigger_threshold=args.trigger_threshold,
            holdout=args.holdout,
            model=args.model,
            verbose=args.verbose,
            live_report_path=live_report_path,
            log_dir=log_dir,
            history_ref=history_backup,
        )
    except KeyboardInterrupt:
        print("\nInterrupted by user; saving what we have.", file=sys.stderr)
        output = {"exit_reason": "aborted (keyboard-interrupt)", "history": list(history_backup)}
    except Exception as e:
        # 异常也要保住成果：记录原因后带壳继续，走完落盘与报告——
        # history 取 history_backup（已完成迭代），不再清空
        import traceback
        traceback.print_exc()
        output = {
            "exit_reason": f"aborted (exception: {type(e).__name__})",
            "error": str(e),
            "history": list(history_backup),
        }

    # Save JSON output
    json_output = json.dumps(output, indent=2)
    print(json_output)
    if results_dir:
        (results_dir / "results.json").write_text(json_output, encoding="utf-8")

    # Write final HTML report (without auto-refresh)
    if live_report_path:
        live_report_path.write_text(generate_html(output, auto_refresh=False, skill_name=name), encoding="utf-8")
        print(f"\nReport: {live_report_path}", file=sys.stderr)

    if results_dir and live_report_path:
        (results_dir / "report.html").write_text(generate_html(output, auto_refresh=False, skill_name=name), encoding="utf-8")

    if results_dir:
        print(f"Results saved to: {results_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()

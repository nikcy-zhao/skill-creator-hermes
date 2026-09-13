#!/usr/bin/env python3
"""批量执行各 eval 目录的 check.py，产出/更新 grading.json。

Usage:
    python3 -m scripts.grade_runs <workspace>/iteration-N [--dry-run]

扫描 iteration 下所有 eval-*/{with_skill,without_skill}/run-*：
- run 有 outputs 且父 eval 目录有 check.py → 在技能根运行 `check.py <outputs>`，
  stdout 合法 JSON 则写为该 run 的 grading.json（脚本判段）；
- 已有 grading.json 且无 check.py → 不动（模型判产物）；
- --dry-run 只列将执行的动作不落盘。
汇总报告：脚本判 x run、模型判待跑 y run（z 个有 check.py 但执行失败）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def run_check(check_py: Path, outputs: Path, timeout: int = 60) -> tuple[str, dict | None, str]:
    """跑一个 run 的 check.py；返回 (状态, 解析后 JSON 或 None, 诊断信息)。

    状态: "ok"（stdout 合法且含 expectations）/ "bad"（输出不合法）/ "timeout"（挂起）。
    挂起不再让 TimeoutExpired 炸掉整个批量评分——记入失败清单继续。
    """
    try:
        r = subprocess.run(
            [sys.executable, str(check_py), str(outputs)],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", None, f"check.py 超时（>{timeout}s）被终止"
    try:
        data = json.loads(r.stdout)
        if "expectations" not in data:
            raise ValueError("missing 'expectations'")
    except (json.JSONDecodeError, ValueError) as e:
        return "bad", None, f"输出不合法（{e}；stderr: {r.stderr[:200]}）"
    return "ok", data, ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-run check.py for all runs in an iteration")
    parser.add_argument("iteration_dir", type=Path, help="Path to iteration-N directory")
    parser.add_argument("--dry-run", action="store_true", help="List actions without writing")
    args = parser.parse_args()

    it = args.iteration_dir.resolve()
    if not it.is_dir():
        print(f"ERROR: {it} is not a directory", file=sys.stderr)
        sys.exit(1)

    scripted, model_only, model_pending, failed = [], [], [], []
    for check_py in sorted(it.glob("eval-*/check.py")):
        eval_dir = check_py.parent
        for cfg_dir in sorted(p for p in eval_dir.iterdir() if p.is_dir()):
            for run_dir in sorted(cfg_dir.glob("run-*")):
                outputs = run_dir / "outputs"
                grading = run_dir / "grading.json"
                if not outputs.is_dir():
                    # 无 outputs 的 run 记入模型判待跑（见下方第二遍循环），不在此报错——
                    # 用例没跑完是常态，静默跳过并在汇总里留缺口提示
                    continue
                if args.dry_run:
                    scripted.append(f"{run_dir} (dry)")
                    continue
                r_status, data, diag = run_check(check_py, outputs)
                if r_status != "ok":
                    failed.append(f"{run_dir}: {diag}")
                    continue
                grading.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                scripted.append(str(run_dir))
        # 无 outputs 的 run 落入下方第二遍循环的 model_pending 缺口提示
    # 无 check.py 的 run 分两报：已有 grading.json＝模型判已完成；没有＝模型判待跑（真缺口）
    for eval_dir in sorted(it.glob("eval-*")):
        for cfg_dir in sorted(p for p in eval_dir.iterdir() if p.is_dir()):
            for run_dir in sorted(cfg_dir.glob("run-*")):
                g = run_dir / "grading.json"
                if (eval_dir / "check.py").exists():
                    continue
                if g.exists():
                    model_only.append(str(run_dir))
                else:
                    model_pending.append(str(run_dir))

    print(f"脚本判完成: {len(scripted)} run")
    for s in scripted:
        print(f"  ✓ {s}")
    if model_only:
        print(f"模型判已完成（无 check.py、已有 grading）: {len(model_only)} run")
        for s in model_only:
            print(f"  ? {s}")
    if model_pending:
        print(f"模型判待跑（无 check.py、无 grading——真正的缺口）: {len(model_pending)} run", file=sys.stderr)
        for s in model_pending:
            print(f"  ! {s}", file=sys.stderr)
    if failed:
        print(f"执行失败: {len(failed)}", file=sys.stderr)
        for f in failed:
            print(f"  ✗ {f}", file=sys.stderr)
        sys.exit(5)


if __name__ == "__main__":
    main()

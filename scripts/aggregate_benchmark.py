#!/usr/bin/env python3
"""
Aggregate individual run results into benchmark summary statistics.

Reads grading.json files from run directories and produces:
- run_summary with mean, stddev, min, max for each metric
- delta between with_skill and without_skill configurations

Usage:
    python3 -m scripts.aggregate_benchmark <benchmark_dir>

Example:
    python3 -m scripts.aggregate_benchmark <workspace>/iteration-1/

The script supports two directory layouts:

    Workspace layout (from skill-creator iterations):
    <benchmark_dir>/
    └── eval-N/
        ├── with_skill/
        │   ├── run-1/grading.json
        │   └── run-2/grading.json
        └── without_skill/
            ├── run-1/grading.json
            └── run-2/grading.json

    Legacy layout (with runs/ subdirectory):
    <benchmark_dir>/
    └── runs/
        └── eval-N/
            ├── with_skill/
            │   └── run-1/grading.json
            └── without_skill/
                └── run-1/grading.json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.utils import estimate_tokens


def compute_skill_size(skill_path: str) -> dict | None:
    """测 SKILL.md 字符数/行数＋token估算；路径缺失返回 None。"""
    if not skill_path:
        return None
    candidate = Path(skill_path)
    if candidate.is_file() and candidate.name == "SKILL.md":
        skill_md = candidate
    else:
        skill_md = candidate / "SKILL.md"
    if not skill_md.exists():
        return None
    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    return {
        "chars": len(content),
        "lines": len(content.splitlines()),
        "tokens_est": estimate_tokens(content),
    }


def calculate_stats(values: list[float]) -> dict:
    """Calculate mean, stddev, min, max for a list of values."""
    if not values:
        return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}

    n = len(values)
    mean = sum(values) / n

    if n > 1:
        variance = sum((x - mean) ** 2 for x in values) / (n - 1)
        stddev = math.sqrt(variance)
    else:
        stddev = 0.0

    return {
        "mean": round(mean, 4),
        "stddev": round(stddev, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4)
    }


def _read_grading(grading_file: Path) -> dict | None:
    """Load one grading.json, warning and skipping on invalid JSON."""
    try:
        with open(grading_file, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print(f"Warning: Cannot read {grading_file}: {e}")
        return None
    # 顶层非对象（如 []、"ok"）时与缺字段同等对待：warning+skip，不让
    # AttributeError 炸掉整个聚合
    if not isinstance(data, dict):
        print(f"Warning: {grading_file}: top-level JSON is {type(data).__name__}, expected object — skipped")
        return None
    return data


def _dict_or_empty(data: dict, key: str) -> dict:
    """Safely fetch a nested dict: returns {} when the key is missing OR the value is null (grading.json 的 null 字段会让 .get(key, {}) 返回 None 而后继 .get 崩溃)."""
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _num(data: dict, key: str, default: float | int = 0) -> float | int:
    """Safely fetch a scalar: null 或非数值时给默认值（.get(key, default) 防不住值为 null）."""
    value = data.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    return default


def _natural_sort_key(p: Path):
    """目录名内数字按数值排（eval-2 < eval-10），无数字排最后."""
    m = re.search(r"\d+", p.name)
    return (0, int(m.group()), p.name) if m else (1, 0, p.name)


def _tokens_from_delegations(run_dir: Path, workspace: Path, db_path: Path | None = None):
    """从 state.db 的委派记录按 run 路径精确取回子代理真实token数。

    前提（评测编排契约）：派发提示词里必须写明该 run 的输出路径
    （如 .../eval-x/with_skill/run-1/outputs/report.md）——task_json 的
    goal 与 result_json 的 results[].goal 都保留该原文，按它匹配即精确
    归因到单个子代理，不会混入评分批/主会话/无关任务。
    匹配不到返回 None（回退链继续走下一级）。
    """
    db = db_path or (Path.home() / ".hermes" / "state.db")
    if not db.exists():
        return None
    # run 相对路径锚点：iteration-N/eval-x/with_skill/run-1（含 outputs/ 防误配
    # grading 路径）。锚点集由三部分组成，缺一会有静默漏配/串账：
    #   ① 绝对路径形态（resolved + 未 resolve 两种，后者覆盖 /tmp vs /private/tmp）
    #   ② 相对形态带 workspace 父目录名（<技能名>-workspace/…）：仅 iteration-N
    #      不唯一，任何技能的同构评测目录都含该子串，会把别人子代理的token账记进来
    try:
        rel = run_dir.relative_to(workspace).as_posix()
    except ValueError:
        return None
    anchors = []
    for w in (workspace.resolve(), workspace.absolute()):
        prefix = w.as_posix()
        a = f"{prefix}/{rel}/outputs/"
        if a not in anchors:
            anchors.append(a)
    parent_name = workspace.parent.name
    if parent_name and parent_name not in ("/", ""):
        # 相对形态必须含 workspace.name（iteration-N）层：rel 不含它，
        # 漏了这层则 '<技能名>-workspace/iteration-N/eval-x/...' 永不命中
        rel_anchor = f"{parent_name}/{workspace.name}/{rel}/outputs/"
        if rel_anchor not in anchors:
            anchors.append(rel_anchor)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)  # 只读，不锁 Hermes 活库
    except sqlite3.Error:
        return None
    try:
        # 注意：task_json/result_json 的非 ASCII 以 \uXXXX 转义存储，SQL LIKE
        # 匹配不上中文路径——改为拉取近期委派在 Python 侧解码匹配。
        # result_json 的 results[] 无 goal 字段，只有 task_index——goal 原文
        # 在 task_json.goals[] 按同序对应，需两表同拉、按下标回填。
        rows = con.execute(
            "SELECT task_json, result_json FROM async_delegations "
            "WHERE state='completed' AND result_json IS NOT NULL "
            "ORDER BY dispatched_at DESC LIMIT 500"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    # 在委派结果里找 goal 精确含本 run 输出路径的条目。
    # 防污染三重门：
    #   ① 锚点含 workspace 独有片段（绝对路径或 <技能名>-workspace 父目录名），
    #      跨 workspace/跨 iteration 的同构 run 路径不会串账
    #   ② 必须是"写入方向"命中：goal 里 outputs 路径紧跟写入动词（写入/写到/
    #      保存到/save to/write to），评分批只"读/对比"不写入，进不来
    #   ③ 路径必须以 outputs/ 结尾的文件路径出现在 goal 原文里
    _WRITE_PATTERNS = ("写入", "写到", "保存到", "输出到", "存到", "save to", "write to", "output to")
    _READ_PATTERNS = ("评分", "对比", "读取", "检查", "审阅", "读 ", "grade", "compare", "review")

    def _is_writer_goal(goal: str) -> bool:
        # 任一锚点命中（取最靠前的位置），且写入动词在锚点所在从句内、
        # 无读取类动词，才判执行器
        start = 0
        while True:
            i = -1
            for a in anchors:
                j = goal.find(a, start)
                if j >= 0 and (i < 0 or j < i):
                    i = j
            if i < 0:
                return False
            clause_start = max(goal.rfind(p, 0, i) for p in "。；;，,\n") + 1
            clause = goal[clause_start:i]
            has_write = any(w in clause for w in _WRITE_PATTERNS)
            has_read = any(w in clause for w in _READ_PATTERNS)
            if has_write and not has_read:
                return True
            start = i + 1

    for task_json, result_json in rows:
        try:
            result = json.loads(result_json)
            task = json.loads(task_json) if task_json else {}
        except (json.JSONDecodeError, TypeError):
            continue
        goals = task.get("goals") or ([task["goal"]] if task.get("goal") else [])
        for item in result.get("results") or []:
            idx = item.get("task_index")
            goal = goals[idx] if isinstance(idx, int) and 0 <= idx < len(goals) else ""
            if any(a in goal for a in anchors) and _is_writer_goal(goal):
                tokens = item.get("tokens") or {}
                return {
                    "total_tokens": int(tokens.get("input") or 0) + int(tokens.get("output") or 0),
                    "input": int(tokens.get("input") or 0),
                    "output": int(tokens.get("output") or 0),
                    "api_calls": item.get("api_calls"),
                    "duration_seconds": item.get("duration_seconds"),
                    "source": "state.db-delegation",
                }
    return None


def _load_json_dict(path: Path) -> dict:
    """读一个 JSON 文件并保证返回 dict（任何解析/类型问题返回 {}，不抛）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _reuse_baselines(cur_dir: Path, prev_dir: Path) -> None:
    """基线复用：把上一轮 without_skill 各 run 的 grading.json/timing.json 复制进本轮对应 run。

    本轮已有 grading.json 的 run 不动（新跑过的优先）；上轮找不到对应 run 或缺文件即报错退出——
    不允许静默漏带 timing.json（token真值藏在其中，漏了会被估算级闸门拦下）。
    """
    errors: list[str] = []
    copied = 0
    for eval_dir in sorted(cur_dir.glob("eval-*")):
        base_cfg = eval_dir / "without_skill"
        if not base_cfg.is_dir():
            continue
        for run_dir in sorted(base_cfg.glob("run-*")):
            if (run_dir / "grading.json").exists():
                continue  # 本轮已重跑，不覆盖
            src_run = prev_dir / eval_dir.name / "without_skill" / run_dir.name
            if not src_run.is_dir():
                errors.append(f"{eval_dir.name}/without_skill/{run_dir.name}: 上轮无对应 run（{src_run}）")
                continue
            missing = [f for f in ("grading.json", "timing.json") if not (src_run / f).exists()]
            if missing:
                errors.append(f"{eval_dir.name}/without_skill/{run_dir.name}: 上轮缺 {'、'.join(missing)}")
                continue
            for fname in ("grading.json", "timing.json"):
                shutil.copy2(src_run / fname, run_dir / fname)
            copied += 1
    if errors:
        print("ERROR: --reuse-baseline 无法完成基线复用：", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(3)
    if copied:
        print(f"Reused baseline: {copied} run(s) copied from {prev_dir} (grading.json + timing.json)")


def _tokens_from_session_usage(run_dir: Path, db_path: Path | None = None):
    """从 state.db 按产物写入者归因token数（不依赖任何提示词措辞）。

    链路：run 的 outputs/ → 写入它的子会话（messages.tool_calls 里
    write_file/write 且 arguments 含 outputs 锚点路径）→ 该会话的
    session_model_usage 全表求和（input+cache_read+output，与
    state.db-delegation 级口径一致——委派账的 input 已摊入缓存读）。
    多个写入会话（executor+grader 都动过 outputs）时各会话求和后取
    min（单 run 的产物树只该有一个 executor，其余是误锚或复核，
    取小防止把无关会话的消耗记进来）。
    匹配不到返回 None（回退链继续走下一级）。
    """
    db = db_path or (Path.home() / ".hermes" / "state.db")
    if not db.exists():
        return None
    outputs = run_dir / "outputs"
    if not outputs.is_dir():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        # tool_calls JSON 里中文路径以 \uXXXX 转义存储，明文 LIKE 不中——
        # 锚点同时生成明文与 json.dumps 转义两种形态（含 outputs/ 防误配
        # grading 路径；转义形态的反斜杠经 _escape_like 处理）
        anchor_plain = f"{outputs.as_posix()}"
        anchor_esc = json.dumps(anchor_plain)[1:-1]
        queries = [anchor_plain, anchor_esc]
        for anchor in _writer_anchors(outputs)[1:]:
            queries.append(anchor)
            queries.append(json.dumps(anchor)[1:-1])
        rows = []
        for q in queries:
            rows = con.execute(
                "SELECT DISTINCT session_id FROM messages "
                "WHERE role='assistant' AND tool_calls LIKE '%write%' "
                "AND tool_calls LIKE ? ESCAPE '\\'",
                ("%" + _escape_like(q) + "%",)).fetchall()
            if rows:
                break
        if not rows:
            return None
        best = None
        for (sid,) in rows:
            usage = con.execute(
                "SELECT COALESCE(SUM(input_tokens+cache_read_tokens+output_tokens),0), "
                "COALESCE(SUM(api_call_count),0) FROM session_model_usage WHERE session_id=?",
                (sid,)).fetchone()
            total = int(usage[0] or 0)
            if total > 0 and (best is None or total < best[0]):
                best = (total, int(usage[1] or 0))
        if best is None:
            return None
        total, api = best
        return {
            "total_tokens": total,
            "api_calls": api,
            "source": "state.db-session",
        }
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _writer_anchors(outputs_dir: Path) -> list[str]:
    """构造 outputs 目录的 LIKE 匹配锚点（resolve + 未 resolve 双形态）。

    消息里存的可能是规范路径（/private/tmp/...）而调用方传的是 /tmp/...（macOS
    symlink），或反之——两种形态都构造；LIKE 通配符（%_）的转义用 _escape_like。
    """
    seen: list[str] = []
    for p in (outputs_dir.resolve(), outputs_dir.absolute(), outputs_dir):
        s = str(p)
        if s not in seen:
            seen.append(s)
    return seen


def _escape_like(s: str) -> str:
    """LIKE 模式转义：% _ \\ 前加反斜杠（配合查询侧 ESCAPE '\\'）。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _skill_view_hit(con, session_id: str, skill_name: str) -> bool:
    """该会话是否 skill_view 过指定技能（非任意技能）。

    查两条路径（与 run_eval._session_viewed_skill 同思路，独立实现避免跨脚本依赖）：
    1. assistant 行 tool_calls：skill_view 调用的 arguments 含 {"name": "<技能名>"}；
    2. tool 结果行（tool_name='skill_view'）的 content 含 "name" 字段。
    """
    rows = con.execute(
        "SELECT tool_calls FROM messages WHERE session_id=? AND role='assistant' "
        "AND tool_calls LIKE '%skill_view%'",
        (session_id,)).fetchall()
    for (tc,) in rows:
        compact = (tc or "").replace(" ", "").replace("\\", "")
        if f'"name":"{skill_name}"' in compact:
            return True
    rows = con.execute(
        "SELECT content FROM messages WHERE session_id=? AND tool_name='skill_view'",
        (session_id,)).fetchall()
    for (c,) in rows:
        compact = (c or "").replace(" ", "").replace("\\", "")
        if f'"name":"{skill_name}"' in compact:
            return True
    return False


def _verify_runs(benchmark_dir: Path, skill_name: str) -> None:
    """汇总前核对（原手工两件事的代码化）：

    1. 产物作者会话归属：state.db 查写入各 run outputs 路径的会话，须存在——
       防基线代理交付后越权代写兄弟 run 产物。
    2. 带技能组真用了技能：with_skill 各 run 的作者会话里须有对**被测技能**的
       skill_view 调用（请求参数或结果行含其名）——读了别的技能不算数，没读则该轮无效。
    违规打印明细并 sys.exit(4)；skill_name 为空时跳过第 2 项。
    """
    import sqlite3
    db_path = Path.home() / ".hermes" / "state.db"
    if not db_path.exists():
        print("ERROR: --verify needs ~/.hermes/state.db (not found)", file=sys.stderr)
        sys.exit(4)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # 只读，不锁活库
    violations: list[str] = []
    sessions_by_run: dict[str, set] = {}
    for eval_dir in sorted(benchmark_dir.glob("eval-*")):
        for cfg_dir in sorted(p for p in eval_dir.iterdir() if p.is_dir() and list(p.glob("run-*"))):
            for run_dir in sorted(cfg_dir.glob("run-*"), key=_natural_sort_key):
                outputs = run_dir / "outputs"
                if not outputs.is_dir():
                    continue
                writer_sessions: set = set()
                for anchor in _writer_anchors(outputs):
                    rows = con.execute(
                        "SELECT DISTINCT session_id FROM messages WHERE content LIKE ? ESCAPE '\\' LIMIT 50",
                        ("%" + _escape_like(anchor) + "%",)).fetchall()
                    writer_sessions |= {r[0] for r in rows}
                sessions_by_run[str(run_dir)] = writer_sessions
                if not writer_sessions:
                    violations.append(f"{run_dir}: state.db 找不到写入该 outputs 的会话（产物无主）")
    # 带技能组真用技能（核对调用对象＝被测技能，非任意 skill_view）
    if skill_name:
        for run_dir, sessions in sessions_by_run.items():
            if "with_skill" not in run_dir:
                continue
            if not any(_skill_view_hit(con, sid, skill_name) for sid in sessions):
                violations.append(
                    f"{run_dir}: with_skill 组未见对被测技能 {skill_name!r} 的 skill_view 调用"
                    "（技能未真用，该轮无效）")
    con.close()
    if violations:
        print("ERROR: pre-aggregation verification failed:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print("Fix the runs (re-execute polluted/unused runs) and re-aggregate.", file=sys.stderr)
        sys.exit(4)


def load_run_results(benchmark_dir: Path) -> dict:
    """
    Load all run results from a benchmark directory.

    Returns dict keyed by config name (e.g. "with_skill"/"without_skill",
    or "new_skill"/"old_skill"), each containing a list of run results.
    """
    # Support both layouts: eval dirs directly under benchmark_dir, or under runs/
    runs_dir = benchmark_dir / "runs"
    if runs_dir.exists():
        search_dir = runs_dir
    elif list(benchmark_dir.glob("eval-*")):
        search_dir = benchmark_dir
    else:
        print(f"No eval directories found in {benchmark_dir} or {benchmark_dir / 'runs'}")
        return {}

    results: dict[str, list] = {}

    for eval_dir in sorted(search_dir.glob("eval-*"), key=_natural_sort_key):
        metadata_path = eval_dir / "eval_metadata.json"
        eval_name = None
        if metadata_path.exists():
            try:
                with open(metadata_path, encoding="utf-8") as mf:
                    meta = json.load(mf)
                eval_id = meta.get("eval_id")
                eval_name = meta.get("eval_name")
            except (json.JSONDecodeError, OSError):
                # 回退 None（不是 enumerate 序号）：viewer 按 eval_name 分组，
                # 0 基序号会与目录名解析出的 1 基编号碰撞、把不同 eval 混算
                eval_id = None
        else:
            try:
                eval_id = int(eval_dir.name.split("-")[1])
            except ValueError:
                eval_id = None

        # Discover config directories dynamically rather than hardcoding names
        for config_dir in sorted(eval_dir.iterdir()):
            if not config_dir.is_dir():
                continue
            # Skip non-config directories (inputs, outputs, etc.)
            if not list(config_dir.glob("run-*")):
                continue
            config = config_dir.name
            if config not in results:
                results[config] = []

            for run_dir in sorted(config_dir.glob("run-*"), key=_natural_sort_key):
                m_num = re.fullmatch(r"run-(\d+)", run_dir.name)
                if not m_num:
                    print(f"Warning: skipping non-run directory {run_dir}")
                    continue
                run_number = int(m_num.group(1))
                grading_file = run_dir / "grading.json"

                if not grading_file.exists():
                    print(f"Warning: grading.json not found in {run_dir}")
                    continue

                grading = _read_grading(grading_file)
                if grading is None:
                    continue

                # Extract metrics；pass_rate 缺失时由 passed/total 现算——
                # 脚本判/模型判都没有义务写该字段，静默当 0 会把整轮评测变假数据
                summary = _dict_or_empty(grading, "summary")
                passed_n = _num(summary, "passed", 0)
                total_n = _num(summary, "total", 0)
                pass_rate = _num(summary, "pass_rate", None)
                if pass_rate is None:
                    pass_rate = (passed_n / total_n) if total_n else 0.0
                result = {
                    "eval_id": eval_id,
                    "eval_name": eval_name or eval_dir.name,
                    "run_number": run_number,
                    "pass_rate": pass_rate,
                    "passed": passed_n,
                    "failed": _num(summary, "failed", 0),
                    "total": total_n,
                }

                # Extract timing — check grading.json first, then sibling timing.json
                # （timing.json 只读一次，计时与 token 两处共用）
                timing = _dict_or_empty(grading, "timing")
                result["time_seconds"] = _num(timing, "total_duration_seconds", 0.0)
                timing_data: dict = {}
                timing_file = run_dir / "timing.json"
                if timing_file.exists():
                    timing_data = _load_json_dict(timing_file)
                    if not result["time_seconds"]:
                        # 兼容两种字段：total_duration_seconds（秒）或 duration_ms（毫秒）
                        secs = _num(timing_data, "total_duration_seconds", 0.0)
                        if not secs:
                            msecs = _num(timing_data, "duration_ms", 0.0)
                            secs = msecs / 1000.0
                        result["time_seconds"] = secs

                # token回退链：timing.json > usage.json > outputs 文本估算
                # （各级独立判定，不与计时条件耦合；来源记入 tokens_source 供 viewer 展示可信度）
                result["tokens"] = 0
                result["tokens_source"] = "none"
                timing_tokens = _num(timing_data, "total_tokens", 0)
                if timing_tokens:
                    result["tokens"] = timing_tokens
                    result["tokens_source"] = "timing.json"
                if not result["tokens"]:
                    usage_file = run_dir / "usage.json"
                    if usage_file.exists():
                        usage = _load_json_dict(usage_file)
                        usage_tokens = _num(usage, "total_tokens", 0)
                        if not usage_tokens:
                            usage_inner = _dict_or_empty(usage, "usage")
                            usage_tokens = _num(usage_inner, "total_tokens", 0) or (
                                _num(usage_inner, "prompt_tokens", 0) + _num(usage_inner, "completion_tokens", 0))
                        if usage_tokens:
                            result["tokens"] = usage_tokens
                            result["tokens_source"] = "usage.json"

                # state.db 委派血缘回填：delegate_task 场景的真值（input+output，
                # 口径不含 cache_read——result_json 的 input 已摊入；详见函数 docstring）
                if not result["tokens"]:
                    deleg = _tokens_from_delegations(run_dir, benchmark_dir)
                    if deleg:
                        result["tokens"] = deleg["total_tokens"]
                        result["tokens_source"] = deleg["source"]
                        result["api_calls"] = deleg.get("api_calls")
                        if deleg.get("duration_seconds") and not result["time_seconds"]:
                            result["time_seconds"] = deleg["duration_seconds"]

                # state.db 按写入会话归因：不依赖提示词措辞的兜底真值——
                # goal 锚点失配（路径写在 context、措辞缺写入动词等）时在此兜住
                if not result["tokens"]:
                    sess = _tokens_from_session_usage(run_dir)
                    if sess:
                        result["tokens"] = sess["total_tokens"]
                        result["tokens_source"] = sess["source"]
                        result["api_calls"] = sess.get("api_calls")

                # Extract metrics if available
                metrics = _dict_or_empty(grading, "execution_metrics")
                result["tool_calls"] = _num(metrics, "total_tool_calls", 0)
                if not result["tokens"]:
                    metrics_chars = _num(metrics, "output_chars", 0)
                    if not metrics_chars:
                        # outputs/ 文本逐文件用 estimate_tokens 口径估算后求和
                        # （不能拿 "x"*n 整串估算——[A-Za-z0-9]+ 会把它当成一个词，恒为 1）
                        outputs_dir = run_dir / "outputs"
                        est = 0  # 先置零：outputs 目录不存在时下面 if est 直接引用会炸
                        if outputs_dir.is_dir():
                            for of in outputs_dir.rglob("*"):
                                if of.is_file():
                                    try:
                                        est += estimate_tokens(of.read_text(encoding="utf-8", errors="ignore"))
                                    except OSError:
                                        pass
                        # outputs 文本估算级（最不可信）——仅在前四级全部落空时使用；
                        # 打强警告到 stderr：数量级失真风险，见 Pitfalls
                        if est:
                            result["tokens"] = est
                            result["tokens_source"] = "outputs_estimate"
                            print(
                                f"WARNING: run {run_dir} tokens fell back to outputs_estimate "
                                f"(no timing.json/usage.json/delegation match). DO NOT trust "
                                f"this number; fix the truth source and re-aggregate.",
                                file=sys.stderr,
                            )
                    else:
                        result["tokens"] = metrics_chars
                        result["tokens_source"] = "metrics.output_chars"
                        print(
                            f"WARNING: run {run_dir} tokens fell back to metrics.output_chars "
                            f"(proxy value, not real spend). Prefer timing.json/usage.json/",
                            file=sys.stderr,
                        )
                result["errors"] = _num(metrics, "errors_encountered", 0)

                # Extract expectations — viewer requires fields: text, passed, evidence
                raw_expectations = grading.get("expectations") or []
                raw_expectations = [e for e in raw_expectations if isinstance(e, dict)]
                for exp in raw_expectations:
                    if "text" not in exp or "passed" not in exp:
                        print(f"Warning: expectation in {grading_file} missing required fields (text, passed, evidence): {exp}")
                result["expectations"] = raw_expectations

                # Extract notes from user_notes_summary
                notes_summary = _dict_or_empty(grading, "user_notes_summary")
                notes = []
                for key in ("uncertainties", "needs_review", "workarounds"):
                    val = notes_summary.get(key)
                    if isinstance(val, list):
                        notes.extend(str(x) for x in val)
                result["notes"] = notes

                results[config].append(result)

    return results


def aggregate_results(results: dict) -> dict:
    """
    Aggregate run results into summary statistics.

    Returns run_summary with stats for each configuration and delta.
    """
    run_summary = {}
    configs = list(results.keys())

    for config in configs:
        runs = results.get(config, [])

        if not runs:
            run_summary[config] = {
                "pass_rate": {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0},
                "time_seconds": {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0},
                "tokens": {"mean": 0, "stddev": 0, "min": 0, "max": 0}
            }
            continue

        pass_rates = [r["pass_rate"] for r in runs]
        times = [r["time_seconds"] for r in runs]
        tokens = [r.get("tokens", 0) for r in runs]

        run_summary[config] = {
            "pass_rate": calculate_stats(pass_rates),
            "time_seconds": calculate_stats(times),
            "tokens": calculate_stats(tokens)
        }

    # Calculate delta between the first two configs (if two exist)
    if len(configs) >= 2:
        primary = run_summary.get(configs[0], {})
        baseline = run_summary.get(configs[1], {})
    else:
        primary = run_summary.get(configs[0], {}) if configs else {}
        baseline = {}

    delta_pass_rate = primary.get("pass_rate", {}).get("mean", 0) - baseline.get("pass_rate", {}).get("mean", 0)
    delta_time = primary.get("time_seconds", {}).get("mean", 0) - baseline.get("time_seconds", {}).get("mean", 0)
    delta_tokens = primary.get("tokens", {}).get("mean", 0) - baseline.get("tokens", {}).get("mean", 0)

    run_summary["delta"] = {
        "pass_rate": f"{delta_pass_rate:+.2f}",
        "time_seconds": f"{delta_time:+.1f}",
        "tokens": f"{delta_tokens:+.0f}"
    }

    return run_summary


def generate_benchmark(benchmark_dir: Path, skill_name: str = "", skill_path: str = "",
                       executor_model: str = "", analyzer_model: str = "") -> dict:
    """
    Generate complete benchmark.json from run results.
    """
    results = load_run_results(benchmark_dir)
    run_summary = aggregate_results(results)

    # Build runs array for benchmark.json
    runs = []
    for config in results:
        for result in results[config]:
            runs.append({
                "eval_id": result["eval_id"],
                "eval_name": result.get("eval_name"),
                "configuration": config,
                "run_number": result["run_number"],
                "result": {
                    "pass_rate": result["pass_rate"],
                    "passed": result["passed"],
                    "failed": result["failed"],
                    "total": result["total"],
                    "time_seconds": result["time_seconds"],
                    "tokens": result.get("tokens", 0),
                    "tokens_source": result.get("tokens_source", "none"),
                    "tool_calls": result.get("tool_calls", 0),
                    "errors": result.get("errors", 0)
                },
                "expectations": result["expectations"],
                "notes": result["notes"]
            })

    # Determine eval IDs from results; eval_id may be None (explicit null in
    # metadata) — drop None entries: the eval's runs keep the null id and the
    # viewer groups them by eval_name, while sorted() never mixes None with
    # int (TypeError).
    eval_ids = sorted(set(
        r["eval_id"]
        for config in results.values()
        for r in config
        if r["eval_id"] is not None
    ))

    # Actual runs per (eval_id, configuration) instead of a hardcoded guess
    runs_per_config = max(
        (
            sum(1 for r in config_runs if r["eval_id"] == eval_id)
            for config, config_runs in results.items()
            for eval_id in {r["eval_id"] for r in config_runs}
        ),
        default=0,
    )

    benchmark = {
        "metadata": {
            "skill_name": skill_name or "<skill-name>",
            "skill_path": skill_path or "<path/to/skill>",
            "skill_size": compute_skill_size(skill_path),
            "executor_model": executor_model or "<model-name>",
            "analyzer_model": analyzer_model or executor_model or "<model-name>",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "evals_run": eval_ids,
            "runs_per_configuration": runs_per_config
        },
        "runs": runs,
        "run_summary": run_summary,
        "notes": []  # To be filled by analyzer
    }

    return benchmark


def generate_markdown(benchmark: dict) -> str:
    """Generate human-readable benchmark.md from benchmark data."""
    metadata = benchmark["metadata"]
    run_summary = benchmark["run_summary"]

    # Determine config names (excluding "delta")
    configs = [k for k in run_summary if k != "delta"]
    config_a = configs[0] if len(configs) >= 1 else "config_a"
    config_b = configs[1] if len(configs) >= 2 else "config_b"
    label_a = config_a.replace("_", " ").title()
    label_b = config_b.replace("_", " ").title()

    lines = [
        f"# Skill Benchmark: {metadata['skill_name']}",
        "",
        f"**Model**: {metadata['executor_model']}",
        f"**Date**: {metadata['timestamp']}",
        f"**Evals**: {', '.join(map(str, metadata['evals_run']))} ({metadata['runs_per_configuration']} runs each per configuration)",
        "",
        "## Summary",
        "",
        f"| Metric | {label_a} | {label_b} | Delta |",
        "|--------|------------|---------------|-------|",
    ]

    a_summary = run_summary.get(config_a, {})
    b_summary = run_summary.get(config_b, {})
    delta = run_summary.get("delta", {})

    # Format pass rate
    a_pr = a_summary.get("pass_rate", {})
    b_pr = b_summary.get("pass_rate", {})
    lines.append(f"| Pass Rate | {a_pr.get('mean', 0)*100:.0f}% ± {a_pr.get('stddev', 0)*100:.0f}% | {b_pr.get('mean', 0)*100:.0f}% ± {b_pr.get('stddev', 0)*100:.0f}% | {delta.get('pass_rate', '—')} |")

    # Format time
    a_time = a_summary.get("time_seconds", {})
    b_time = b_summary.get("time_seconds", {})
    lines.append(f"| Time | {a_time.get('mean', 0):.1f}s ± {a_time.get('stddev', 0):.1f}s | {b_time.get('mean', 0):.1f}s ± {b_time.get('stddev', 0):.1f}s | {delta.get('time_seconds', '—')}s |")

    # 格式化token行
    a_tokens = a_summary.get("tokens", {})
    b_tokens = b_summary.get("tokens", {})
    lines.append(f"| Tokens | {a_tokens.get('mean', 0):.0f} ± {a_tokens.get('stddev', 0):.0f} | {b_tokens.get('mean', 0):.0f} ± {b_tokens.get('stddev', 0):.0f} | {delta.get('tokens', '—')} |")

    # Notes section
    if benchmark.get("notes"):
        lines.extend([
            "",
            "## Notes",
            ""
        ])
        for note in benchmark["notes"]:
            lines.append(f"- {note}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Aggregate benchmark run results into summary statistics"
    )
    parser.add_argument(
        "benchmark_dir",
        type=Path,
        help="Path to the benchmark directory"
    )
    parser.add_argument(
        "--skill-name",
        default="",
        help="Name of the skill being benchmarked"
    )
    parser.add_argument(
        "--skill-path",
        default="",
        help="Path to the skill being benchmarked"
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        help="Output path for benchmark.json (default: <benchmark_dir>/benchmark.json)"
    )
    parser.add_argument(
        "--allow-estimate",
        action="store_true",
        help="允许估算级token数（outputs_estimate/metrics.output_chars）写入产物。 "
             "默认：任一 run 缺真值来源token数即报错退出。"
    )
    parser.add_argument(
        "--reuse-baseline",
        type=Path,
        default=None,
        help="Previous iteration dir: copy its without_skill grading.json/timing.json into "
             "this iteration's empty baseline runs (error out if missing, never silently skip)."
    )
    parser.add_argument(
        "--executor-model",
        default="",
        help="Executor model ID written into metadata.executor_model (default: '<model-name>' placeholder)"
    )
    parser.add_argument(
        "--analyzer-model",
        default="",
        help="Analyzer model ID written into metadata.analyzer_model (default: same as --executor-model)"
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Pre-aggregation verification: check run authorship (session provenance from "
             "state.db) and that with_skill runs actually loaded the skill (skill_view call). "
             "Exit 4 on any violation."
    )

    args = parser.parse_args()

    if not args.benchmark_dir.exists():
        print(f"Directory not found: {args.benchmark_dir}", file=sys.stderr)
        sys.exit(1)

    # 基线复用：在汇总前把上轮基线产物补进本轮空 run
    if args.reuse_baseline:
        _reuse_baselines(args.benchmark_dir, args.reuse_baseline.resolve())

    # 汇总前核对：产物归属 + 技能真用（原手工两件事）
    if args.verify:
        _verify_runs(args.benchmark_dir, args.skill_name)

    # Generate benchmark
    benchmark = generate_benchmark(args.benchmark_dir, args.skill_name, args.skill_path,
                                   executor_model=args.executor_model, analyzer_model=args.analyzer_model)

    # 估算级闸门：任一 run 的token数非真值来源即拒绝出报告（防数量级失真的假数字进报告）
    # 真值来源：timing.json / usage.json / state.db-delegation / state.db-session；非真值即闸门拦下
    if not args.allow_estimate:
        bad = [r for r in benchmark["runs"]
               if r["result"].get("tokens_source") not in ("timing.json", "usage.json", "state.db-delegation", "state.db-session")]
        if bad:
            print("ERROR: token真值闸门未过——拒绝以估算级token数写出基准报告：", file=sys.stderr)
            for r in bad:
                print(f"  {r['configuration']}/run{r['run_number']} (eval {r['eval_id']}): "
                      f"tokens_source={r['result'].get('tokens_source')}", file=sys.stderr)
            print("Fix the truth source (copy timing.json with reused baselines / add write-path "
                  "anchor to dispatch prompt) and re-run, or pass --allow-estimate to override.",
                  file=sys.stderr)
            sys.exit(2)

    # Determine output paths
    output_json = args.output or (args.benchmark_dir / "benchmark.json")
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md = output_json.with_suffix(".md")

    # Write benchmark.json
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(benchmark, f, indent=2)
    print(f"Generated: {output_json}")

    # Write benchmark.md
    markdown = generate_markdown(benchmark)
    with open(output_md, "w", encoding="utf-8") as f:
        f.write(markdown)
    print(f"Generated: {output_md}")

    # Print summary
    run_summary = benchmark["run_summary"]
    configs = [k for k in run_summary if k != "delta"]
    delta = run_summary.get("delta", {})

    print(f"\nSummary:")
    for config in configs:
        pr = run_summary[config]["pass_rate"]["mean"]
        label = config.replace("_", " ").title()
        print(f"  {label}: {pr*100:.1f}% pass rate")
    print(f"  Delta:         {delta.get('pass_rate', '—')}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run trigger evaluation for a skill description (Hermes 版).

Tests whether a skill's description causes the Hermes agent to trigger
(consult the skill via the skill_view tool) for a set of queries.
Outputs results as JSON.

Hermes 适配说明：
  - 用 `hermes -z`（one-shot 模式）执行查询，模型与登录态走本机 Hermes 配置。
  - 触发检测（主路径）：hermes -z 子会话结束后，到 state.db 按会话查
    skill_view 调用记录（请求参数与工具结果都查）——会话隔离，并行安全；
    归因口径：只认对夹具（<name>-eval-<uuid>，候选 description 的唯一载体）的
    skill_view——被测技能已安装时，触发已装副本（载的是旧 description）不计入。
    定位不到会话时退回 usage.json 计数增量判定（全局信号，仅串行可信，
    夹具＋真技能都盯以防定位失败漏报）。
  - 每个查询运行使用唯一技能名（<name>-eval-<uuid>）写入
    ~/.hermes/skills/，保证并行运行之间的归属无歧义；运行结束后删除
    技能目录，并尽力清理评测产生的 usage 记录。
  - 请勿在此测试中使用 `hermes --skills` 预加载——预加载等于强制注入，
    会绕过"模型自主决定是否触发"这一被测行为。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import sqlite3
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from scripts.utils import hermes_bin, parse_skill_md


def find_project_root() -> Path:
    """Find the project root by walking up from cwd looking for .hermes/.

    Mirrors Hermes' repo-local discovery (./.hermes/skills 等), so the
    one-shot run lands in the same workspace context as the caller.
    """
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".hermes").is_dir():
            return parent
    return current


def _usage_file() -> Path:
    return Path.home() / ".hermes" / "skills" / ".usage.json"


def _read_usage_counts() -> dict:
    """Read the whole usage file (read-only, best-effort)."""
    path = _usage_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _counts(rec) -> int:
    if not isinstance(rec, dict):
        return 0
    return (rec.get("view_count") or 0) + (rec.get("use_count") or 0)


def _snapshot_counts(*names: str) -> dict[str, int]:
    """本轮开始时的计数快照（供 _was_triggered 算增量）。"""
    data = _read_usage_counts()
    return {n: _counts(data.get(n)) for n in names}


def _was_triggered(snapshot: dict[str, int], wait_seconds: float = 6.0) -> bool:
    """Fallback 判定：任何被盯技能的 usage 计数在快照后增长即触发（全局信号，并行会互记）。"""
    deadline = time.monotonic() + wait_seconds
    while True:
        data = _read_usage_counts()
        if any(_counts(data.get(n)) > base for n, base in snapshot.items()):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _session_viewed_skill(session_id: str, *names: str, db_path: Path | None = None) -> bool:
    """主判定：该子会话是否 skill_view 过任一被盯技能（state.db 会话隔离，可安全并行）。

    查两条路径：
    1. assistant 消息的 tool_calls（skill_view 调用的请求参数，arguments 含技能名）；
    2. tool 结果行的 content（含 "name" 字段的 JSON）。
    """
    db = db_path or _state_db()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        # 路径1：请求参数——skill_view 调用行 role='assistant'、tool_calls JSON 的
        # function.arguments 里有 {"name": "<技能名>"}（tool_name 字段在这些行上是 NULL；
        # arguments 是 JSON 套 JSON，"name\" 带反斜杠转义——一并剥掉）
        rows = con.execute(
            "SELECT tool_calls FROM messages WHERE session_id=? AND role='assistant' "
            "AND tool_calls LIKE '%skill_view%'",
            (session_id,)).fetchall()
        for (tc,) in rows:
            compact = (tc or "").replace(" ", "").replace("\\", "")
            if any(f'"name":"{n}"' in compact for n in names):
                return True
        # 路径2：工具结果行（tool_name='skill_view'，其 tool_calls 字段恒空）
        rows = con.execute(
            "SELECT content FROM messages WHERE session_id=? AND tool_name='skill_view'",
            (session_id,)).fetchall()
        for (c,) in rows:
            compact = (c or "").replace(" ", "").replace("\\", "")
            if any(f'"name":"{n}"' in compact for n in names):
                return True
    except sqlite3.Error:
        return False
    finally:
        con.close()
    return False


def _find_session_for_query(query: str, since_ts: float) -> str | None:
    """按 user 消息内容＋时间窗定位本次 hermes -z 子会话的 session_id。

    并行时同 query 的多个子会话都在窗内，取 rowid 最大（最新）的——
    各 worker 各自的窗只覆盖自己子进程的存活期，同窗内出现别人的同文
    消息概率极低；配合调用时刻紧贴子进程退出，定位即准确。
    """
    try:
        db = sqlite3.connect(f"file:{_state_db()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = db.execute(
            "SELECT session_id FROM messages WHERE role='user' AND content=? "
            "AND timestamp>=? ORDER BY rowid DESC LIMIT 1",
            (query, since_ts)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        db.close()


def _state_db() -> Path:
    return Path.home() / ".hermes" / "state.db"


def run_single_query(
    query: str,
    skill_name: str,
    skill_description: str,
    timeout: int,
    project_root: str,
    model: str | None = None,
) -> bool:
    """Run a single query and return whether the skill was triggered.

    Creates a minimal skill directory under ~/.hermes/skills/ with a unique
    name carrying the description under test, runs `hermes -z` with the raw
    query, then checks whether Hermes called skill_view on it (usage record
    bump). The unique name isolates parallel runs from each other.
    """
    # uuid4().hex[:8] 撞名时静默复用会互相覆盖——重试拿新名字
    for _ in range(3):
        clean_name = f"{skill_name}-eval-{uuid.uuid4().hex[:8]}"
        skill_dir = Path.home() / ".hermes" / "skills" / clean_name
        try:
            skill_dir.mkdir(parents=True)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("could not allocate a unique eval skill dir")
    skill_md = skill_dir / "SKILL.md"

    try:
        # description 里混入 tab 会让手工拼的 block scalar 变非法 YAML——先清洗
        safe_desc = skill_description.replace("\t", "  ").rstrip()
        indented_desc = "\n  ".join(safe_desc.split("\n"))
        # name 经 json.dumps 输出带引号标量：name 含 ': ' 等 YAML 特殊字符时
        # 裸拼会产出非法 YAML，整轮评测静默判未触发
        skill_md.write_text(
            f"---\n"
            f"name: {json.dumps(clean_name)}\n"
            f"description: |\n"
            f"  {indented_desc}\n"
            f"---\n\n"
            f"# {skill_name}\n\n"
            f"This skill handles: {safe_desc}\n",
            encoding="utf-8",
        )

        cmd = [hermes_bin(), "-z", query]
        if model:
            cmd.extend(["--model", model])

        # 快照与启动时刻：夹具＋真技能都盯（兜底用）；proc_start_ts 供会话定位
        snapshot = _snapshot_counts(clean_name, skill_name)
        proc_start_ts = time.time() - 5

        # start_new_session：超时才能 killpg 杀掉 hermes 派生的整个进程组，
        # 否则孙进程继承管道不退出，communicate() 永久阻塞拖死工作线程
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=project_root,
            start_new_session=True,
        )
        # communicate() 返回后 proc.stderr 已是关闭的管道对象（不可再切片），
        # 告警文本必须用保存下来的 stderr 字符串，否则非零退出码路径必抛 TypeError
        stderr = ""
        try:
            _, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # 触发（skill_view bump）可能发生在超时之前——仍按已触发计
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                _, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        finally:
            if proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            if proc.returncode not in (0, None, -signal.SIGKILL, -signal.SIGTERM):
                print(
                    f"Warning: hermes -z exited {proc.returncode} for query {query[:50]!r}: "
                    f"{(stderr or '')[:200]}",
                    file=sys.stderr,
                )

        # 主判定：state.db 会话隔离（可安全并行）。只认夹具名——夹具是候选
        # description 的唯一载体；被测技能已安装时若模型触发的是已装副本（载的
        # 是旧 description），不能计到候选头上。真技能名仅保留在 usage 计数兜底级。
        session_id = _find_session_for_query(query, proc_start_ts)
        if session_id and _session_viewed_skill(session_id, clean_name):
            return True
        return _was_triggered(snapshot)
    finally:
        shutil.rmtree(skill_dir, ignore_errors=True)


def _cleanup_usage_records(skill_name: str) -> int:
    """Best-effort removal of usage records left by eval runs (pattern sweep).

    Records are keyed `<skill_name>-eval-<uuid>`; after the executor has shut
    down no new ones appear, so a prefix sweep is safe. Uses the same
    tempfile+os.replace atomic pattern as hermes itself.
    """
    path = _usage_file()
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0
    prefix = f"{skill_name}-eval-"
    stale = [k for k in data if isinstance(k, str) and k.startswith(prefix)]
    if not stale:
        return 0
    for k in stale:
        data.pop(k, None)
    fd = None
    tmp = None
    try:
        import tempfile

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".usage_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None  # fdopen 已接管，避免 except 路径二次关闭
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, path)
        tmp = None  # 已原子替换，无需清理
    except Exception as e:
        # 丢更新窗口说明（读→写之间别人写入会被覆盖）：usage 记录只是计数缓存，
        # 下次触发会重写——失败时保文件完整优先，警告即可
        print(f"Warning: usage-record cleanup failed: {e}", file=sys.stderr)
        return 0
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return len(stale)


def validate_eval_set(eval_set) -> tuple[bool, str]:
    """加载即校验：结构错误当场报，不在 worker 里逐条吞成 False."""
    if not isinstance(eval_set, list) or not eval_set:
        return False, "eval set must be a non-empty list"
    seen: set[str] = set()
    for i, item in enumerate(eval_set):
        if not isinstance(item, dict):
            return False, f"eval set[{i}] must be an object, got {type(item).__name__}"
        q = item.get("query")
        if not isinstance(q, str) or not q.strip():
            return False, f"eval set[{i}] missing non-empty 'query'"
        if q in seen:
            return False, f"duplicate query in eval set: {q[:60]!r}"
        seen.add(q)
        st = item.get("should_trigger")
        if not isinstance(st, bool):
            return False, f"eval set[{i}] 'should_trigger' must be a bool, got {st!r}"
    return True, ""


def run_eval(
    eval_set: list[dict],
    skill_name: str,
    description: str,
    num_workers: int,
    timeout: int,
    project_root: Path,
    runs_per_query: int = 1,
    trigger_threshold: float = 0.5,
    model: str | None = None,
) -> dict:
    """Run the full eval set and return results."""
    results = []

    try:
        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            future_to_info = {}
            for item in eval_set:
                for run_idx in range(runs_per_query):
                    future = executor.submit(
                        run_single_query,
                        item["query"],
                        skill_name,
                        description,
                        timeout,
                        str(project_root),
                        model,
                    )
                    future_to_info[future] = (item, run_idx)

            query_triggers: dict[str, list[bool]] = {}
            query_items: dict[str, dict] = {}
            for future in as_completed(future_to_info):
                item, _ = future_to_info[future]
                query = item["query"]
                query_items[query] = item
                if query not in query_triggers:
                    query_triggers[query] = []
                try:
                    query_triggers[query].append(future.result())
                except (FileNotFoundError, PermissionError, RuntimeError):
                    # 环境类系统性故障（hermes 不存在/不可写/目录分配失败）向上传播
                    # fail-fast：吞成 False 会让整轮评测静默变成"触发率 0%"假数据
                    raise
                except Exception as e:
                    print(f"Warning: query failed: {e}", file=sys.stderr)
                    query_triggers[query].append(False)

        for query, triggers in query_triggers.items():
            item = query_items[query]
            trigger_rate = sum(triggers) / len(triggers)
            should_trigger = item["should_trigger"]
            if should_trigger:
                did_pass = trigger_rate >= trigger_threshold
            else:
                did_pass = trigger_rate < trigger_threshold
            results.append({
                "query": query,
                "should_trigger": should_trigger,
                "trigger_rate": trigger_rate,
                "triggers": sum(triggers),
                "runs": len(triggers),
                "pass": did_pass,
            })
    finally:
        # fail-fast 异常传播也要清 usage 记录（executor 已随 with 关闭，
        # 前缀扫描安全）——不留 <name>-eval-<uuid> 残骸
        _cleanup_usage_records(skill_name)

    passed = sum(1 for r in results if r["pass"])
    total = len(results)

    return {
        "skill_name": skill_name,
        "description": description,
        "results": results,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Run trigger evaluation for a skill description (Hermes)")
    parser.add_argument("--eval-set", required=True, help="Path to eval set JSON file")
    parser.add_argument("--skill-path", required=True, help="Path to skill directory")
    parser.add_argument("--description", default=None, help="Override description to test")
    parser.add_argument("--num-workers", type=int, default=1, help="Number of parallel workers（真技能计数是共享信号，并发会互记触发，保持 1）")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout per query in seconds (hermes -z 含代理启动与真实任务，留宽一些)")
    parser.add_argument("--runs-per-query", type=int, default=3, help="Number of runs per query")
    parser.add_argument("--trigger-threshold", type=float, default=0.5, help="Trigger rate threshold")
    parser.add_argument("--model", default=None, help="Model for hermes -z (default: user's configured model)")
    parser.add_argument("--verbose", action="store_true", help="Print progress to stderr")
    args = parser.parse_args()

    # 下界校验：0/negative 会让 ProcessPoolExecutor 抛裸 ValueError、
    # runs-per-query=0 让触发率算式 ZeroDivisionError——提前给出可读报错
    if args.num_workers < 1:
        parser.error(f"--num-workers must be >= 1, got {args.num_workers}")
    if args.runs_per_query < 1:
        parser.error(f"--runs-per-query must be >= 1, got {args.runs_per_query}")
    if not 0 < args.trigger_threshold < 1:
        parser.error(f"--trigger-threshold must be in (0, 1), got {args.trigger_threshold}")

    try:
        eval_set = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"Error: cannot read eval set {args.eval_set}: {e}", file=sys.stderr)
        sys.exit(1)
    ok, msg = validate_eval_set(eval_set)
    if not ok:
        print(f"Error: invalid eval set {args.eval_set}: {msg}", file=sys.stderr)
        sys.exit(1)
    skill_path = Path(args.skill_path)

    if not (skill_path / "SKILL.md").exists():
        print(f"Error: No SKILL.md found at {skill_path}", file=sys.stderr)
        sys.exit(1)

    name, original_description, content = parse_skill_md(skill_path)
    description = args.description or original_description
    project_root = find_project_root()

    if args.verbose:
        print(f"Evaluating: {description}", file=sys.stderr)

    output = run_eval(
        eval_set=eval_set,
        skill_name=name,
        description=description,
        num_workers=args.num_workers,
        timeout=args.timeout,
        project_root=project_root,
        runs_per_query=args.runs_per_query,
        trigger_threshold=args.trigger_threshold,
        model=args.model,
    )

    if args.verbose:
        summary = output["summary"]
        print(f"Results: {summary['passed']}/{summary['total']} passed", file=sys.stderr)
        for r in output["results"]:
            status = "过" if r["pass"] else "不过"
            rate_str = f"{r['triggers']}/{r['runs']}"
            print(f"  [{status}] rate={rate_str} expected={r['should_trigger']}: {r['query'][:70]}", file=sys.stderr)

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()

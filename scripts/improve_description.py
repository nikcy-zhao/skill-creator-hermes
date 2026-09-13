#!/usr/bin/env python3
"""Improve a skill description based on eval results (Hermes 版).

Takes eval results (from run_eval.py) and generates an improved description
by calling `hermes -z` as a subprocess (same auth pattern as run_eval.py —
uses the locally configured Hermes provider/model, no separate API key
needed).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from scripts.utils import hermes_bin, parse_skill_md

MAX_DESCRIPTION_CHARS = 1024
# 历史明细最多带最近几轮完整内容（更早的只留一行分数），防 prompt 线性膨胀撞 250KB argv 上限
MAX_FULL_HISTORY = 2


def build_history_block(history: list[dict]) -> str:
    """Render the PREVIOUS ATTEMPTS prompt block, trimming old detail.

    最近 MAX_FULL_HISTORY 轮带完整 train_results 明细；更早的只留分数摘要。
    history 来自外部 JSON（--history），缺键/坏条目兜底渲染，不让整个 improve 崩。
    """
    if not history:
        return ""
    parts: list[str] = []
    recent = history[-MAX_FULL_HISTORY:]
    recent_ids = {id(h) for h in recent}
    for h in history:
        if not isinstance(h, dict):
            continue
        train_s = f"{h.get('train_passed', 0)}/{h.get('train_total', 0)}"
        test_s = f"{h.get('test_passed', '?')}/{h.get('test_total', '?')}" if h.get('test_passed') is not None else None
        score_str = f"train={train_s}" + (f", test={test_s}" if test_s else "")
        parts.append(f'<attempt {score_str}>')
        parts.append(f'Description: "{h.get("description", "")}"')
        if id(h) in recent_ids:
            train_attempt_results = h.get("train_results") or h.get("results") or []
            if train_attempt_results:
                parts.append("Train results:")
                for r in train_attempt_results:
                    if not isinstance(r, dict):
                        continue
                    status = "过" if r.get("pass") else "不过"
                    query = str(r.get("query", ""))[:80]
                    parts.append(f'  [{status}] "{query}" (triggered {r.get("triggers", 0)}/{r.get("runs", 0)})')
        if h.get("note"):
            parts.append(f'Note: {h["note"]}')
        parts.append("</attempt>")
    return "\n".join(parts)


def enforce_description_limit(description: str, max_chars: int = MAX_DESCRIPTION_CHARS) -> str:
    """超限兜底：模型仍超长时按词边界硬截断，保证不违反 1024 硬约束."""
    if len(description) <= max_chars:
        return description
    cut = description[:max_chars]
    # 在最近的空白处断开，避免截半个词
    for sep in ("\n", "。", "；", "; ", " "):
        idx = cut.rfind(sep)
        if idx > max_chars * 0.6:
            return cut[:idx].rstrip()
    return cut.rstrip()


def _call_hermes(prompt: str, model: str | None, timeout: int = 300) -> str:
    """Run `hermes -z` with the prompt and return the text response.

    The prompt goes over argv (hermes one-shot takes the prompt as its
    argument). It embeds the full SKILL.md body — guard against absurdly
    large prompts that would exceed the OS argv limit.
    """
    if len(prompt.encode("utf-8")) > 250_000:
        raise RuntimeError(
            "prompt exceeds 250KB argv limit; trim the skill content before retrying"
        )
    cmd = [hermes_bin(), "-z", prompt]
    if model:
        cmd.extend(["--model", model])

    # start_new_session + killpg：与 run_eval.run_single_query 对齐——hermes 会
    # 派生持有管道的孙进程，subprocess.run 超时只 kill 直接子进程，孙进程会
    # 泄漏成孤儿；这里成组杀干净
    import signal as _signal
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    if proc.returncode != 0:
        raise RuntimeError(
            f"hermes -z exited {proc.returncode}\nstderr: {stderr}"
        )
    return stdout


def improve_description(
    skill_name: str,
    skill_content: str,
    current_description: str,
    eval_results: dict,
    history: list[dict],
    model: str,
    test_results: dict | None = None,
    log_dir: Path | None = None,
    iteration: int | None = None,
) -> str:
    """Call Hermes to improve the description based on eval results."""
    failed_triggers = [
        r for r in eval_results["results"]
        if r["should_trigger"] and not r["pass"]
    ]
    false_triggers = [
        r for r in eval_results["results"]
        if not r["should_trigger"] and not r["pass"]
    ]

    # Build scores summary
    train_score = f"{eval_results['summary']['passed']}/{eval_results['summary']['total']}"
    if test_results:
        test_score = f"{test_results['summary']['passed']}/{test_results['summary']['total']}"
        scores_summary = f"Train: {train_score}, Test: {test_score}"
    else:
        scores_summary = f"Train: {train_score}"

    prompt = f"""You are optimizing a skill description for a Hermes agent skill called "{skill_name}". A "skill" is sort of like a prompt, but with progressive disclosure -- there's a title and description that the agent sees when deciding whether to use the skill, and then if it does use the skill, it reads the .md file which has lots more details and potentially links to other resources in the skill folder like helper files and scripts and additional documentation or examples.

The description appears in Hermes' skills list (the skills_list tool shows it in full) and, truncated to its first ~60 characters, in the system prompt skill index. When a user sends a query, the agent decides whether to consult the skill (via skill_view) based on the title and this description. Your goal is to write a description that triggers for relevant queries, and doesn't trigger for irrelevant ones.

Here's the current description:
<current_description>
"{current_description}"
</current_description>

Current scores ({scores_summary}):
<scores_summary>
"""
    if failed_triggers:
        prompt += "FAILED TO TRIGGER (should have triggered but didn't):\n"
        for r in failed_triggers:
            prompt += f'  - "{r["query"]}" (triggered {r["triggers"]}/{r["runs"]} times)\n'
        prompt += "\n"

    if false_triggers:
        prompt += "FALSE TRIGGERS (triggered but shouldn't have):\n"
        for r in false_triggers:
            prompt += f'  - "{r["query"]}" (triggered {r["triggers"]}/{r["runs"]} times)\n'
        prompt += "\n"

    if history:
        prompt += "PREVIOUS ATTEMPTS (do NOT repeat these — try something structurally different):\n\n"
        prompt += build_history_block(history)
        prompt += "\n\n"

    prompt += f"""</scores_summary>

Skill content (for context on what the skill does):
<skill_content>
{skill_content}
</skill_content>

Based on the failures, write a new and improved description that is more likely to trigger correctly. When I say "based on the failures", it's a bit of a tricky line to walk because we don't want to overfit to the specific cases you're seeing. So what I DON'T want you to do is produce an ever-expanding list of specific queries that this skill should or shouldn't trigger for. Instead, try to generalize from the failures to broader categories of user intent and situations where this skill would be useful or not useful. The reason for this is twofold:

1. Avoid overfitting
2. The list might get loooong and it's injected into ALL queries and there might be a lot of skills, so we don't want to blow too much space on any given description.

Concretely, your description should not be more than about 100-200 words, even if that comes at the cost of accuracy. Keep it under 1024 characters — it gets injected into skills_list responses, so bloat costs context on every turn.

Here are some tips that we've found to work well in writing these descriptions:
- The skill should be phrased in the imperative -- "Use this skill for" rather than "this skill does"
- The skill description should focus on the user's intent, what they are trying to achieve, vs. the implementation details of how the skill works.
- Hermes truncates the description in the system prompt skill index to its first ~60 characters -- front-load the most distinctive trigger keywords at the very start of the description.
- The description competes with other skills for the agent's attention — make it distinctive and immediately recognizable.
- If you're getting lots of failures after repeated attempts, change things up. Try different sentence structures or wordings.

I'd encourage you to be creative and mix up the style in different iterations since you'll have multiple opportunities to try different approaches and we'll just grab the highest-scoring one at the end. 

Please respond with only the new description text in <new_description> tags, nothing else."""

    text = _call_hermes(prompt, model)

    match = re.search(r"<new_description>(.*?)</new_description>", text, re.DOTALL)
    description = match.group(1).strip() if match else ""
    if not description:
        # 失配时不能把整个 stdout 当 description（思考过程/前言会污染 SKILL.md）——报错重试
        raise RuntimeError(
            f"improve response missing <new_description> tags; raw output head: {text[:200]!r}"
        )
    # 只去成对包裹引号，避免误删首尾合法引号
    if len(description) >= 2 and description[0] == description[-1] and description[0] in "\"'":
        description = description[1:-1]

    transcript: dict = {
        "iteration": iteration,
        "prompt": prompt,
        "response": text,
        "parsed_description": description,
        "char_count": len(description),
        "over_limit": len(description) > MAX_DESCRIPTION_CHARS,
    }

    # Safety net: the prompt already states the 1024-char target, but if
    # the model blew past it anyway, make one fresh single-turn call that
    # quotes ONLY the too-long description (not the whole prompt again —
    # 重嵌完整 prompt 会叠加体积，更容易撞 250KB argv 上限).
    if len(description) > MAX_DESCRIPTION_CHARS:
        shorten_prompt = (
            f"The following skill description is {len(description)} characters, "
            f"over the {MAX_DESCRIPTION_CHARS}-character limit:\n\n"
            f'"{description}"\n\n'
            f"Rewrite it to be under 1024 characters while keeping the most "
            f"important trigger words and intent coverage. Respond with only "
            f"the new description in <new_description> tags."
        )
        shorten_text = _call_hermes(shorten_prompt, model)
        match = re.search(r"<new_description>(.*?)</new_description>", shorten_text, re.DOTALL)
        if match:
            shortened = match.group(1).strip()
            if len(shortened) >= 2 and shortened[0] == shortened[-1] and shortened[0] in "\"'":
                shortened = shortened[1:-1]
        else:
            shortened = ""

        transcript["rewrite_prompt"] = shorten_prompt
        transcript["rewrite_response"] = shorten_text
        transcript["rewrite_description"] = shortened
        transcript["rewrite_char_count"] = len(shortened)
        # 二次校验：rewrite 后仍超限则硬截断兜底，绝不带着超限描述返回
        # （先记原长度再重新赋值，否则 hard_truncated 恒 False）
        pre_truncate_len = len(shortened) if shortened else len(description)
        description = enforce_description_limit(shortened) if shortened else enforce_description_limit(description)
        transcript["hard_truncated"] = len(description) < pre_truncate_len

    transcript["final_description"] = description

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"improve_iter_{iteration or 'unknown'}.json"
        log_file.write_text(json.dumps(transcript, indent=2))

    return description


def main():
    parser = argparse.ArgumentParser(description="Improve a skill description based on eval results")
    parser.add_argument("--eval-results", required=True, help="Path to eval results JSON (from run_eval.py)")
    parser.add_argument("--skill-path", required=True, help="Path to skill directory")
    parser.add_argument("--history", default=None, help="Path to history JSON (previous attempts)")
    parser.add_argument("--model", required=True, help="Model for improvement")
    parser.add_argument("--verbose", action="store_true", help="Print thinking to stderr")
    args = parser.parse_args()

    skill_path = Path(args.skill_path)
    if not (skill_path / "SKILL.md").exists():
        print(f"Error: No SKILL.md found at {skill_path}", file=sys.stderr)
        sys.exit(1)

    try:
        eval_results = json.loads(Path(args.eval_results).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"Error: cannot read eval results {args.eval_results}: {e}", file=sys.stderr)
        sys.exit(1)
    history = []
    if args.history:
        try:
            history = json.loads(Path(args.history).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f"Error: cannot read history {args.history}: {e}", file=sys.stderr)
            sys.exit(1)
    if not isinstance(history, list):
        print(f"Error: history must be a JSON array, got {type(history).__name__}", file=sys.stderr)
        sys.exit(1)

    name, _, content = parse_skill_md(skill_path)
    current_description = eval_results["description"]

    if args.verbose:
        print(f"Current: {current_description}", file=sys.stderr)
        print(f"Score: {eval_results['summary']['passed']}/{eval_results['summary']['total']}", file=sys.stderr)

    new_description = improve_description(
        skill_name=name,
        skill_content=content,
        current_description=current_description,
        eval_results=eval_results,
        history=history,
        model=args.model,
    )

    if args.verbose:
        print(f"Improved: {new_description}", file=sys.stderr)

    # Output as JSON with both the new description and updated history
    output = {
        "description": new_description,
        "history": history + [{
            "description": current_description,
            "train_passed": eval_results["summary"]["passed"],
            "train_failed": eval_results["summary"]["failed"],
            "train_total": eval_results["summary"]["total"],
            "train_results": eval_results["results"],
        }],
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()

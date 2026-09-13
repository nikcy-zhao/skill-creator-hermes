#!/usr/bin/env python3
from __future__ import annotations
"""Generate and serve a review page for eval results.

Reads the workspace directory, discovers runs (directories with outputs/),
embeds all output data into a self-contained HTML page, and serves it via
a tiny HTTP server. Feedback auto-saves to feedback.json in the workspace.

Usage:
    python3 -m scripts.generate_review <workspace-path> [--port PORT] [--skill-name NAME]
    python3 -m scripts.generate_review <workspace-path> --previous-feedback /path/to/old/feedback.json

No dependencies beyond the Python stdlib are required.
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import time
import webbrowser
from functools import partial
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Files to exclude from output listings
METADATA_FILES = {"transcript.md", "user_notes.md", "metrics.json"}

# Extensions we render as inline text
TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".yaml", ".yml", ".xml", ".html", ".css", ".sh", ".rb", ".go", ".rs",
    ".java", ".c", ".cpp", ".h", ".hpp", ".sql", ".r", ".toml",
}

# Extensions we render as inline images
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# MIME type overrides for common types
MIME_OVERRIDES = {
    ".svg": "image/svg+xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def get_mime_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext]
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def _b64_file(path: Path) -> str | None:
    """Base64-encode a file, None on read error."""
    try:
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None


def _load_json_optional(path: Path | None) -> dict | None:
    """Load a JSON file if it exists, None on missing or invalid JSON."""
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def find_runs(workspace: Path) -> list[dict]:
    """Recursively find run directories: 有 outputs/ 子目录，或有 grading.json 的 run 目录（两者都是有效 run，汇总器按 grading 收录、查看器不该漏掉只有 grading 的 run）."""
    runs: list[dict] = []
    _find_runs_recursive(workspace, workspace, runs)
    # eval_id may be absent OR None (metadata exists but key missing/null);
    # mixed None + int would crash sort — coerce None to +inf so runs without
    # an eval_id sort last instead of raising TypeError. 注意判 None 而非 falsy：
    # 合法的 eval_id=0 不能被当成缺失。
    def _eid(r: dict) -> float:
        v = r.get("eval_id")
        return float("inf") if v is None else float(v)

    def _id_natural_key(s: str) -> list:
        # 字符串 id 内数字段按数值排（run-10 在 run-2 后），与 aggregate 的
        # _natural_sort_key 同口径；否则 viewer 顺序与 benchmark 汇总错位。
        # isascii 先行：'²'.isdigit() 为 True 但 int() 拒收，会抛 ValueError
        return [int(p) if p.isascii() and p.isdigit() else p for p in re.split(r"(\d+)", s)]

    runs.sort(key=lambda r: (r.get("eval_id") is None, _eid(r), _id_natural_key(r["id"])))
    return runs


def _find_runs_recursive(root: Path, current: Path, runs: list[dict]) -> None:
    if not current.is_dir():
        return

    outputs_dir = current / "outputs"
    grading_file = current / "grading.json"
    if outputs_dir.is_dir() or grading_file.is_file():
        run = build_run(root, current)
        if run:
            runs.append(run)
        return

    skip = {"node_modules", ".git", "__pycache__", "skill", "inputs"}
    for child in sorted(current.iterdir()):
        if child.is_dir() and child.name not in skip:
            _find_runs_recursive(root, child, runs)


def build_run(root: Path, run_dir: Path) -> dict | None:
    """Build a run dict with prompt, outputs, and grading data."""
    prompt = ""
    eval_id = None

    # Try eval_metadata.json: run dir, config dir, eval dir (up to three levels)
    # 逐级回退时只补缺失字段，不拿后来者的 null 覆盖已取到的值
    for candidate in [run_dir / "eval_metadata.json", run_dir.parent / "eval_metadata.json", run_dir.parent.parent / "eval_metadata.json"]:
        metadata = _load_json_optional(candidate)
        if metadata:
            if not prompt:
                prompt = metadata.get("prompt", "") or ""
            if eval_id is None:
                eval_id = metadata.get("eval_id")
            if prompt and eval_id is not None:
                break

    # Fall back to transcript.md
    if not prompt:
        for candidate in [run_dir / "transcript.md", run_dir / "outputs" / "transcript.md"]:
            if candidate.exists():
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    match = re.search(r"## Eval Prompt\n\n([\s\S]*?)(?=\n##|$)", text)
                    if match:
                        prompt = match.group(1).strip()
                except OSError:
                    pass
                if prompt:
                    break

    if not prompt:
        prompt = "（未找到提示词）"

    run_id = str(run_dir.relative_to(root)).replace("/", "-").replace("\\", "-")

    # Collect output files
    outputs_dir = run_dir / "outputs"
    output_files: list[dict] = []
    if outputs_dir.is_dir():
        for f in sorted(outputs_dir.iterdir()):
            if f.is_file() and f.name not in METADATA_FILES:
                output_files.append(embed_file(f))

    # Load grading if present
    grading = None
    for candidate in [run_dir / "grading.json", run_dir.parent / "grading.json"]:
        grading = _load_json_optional(candidate)
        if grading:
            break

    return {
        "id": run_id,
        "prompt": prompt,
        "eval_id": eval_id,
        "outputs": output_files,
        "grading": grading,
    }


def embed_file(path: Path) -> dict:
    """Read a file and return an embedded representation."""
    ext = path.suffix.lower()
    mime = get_mime_type(path)

    if ext in TEXT_EXTENSIONS:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = "(Error reading file)"
        return {
            "name": path.name,
            "type": "text",
            "content": content,
        }

    b64 = _b64_file(path)
    if b64 is None:
        return {"name": path.name, "type": "error", "content": "(Error reading file)"}

    if ext in IMAGE_EXTENSIONS:
        return {"name": path.name, "type": "image", "mime": mime, "data_uri": f"data:{mime};base64,{b64}"}
    if ext == ".pdf":
        return {"name": path.name, "type": "pdf", "mime": mime, "data_uri": f"data:{mime};base64,{b64}"}
    if ext == ".xlsx":
        return {"name": path.name, "type": "xlsx", "data_b64": b64}
    # Binary / unknown — base64 download link
    return {"name": path.name, "type": "binary", "mime": mime, "data_uri": f"data:{mime};base64,{b64}"}


def load_previous_iteration(workspace: Path) -> dict[str, dict]:
    """Load previous iteration's feedback and outputs.

    Returns a map of run_id -> {"feedback": str, "outputs": list[dict]}.
    """
    result: dict[str, dict] = {}

    # Load feedback
    feedback_map: dict[str, str] = {}
    data = _load_json_optional(workspace / "feedback.json")
    if data:
        feedback_map = {
            r["run_id"]: r["feedback"]
            for r in data.get("reviews", [])
            if r.get("feedback", "").strip()
        }

    # Load runs (to get outputs)
    prev_runs = find_runs(workspace)
    for run in prev_runs:
        result[run["id"]] = {
            "feedback": feedback_map.get(run["id"], ""),
            "outputs": run.get("outputs", []),
        }

    # Also add feedback for run_ids that had feedback but no matching run
    for run_id, fb in feedback_map.items():
        if run_id not in result:
            result[run_id] = {"feedback": fb, "outputs": []}

    return result


def generate_html(
    runs: list[dict],
    skill_name: str,
    previous: dict[str, dict] | None = None,
    benchmark: dict | None = None,
    trigger_report: dict | None = None,
) -> str:
    """Generate the complete standalone HTML page with embedded data."""
    template_path = Path(__file__).parent.parent / "assets" / "viewer.html"
    template = template_path.read_text(encoding="utf-8")

    # Build previous_feedback and previous_outputs maps for the template
    previous_feedback: dict[str, str] = {}
    previous_outputs: dict[str, list[dict]] = {}
    if previous:
        for run_id, data in previous.items():
            if data.get("feedback"):
                previous_feedback[run_id] = data["feedback"]
            if data.get("outputs"):
                previous_outputs[run_id] = data["outputs"]

    embedded = {
        "skill_name": skill_name,
        "runs": runs,
        "previous_feedback": previous_feedback,
        "previous_outputs": previous_outputs,
    }
    if benchmark:
        embedded["benchmark"] = benchmark
    if trigger_report:
        embedded["trigger_report"] = trigger_report

    data_json = json.dumps(embedded, ensure_ascii=False)
    # 防 </script> 提前截断脚本块（嵌入数据含用户可控文本，json.dumps 默认不转义 /）
    data_json = data_json.replace("</", "<\\/")

    return template.replace("/*__EMBEDDED_DATA__*/", f"const EMBEDDED_DATA = {data_json};")


# ---------------------------------------------------------------------------
# HTTP server (stdlib only, zero dependencies)
# ---------------------------------------------------------------------------

def _kill_port(port: int) -> None:
    """Kill a process listening on the port — 仅限本脚本自己起的 viewer（防误杀无关服务）."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN", "-P", "-n"],
            capture_output=True, text=True, timeout=5,
        )
        for pid_str in result.stdout.strip().split("\n"):
            pid = pid_str.strip()
            if not pid:
                continue
            try:
                # 确认进程命令行里确实是本技能的 viewer，才动手；不匹配
                # "hermes"——那会命中用户机器上所有 Hermes 相关进程
                cmd_check = subprocess.run(
                    ["ps", "-p", pid, "-o", "command="],
                    capture_output=True, text=True, timeout=5,
                )
                cmd_line = cmd_check.stdout
                if "generate_review" in cmd_line:
                    os.kill(int(pid), signal.SIGTERM)
                    time.sleep(0.5)
            except (ProcessLookupError, ValueError, PermissionError, subprocess.TimeoutExpired):
                pass
    except subprocess.TimeoutExpired:
        pass
    except FileNotFoundError:
        print("Note: lsof not found, cannot check if port is in use", file=sys.stderr)

class ReviewHandler(BaseHTTPRequestHandler):
    """Serves the review HTML and handles feedback saves.

    Regenerates the HTML on each page load so that refreshing the browser
    picks up new eval outputs without restarting the server.
    """

    def __init__(
        self,
        workspace: Path,
        skill_name: str,
        feedback_path: Path,
        previous: dict[str, dict],
        benchmark_path: Path | None,
        trigger_path: Path | None = None,
        *args,
        **kwargs,
    ):
        self.workspace = workspace
        self.skill_name = skill_name
        self.feedback_path = feedback_path
        self.previous = previous
        self.benchmark_path = benchmark_path
        self.trigger_path = trigger_path
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            # Regenerate HTML on each request (re-scans workspace for new outputs)
            runs = find_runs(self.workspace)
            benchmark = _load_json_optional(self.benchmark_path) if self.benchmark_path else None
            trigger_report = _load_json_optional(self.trigger_path) if self.trigger_path else None
            html = generate_html(runs, self.skill_name, self.previous, benchmark, trigger_report)
            content = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/ping":
            # 页面心跳：viewer.html 每 5s 请求一次，服务器据此判活
            self.server.last_heartbeat = time.time()  # type: ignore[attr-defined]
            resp = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        elif self.path == "/api/feedback":
            data = b"{}"
            if self.feedback_path.exists():
                data = self.feedback_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/feedback":
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                if not isinstance(data, dict) or "reviews" not in data:
                    raise ValueError("Expected JSON object with 'reviews' key")
                # 原子写：临时文件 + os.replace，进程被杀不留半截 feedback.json
                import tempfile
                fd, tmp = tempfile.mkstemp(dir=str(self.feedback_path.parent), prefix=".feedback_", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(json.dumps(data, indent=2) + "\n")
                    os.replace(tmp, self.feedback_path)
                except OSError:
                    # 写失败（如磁盘满）不留 .feedback_*.tmp 垃圾
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
                resp = b'{"ok":true}'
                self.send_response(200)
            except (json.JSONDecodeError, OSError, ValueError) as e:
                resp = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        # Suppress request logging to keep terminal clean
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and serve eval review")
    parser.add_argument("workspace", type=Path, help="Path to workspace directory")
    parser.add_argument("--port", "-p", type=int, default=3117, help="Server port (default: 3117)")
    parser.add_argument("--skill-name", "-n", type=str, default=None, help="Skill name for header")
    parser.add_argument(
        "--previous-workspace", type=Path, default=None,
        help="Path to previous iteration's workspace (shows old outputs and feedback as context)",
    )
    parser.add_argument(
        "--benchmark", type=Path, default=None,
        help="Path to benchmark.json to show in the Benchmark tab",
    )
    parser.add_argument(
        "--trigger-report", type=Path, default=None,
        help="Path to run_loop.py output JSON (description optimization results) to show in the Trigger tab",
    )
    parser.add_argument(
        "--idle-timeout", type=int, default=30,
        help="Server self-exits after N seconds without a page heartbeat (0=never). Default: 30",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="Do not auto-open the browser (restart/silent scenarios; user refreshes the page themselves)",
    )
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"Error: {workspace} is not a directory", file=sys.stderr)
        sys.exit(1)

    runs = find_runs(workspace)
    if not runs:
        print(f"No runs found in {workspace}", file=sys.stderr)
        sys.exit(1)

    skill_name = args.skill_name or workspace.name.replace("-workspace", "")
    feedback_path = workspace / "feedback.json"

    previous: dict[str, dict] = {}
    if args.previous_workspace:
        previous = load_previous_iteration(args.previous_workspace.resolve())

    benchmark_path = args.benchmark.resolve() if args.benchmark else None
    benchmark = _load_json_optional(benchmark_path) if benchmark_path else None

    trigger_path = args.trigger_report.resolve() if args.trigger_report else None
    trigger_report = _load_json_optional(trigger_path) if trigger_path else None

    # Kill any existing process on the target port
    port = args.port
    _kill_port(port)
    handler = partial(ReviewHandler, workspace, skill_name, feedback_path, previous, benchmark_path, trigger_path)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        # Port still in use after kill attempt — find a free one
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]

    url = f"http://localhost:{port}"
    print(f"\n  Eval Viewer")
    print(f"  ─────────────────────────────────")
    print(f"  URL:       {url}")
    print(f"  Workspace: {workspace}")
    print(f"  Feedback:  {feedback_path}")
    if previous:
        print(f"  Previous:  {args.previous_workspace} ({len(previous)} runs)")
    if benchmark_path:
        print(f"  Benchmark: {benchmark_path}")
    print(f"\n  Press Ctrl+C to stop.\n")

    if not args.no_open:
        webbrowser.open(url)

    if args.idle_timeout > 0:
        # 心跳判活：服务器在后台线程伺服请求；主线程监护——页面开着每 5s 有 /ping，
        # 页面关闭后超过 idle_timeout 无心跳即自动退出释放
        import threading
        server.last_heartbeat = time.time()  # type: ignore[attr-defined]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            while True:
                time.sleep(1)
                if time.time() - server.last_heartbeat > args.idle_timeout:  # type: ignore[attr-defined]
                    print(f"\nIdle {args.idle_timeout}s without page heartbeat — viewer released.")
                    break
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            server.server_close()
    else:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
            server.server_close()


if __name__ == "__main__":
    main()

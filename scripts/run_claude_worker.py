#!/usr/bin/env python3
"""Run a read-only Claude Code CLI worker and normalize its JSON result."""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO


WORKER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["done", "needs_user_input", "blocked"]},
        "questions": {"type": "array", "items": {"type": "string"}},
        "findings": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "string"},
    },
    "required": [
        "status",
        "questions",
        "findings",
        "evidence",
        "risks",
        "recommendation",
    ],
}


REQUIRED_KEYS = tuple(WORKER_SCHEMA["required"])
ALLOWED_STATUSES = frozenset(WORKER_SCHEMA["properties"]["status"]["enum"])

READ_ONLY_BASH_ALLOWLIST = (
    "Bash(git status)",
    "Bash(git status --short)",
    "Bash(git status --porcelain)",
    "Bash(git diff)",
    "Bash(git diff --stat)",
    "Bash(git diff --name-only)",
    "Bash(git rev-parse --show-toplevel)",
    "Bash(git rev-parse --abbrev-ref HEAD)",
    "Bash(git ls-files)",
    "Bash(git remote -v)",
    "Bash(git branch --show-current)",
    "Bash(pwd)",
    "Bash(whoami)",
    "Bash(id)",
    "Bash(uname -a)",
    "Bash(date)",
    "Bash(hostname)",
)

MUTATING_OR_UNSCOPED_BASH_DENYLIST = (
    "Bash(git add)",
    "Bash(git add *)",
    "Bash(git commit)",
    "Bash(git commit *)",
    "Bash(git push)",
    "Bash(git push *)",
    "Bash(git merge)",
    "Bash(git merge *)",
    "Bash(git rebase)",
    "Bash(git rebase *)",
    "Bash(git checkout)",
    "Bash(git checkout *)",
    "Bash(git switch)",
    "Bash(git switch *)",
    "Bash(git reset)",
    "Bash(git reset *)",
    "Bash(git stash)",
    "Bash(git stash *)",
    "Bash(git cherry-pick)",
    "Bash(git cherry-pick *)",
    "Bash(git tag)",
    "Bash(git tag *)",
    "Bash(git apply)",
    "Bash(git apply *)",
    "Bash(git restore)",
    "Bash(git restore *)",
    "Bash(git update-ref)",
    "Bash(git update-ref *)",
    "Bash(git fast-import)",
    "Bash(git fast-import *)",
    "Bash(git replace)",
    "Bash(git replace *)",
    "Bash(git notes)",
    "Bash(git notes *)",
    "Bash(git filter-branch)",
    "Bash(git filter-branch *)",
    "Bash(rm *)",
    "Bash(mv *)",
    "Bash(cp *)",
    "Bash(mkdir *)",
    "Bash(touch *)",
    "Bash(chmod *)",
    "Bash(chown *)",
    "Bash(python3 -c *)",
    "Bash(python -c *)",
    "Bash(node -e *)",
    "Bash(perl -e *)",
    "Bash(ruby -e *)",
    "Bash(python3 *)",
    "Bash(python *)",
    "Bash(node *)",
    "Bash(perl *)",
    "Bash(ruby *)",
    "Bash(make)",
    "Bash(make *)",
    "Bash(cargo *)",
    "Bash(npm run)",
    "Bash(npm run *)",
    "Bash(yarn *)",
    "Bash(pnpm *)",
    "Bash(curl *)",
    "Bash(wget *)",
    "Bash(* > *)",
    "Bash(* >> *)",
    "Bash(* 2> *)",
    "Bash(* &> *)",
    "Bash(* | tee *)",
    "Bash(* | tee -a *)",
    "Bash(sed -i *)",
    "Bash(find * -delete *)",
    "Bash(npm run format *)",
    "Bash(yarn format *)",
    "Bash(pnpm format *)",
    "Bash(ruff format *)",
    "Bash(black *)",
    "Bash(prettier --write *)",
)

BASE_ALLOWED_TOOLS = ("Read", "Grep", "Glob", "LS")
BASE_DISALLOWED_TOOLS = (
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
)

BROAD_ADD_DIRS = {
    Path("/").resolve(),
    Path.home().resolve(),
    Path("/Users").resolve(),
    Path.home().parent.resolve(),
}


SYSTEM_PROMPT = """You are the Claude worker in a With Claude v1 workflow.

Return only structured analysis/review/planning output. You are read-only:
- You may read files, inspect directories, and search text.
- Bash is disabled by default. If the host agent explicitly enabled it, use only the
  narrow allowed commands and do not assume unmatched Bash commands are allowed.
- Do not edit files, write files, delete, move, rename, apply patches, run formatters
  that write files, commit, push, or perform release actions.
- Do not run arbitrary project scripts unless the host agent explicitly listed the
  exact command as read-only in the task packet.
- Do not use network commands, package installation commands, or arbitrary interpreter
  execution such as python -c or node -e.

If the task requires mutation, provide analysis or a plan and mark the limitation in risks.

Use the required JSON schema:
- status: done, needs_user_input, or blocked
- questions: user questions that are truly needed before a reliable answer
- findings: concise analysis or review findings
- evidence: exact file paths, symbols, logs, or facts that support findings
- risks: uncertainty, missing context, or safety concerns
- recommendation: final recommendation from the Claude pass
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Claude Code CLI as a read-only With Claude worker."
    )
    parser.add_argument("--cwd", default=os.getcwd(), help="Workspace for Claude.")
    parser.add_argument("--task", help="Task packet text. If omitted, stdin is used.")
    parser.add_argument("--task-file", help="Path to a task packet file.")
    parser.add_argument("--claude-bin", default="claude", help="Claude CLI binary.")
    parser.add_argument("--model", help="Optional Claude model or alias.")
    parser.add_argument(
        "--legacy-json-output",
        action="store_true",
        help="Use single JSON output instead of stream-json progress mode.",
    )
    parser.add_argument(
        "--include-partial-messages",
        action="store_true",
        help="Include partial message chunks in stream-json progress mode.",
    )
    parser.add_argument(
        "--progress-log-file",
        help="Optional debug JSONL file for incremental worker progress events.",
    )
    parser.add_argument(
        "--enable-read-only-bash",
        action="store_true",
        help=(
            "Opt into a narrow read-only Bash allowlist. Use only after confirming "
            "the Claude CLI default-denies unmatched Bash patterns."
        ),
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=30.0,
        help="Seconds between progress heartbeats in stream mode.",
    )
    parser.add_argument(
        "--worker-id",
        default="claude",
        help="Worker id written to progress events.",
    )
    parser.add_argument(
        "--add-dir",
        action="append",
        default=[],
        help="Additional read-only directory to expose to Claude; may be repeated.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Timeout in seconds for the Claude worker.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command and prompt without calling Claude.",
    )
    parser.add_argument(
        "--raw-log-file",
        help="Optional path for raw Claude stdout/stderr and command metadata.",
    )
    return parser.parse_args()


def read_task(args: argparse.Namespace) -> str:
    sources = [bool(args.task), bool(args.task_file)]
    if sum(sources) > 1:
        raise SystemExit("Use only one of --task or --task-file.")
    if args.task:
        return args.task
    if args.task_file:
        return Path(args.task_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("Provide --task, --task-file, or stdin.")


def build_prompt(task: str, cwd: str) -> str:
    return (
        "With Claude v1 task packet\n"
        f"Workspace: {cwd}\n\n"
        "Task:\n"
        f"{task.strip()}\n"
    )


def parse_task_json(task: str) -> dict[str, Any]:
    try:
        value = json.loads(task)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def task_add_dirs(task: str) -> list[str]:
    task_data = parse_task_json(task)
    dirs: list[str] = []
    for key in ("add_read_dirs", "readonly_extra_dirs", "read_only_dirs"):
        value = task_data.get(key)
        if isinstance(value, str):
            dirs.append(value)
        elif isinstance(value, list):
            dirs.extend(item for item in value if isinstance(item, str))
    return dirs


def resolve_add_dirs(cwd: str, dirs: list[str]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    cwd_path = Path(cwd).resolve()
    for item in dirs:
        path = Path(item).expanduser()
        if not path.is_absolute():
            path = cwd_path / path
        try:
            real_path = path.resolve()
        except OSError as exc:
            raise SystemExit(f"Invalid --add-dir path {item!r}: {exc}") from exc
        if real_path in BROAD_ADD_DIRS:
            raise SystemExit(f"Refusing broad --add-dir path: {real_path}")
        if not real_path.exists():
            raise SystemExit(f"--add-dir path does not exist: {real_path}")
        if not real_path.is_dir():
            raise SystemExit(f"--add-dir path is not a directory: {real_path}")
        text = str(real_path)
        if text not in seen:
            resolved.append(text)
            seen.add(text)
    return resolved


def build_command(args: argparse.Namespace, prompt: str) -> list[str]:
    claude_bin = shutil.which(args.claude_bin) or args.claude_bin
    legacy_json_output = getattr(args, "legacy_json_output", False)
    tools = [*BASE_ALLOWED_TOOLS]
    allowed_tools = [*BASE_ALLOWED_TOOLS]
    disallowed_tools = [*BASE_DISALLOWED_TOOLS]
    if getattr(args, "enable_read_only_bash", False):
        tools.append("Bash")
        allowed_tools.extend(READ_ONLY_BASH_ALLOWLIST)
        disallowed_tools.extend(MUTATING_OR_UNSCOPED_BASH_DENYLIST)
    else:
        disallowed_tools.append("Bash")
    command = [
        claude_bin,
        "-p",
        "--bare",
        "--output-format",
        "json" if legacy_json_output else "stream-json",
    ]
    if not legacy_json_output:
        command.append("--include-hook-events")
        command.append("--verbose")
        if getattr(args, "include_partial_messages", False):
            command.append("--include-partial-messages")
    command.extend([
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
        "--tools",
        ",".join(tools),
        "--allowedTools",
        ",".join(allowed_tools),
        "--disallowedTools",
        ",".join(disallowed_tools),
        "--system-prompt",
        SYSTEM_PROMPT,
        "--json-schema",
        json.dumps(WORKER_SCHEMA, ensure_ascii=False),
    ])
    for add_dir in getattr(args, "resolved_add_dirs", []):
        command.extend(["--add-dir", add_dir])
    if args.model:
        command.extend(["--model", args.model])
    command.append(prompt)
    return command


def blocked(reason: str, evidence: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": "blocked",
        "questions": [],
        "findings": [],
        "evidence": evidence or [],
        "risks": [reason],
        "recommendation": "Claude worker was unavailable; continue with the non-Claude pass and host-agent judgment.",
    }


def has_worker_contract(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) != set(REQUIRED_KEYS):
        return False
    if value["status"] not in ALLOWED_STATUSES:
        return False
    for key in ("questions", "findings", "evidence", "risks"):
        if not isinstance(value[key], list):
            return False
        if not all(isinstance(item, str) for item in value[key]):
            return False
    return isinstance(value["recommendation"], str)


def parse_json_value(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return value


def parse_claude_json(stdout: str) -> dict[str, Any]:
    parsed = json.loads(stdout)
    if has_worker_contract(parsed):
        return parsed

    if not isinstance(parsed, dict):
        return blocked(
            "Claude CLI returned JSON that was not an object.",
            [stdout[:4000]],
        )

    structured_output = parse_json_value(parsed.get("structured_output"))
    if has_worker_contract(structured_output):
        return structured_output

    result = parsed.get("result")
    parsed_result = parse_json_value(result)
    if has_worker_contract(parsed_result):
        return parsed_result

    if isinstance(result, str):
        result = result.strip()
        if not result:
            return blocked(
                "Claude CLI returned an empty result string and no structured_output worker contract.",
                [stdout[:4000]],
            )
        return blocked(
            "Claude returned plain text instead of the With Claude worker schema.",
            [result[:2000], stdout[:4000]],
        )

    return blocked(
        "Claude output did not match the With Claude worker schema.",
        [stdout[:4000]],
    )


def parse_stream_json(stdout: str) -> dict[str, Any]:
    final_contract: dict[str, Any] | None = None
    final_blocked: dict[str, Any] | None = None
    for line in stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            continue
        if has_worker_contract(event):
            final_contract = event
            continue
        if isinstance(event, dict) and (
            event.get("type") == "result"
            or "structured_output" in event
            or "result" in event
        ):
            parsed = parse_claude_json(json.dumps(event, ensure_ascii=False))
            if parsed["status"] != "blocked":
                final_contract = parsed
                continue
            final_blocked = parsed
    return final_contract or final_blocked or blocked(
        "Claude stream did not include a schema-conforming result event.",
        [stdout[-4000:]],
    )


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def truncate_value(value: Any, limit: int = 240) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def append_progress_event(
    progress_log_file: str | None,
    *,
    worker_id: str,
    event: str,
    data: dict[str, Any] | None = None,
    progress_stream: TextIO | None = None,
) -> None:
    payload = {
        "ts": utc_timestamp(),
        "worker_id": worker_id,
        "event": event,
        **(data or {}),
    }
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    stream = progress_stream if progress_stream is not None else sys.stderr
    stream.write(line)
    stream.flush()
    if progress_log_file:
        path = Path(progress_log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def display_target(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.rstrip("/")
    if not text:
        return value
    return text.rsplit("/", 1)[-1]


def tool_input_value(tool_input: Any, *keys: str) -> Any:
    if not isinstance(tool_input, dict):
        return None
    for key in keys:
        value = tool_input.get(key)
        if value:
            return value
    return None


def summarize_tool_use(tool_name: str, tool_input: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_input_preview": truncate_value(tool_input),
    }
    if tool_name == "Read":
        target = tool_input_value(tool_input, "file_path", "path")
        payload["target"] = target
        payload["summary"] = f"Claude is reading {display_target(target) or 'a file'}"
    elif tool_name == "Grep":
        pattern = tool_input_value(tool_input, "pattern", "query")
        target = tool_input_value(tool_input, "path", "glob")
        payload["search"] = pattern
        payload["target"] = target
        if target:
            payload["summary"] = (
                f"Claude is searching {truncate_value(pattern, 80)} in "
                f"{display_target(target) or target}"
            )
        else:
            payload["summary"] = f"Claude is searching {truncate_value(pattern, 80)}"
    elif tool_name == "Glob":
        pattern = tool_input_value(tool_input, "pattern")
        target = tool_input_value(tool_input, "path")
        payload["search"] = pattern
        payload["target"] = target
        payload["summary"] = f"Claude is listing matches for {truncate_value(pattern, 80)}"
    elif tool_name == "LS":
        target = tool_input_value(tool_input, "path")
        payload["target"] = target
        payload["summary"] = f"Claude is listing {display_target(target) or target or 'a directory'}"
    elif tool_name == "Bash":
        command = tool_input_value(tool_input, "command") or tool_input
        payload["command"] = command
        payload["summary"] = f"Claude is running read-only Bash: {truncate_value(command, 120)}"
    else:
        payload["summary"] = f"Claude used {tool_name}"
    return payload


def iter_tool_uses(event: dict[str, Any]) -> list[dict[str, Any]]:
    tool_uses: list[dict[str, Any]] = []
    tool_name = event.get("tool_name")
    if isinstance(tool_name, str):
        tool_uses.append({"name": tool_name, "input": event.get("tool_input", {})})

    if event.get("type") == "tool_use":
        name = event.get("name")
        if isinstance(name, str):
            tool_uses.append({"name": name, "input": event.get("input", {})})

    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                name = item.get("name")
                if isinstance(name, str):
                    tool_uses.append({"name": name, "input": item.get("input", {})})
    return tool_uses


def progress_events_from_claude_event(
    state: dict[str, Any],
    event: dict[str, Any],
    start_time: float,
) -> list[tuple[str, dict[str, Any]]]:
    snapshot = progress_snapshot(state, start_time)
    event_type = event.get("type")
    progress_events: list[tuple[str, dict[str, Any]]] = []

    for tool_use in iter_tool_uses(event):
        name = tool_use["name"]
        tool_input = tool_use.get("input", {})
        data = {
            **snapshot,
            "source_event_type": event_type,
            **summarize_tool_use(name, tool_input),
        }
        progress_events.append(("tool", data))

    permission_denials = event.get("permission_denials")
    if isinstance(permission_denials, list):
        for denial in permission_denials:
            data = {
                **snapshot,
                "source_event_type": event_type,
                "summary": "Claude hit a permission denial",
                "denial": truncate_value(denial),
            }
            progress_events.append(("permission_denial", data))

    if event_type == "result":
        progress_events.append((
            "result",
            {
                **snapshot,
                "source_event_type": event_type,
                "summary": "Claude produced a final result event",
            },
        ))
    return progress_events


def update_progress_state(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    state["last_event_at"] = utc_timestamp()
    event_type = event.get("type")
    if isinstance(event_type, str):
        state["last_event_type"] = event_type

    if event_type == "result":
        state["state"] = "finishing"
        for key in ("duration_ms", "duration_api_ms", "num_turns", "total_cost_usd"):
            if key in event:
                state[key] = event[key]
        permission_denials = event.get("permission_denials")
        if isinstance(permission_denials, list):
            state["permission_denials"] = len(permission_denials)
        usage = event.get("usage")
        if isinstance(usage, dict):
            for key in ("input_tokens", "cache_read_input_tokens", "output_tokens"):
                if key in usage:
                    state[key] = usage[key]

    tool_name = event.get("tool_name")
    if isinstance(tool_name, str):
        state["last_tool_name"] = tool_name
        state["last_tool_input_preview"] = truncate_value(event.get("tool_input", ""))
        state["last_summary"] = summarize_tool_use(
            tool_name,
            event.get("tool_input", {}),
        )["summary"]
    elif event_type == "tool_use":
        name = event.get("name")
        if isinstance(name, str):
            state["last_tool_name"] = name
            state["last_tool_input_preview"] = truncate_value(event.get("input", ""))
            state["last_summary"] = summarize_tool_use(
                name,
                event.get("input", {}),
            )["summary"]
    for tool_use in iter_tool_uses(event):
        name = tool_use["name"]
        tool_input = tool_use.get("input", {})
        state["last_tool_name"] = name
        state["last_tool_input_preview"] = truncate_value(tool_input)
        state["last_summary"] = summarize_tool_use(name, tool_input)["summary"]
    return state


def progress_snapshot(state: dict[str, Any], start_time: float) -> dict[str, Any]:
    snapshot = dict(state)
    snapshot["elapsed_sec"] = round(time.time() - start_time, 1)
    return snapshot


def enqueue_lines(
    stream: Any,
    source: str,
    output_queue: "queue.Queue[tuple[str, str]]",
) -> None:
    try:
        for line in iter(stream.readline, ""):
            output_queue.put((source, line))
    finally:
        stream.close()


def run_streaming_command(
    args: argparse.Namespace,
    *,
    cwd: str,
    command: list[str],
    prompt: str,
) -> subprocess.CompletedProcess[str]:
    start_time = time.time()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    progress_state: dict[str, Any] = {
        "state": "running",
        "pid": None,
        "started_at": utc_timestamp(),
        "last_event_at": None,
    }

    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
    except FileNotFoundError:
        raise

    progress_state["pid"] = process.pid
    append_progress_event(
        args.progress_log_file,
        worker_id=args.worker_id,
        event="started",
        data={
            **progress_snapshot(progress_state, start_time),
            "summary": "Claude worker started",
        },
        progress_stream=getattr(args, "progress_stream", None),
    )

    output_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
    threads = [
        threading.Thread(
            target=enqueue_lines,
            args=(process.stdout, "stdout", output_queue),
            daemon=True,
        ),
        threading.Thread(
            target=enqueue_lines,
            args=(process.stderr, "stderr", output_queue),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    last_heartbeat = time.time()
    timed_out = False
    while True:
        if time.time() - start_time > args.timeout and process.poll() is None:
            timed_out = True
            progress_state["state"] = "timeout"
            append_progress_event(
                args.progress_log_file,
                worker_id=args.worker_id,
                event="timeout",
                data={
                    **progress_snapshot(progress_state, start_time),
                    "summary": "Claude worker timed out",
                },
                progress_stream=getattr(args, "progress_stream", None),
            )
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            break

        try:
            source, line = output_queue.get(timeout=0.2)
        except queue.Empty:
            source = line = ""

        if source == "stdout":
            stdout_lines.append(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = None
            if isinstance(event, dict):
                progress_state = update_progress_state(progress_state, event)
                for progress_event, data in progress_events_from_claude_event(
                    progress_state,
                    event,
                    start_time,
                ):
                    append_progress_event(
                        args.progress_log_file,
                        worker_id=args.worker_id,
                        event=progress_event,
                        data=data,
                        progress_stream=getattr(args, "progress_stream", None),
                    )
        elif source == "stderr":
            stderr_lines.append(line)
            if line.strip():
                append_progress_event(
                    args.progress_log_file,
                    worker_id=args.worker_id,
                    event="stderr",
                    data={
                        **progress_snapshot(progress_state, start_time),
                        "summary": "Claude wrote to stderr",
                        "preview": truncate_value(line),
                    },
                    progress_stream=getattr(args, "progress_stream", None),
                )

        if time.time() - last_heartbeat >= args.heartbeat_interval:
            append_progress_event(
                args.progress_log_file,
                worker_id=args.worker_id,
                event="heartbeat",
                data={
                    **progress_snapshot(progress_state, start_time),
                    "summary": progress_state.get(
                        "last_summary",
                        "Claude worker is still running",
                    ),
                },
                progress_stream=getattr(args, "progress_stream", None),
            )
            last_heartbeat = time.time()

        if process.poll() is not None and output_queue.empty():
            if not any(thread.is_alive() for thread in threads):
                break

    for thread in threads:
        thread.join(timeout=1)

    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
    returncode = process.returncode if process.returncode is not None else 124
    if timed_out:
        returncode = 124
        stderr = (stderr + "\nClaude worker timed out.").strip()

    progress_state["state"] = "done" if returncode == 0 else "blocked"
    append_progress_event(
        args.progress_log_file,
        worker_id=args.worker_id,
        event="final",
        data={
            **progress_snapshot(progress_state, start_time),
            "summary": (
                "Claude worker finished"
                if returncode == 0
                else "Claude worker finished with an error"
            ),
        },
        progress_stream=getattr(args, "progress_stream", None),
    )
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def write_raw_log(
    raw_log_file: str | None,
    *,
    cwd: str,
    command: list[str],
    prompt: str,
    completed: subprocess.CompletedProcess[str],
) -> None:
    if not raw_log_file:
        return
    log_path = Path(raw_log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(
            {
                "cwd": cwd,
                "command": command[:-1] + ["<prompt>"],
                "prompt": prompt,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    cwd = str(Path(args.cwd).expanduser().resolve())
    task = read_task(args)
    args.resolved_add_dirs = resolve_add_dirs(
        cwd,
        [*args.add_dir, *task_add_dirs(task)],
    )
    prompt = build_prompt(task, cwd)
    command = build_command(args, prompt)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "cwd": cwd,
                    "command": command[:-1] + ["<prompt>"],
                    "prompt": prompt,
                    "schema": WORKER_SCHEMA,
                    "progress_log_file": args.progress_log_file,
                    "resolved_add_dirs": args.resolved_add_dirs,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    try:
        if args.legacy_json_output:
            append_progress_event(
                args.progress_log_file,
                worker_id=args.worker_id,
                event="started",
                data={
                    "state": "running",
                    "mode": "legacy-json",
                    "summary": "Claude worker started in legacy JSON mode",
                },
            )
            completed = subprocess.run(
                command,
                cwd=cwd,
                text=True,
                capture_output=True,
                timeout=args.timeout,
                check=False,
            )
            append_progress_event(
                args.progress_log_file,
                worker_id=args.worker_id,
                event="final",
                data={
                    "state": "done" if completed.returncode == 0 else "blocked",
                    "summary": "Claude worker finished",
                },
            )
        else:
            completed = run_streaming_command(
                args,
                cwd=cwd,
                command=command,
                prompt=prompt,
            )
    except FileNotFoundError:
        print(json.dumps(blocked("Claude CLI was not found on PATH."), ensure_ascii=False))
        return 0
    except subprocess.TimeoutExpired:
        print(json.dumps(blocked("Claude worker timed out."), ensure_ascii=False))
        return 0

    if completed.returncode != 0:
        write_raw_log(
            args.raw_log_file,
            cwd=cwd,
            command=command,
            prompt=prompt,
            completed=completed,
        )
        evidence = []
        if completed.stderr:
            evidence.append(completed.stderr[-2000:])
        if completed.stdout:
            evidence.append(completed.stdout[-2000:])
        print(
            json.dumps(
                blocked(f"Claude CLI exited with code {completed.returncode}.", evidence),
                ensure_ascii=False,
            )
        )
        return 0

    write_raw_log(
        args.raw_log_file,
        cwd=cwd,
        command=command,
        prompt=prompt,
        completed=completed,
    )

    try:
        if args.legacy_json_output:
            normalized = parse_claude_json(completed.stdout)
        else:
            normalized = parse_stream_json(completed.stdout)
    except json.JSONDecodeError:
        normalized = blocked(
            "Claude CLI did not return valid JSON.",
            [completed.stdout[:2000]],
        )

    print(json.dumps(normalized, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

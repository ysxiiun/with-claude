#!/usr/bin/env python3
"""Run a read-only Claude Code CLI worker and normalize its JSON result."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


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


SYSTEM_PROMPT = """You are the Claude worker in a With Claude v1 workflow.

Return only structured analysis/review/planning output. Do not edit files, write files,
apply patches, run formatters, commit, push, or perform release actions. If the task
requires mutation, provide analysis or a plan and mark the limitation in risks.

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


def build_command(args: argparse.Namespace, prompt: str) -> list[str]:
    claude_bin = shutil.which(args.claude_bin) or args.claude_bin
    command = [
        claude_bin,
        "-p",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Grep,Glob,LS",
        "--disallowedTools",
        "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch",
        "--system-prompt",
        SYSTEM_PROMPT,
        "--json-schema",
        json.dumps(WORKER_SCHEMA, ensure_ascii=False),
    ]
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
        "recommendation": "Claude worker was unavailable; continue with the Codex pass and main-agent judgment.",
    }


def parse_claude_json(stdout: str) -> dict[str, Any]:
    parsed = json.loads(stdout)
    if all(key in parsed for key in WORKER_SCHEMA["required"]):
        return parsed

    result = parsed.get("result")
    if isinstance(result, str):
        result = result.strip()
        try:
            nested = json.loads(result)
        except json.JSONDecodeError:
            return {
                "status": "done",
                "questions": [],
                "findings": [result],
                "evidence": [],
                "risks": ["Claude returned JSON wrapper output but the result field was plain text."],
                "recommendation": result,
            }
        if all(key in nested for key in WORKER_SCHEMA["required"]):
            return nested

    return blocked(
        "Claude output did not match the With Claude worker schema.",
        [stdout[:2000]],
    )


def main() -> int:
    args = parse_args()
    cwd = str(Path(args.cwd).expanduser().resolve())
    task = read_task(args)
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
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=args.timeout,
            check=False,
        )
    except FileNotFoundError:
        print(json.dumps(blocked("Claude CLI was not found on PATH."), ensure_ascii=False))
        return 0
    except subprocess.TimeoutExpired:
        print(json.dumps(blocked("Claude worker timed out."), ensure_ascii=False))
        return 0

    if completed.returncode != 0:
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

    try:
        normalized = parse_claude_json(completed.stdout)
    except json.JSONDecodeError:
        normalized = blocked(
            "Claude CLI did not return valid JSON.",
            [completed.stdout[:2000]],
        )

    print(json.dumps(normalized, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Regression tests for With Claude worker result normalization."""

from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_claude_worker as worker  # noqa: E402


def worker_contract(**overrides: object) -> dict[str, object]:
    contract: dict[str, object] = {
        "status": "done",
        "questions": [],
        "findings": ["contract result"],
        "evidence": ["some/file.py:1"],
        "risks": [],
        "recommendation": "Use structured_output.",
    }
    contract.update(overrides)
    return contract


def build_args(**overrides: object) -> Namespace:
    args = Namespace(
        claude_bin="claude",
        model=None,
        legacy_json_output=False,
        include_partial_messages=False,
        enable_read_only_bash=False,
        resolved_add_dirs=[],
        progress_log_file=None,
        progress_stream=io.StringIO(),
        worker_id="claude",
        heartbeat_interval=30.0,
        timeout=5,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class ParseClaudeJsonTest(unittest.TestCase):
    def test_prefers_structured_output_from_claude_wrapper(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "Human-readable summary.",
                "structured_output": worker_contract(),
            }
        )

        parsed = worker.parse_claude_json(stdout)

        self.assertEqual(parsed["status"], "done")
        self.assertEqual(parsed["findings"], ["contract result"])
        self.assertEqual(parsed["recommendation"], "Use structured_output.")

    def test_accepts_stringified_structured_output(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "result": "Human-readable summary.",
                "structured_output": json.dumps(
                    worker_contract(findings=["stringified structured output"])
                ),
            }
        )

        parsed = worker.parse_claude_json(stdout)

        self.assertEqual(parsed["status"], "done")
        self.assertEqual(parsed["findings"], ["stringified structured output"])

    def test_accepts_json_result_fallback(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "result": json.dumps(worker_contract(findings=["result fallback"])),
            }
        )

        parsed = worker.parse_claude_json(stdout)

        self.assertEqual(parsed["status"], "done")
        self.assertEqual(parsed["findings"], ["result fallback"])

    def test_invalid_schema_is_blocked(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "structured_output": worker_contract(status="success"),
                "result": "",
            }
        )

        parsed = worker.parse_claude_json(stdout)

        self.assertEqual(parsed["status"], "blocked")

    def test_non_object_json_is_blocked(self) -> None:
        stdout = json.dumps([worker_contract()])

        parsed = worker.parse_claude_json(stdout)

        self.assertEqual(parsed["status"], "blocked")
        self.assertIn("not an object", parsed["risks"][0])

    def test_plain_text_result_is_blocked(self) -> None:
        stdout = json.dumps({"type": "result", "result": "Plain text only."})

        parsed = worker.parse_claude_json(stdout)

        self.assertEqual(parsed["status"], "blocked")
        self.assertIn("plain text", parsed["risks"][0])

    def test_empty_result_is_blocked(self) -> None:
        stdout = json.dumps({"type": "result", "result": ""})

        parsed = worker.parse_claude_json(stdout)

        self.assertEqual(parsed["status"], "blocked")
        self.assertIn("empty result", parsed["risks"][0])


class ParseStreamJsonTest(unittest.TestCase):
    def test_stream_result_uses_structured_output(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps({"type": "assistant", "message": "working"}),
                json.dumps(
                    {
                        "type": "result",
                        "structured_output": worker_contract(
                            findings=["stream contract"]
                        ),
                    }
                ),
            ]
        )

        parsed = worker.parse_stream_json(stdout)

        self.assertEqual(parsed["status"], "done")
        self.assertEqual(parsed["findings"], ["stream contract"])

    def test_stream_without_result_is_blocked(self) -> None:
        stdout = json.dumps({"type": "assistant", "message": "no final"})

        parsed = worker.parse_stream_json(stdout)

        self.assertEqual(parsed["status"], "blocked")
        self.assertIn("stream", parsed["risks"][0])

    def test_stream_uses_last_valid_contract(self) -> None:
        stdout = "\n".join(
            [
                json.dumps(worker_contract(findings=["early"])),
                json.dumps(
                    {
                        "type": "result",
                        "structured_output": worker_contract(findings=["final"]),
                    }
                ),
            ]
        )

        parsed = worker.parse_stream_json(stdout)

        self.assertEqual(parsed["findings"], ["final"])


class BuildCommandTest(unittest.TestCase):
    def test_claude_command_uses_bare_and_has_no_budget_cap(self) -> None:
        args = build_args()

        command = worker.build_command(args, "task")

        self.assertIn("--bare", command)
        self.assertIn("stream-json", command)
        self.assertIn("--include-hook-events", command)
        self.assertIn("--verbose", command)
        self.assertNotIn("--max-budget-usd", command)

    def test_claude_command_disables_bash_by_default(self) -> None:
        args = build_args()

        command = worker.build_command(args, "task")
        tools = command[command.index("--tools") + 1].split(",")
        allowed = command[command.index("--allowedTools") + 1].split(",")
        disallowed = command[command.index("--disallowedTools") + 1].split(",")

        self.assertEqual(tools, ["Read", "Grep", "Glob", "LS"])
        self.assertIn("Read", allowed)
        self.assertIn("Grep", allowed)
        self.assertNotIn("Bash", tools)
        self.assertNotIn("Bash(git diff)", allowed)
        self.assertIn("Bash", disallowed)
        self.assertIn("Edit", disallowed)
        self.assertIn("Write", disallowed)

    def test_claude_command_can_opt_into_narrow_read_only_bash(self) -> None:
        args = build_args(enable_read_only_bash=True)

        command = worker.build_command(args, "task")
        tools = command[command.index("--tools") + 1].split(",")
        allowed = command[command.index("--allowedTools") + 1].split(",")
        disallowed = command[command.index("--disallowedTools") + 1].split(",")

        self.assertIn("Bash", tools)
        self.assertIn("Bash(git diff)", allowed)
        self.assertIn("Bash(git diff --stat)", allowed)
        self.assertIn("Bash(git status --short)", allowed)
        self.assertIn("Bash(pwd)", allowed)
        self.assertNotIn("Bash(rg *)", allowed)
        self.assertNotIn("Bash(find *)", allowed)
        self.assertNotIn("Bash(python3 --version)", allowed)
        self.assertNotIn("Bash(git push *)", allowed)
        self.assertNotIn("Bash", disallowed)
        self.assertIn("Edit", disallowed)
        self.assertIn("Write", disallowed)
        self.assertIn("Bash(git push *)", disallowed)
        self.assertIn("Bash(git apply *)", disallowed)
        self.assertIn("Bash(npm run *)", disallowed)
        self.assertIn("Bash(make *)", disallowed)
        self.assertIn("Bash(rm *)", disallowed)
        self.assertIn("Bash(python3 *)", disallowed)
        self.assertIn("Bash(curl *)", disallowed)
        self.assertIn("Bash(* > *)", disallowed)
        self.assertIn("Bash(* | tee *)", disallowed)
        self.assertIn("Bash(sed -i *)", disallowed)

    def test_claude_command_accepts_additional_read_dirs(self) -> None:
        args = build_args(resolved_add_dirs=["/tmp/example-read-root"])

        command = worker.build_command(args, "task")

        self.assertIn("--add-dir", command)
        self.assertIn("/tmp/example-read-root", command)

    def test_legacy_json_output_keeps_single_result_mode(self) -> None:
        args = build_args(legacy_json_output=True)

        command = worker.build_command(args, "task")

        self.assertIn("json", command)
        self.assertNotIn("stream-json", command)
        self.assertNotIn("--include-hook-events", command)
        self.assertNotIn("--verbose", command)


class AddDirValidationTest(unittest.TestCase):
    def test_task_add_dirs_reads_supported_keys(self) -> None:
        task = json.dumps(
            {
                "add_read_dirs": ["/tmp/a"],
                "readonly_extra_dirs": "/tmp/b",
                "read_only_dirs": ["/tmp/c", 1],
            }
        )

        self.assertEqual(
            worker.task_add_dirs(task),
            ["/tmp/a", "/tmp/b", "/tmp/c"],
        )

    def test_task_add_dirs_ignores_non_json_task(self) -> None:
        self.assertEqual(worker.task_add_dirs("plain task"), [])

    def test_resolve_add_dirs_rejects_home(self) -> None:
        with self.assertRaises(SystemExit):
            worker.resolve_add_dirs("/tmp", [str(Path.home())])

    def test_resolve_add_dirs_rejects_broad_roots(self) -> None:
        for path in ("/", "/Users"):
            with self.subTest(path=path):
                with self.assertRaises(SystemExit):
                    worker.resolve_add_dirs("/tmp", [path])

    def test_resolve_add_dirs_rejects_missing_and_file_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = str(Path(tmpdir) / "missing")
            file_path = Path(tmpdir) / "file.txt"
            file_path.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(SystemExit):
                worker.resolve_add_dirs("/tmp", [missing])
            with self.assertRaises(SystemExit):
                worker.resolve_add_dirs("/tmp", [str(file_path)])

    def test_resolve_add_dirs_accepts_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            resolved = worker.resolve_add_dirs("/tmp", [tmpdir])

        self.assertEqual(len(resolved), 1)


class ProgressLogTest(unittest.TestCase):
    def test_progress_event_defaults_to_stream_without_file(self) -> None:
        stream = io.StringIO()
        worker.append_progress_event(
            None,
            worker_id="claude",
            event="heartbeat",
            data={"state": "running"},
            progress_stream=stream,
        )

        payload = json.loads(stream.getvalue())

        self.assertEqual(payload["worker_id"], "claude")
        self.assertEqual(payload["event"], "heartbeat")
        self.assertEqual(payload["state"], "running")

    def test_progress_event_can_also_write_debug_jsonl_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            progress = Path(tmpdir) / "progress.jsonl"
            stream = io.StringIO()
            worker.append_progress_event(
                str(progress),
                worker_id="claude",
                event="heartbeat",
                data={"state": "running"},
                progress_stream=stream,
            )
            payload = json.loads(progress.read_text(encoding="utf-8"))
            stream_payload = json.loads(stream.getvalue())

        self.assertEqual(payload["worker_id"], "claude")
        self.assertEqual(payload["event"], "heartbeat")
        self.assertEqual(payload["state"], "running")
        self.assertEqual(stream_payload["event"], "heartbeat")

    def test_progress_summarizes_nested_tool_use(self) -> None:
        state = {
            "state": "running",
            "pid": 1,
            "started_at": worker.utc_timestamp(),
            "last_event_at": None,
        }
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/tmp/project/SKILL.md"},
                    }
                ]
            },
        }
        start = 0.0

        updated = worker.update_progress_state(state, event)
        progress_events = worker.progress_events_from_claude_event(updated, event, start)

        self.assertEqual(progress_events[0][0], "tool")
        self.assertEqual(progress_events[0][1]["tool_name"], "Read")
        self.assertEqual(progress_events[0][1]["target"], "/tmp/project/SKILL.md")
        self.assertIn("SKILL.md", progress_events[0][1]["summary"])

    def test_progress_reports_permission_denial_and_result(self) -> None:
        state = {
            "state": "running",
            "pid": 1,
            "started_at": worker.utc_timestamp(),
            "last_event_at": None,
        }
        event = {"type": "result", "permission_denials": [{"tool": "Bash"}]}

        updated = worker.update_progress_state(state, event)
        progress_events = worker.progress_events_from_claude_event(updated, event, 0.0)

        self.assertEqual(
            [event_name for event_name, _ in progress_events],
            ["permission_denial", "result"],
        )


class ReadTaskTest(unittest.TestCase):
    def test_read_task_defaults_to_stdin(self) -> None:
        args = Namespace(task=None, task_file=None)

        with patch("sys.stdin", io.StringIO("stdin task")):
            self.assertEqual(worker.read_task(args), "stdin task")

    def test_read_task_rejects_multiple_sources(self) -> None:
        args = Namespace(task="inline", task_file="task.json")

        with self.assertRaises(SystemExit):
            worker.read_task(args)


class StreamingCommandTest(unittest.TestCase):
    def test_run_streaming_command_emits_progress_to_stream(self) -> None:
        contract = json.dumps(worker_contract(findings=["streamed"]))
        code = (
            "import json\n"
            "print(json.dumps({'type':'assistant','message':{'content':["
            "{'type':'tool_use','name':'Read','input':{'file_path':'SKILL.md'}}"
            "]}}), flush=True)\n"
            f"print(json.dumps({{'type':'result','structured_output':{contract}}}), flush=True)\n"
        )
        progress = io.StringIO()
        args = build_args(
            progress_stream=progress,
            heartbeat_interval=60.0,
            timeout=5,
        )

        completed = worker.run_streaming_command(
            args,
            cwd=str(ROOT),
            command=[sys.executable, "-c", code],
            prompt="task",
        )
        progress_events = [
            json.loads(line)
            for line in progress.getvalue().splitlines()
            if line.strip()
        ]

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(worker.parse_stream_json(completed.stdout)["findings"], ["streamed"])
        self.assertEqual(completed.stderr, "")
        self.assertIn("started", [event["event"] for event in progress_events])
        self.assertIn("tool", [event["event"] for event in progress_events])
        self.assertIn("result", [event["event"] for event in progress_events])
        self.assertIn("final", [event["event"] for event in progress_events])


if __name__ == "__main__":
    unittest.main()

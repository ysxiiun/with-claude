#!/usr/bin/env python3
"""Regression tests for With Claude worker result normalization."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_claude_worker as worker  # noqa: E402


class ParseClaudeJsonTest(unittest.TestCase):
    def test_prefers_structured_output_from_claude_wrapper(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "Human-readable summary.",
                "structured_output": {
                    "status": "done",
                    "questions": [],
                    "findings": ["contract result"],
                    "evidence": ["some/file.py:1"],
                    "risks": [],
                    "recommendation": "Use structured_output.",
                },
            }
        )

        parsed = worker.parse_claude_json(stdout)

        self.assertEqual(parsed["status"], "done")
        self.assertEqual(parsed["findings"], ["contract result"])
        self.assertEqual(parsed["recommendation"], "Use structured_output.")

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


if __name__ == "__main__":
    unittest.main()

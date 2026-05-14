---
name: with-claude
description: Explicit-invocation-only workflow for using a Codex subagent and a Claude Code CLI worker as independent read-only analysts, then merging their analysis, review findings, or solution plans. Use only when the user explicitly writes `$with-claude`, `with-claude`, or clearly asks to load the With Claude skill; do not use for ordinary mentions of Claude, analysis, review, or planning.
---

# With Claude

Use this skill only after explicit user invocation. v1 is for dual-agent analysis, review, and plan merging. It is not a coding or file-editing workflow.

## Hard Boundaries

- Do not trigger this skill from semantic keywords such as Claude, review, analysis, or plan.
- Do not let either worker edit files, apply patches, run formatters, commit, push, or perform release actions.
- Treat implementation, patch generation, and workspace mutation as out of scope for v1.
- Keep the main agent responsible for user communication, worker orchestration, and final judgment.
- Do not set a Claude budget limit. The Claude worker should not stop because of a caller-imposed budget cap; only Claude CLI, account, or service refusal should block the Claude pass.

## Workflow

1. Build one shared task packet with:
   - user request
   - current working directory
   - relevant constraints and known context
   - expected output type: analysis, review, or plan
   - v1 safety note: no code mutation
2. Start two independent passes:
   - Codex subagent: ask for analysis, review, or plan only.
   - Claude worker: run `scripts/run_claude_worker.py` with the same task packet.
3. Require both workers to return the common contract:
   - `status`: `done`, `needs_user_input`, or `blocked`
   - `questions`
   - `findings`
   - `evidence`
   - `risks`
   - `recommendation`
4. If either worker needs input, merge and deduplicate questions before asking the user once.
5. Send the user's answer back to both workers when continuing the same task.
6. Produce the final answer from the main agent, not directly from either worker.

## Claude Worker

Use the bundled script instead of hand-writing a Claude command:

```bash
python3 -B scripts/run_claude_worker.py --cwd "$PWD" --task-file /path/to/task.json
```

The script calls Claude Code CLI non-interactively with JSON output and read-only tools by default. It does not pass `--max-budget-usd`. Claude Code CLI may return a human-readable `result` plus the schema-conforming worker payload in `structured_output`; the script must normalize `structured_output` as the authoritative worker contract. If the script reports `blocked`, continue with the Codex-only result and clearly state that the Claude pass failed or was unavailable.

For debugging Claude worker issues, capture the raw CLI wrapper:

```bash
python3 -B scripts/run_claude_worker.py --cwd "$PWD" --task-file /path/to/task.json --raw-log-file /tmp/with-claude-worker.json
```

## Merge Rules

- Lead with consensus when both workers agree.
- Call out disagreements explicitly and decide which side is better supported by evidence.
- Prefer local repo facts, exact file paths, logs, and command output over unsupported reasoning.
- If both workers miss a necessary user decision, ask the user before finalizing.
- Final output should include:
  - consensus conclusions
  - disagreements or gaps
  - main-agent judgment
  - final review findings, recommendation, or plan

## Safety Check

Before finalizing, verify that no worker was asked to mutate the repository. If the user asks for coding while using v1, respond with an analysis or implementation plan only and note that code changes require a later coding-capable mode.

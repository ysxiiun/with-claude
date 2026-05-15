---
name: with-claude
description: Explicit-invocation-only workflow for pairing the current host agent with a read-only Claude Code CLI worker, then merging Claude's analysis, review findings, or solution plans into the host agent's final judgment. Use only when the user explicitly writes `$with-claude`, `with-claude`, or clearly asks to load the With Claude skill; do not use for ordinary mentions of Claude, analysis, review, or planning.
---

# With Claude

Use this skill only after explicit user invocation. v1 is for analysis, review, and plan merging with Claude as a read-only advisor. It can be used by any host agent that can orchestrate workers.

## Hard Boundaries

- Do not trigger this skill from semantic keywords such as Claude, review, analysis, or plan.
- Claude Worker is always read-only. Do not let Claude edit files, apply patches, run formatters that write files, delete, move, rename, commit, push, or perform release actions.
- The current host agent and its delegated workers keep the permissions granted by the current session, mode, tools, and user approvals. This skill does not force them to be read-only.
- If the current task is analysis-only, ask delegated workers for analysis only. If the user authorizes implementation, the host-agent side may follow normal permission controls, while Claude remains read-only.
- Keep the host agent responsible for user communication, task packet design, worker orchestration, evidence comparison, and final judgment.
- Do not set a Claude budget limit. The Claude worker should not stop because of a caller-imposed budget cap; only Claude CLI, account, or service refusal should block the Claude pass.

## Workflow

1. Build one shared task packet with:
   - user request
   - current working directory
   - relevant constraints and known context
   - expected output type: analysis, review, or plan
   - v1 safety note: no code mutation
   - optional `add_read_dirs`: exact extra directories Claude may read via `--add-dir`
2. Start independent passes:
   - Delegated worker: use whatever independent worker mechanism the host agent has available, with permissions matching the current task and user authorization.
   - Claude worker: run `scripts/run_claude_worker.py` with the same task packet on stdin. Do not create a task packet file by default.
3. Require both workers to return the common contract:
   - `status`: `done`, `needs_user_input`, or `blocked`
   - `questions`
   - `findings`
   - `evidence`
   - `risks`
   - `recommendation`
4. If either worker needs input, merge and deduplicate questions before asking the user once.
5. Send the user's answer back to both workers when continuing the same task.
6. Produce the final answer from the host agent, not directly from either worker. The host agent should understand the global shape of the task, but should avoid doing a full duplicate deep dive that conflicts with or repeats delegated worker work.

## Progress Monitoring

- Claude runs can take minutes. The wrapper emits progress JSONL to stderr by default; stdout is reserved for the final worker contract.
- Host agents should read stderr progress events directly instead of requiring temporary progress files.
- Use `--progress-log-file /tmp/with-claude/<task-id>/claude.progress.jsonl` only as an explicit debug option when a durable progress copy is needed.
- The host agent should check Claude progress and delegated-worker state every 30-60 seconds and briefly update the user with elapsed time, latest event/tool summary, and whether either worker appears stalled.
- Progress summaries may describe visible tool actions such as `Read SKILL.md`, `Grep run_claude_worker`, or `LS scripts`; do not present them as Claude's private reasoning.
- If Claude has no new progress event for more than 180 seconds, do not assume it failed. Tell the user the process is still running but quiet, then keep waiting or ask whether to stop when appropriate.
- If the delegated worker cannot emit a progress log, report only its known state honestly, such as "still running; no detailed progress channel".

## Claude Worker

Use the bundled script instead of hand-writing a Claude command:

```bash
python3 -B scripts/run_claude_worker.py --cwd "$PWD" < task-packet-stdin
```

The script calls Claude Code CLI non-interactively with `stream-json`, `--bare`, and read-only tools. It keeps final stdout compatible with the common worker contract, while progress events are emitted as JSONL on stderr. It allows file reads, directory listing, globbing, and text search through `Read`, `LS`, `Glob`, and `Grep`. Bash is disabled by default.

Only enable Bash with `--enable-read-only-bash` after confirming the installed Claude Code CLI default-denies Bash commands that match neither `--allowedTools` nor `--disallowedTools`. When enabled, keep Bash on a narrow read-only allowlist and still deny mutation, arbitrary interpreter execution, network fetches, package installs, and formatter write-backs. Do not claim Bash-backed read-only safety before this default-deny behavior is confirmed. The script does not pass `--max-budget-usd`.

If Claude needs to inspect paths outside `--cwd`, list exact directories in the task packet as `add_read_dirs` or pass `--add-dir` explicitly. Never use broad roots such as `/`, `$HOME`, `/Users`, or `/Users/<user>`.

Do not allow arbitrary project scripts by default. If a task truly needs a read-only script, the host agent must list the exact command in the task packet and adjust the Claude worker command deliberately.

Claude Code CLI may return a human-readable `result` plus the schema-conforming worker payload in `structured_output`; the script must normalize `structured_output` as the authoritative worker contract. If the script reports `blocked`, continue with the non-Claude result and clearly state that the Claude pass failed or was unavailable.

For debugging Claude worker issues, capture optional files explicitly:

```bash
python3 -B scripts/run_claude_worker.py \
  --cwd "$PWD" \
  --progress-log-file /tmp/with-claude/task/claude.progress.jsonl \
  --raw-log-file /tmp/with-claude/task/raw-worker.json \
  < task-packet-stdin
```

## Merge Rules

- Lead with the host agent's overall conclusion.
- Summarize what Claude thought before discussing adoption.
- Explain which Claude points were adopted, partially adopted, or rejected.
- Call out disagreements only when they exist, and decide which side is better supported by evidence.
- Prefer local repo facts, exact file paths, logs, and command output over unsupported reasoning.
- If both workers miss a necessary user decision, ask the user before finalizing.
- Do not separately repeat what the current host agent thought. Its judgment should appear through the overall conclusion, adoption decisions, and conflict resolution.

Use this final-output template by default:

```md
## 总体结论

## Claude 观点简述

## 采纳情况
- 已采纳:
- 部分采纳:
- 未采纳:

## 与 Claude 的冲突点
```

Omit `## 与 Claude 的冲突点` when there is no meaningful conflict.

## Safety Check

Before finalizing, verify that Claude was not asked to mutate the repository. Also verify that any host-agent-side worker followed the current task's permission mode and user authorization.
Verify that any `add_read_dirs` are narrow and task-relevant. Summarize stalled periods, permission denials, and final elapsed time from progress events without dumping large source snippets or sensitive config. If debug progress or raw logs were written to files, mention that they were explicitly requested.

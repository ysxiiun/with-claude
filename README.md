# with-claude

`with-claude` 是一个显式调用的 Codex skill，用于让当前 host agent 与一个只读 Claude Code CLI worker 协作完成分析、Review 或方案判断。它的核心目标不是让 Claude 接管任务，而是把 Claude 作为独立参谋，引入第二视角，再由 host agent 负责最终判断、用户沟通和后续执行。

本 README 是入门导览；具体行为契约以 `SKILL.md` 为准。

## 目标

- 只有用户明确写出 `$with-claude`、`with-claude` 或明确要求加载 With Claude skill 时才启用。
- 用 Claude Code CLI 做只读分析，帮助发现遗漏、风险、分歧和替代方案。
- 让 host agent 合并 Claude 观点、其他 worker 观点和本地证据，输出最终结论。
- 在 Claude 长时间运行时提供可观测进度，让 host agent 能说明 Claude 正在读文件、搜索或列目录。
- 默认不生成临时 task/progress 文件，减少写权限确认和临时文件堆积。

## 功能

- 显式调用保护：不会因为普通提到 Claude、review、分析、方案等词语而自动触发。
- 只读 Claude worker：默认只开放 `Read`、`Grep`、`Glob`、`LS`，禁止编辑、写入、提交、推送、联网搜索和 release 操作。
- 通用 worker contract：Claude 最终输出会被归一化为 `status`、`questions`、`findings`、`evidence`、`risks`、`recommendation`。
- 进度事件：worker 通过 stderr 输出 JSONL 进度事件，stdout 只保留最终 worker contract。
- 无文件默认模式：task packet 推荐通过 stdin 传入；`--progress-log-file` 和 `--raw-log-file` 仅用于显式调试。
- 窄范围跨目录读取：需要读 cwd 外目录时，必须通过 `add_read_dirs` 或 `--add-dir` 明确声明，且拒绝 `/`、`$HOME`、`/Users` 等宽目录。
- 可选 Bash：Bash 默认关闭；只有确认当前 Claude CLI 对未匹配 Bash pattern 默认拒绝后，才可显式启用极窄只读 Bash 白名单。

## 必备条件

- 已安装并可执行 Claude Code CLI，默认命令名为 `claude`。
- 当前运行环境支持 Python 3.10 或更高版本。
- host agent 能启动本仓库中的 `scripts/run_claude_worker.py`。
- host agent 具备 worker 调度与 stdout/stderr 流读取能力。
- 目标任务适合分析、Review 或方案合并；需要代码修改时，Claude 仍然只读，实际修改由 host agent 按当前会话权限执行。
- 如果需要 cwd 外读取，调用方必须提前给出窄范围目录，不允许放开用户主目录或系统级目录。

## 项目结构

```text
.
├── SKILL.md
├── agents/
│   └── openai.yaml
├── scripts/
│   └── run_claude_worker.py
└── tests/
    └── test_run_claude_worker.py
```

- `SKILL.md`：skill 主说明，定义触发边界、工作流、合并规则和安全检查。
- `agents/openai.yaml`：OpenAI agent 入口配置，强调显式调用和默认无文件进度流。
- `scripts/run_claude_worker.py`：Claude worker 包装器，负责构造 Claude CLI 命令、进度事件、输出归一化和安全边界。
- `tests/test_run_claude_worker.py`：worker 包装器的回归测试。

## 使用说明

在用户明确调用 skill 后，host agent 应构造 task packet，并通过 stdin 传给 worker：

```bash
python3 -B scripts/run_claude_worker.py --cwd "$PWD" < task-packet-stdin
```

task packet 建议包含：

```json
{
  "user_request": "$with-claude 分析当前设计",
  "cwd": "/path/to/workspace",
  "expected_output_type": "analysis",
  "constraints": [
    "Claude worker must remain read-only",
    "do not mutate repository files"
  ],
  "known_context": [
    "Relevant facts the host agent already knows"
  ],
  "add_read_dirs": []
}
```

worker 的 stdout 只输出最终 JSON contract，便于 host agent 稳定解析：

```json
{
  "status": "done",
  "questions": [],
  "findings": [],
  "evidence": [],
  "risks": [],
  "recommendation": ""
}
```

worker 的 stderr 输出 JSONL 进度事件。host agent 可以读取这些事件，并用简短语言向用户同步：

```jsonl
{"event":"started","summary":"Claude worker started"}
{"event":"tool","tool_name":"Read","summary":"Claude is reading SKILL.md"}
{"event":"heartbeat","summary":"Claude worker is still running"}
{"event":"result","summary":"Claude produced a final result event"}
{"event":"final","summary":"Claude worker finished"}
```

注意：这些进度只代表可见工具动作，不是 Claude 的私有推理过程。

## 调试选项

默认模式不写 task 文件，也不写 progress 文件。如果需要保留调试材料，可以显式传入文件路径：

```bash
python3 -B scripts/run_claude_worker.py \
  --cwd "$PWD" \
  --progress-log-file /tmp/with-claude/task/claude.progress.jsonl \
  --raw-log-file /tmp/with-claude/task/raw-worker.json \
  < task-packet-stdin
```

常用参数：

- `--task`：直接从命令行传入 task packet。
- `--task-file`：从文件读取 task packet，主要用于兼容或调试。
- `--add-dir`：增加一个额外只读目录，可重复传入。
- `--model`：指定 Claude 模型或别名。
- `--timeout`：设置 worker 超时时间，默认 1800 秒。
- `--heartbeat-interval`：设置进度心跳间隔，默认 30 秒。
- `--legacy-json-output`：使用单次 JSON 输出模式，而不是默认 stream-json。
- `--include-partial-messages`：在 stream-json 模式下包含增量消息片段。
- `--enable-read-only-bash`：显式启用窄范围只读 Bash 白名单，默认关闭。
- `--dry-run`：打印将要执行的 Claude 命令和 prompt，不实际调用 Claude。

失败时，wrapper 会尽量输出符合 worker contract 的 `status: "blocked"`。例如 Claude CLI 不存在、超时、退出非零、返回非 JSON 或不符合 schema 时，host agent 都应读取 stdout 中的 `status` 字段判断结果，而不是只看进程退出码。

## Bash 策略

Bash 默认关闭，这是当前推荐的安全基线。默认命令只包含：

```text
Read,Grep,Glob,LS
```

只有在满足以下条件时，才考虑使用 `--enable-read-only-bash`：

- 已确认当前 Claude Code CLI 对未匹配 `--allowedTools` / `--disallowedTools` 的 Bash pattern 默认拒绝。
- 任务确实需要少量只读 shell 命令。
- host agent 明确知道启用 Bash 会扩大风险面。

启用后，allowlist 仍只包含少量只读命令，例如 `git status`、`git diff --stat`、`git ls-files`、`pwd` 等；denylist 会继续拦截 git mutation、文件写入、网络命令、任意解释器执行、格式化写回等高风险操作。

## 合并规则

Claude 的结果不是最终答案。host agent 必须：

- 先给出自己的总体结论。
- 简述 Claude 观点。
- 说明哪些 Claude 观点被采纳、部分采纳或未采纳。
- 只有存在实质分歧时才写冲突点。
- 优先采用本地代码、文件路径、日志和命令输出等可验证证据。
- 如果 Claude 或其他 worker 提出必要用户问题，合并去重后一次性询问用户。

默认最终输出结构：

```md
## 总体结论

## Claude 观点简述

## 采纳情况
- 已采纳:
- 部分采纳:
- 未采纳:

## 与 Claude 的冲突点
```

没有有意义冲突时，省略 `## 与 Claude 的冲突点`。

## 注意事项

- 不要把这个 skill 用作自动触发的通用 Claude 集成；必须显式调用。
- 不要让 Claude worker 修改仓库、生成 patch、运行 formatter、提交、推送或发布。
- 不要把宽目录传给 `add_read_dirs` 或 `--add-dir`。
- 不要默认生成 `/tmp/with-claude/...` 文件；只有调试需要时才显式写文件。
- 不要把 stderr 进度摘要表述为 Claude 思维链。
- 不要让 README 和 `SKILL.md` 漂移；行为规则变化时优先更新 `SKILL.md`，再同步 README。
- 如果 Claude worker `blocked`，继续使用非 Claude 结果，并说明 Claude pass 不可用。
- 如果 progress 长时间只有 heartbeat，不要直接判定失败；超过 180 秒没有新事件时，向用户说明进程仍在安静运行。

## 测试

运行回归测试：

```bash
python3 -B tests/test_run_claude_worker.py
```

建议在修改 wrapper 后至少确认：

- stdout 只输出最终 worker contract。
- 默认不创建 task/progress 文件。
- stderr progress JSONL 可解析。
- Bash 默认关闭。
- `--enable-read-only-bash` 只加入窄白名单。
- `add_read_dirs` / `--add-dir` 拒绝宽目录和不存在路径。
- `structured_output` 优先于 Claude 的纯文本 result。

也建议覆盖以下故障场景：

- Claude CLI 不存在。
- Claude worker 超时。
- Claude 返回纯文本或不符合 schema 的 JSON。
- `--add-dir` 传入 `$HOME`、`/Users`、不存在路径或文件路径。

# claude-code-monitor

Live dashboards for monitoring Claude Code sessions, agents, and detecting issues. Zero API calls, zero credit cost — reads only local session data.

Two interfaces: a terminal TUI (`claude-monitor`) and a browser web UI (`claude-web`).

![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue) ![License: MIT](https://img.shields.io/badge/license-MIT-green)

## Features

**Dashboard panels** (both interfaces):
- **Sessions** — active PIDs, runtime, first prompt title, CWD, token estimates, tool call counts. Git/no-git separation.
- **Agent Tree** — subagent types, descriptions, spawn history from watchdog hook.
- **Worktrees** — active git worktrees with branch, uncommitted changes, last activity.
- **Tool Usage** — per-session tool breakdown with error counts + recent activity stream.
- **Issues** — proactive detection of loops, high token usage, error rates, stale sessions, config problems.

**Detailed issue report** (`--issues`):
- Expanded findings with timestamped evidence trails
- Session context (title, CWD)
- Actionable suggestions per issue type

**Detection hooks** (install into Claude Code):
- `loop-detect.sh` — detects repeated tool calls (3x same target) and Edit/Read ping-pong cycles
- `agent-watchdog.sh` — logs agent spawns, detects rapid spawning (6+ in 2 minutes)
- `context_budget.py` — PostToolUse; advisory warnings when a session crosses context or cumulative-cost thresholds defined in `budget.toml`. Uses `lib/session_stats.py` for exact usage accounting (not size-based estimation). Fires terminal warnings + macOS `osascript` notifications, once per threshold per session. Always exits 0, never blocks a tool call.
- `session_summary.py` — Stop; appends final session stats (cost, duration, tool mix) to `state/session_log.jsonl` for trend view.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (for auto-dependency management)
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- Claude Code (the tool being monitored)

## Install

```bash
# Clone into your Claude Code config (as submodule or standalone)
git clone git@github.com:nic0le-f/claude-code-monitoring.git ~/.claude/monitor

# Or as a submodule of your claude-config:
cd ~/.claude
git submodule add git@github.com:nic0le-f/claude-code-monitoring.git monitor
```

### Register hooks

Add to your `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Agent",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/monitor/hooks/agent-watchdog.sh"
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/monitor/hooks/loop-detect.sh"
          },
          {
            "type": "command",
            "command": "~/.claude/monitor/hooks/context_budget.py"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/monitor/hooks/session_summary.py"
          }
        ]
      }
    ]
  }
}
```

Omit any hook entry to disable it; none of them are required for the TUI to work.

### Symlink the binaries

```bash
mkdir -p ~/.local/bin
ln -s ~/.claude/monitor/bin/claude-monitor ~/.local/bin/claude-monitor
ln -s ~/.claude/monitor/bin/claude-web ~/.local/bin/claude-web
```

## Usage

### TUI (`claude-monitor`)

Best for terminal-only environments, SSH, tmux panes, or scripting.

```bash
# Live TUI dashboard (run in a tmux pane)
claude-monitor

# Snapshot and exit
claude-monitor --once

# Detailed issue report with evidence
claude-monitor --issues

# Machine-readable JSON (used by hooks)
claude-monitor --json

# Custom refresh interval (seconds)
claude-monitor --interval 5
```

### Web UI (`claude-web`)

Richer interface in a browser with live updates via SSE.

```bash
# Start on http://localhost:7899 (auto-opens browser)
claude-web

# Custom port
claude-web --port 8080

# Don't auto-open browser
claude-web --no-open
```

## Configuration & tuning

Everything the budget/context hooks and the `opus-overkill` nudge use lives in **[`budget.toml`](budget.toml)**. The file is TOML; reload is automatic (read on every hook invocation, so edits take effect on the next tool call).

### Thresholds (`[defaults]`)

| Key | Unit | Purpose |
|---|---|---|
| `context_warn_tokens` | tokens | soft terminal warning + macOS notification when the **current prompt size** (what gets sent on the next inference) crosses this. Default 100 000. |
| `context_alarm_tokens` | tokens | louder wording, stronger `/clear` nudge. Default 150 000. Reasoning: sustained ≥150 K sessions are the pattern that drives the most cost at standard tier. |
| `session_soft_usd` | USD | notification only (no terminal noise). Default $5. |
| `session_loud_usd` | USD | notification + terminal warning. Default $15. |
| `session_alarm_usd` | USD | notification + stronger terminal warning. Default $30. |
| `sonnet_nudge_min_turns` | count | minimum recent tool calls before `opus-overkill` can fire. Default 20. Raising it = fewer false positives on short sessions. |
| `sonnet_nudge_tool_ratio` | 0..1 | minimum fraction of those calls that must be Bash/Grep/Read/Edit. Default 0.80. |

Tiers are hierarchical — crossing `cost_alarm` suppresses `cost_loud` and `cost_soft` for the rest of the session. Each threshold fires **once per session**; state is kept per-session in `state/warned/<session_id>.json`.

### Per-project overrides

Any `[defaults]` key can be overridden for a specific session CWD. Match is exact on the directory path recorded in the transcript.

```toml
[project."/path/to/ml-research"]
# Long-context work is legitimate here; raise the bar.
session_loud_usd  = 30.0
session_alarm_usd = 50.0

[project."/path/to/config-repo"]
# Config/meta-work — should rarely need big budgets.
session_loud_usd  = 10.0
session_alarm_usd = 20.0
```

Add a new section per project you care about. Keys not listed fall back to `[defaults]`.

### Things worth revisiting

Open knobs we consciously left as defaults — tweak when actual use justifies it:

### Disabling a hook

Remove its entry from `settings.json`. The TUI is fully independent of the hooks — it can run without any of them installed.

## Data sources

All data is read from local files — no API calls, no network, no credits.

| Source | Path | What |
|--------|------|------|
| Session metadata | `~/.claude/sessions/*.json` | PIDs, CWDs, start times |
| Session logs | `~/.claude/projects/**/*.jsonl` | Tool calls, messages, timestamps |
| Subagent metadata | `**/subagents/*.meta.json` | Agent types, descriptions |
| Agent tree | `~/.claude/state/agent-tree/*.jsonl` | Watchdog spawn logs |
| Alerts | `~/.claude/state/alerts.jsonl` | Hook-generated alerts |
| Global history | `~/.claude/history.jsonl` | Cross-session command history |

## Security & privacy

- **No network access.** The TUI and all hooks read local files only — no API calls, no telemetry, no outbound connections.
- **No credentials logged.** Session `.jsonl` files may contain tool arguments and file paths from your work, but the monitor never reads or stores API keys or conversation content beyond what Claude Code already writes locally.
- **`state/` contains your session paths.** `state/session_log.jsonl` and `state/stats_*.json` record per-session token counts and working directories. These are `.gitignore`d — don't commit or share the `state/` directory.
- **`budget.toml` project keys are local paths.** If you add `[project."/your/path"]` overrides, those paths won't appear to other users unless you share your `budget.toml`.

## Issue types detected

| Type | Severity | Trigger |
|------|----------|---------|
| `possible-loop` | WARN | Same tool+target called 3+ times in recent window |
| `high-error-rate` | WARN | >30% of tool calls returning errors |
| `high-tokens` | WARN | >500k estimated tokens in session |
| `hook-not-executable` | WARN | Hook script missing execute permission |
| `agent-spawn-loop` | WARN | 6+ agents spawned in 2 minutes |
| `token-usage` | INFO | >300k estimated tokens (approaching high) |
| `no-git` | INFO | Session CWD not in a git repo with significant work |
| `many-agents` | INFO | >8 subagents spawned in one session |
| `dead-session` | INFO | Session PID no longer running |
| `dirty-worktree` | INFO | Worktree with >5 uncommitted changes |
| `opus-overkill` | INFO | Opus session whose recent 20 tool calls are ≥80% Bash/Grep/Read/Edit — candidate for `/model sonnet` |

## License

MIT

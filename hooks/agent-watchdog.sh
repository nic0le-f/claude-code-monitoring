#!/bin/bash
# agent-watchdog.sh - Track agent spawns and detect long-running agents
#
# Fires on PostToolUse for Agent tool. Logs spawn events to agent-tree JSONL.
# Checks for sessions with rapid agent spawning (possible spawn loop).
# Non-blocking — advisory warnings only.

mkdir -p "$HOME/.claude/state/agent-tree" "$HOME/.claude/state"

INPUT=$(cat)

export WATCHDOG_INPUT="$INPUT"
python3 -c '
import json, os, sys, datetime, time, glob

alert_file = os.path.expanduser("~/.claude/state/alerts.jsonl")
tree_dir = os.path.expanduser("~/.claude/state/agent-tree")

try:
    d = json.loads(os.environ.get("WATCHDOG_INPUT", "{}"))
except Exception:
    sys.exit(0)

session_id = d.get("session_id", "default")
tool_input = d.get("tool_input", {})
tool_response = d.get("tool_response", "")

agent_type = tool_input.get("subagent_type", "general-purpose")
description = tool_input.get("description", "")
prompt_preview = tool_input.get("prompt", "")[:100]
background = tool_input.get("run_in_background", False)

now = datetime.datetime.now(datetime.UTC)

# Log spawn event
tree_file = os.path.join(tree_dir, f"{session_id}.jsonl")
entry = {
    "ts": now.isoformat() + "Z",
    "event": "spawn",
    "session": session_id,
    "agent_type": agent_type,
    "description": description,
    "prompt_preview": prompt_preview,
    "background": background,
    "response_len": len(str(tool_response)),
}
with open(tree_file, "a") as f:
    f.write(json.dumps(entry) + "\n")

resp_len = len(str(tool_response))
mode = "background" if background else "foreground"
print(f"AGENT TRACKED [watchdog]: {agent_type} ({mode}) — {description} [{resp_len} chars returned]")

# Check for rapid agent spawning (>= 6 in 2 minutes)
try:
    recent_spawns = []
    with open(tree_file, "r") as f:
        for line in f:
            try:
                e = json.loads(line)
                if e.get("event") == "spawn":
                    ts = datetime.datetime.fromisoformat(e["ts"].rstrip("Z"))
                    if (now - ts).total_seconds() < 120:
                        recent_spawns.append(e)
            except Exception:
                continue

    if len(recent_spawns) >= 6:
        alert_msg = f"Rapid agent spawning: {len(recent_spawns)} agents in 2 minutes — possible spawn loop"
        alert = {
            "ts": now.isoformat() + "Z",
            "session": session_id,
            "type": "agent-spawn-loop",
            "detail": alert_msg,
            "severity": "warn",
        }
        with open(alert_file, "a") as f:
            f.write(json.dumps(alert) + "\n")
        print(f"AGENT WARNING [watchdog]: {alert_msg}", file=sys.stderr)
        print(f"AGENT WARNING [watchdog]: {alert_msg}")
except Exception:
    pass

# Prune tree files older than 48h
for path in glob.glob(os.path.join(tree_dir, "*.jsonl")):
    try:
        if time.time() - os.path.getmtime(path) > 172800:
            os.unlink(path)
    except Exception:
        pass
'

exit 0

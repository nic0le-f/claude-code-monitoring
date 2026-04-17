#!/bin/bash
# loop-detect.sh - Detect repeated tool call patterns that indicate a stuck loop
#
# Fires on PostToolUse. Tracks a rolling window of recent tool calls per session.
# Detects: same tool+target 3+ times in 5 calls, Edit/Read ping-pong cycles.
# Non-blocking — warns via stderr, writes to alerts.jsonl.

WINDOW_SIZE=8
REPEAT_THRESHOLD=3
STATE_DIR="$HOME/.claude/state/tool-history"
ALERT_FILE="$HOME/.claude/state/alerts.jsonl"
mkdir -p "$STATE_DIR" "$(dirname "$ALERT_FILE")"

INPUT=$(cat)

# Extract tool name, key target arg, and session_id
PARSED=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    tool = d.get('tool_name', 'unknown')
    sid = d.get('session_id', 'default')
    ti = d.get('tool_input', {})
    # Build a signature: tool + primary target
    target = ''
    if isinstance(ti, dict):
        target = ti.get('file_path', ti.get('command', ti.get('pattern', ti.get('prompt', ''))))
        if isinstance(target, str) and len(target) > 120:
            target = target[:120]
    print(f'{tool}')
    print(f'{target}')
    print(f'{sid}')
except Exception:
    print('unknown')
    print('')
    print('default')
" 2>/dev/null)

TOOL=$(echo "$PARSED" | sed -n '1p')
TARGET=$(echo "$PARSED" | sed -n '2p')
SESSION_ID=$(echo "$PARSED" | sed -n '3p')

HISTORY_FILE="$STATE_DIR/$SESSION_ID"

# Append current call signature
SIGNATURE="${TOOL}|${TARGET}"
echo "$SIGNATURE" >> "$HISTORY_FILE"

# Keep only last WINDOW_SIZE entries
TAIL_LINES=$(tail -n "$WINDOW_SIZE" "$HISTORY_FILE")
echo "$TAIL_LINES" > "$HISTORY_FILE"

# Count occurrences of current signature in window
COUNT=$(echo "$TAIL_LINES" | grep -cF "$SIGNATURE")

ALERT=""

if [[ "$COUNT" -ge "$REPEAT_THRESHOLD" ]]; then
    ALERT="Loop detected: '$TOOL' called ${COUNT}x on same target in last ${WINDOW_SIZE} calls"
fi

# Detect Edit/Read ping-pong (alternating Edit and Read on same file)
if [[ -z "$ALERT" && ("$TOOL" == "Edit" || "$TOOL" == "Read") ]]; then
    PINGPONG=$(echo "$TAIL_LINES" | grep -E "^(Edit|Read)\|" | tail -6 | awk -F'|' '{print $1}' | paste -d'' - - - | grep -c "EditReadEdit\|ReadEditRead")
    if [[ "$PINGPONG" -ge 1 ]]; then
        ALERT="Edit/Read ping-pong detected on '${TARGET}' — possible stuck edit cycle"
    fi
fi

if [[ -n "$ALERT" ]]; then
    # Write structured alert
    python3 -c "
import json, sys, datetime
alert = {
    'ts': datetime.datetime.utcnow().isoformat() + 'Z',
    'session': '$SESSION_ID',
    'type': 'loop-detected',
    'detail': sys.argv[1],
    'severity': 'warn'
}
print(json.dumps(alert))
" "$ALERT" >> "$ALERT_FILE" 2>/dev/null

    echo "LOOP WARNING [loop-detect]: $ALERT" >&2
    echo "LOOP WARNING [loop-detect]: $ALERT"
fi

# Prune history files older than 24h
find "$STATE_DIR" -type f -mmin +1440 -delete 2>/dev/null

exit 0

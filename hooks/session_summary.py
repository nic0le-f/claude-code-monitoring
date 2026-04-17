#!/usr/bin/env python3
"""Stop hook — append final session stats to state/session_log.jsonl.

Gives the TUI Issues panel a history of completed sessions (for trend view).
Emits no stdout and no notification. Advisory only; always exits 0.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR.parent))

from lib.session_stats import compute  # noqa: E402

MONITOR_ROOT = HOOK_DIR.parent
STATE_DIR = MONITOR_ROOT / "state"
LOG_FILE = STATE_DIR / "session_log.jsonl"


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        event = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return 0

    try:
        transcript_path = event.get("transcript_path")
        session_id = event.get("session_id") or ""
        if not transcript_path or not session_id:
            return 0

        stats = compute(Path(transcript_path), state_dir=STATE_DIR)
        if stats.msg_count == 0:
            return 0

        record = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "session_id": stats.session_id,
            "cwd": stats.cwd,
            "model": stats.model,
            "msg_count": stats.msg_count,
            "duration_h": round(stats.duration_h, 2),
            "cumulative_cost_usd": round(stats.cumulative_cost_usd, 2),
            "final_context_tokens": stats.current_context_tokens,
            "tool_mix_last_20": stats.tool_mix_last_20,
        }

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    except Exception:
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

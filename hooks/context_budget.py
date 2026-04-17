#!/usr/bin/env python3
"""PostToolUse hook — context + per-session budget alarm. Advisory only.

Reads the harness event from stdin:
    {"session_id": "...", "transcript_path": "...", "cwd": "...", ...}

Incrementally parses the transcript (via lib.session_stats.compute) and, when
a threshold is crossed for the first time in this session, emits:
  * a single-line warning on stdout (the harness surfaces hook stdout inline)
  * a macOS notification via osascript, spawned in the background

All errors are swallowed and the hook always exits 0. It never blocks a tool.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

# Make `lib` importable regardless of how the hook is invoked.
HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR.parent))

from lib.session_stats import compute, SessionStats  # noqa: E402
from lib.budget import load as load_thresholds, Thresholds  # noqa: E402

MONITOR_ROOT = HOOK_DIR.parent
STATE_DIR = MONITOR_ROOT / "state"
WARN_DIR = STATE_DIR / "warned"
ALERTS_FILE = Path.home() / ".claude" / "state" / "alerts.jsonl"


def _load_warned(session_id: str) -> set[str]:
    p = WARN_DIR / f"{session_id}.json"
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text()))
    except (OSError, ValueError):
        return set()


def _save_warned(session_id: str, flags: set[str]) -> None:
    try:
        WARN_DIR.mkdir(parents=True, exist_ok=True)
        (WARN_DIR / f"{session_id}.json").write_text(json.dumps(sorted(flags)))
    except OSError:
        pass


def _log_alert(session_id: str, tier: str, stats: SessionStats) -> None:
    """Append a structured alert for the trend panel. Never raises."""
    severity = "alarm" if tier.endswith("alarm") else "warn"
    entry = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "session": session_id,
        "type": tier,
        "severity": severity,
        "usd": round(stats.cumulative_cost_usd, 4),
        "tokens": stats.current_context_tokens,
        "cwd": stats.cwd or "",
    }
    try:
        ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with ALERTS_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _notify(title: str, body: str) -> None:
    """Fire-and-forget macOS notification. Never blocks."""
    try:
        # osascript is zero-dep on macOS. Escape quotes defensively.
        safe_title = title.replace('"', "'")
        safe_body = body.replace('"', "'")
        script = f'display notification "{safe_body}" with title "{safe_title}"'
        subprocess.Popen(
            ["osascript", "-e", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _evaluate(stats: SessionStats, th: Thresholds, warned: set[str]) -> list[tuple[str, str, str]]:
    """Return list of (flag_key, terminal_msg, notification_body) for newly-crossed thresholds.

    Tiers are hierarchical: if a higher tier was already warned for a given metric,
    lower tiers are suppressed too. Prevents "already alarmed at $30, now warn at $15"
    regressions when state lists only the highest tier crossed.
    """
    triggers: list[tuple[str, str, str]] = []

    # --- context tier (alarm > warn) ---
    ctx = stats.current_context_tokens
    ctx_already = "ctx_alarm" in warned or "ctx_warn" in warned
    if ctx >= th.context_alarm_tokens and "ctx_alarm" not in warned:
        triggers.append((
            "ctx_alarm",
            f"⚠  context at {ctx:,} tok (≥{th.context_alarm_tokens:,}) — strongly consider /clear or a fresh session; every turn is replaying ~{ctx//1000}K cached tokens.",
            f"Context {ctx:,} tokens — start fresh session",
        ))
    elif ctx >= th.context_warn_tokens and not ctx_already:
        triggers.append((
            "ctx_warn",
            f"ℹ  context at {ctx:,} tok — per-turn cache-read cost is climbing. Consider /clear after this task.",
            f"Context {ctx:,} tokens — consider /clear",
        ))

    # --- cost tier (alarm > loud > soft) ---
    cost = stats.cumulative_cost_usd
    cost_any = "cost_alarm" in warned or "cost_loud" in warned or "cost_soft" in warned
    cost_mid_or_higher = "cost_alarm" in warned or "cost_loud" in warned
    if cost >= th.session_alarm_usd and "cost_alarm" not in warned:
        triggers.append((
            "cost_alarm",
            f"🚨 session cost ≈ ${cost:.2f} (≥${th.session_alarm_usd:.0f}). Project {stats.cwd or '?'}",
            f"Session ≈ ${cost:.2f} — alarm threshold",
        ))
    elif cost >= th.session_loud_usd and not cost_mid_or_higher:
        triggers.append((
            "cost_loud",
            f"⚠  session cost ≈ ${cost:.2f} (≥${th.session_loud_usd:.0f}).",
            f"Session ≈ ${cost:.2f} — loud threshold",
        ))
    elif cost >= th.session_soft_usd and not cost_any:
        # soft: notification only, no terminal noise.
        triggers.append((
            "cost_soft",
            "",
            f"Session ≈ ${cost:.2f}",
        ))

    return triggers


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
        cwd = event.get("cwd") or ""

        if not transcript_path or not session_id:
            return 0

        stats = compute(Path(transcript_path), state_dir=STATE_DIR)
        if stats.msg_count == 0:
            return 0
        # Prefer the event's cwd (always current); fall back to the transcript's.
        effective_cwd = cwd or stats.cwd
        thresholds = load_thresholds(cwd=effective_cwd)

        warned = _load_warned(session_id)
        triggers = _evaluate(stats, thresholds, warned)
        if not triggers:
            return 0

        # Emit terminal warnings (joined with newlines) and notifications.
        terminal_lines = [t[1] for t in triggers if t[1]]
        for _, _, notify_body in triggers:
            _notify("Claude budget", notify_body)

        for key, _, _ in triggers:
            _log_alert(session_id, key, stats)
            warned.add(key)
        _save_warned(session_id, warned)

        if terminal_lines:
            # Claude Code surfaces hook stdout to the user.
            sys.stdout.write("\n".join(terminal_lines) + "\n")

    except Exception:
        # Advisory only — never break the session.
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

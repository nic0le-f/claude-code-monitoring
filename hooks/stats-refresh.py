#!/usr/bin/env python3
"""Stop hook — refresh stats-cache.json from transcripts after each session.

Scans all project transcript JSONL files modified since lastComputedDate,
merges new daily activity and model usage into stats-cache.json, and updates
lastComputedDate to today. Safe to run repeatedly (idempotent per day).
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
STATS_CACHE = CLAUDE_DIR / "stats-cache.json"


def _date(ts: str) -> str:
    try:
        return ts[:10]
    except Exception:
        return ""


def _model_family(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "other"


def scan_transcripts(since: str) -> tuple[dict, dict, dict]:
    """Return (daily_activity, model_usage, sessions_per_day) dicts."""
    daily: dict[str, dict] = defaultdict(lambda: {"messageCount": 0, "toolCallCount": 0, "sessions": set()})
    model_usage: dict[str, dict] = defaultdict(lambda: {
        "inputTokens": 0, "outputTokens": 0,
        "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
    })

    for transcript in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            mtime = datetime.fromtimestamp(transcript.stat().st_mtime, tz=timezone.utc)
            if mtime.date().isoformat() < since:
                continue
        except OSError:
            continue

        try:
            with transcript.open() as f:
                session_id = transcript.stem
                last_date = mtime.date().isoformat()
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg = entry.get("message", {})
                    role = msg.get("role", "")
                    ts = entry.get("ts", "")
                    date = _date(ts) or last_date
                    if ts:
                        last_date = date

                    if role == "user":
                        content = msg.get("content", [])
                        if isinstance(content, str) and content.strip():
                            daily[date]["messageCount"] += 1
                            daily[date]["sessions"].add(session_id)
                        elif isinstance(content, list):
                            has_human = any(
                                c.get("type") != "tool_result"
                                for c in content
                                if isinstance(c, dict)
                            )
                            if has_human:
                                daily[date]["messageCount"] += 1
                                daily[date]["sessions"].add(session_id)

                    elif role == "assistant":
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            tool_calls = sum(1 for c in content if isinstance(c, dict) and c.get("type") == "tool_use")
                            daily[date]["toolCallCount"] += tool_calls
                        usage = msg.get("usage", {})
                        if usage:
                            model = entry.get("model", "")
                            fam = _model_family(model)
                            mu = model_usage[fam]
                            mu["inputTokens"] += usage.get("input_tokens", 0) or 0
                            mu["outputTokens"] += usage.get("output_tokens", 0) or 0
                            mu["cacheReadInputTokens"] += usage.get("cache_read_input_tokens", 0) or 0
                            cc = usage.get("cache_creation", {}) or {}
                            mu["cacheCreationInputTokens"] += (
                                cc.get("ephemeral_5m_input_tokens", 0) or 0
                            ) + (
                                cc.get("ephemeral_1h_input_tokens", 0) or 0
                            ) + (usage.get("cache_creation_input_tokens", 0) or 0)
        except Exception:
            continue

    return daily, model_usage


def main() -> None:
    today = datetime.now(timezone.utc).date().isoformat()

    cache: dict = {}
    if STATS_CACHE.exists():
        try:
            cache = json.loads(STATS_CACHE.read_text())
        except Exception:
            pass

    since = cache.get("lastComputedDate", "2000-01-01")
    if since >= today and cache.get("dailyActivity"):
        # Already up to date
        return

    daily, model_usage = scan_transcripts(since)
    if not daily:
        return

    # Merge into existing dailyActivity
    existing_days: dict[str, dict] = {
        d["date"]: d for d in cache.get("dailyActivity", [])
    }
    for date, data in daily.items():
        if date in existing_days:
            existing_days[date]["messageCount"] = max(
                existing_days[date].get("messageCount", 0), data["messageCount"]
            )
            existing_days[date]["toolCallCount"] = max(
                existing_days[date].get("toolCallCount", 0), data["toolCallCount"]
            )
            existing_days[date]["sessionCount"] = max(
                existing_days[date].get("sessionCount", 0), len(data["sessions"])
            )
        else:
            existing_days[date] = {
                "date": date,
                "messageCount": data["messageCount"],
                "toolCallCount": data["toolCallCount"],
                "sessionCount": len(data["sessions"]),
            }

    # Merge model usage
    existing_mu = cache.get("modelUsage", {})
    for fam, data in model_usage.items():
        if fam not in existing_mu:
            existing_mu[fam] = data
        else:
            for k, v in data.items():
                existing_mu[fam][k] = existing_mu[fam].get(k, 0) + v

    cache["version"] = cache.get("version", 3)
    cache["lastComputedDate"] = today
    cache["dailyActivity"] = sorted(existing_days.values(), key=lambda d: d["date"])
    cache["modelUsage"] = existing_mu

    try:
        STATS_CACHE.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        print(f"stats-refresh: failed to write cache: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

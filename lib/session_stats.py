"""Compute per-session statistics from a Claude Code JSONL transcript.

Single source of truth for the monitor TUI and budget hooks.

Public API:
    compute(transcript_path, state_dir=None) -> SessionStats

Reads only the new bytes since the last invocation (offset cached per session in
state_dir) so it is safe to call from a PostToolUse hook on every tool call.
All numbers come from the authoritative `message.usage` block emitted by the
harness; nothing is estimated from file size.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# USD per 1M tokens. Source: Anthropic public pricing (standard tier, <=200K context).
# cw5 / cw1h / cr derived from input with the documented multipliers
# (cache write 5m = 1.25x input, cache write 1h = 2x input, cache read = 0.1x input).
PRICES = {
    "opus":   {"in": 15.0, "out": 75.0, "cw5": 18.75, "cw1h": 30.0, "cr": 1.50},
    "sonnet": {"in":  3.0, "out": 15.0, "cw5":  3.75, "cw1h":  6.0, "cr": 0.30},
    "haiku":  {"in":  1.0, "out":  5.0, "cw5":  1.25, "cw1h":  2.0, "cr": 0.10},
    "other":  {"in":  3.0, "out": 15.0, "cw5":  3.75, "cw1h":  6.0, "cr": 0.30},
}

TOOL_MIX_WINDOW = 20  # how many recent tool_use blocks to remember for the Sonnet nudge


def _model_family(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "other"


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _message_cost(fam: str, usage: dict) -> float:
    p = PRICES[fam]
    cc = usage.get("cache_creation") or {}
    cw5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
    cw1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
    # Fallback: some older rows use the flat field only.
    if not cc and usage.get("cache_creation_input_tokens"):
        cw5 = usage.get("cache_creation_input_tokens", 0) or 0
    return (
        (usage.get("input_tokens", 0) or 0)        * p["in"]   / 1e6 +
        (usage.get("output_tokens", 0) or 0)       * p["out"]  / 1e6 +
        cw5                                        * p["cw5"]  / 1e6 +
        cw1h                                       * p["cw1h"] / 1e6 +
        (usage.get("cache_read_input_tokens", 0) or 0) * p["cr"] / 1e6
    )


def _current_context(usage: dict) -> int:
    """Tokens that will be sent on the next inference if context isn't trimmed."""
    cc = usage.get("cache_creation") or {}
    return (
        (usage.get("input_tokens", 0) or 0)
        + (usage.get("cache_read_input_tokens", 0) or 0)
        + (cc.get("ephemeral_5m_input_tokens", 0) or 0)
        + (cc.get("ephemeral_1h_input_tokens", 0) or 0)
    )


@dataclass
class SessionStats:
    session_id: str
    cwd: str
    model: str  # family: opus/sonnet/haiku/other
    msg_count: int = 0
    duration_h: float = 0.0
    cumulative_cost_usd: float = 0.0
    current_context_tokens: int = 0
    tool_mix_last_20: dict[str, int] = field(default_factory=dict)
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None


@dataclass
class _CacheState:
    offset: int = 0
    msg_count: int = 0
    cumulative_cost: float = 0.0
    current_context: int = 0
    cwd: str = ""
    model: str = "other"
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    tool_tail: list[str] = field(default_factory=list)  # rolling last TOOL_MIX_WINDOW tool names

    def to_json(self) -> dict:
        return {
            "offset": self.offset,
            "msg_count": self.msg_count,
            "cumulative_cost": self.cumulative_cost,
            "current_context": self.current_context,
            "cwd": self.cwd,
            "model": self.model,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "tool_tail": self.tool_tail,
        }

    @classmethod
    def from_json(cls, d: dict) -> "_CacheState":
        return cls(
            offset=d.get("offset", 0),
            msg_count=d.get("msg_count", 0),
            cumulative_cost=d.get("cumulative_cost", 0.0),
            current_context=d.get("current_context", 0),
            cwd=d.get("cwd", ""),
            model=d.get("model", "other"),
            first_ts=d.get("first_ts"),
            last_ts=d.get("last_ts"),
            tool_tail=list(d.get("tool_tail", [])),
        )


def _load_cache(cache_path: Optional[Path]) -> _CacheState:
    if cache_path is None or not cache_path.exists():
        return _CacheState()
    try:
        return _CacheState.from_json(json.loads(cache_path.read_text()))
    except (OSError, ValueError, json.JSONDecodeError):
        return _CacheState()


def _save_cache(cache_path: Optional[Path], state: _CacheState) -> None:
    if cache_path is None:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state.to_json()))
        tmp.replace(cache_path)
    except OSError:
        pass  # advisory-only; never fail the hook on cache write


def compute(transcript_path: Path, state_dir: Optional[Path] = None) -> SessionStats:
    """Compute cumulative stats for a session transcript.

    Reads only new bytes since the last call (offset cached in state_dir).
    Returns zeros for an empty or missing transcript. Never raises on malformed
    JSONL lines — those are skipped.
    """
    transcript_path = Path(transcript_path)
    session_id = transcript_path.stem

    cache_path: Optional[Path] = None
    if state_dir is not None:
        cache_path = Path(state_dir) / f"stats_{session_id}.json"

    cache = _load_cache(cache_path)

    if not transcript_path.exists():
        return SessionStats(session_id=session_id, cwd=cache.cwd, model=cache.model)

    file_size = transcript_path.stat().st_size
    # Detect truncation / replacement: reset cache if file shrank below the cached offset.
    if file_size < cache.offset:
        cache = _CacheState()

    tool_tail: deque[str] = deque(cache.tool_tail, maxlen=TOOL_MIX_WINDOW)

    with open(transcript_path, "rb") as f:
        f.seek(cache.offset)
        buf = f.read()
        new_offset = f.tell()

    # If the tail of buf is an incomplete line (no trailing \n), stop at the last newline
    # so we don't misparse a half-written line. Next call will pick it up.
    if buf and not buf.endswith(b"\n"):
        last_nl = buf.rfind(b"\n")
        if last_nl < 0:
            # no complete line yet
            new_offset = cache.offset
            buf = b""
        else:
            new_offset = cache.offset + last_nl + 1
            buf = buf[: last_nl + 1]

    for raw_line in buf.splitlines():
        if not raw_line.strip():
            continue
        try:
            o = json.loads(raw_line)
        except (ValueError, json.JSONDecodeError):
            continue
        if o.get("type") != "assistant":
            continue
        msg = o.get("message") or {}
        usage = msg.get("usage") or {}
        if not usage:
            continue
        fam = _model_family(msg.get("model", ""))
        cache.msg_count += 1
        cache.cumulative_cost += _message_cost(fam, usage)
        cache.current_context = _current_context(usage)
        cache.model = fam
        cwd = o.get("cwd")
        if cwd:
            cache.cwd = cwd
        ts = o.get("timestamp")
        if ts:
            if not cache.first_ts:
                cache.first_ts = ts
            cache.last_ts = ts
        # Collect tool_use blocks (one assistant message can contain several).
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_tail.append(block.get("name") or "?")

    cache.offset = new_offset
    cache.tool_tail = list(tool_tail)
    _save_cache(cache_path, cache)

    first_dt = _parse_ts(cache.first_ts) if cache.first_ts else None
    last_dt = _parse_ts(cache.last_ts) if cache.last_ts else None
    duration_h = 0.0
    if first_dt and last_dt:
        duration_h = (last_dt - first_dt).total_seconds() / 3600.0

    mix: dict[str, int] = {}
    for name in cache.tool_tail:
        mix[name] = mix.get(name, 0) + 1

    return SessionStats(
        session_id=session_id,
        cwd=cache.cwd,
        model=cache.model,
        msg_count=cache.msg_count,
        duration_h=duration_h,
        cumulative_cost_usd=cache.cumulative_cost,
        current_context_tokens=cache.current_context,
        tool_mix_last_20=mix,
        first_ts=first_dt,
        last_ts=last_dt,
    )


def is_opus_overkill(stats: SessionStats, min_turns: int = 20, ratio: float = 0.8) -> bool:
    """Heuristic: session is Opus but last ~20 tool calls are mechanical dogwork.

    Triggers the Sonnet-nudge in the TUI Issues panel.
    """
    if stats.model != "opus":
        return False
    total = sum(stats.tool_mix_last_20.values())
    if total < min_turns:
        return False
    mechanical = sum(
        n for name, n in stats.tool_mix_last_20.items()
        if name in {"Bash", "Grep", "Read", "Edit"}
    )
    return (mechanical / total) >= ratio

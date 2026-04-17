"""Load budget.toml thresholds with per-project overrides.

Graceful fallback: if the file is missing or malformed, returns built-in
defaults so hooks never fail.
"""

from __future__ import annotations

import sys
import tomllib  # Python 3.11+
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_DEFAULT_THRESHOLDS = {
    "context_warn_tokens":    100_000,
    "context_alarm_tokens":   150_000,
    "session_soft_usd":       5.0,
    "session_loud_usd":       15.0,
    "session_alarm_usd":      30.0,
    "sonnet_nudge_min_turns": 20,
    "sonnet_nudge_tool_ratio": 0.8,
}


@dataclass
class Thresholds:
    context_warn_tokens: int
    context_alarm_tokens: int
    session_soft_usd: float
    session_loud_usd: float
    session_alarm_usd: float
    sonnet_nudge_min_turns: int
    sonnet_nudge_tool_ratio: float


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out:
            out[k] = v
    return out


def load(config_path: Optional[Path] = None, cwd: Optional[str] = None) -> Thresholds:
    """Load thresholds; apply per-project override if cwd matches a [project] section.

    Never raises — returns defaults on any error.
    """
    data: dict = {}
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "budget.toml"
    try:
        if config_path.exists():
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        data = {}

    defaults = _merge(_DEFAULT_THRESHOLDS, data.get("defaults", {}) or {})

    if cwd:
        projects = (data.get("project") or {})
        override = projects.get(cwd)
        if override:
            defaults = _merge(defaults, override)

    try:
        return Thresholds(**defaults)
    except TypeError:
        # Unknown / missing keys — return built-in defaults.
        return Thresholds(**_DEFAULT_THRESHOLDS)


if __name__ == "__main__":
    # Quick self-test: `python3 lib/budget.py [cwd]`
    cwd = sys.argv[1] if len(sys.argv) > 1 else None
    print(load(cwd=cwd))

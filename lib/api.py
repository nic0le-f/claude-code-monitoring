"""lib/api.py — Data collection layer for claude-monitor.

Shared between the TUI (bin/claude-monitor) and web server (bin/claude-web).
All functions are pure reads — no side effects on Claude state.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from .session_stats import compute as _session_compute, is_opus_overkill
from .budget import load as _budget_load

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"
_CLAUDE_STATE = CLAUDE_DIR / "state"
ALERTS_FILE = _CLAUDE_STATE / "alerts.jsonl"
AGENT_TREE_DIR = _CLAUDE_STATE / "agent-tree"
IDE_DIR = CLAUDE_DIR / "ide"
# Monitor's own state dir (session stats cache lives here)
_MONITOR_STATE = Path(__file__).resolve().parent.parent / "state"


# ── Utilities ────────────────────────────────────────────────────


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def time_ago(ts: float) -> str:
    delta = time.time() - ts / 1000
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta / 60)}m"
    if delta < 86400:
        return f"{int(delta / 3600)}h"
    return f"{int(delta / 86400)}d"


def iso_ago(iso: str) -> str:
    try:
        raw = iso.rstrip("Z")
        dt = datetime.datetime.fromisoformat(raw).replace(tzinfo=datetime.UTC)
        delta = (datetime.datetime.now(datetime.UTC) - dt).total_seconds()
        if delta < 0:
            return "now"
        if delta < 60:
            return f"{int(delta)}s ago"
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"
    except Exception:
        return iso


def _preview_input(inp: dict) -> str:
    if not isinstance(inp, dict):
        return str(inp)[:60]
    if "file_path" in inp:
        return os.path.basename(inp["file_path"])
    if "command" in inp:
        cmd = inp["command"]
        return cmd[:60] if isinstance(cmd, str) else str(cmd)[:60]
    if "pattern" in inp:
        return f"/{inp['pattern']}/"
    if "prompt" in inp:
        return inp["prompt"][:50] + "..."
    if "description" in inp:
        return inp["description"]
    return str(inp)[:60]


def _extract_error(msg: dict) -> str:
    """Extract error text from a tool-result message, or return empty string."""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content", "")
    if isinstance(content, str):
        if "error" in content.lower()[:200]:
            return content[:200]
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if "error" in text.lower()[:200]:
                    return text[:200]
    return ""


# ── Session discovery ────────────────────────────────────────────


def _get_session_title(session_id: str) -> str:
    """Extract the first user message from a session's JSONL as a title."""
    if not session_id:
        return ""
    for jsonl in PROJECTS_DIR.rglob(f"{session_id}.jsonl"):
        try:
            with open(jsonl, "r") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("type") != "user":
                            continue
                        msg = entry.get("message", {})
                        if isinstance(msg, dict):
                            for block in msg.get("content", []):
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block["text"].strip()
                                    first_line = text.split("\n")[0]
                                    return first_line[:60]
                        elif isinstance(msg, str):
                            return msg.strip().split("\n")[0][:60]
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception:
            continue
    return ""


def _enrich_session(data: dict) -> dict:
    """Add computed fields (alive, runtime, title, has_git) to a session dict."""
    data["alive"] = pid_alive(data.get("pid", 0))
    data["runtime"] = time_ago(data.get("startedAt", 0))
    if "title" not in data or not data["title"]:
        data["title"] = _get_session_title(data.get("sessionId", ""))
    cwd = data.get("cwd", "")
    if cwd and os.path.isdir(cwd):
        try:
            result = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--git-dir"],
                capture_output=True, text=True, timeout=3,
            )
            data["has_git"] = result.returncode == 0
        except Exception:
            data["has_git"] = False
    else:
        data["has_git"] = False
    return data


def _get_ide_sessions() -> list[dict]:
    """Parse IDE lock files (VS Code, JetBrains) from ~/.claude/ide/."""
    sessions = []
    if not IDE_DIR.exists():
        return sessions
    for f in IDE_DIR.glob("*.lock"):
        try:
            data = json.loads(f.read_text())
            pid = data.get("pid", 0)
            ide_name = data.get("ideName", "IDE")
            workspace = data.get("workspaceFolders", [])
            cwd = workspace[0] if workspace else ""
            sessions.append({
                "pid": pid,
                "cwd": cwd,
                "kind": "ide",
                "entrypoint": ide_name,
                "startedAt": int(f.stat().st_mtime * 1000),
                "sessionId": data.get("authToken", f.stem),
                "title": f"{ide_name}",
            })
        except Exception:
            continue
    return sessions


def _parse_etime(etime: str) -> int:
    """Parse ps etime (e.g. '02:30', '1-03:15:20', '15') into epoch ms of start time."""
    etime = etime.strip()
    try:
        total_secs = 0
        if "-" in etime:
            days_str, rest = etime.split("-", 1)
            total_secs += int(days_str) * 86400
            etime = rest
        parts = etime.split(":")
        if len(parts) == 3:
            total_secs += int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            total_secs += int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 1:
            total_secs += int(parts[0])
        return int((time.time() - total_secs) * 1000)
    except Exception:
        return 0


def _get_process_sessions() -> list[dict]:
    """Discover claude processes via ps that aren't tracked elsewhere."""
    sessions = []
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,etime,command"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            etime = parts[1]
            cmd = parts[2]
            if not (cmd.startswith("claude") or cmd.startswith("/") and "/claude" in cmd.split()[0]):
                continue
            if any(skip in cmd for skip in ["claude-monitor", "claude-web", "hook", "uv run", "python", "/bin/zsh", "/bin/bash", "git"]):
                continue
            started_at = _parse_etime(etime)
            flags = cmd.split()
            kind = "cli"
            for flag in flags:
                if flag in ("--resume", "-r"):
                    kind = "resumed"
                elif flag == "--remote":
                    kind = "remote"
            cwd = ""
            try:
                lsof_result = subprocess.run(
                    ["lsof", "-p", str(pid), "-Fn", "-d", "cwd"],
                    capture_output=True, text=True, timeout=3,
                )
                for lsof_line in lsof_result.stdout.split("\n"):
                    if lsof_line.startswith("n/"):
                        cwd = lsof_line[1:]
                        break
            except Exception:
                pass
            sessions.append({
                "pid": pid,
                "cwd": cwd,
                "kind": kind,
                "entrypoint": "cli",
                "startedAt": started_at,
                "sessionId": "",
                "title": "",
                "_cmd": cmd,
            })
    except Exception:
        pass
    return sessions


def _resolve_session_id(pid: int, cwd: str) -> str:
    """Try to find a sessionId for a process by matching JSONL files with recent mtime."""
    if cwd and cwd != "/":
        project_key = cwd.replace("/", "-")
        for prefix in [project_key, "-" + project_key.lstrip("-")]:
            project_dir = PROJECTS_DIR / prefix
            if project_dir.exists():
                best_jsonl = None
                best_mtime = 0.0
                for jsonl in project_dir.glob("*.jsonl"):
                    mt = jsonl.stat().st_mtime
                    if mt > best_mtime:
                        best_mtime = mt
                        best_jsonl = jsonl
                if best_jsonl and (time.time() - best_mtime < 86400):
                    return best_jsonl.stem
    return ""


def _resolve_project_cwd(session_id: str) -> str:
    """Reverse-map a sessionId to a project CWD from the projects dir name."""
    if not session_id:
        return ""
    for jsonl in PROJECTS_DIR.rglob(f"{session_id}.jsonl"):
        project_dir_name = jsonl.parent.name
        decoded = "/" + project_dir_name.lstrip("-").replace("-", "/")
        if os.path.isdir(decoded):
            return decoded
    return ""


def get_active_sessions() -> list[dict]:
    """Discover sessions from all sources: metadata files, IDE locks, and live processes."""
    seen_pids: dict[int, dict] = {}

    # Source 1: session metadata files (highest fidelity)
    if SESSIONS_DIR.exists():
        for f in SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                seen_pids[data.get("pid", 0)] = data
            except Exception:
                continue

    # Source 2: IDE lock files (VS Code, JetBrains)
    for data in _get_ide_sessions():
        pid = data.get("pid", 0)
        if pid not in seen_pids:
            seen_pids[pid] = data

    # Source 3: live processes (fills in anything not tracked above)
    for data in _get_process_sessions():
        pid = data.get("pid", 0)
        if pid in seen_pids:
            existing = seen_pids[pid]
            if not existing.get("cwd") and data.get("cwd"):
                existing["cwd"] = data["cwd"]
            if not existing.get("kind") or existing["kind"] == "interactive":
                existing.setdefault("kind", data.get("kind", "cli"))
            if data.get("_cmd"):
                existing["_cmd"] = data["_cmd"]
        else:
            if not data.get("sessionId"):
                data["sessionId"] = _resolve_session_id(pid, data.get("cwd", ""))
            if (not data.get("cwd") or data["cwd"] == "/") and data.get("sessionId"):
                resolved_cwd = _resolve_project_cwd(data["sessionId"])
                if resolved_cwd:
                    data["cwd"] = resolved_cwd
            seen_pids[pid] = data

    sessions = []
    for data in seen_pids.values():
        try:
            sessions.append(_enrich_session(data))
        except Exception:
            continue
    return sorted(sessions, key=lambda s: s.get("startedAt", 0), reverse=True)


# ── Session activity ─────────────────────────────────────────────


def get_session_activity(session_id: str, max_entries: int = 10) -> list[dict]:
    """Get recent tool calls from a session's JSONL log."""
    entries = []
    for jsonl in PROJECTS_DIR.rglob(f"{session_id}.jsonl"):
        try:
            with open(jsonl, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 32768))
                tail = f.read().decode("utf-8", errors="replace")
            for line in tail.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "assistant" and "message" in entry:
                        msg = entry["message"]
                        if isinstance(msg, dict) and msg.get("role") == "assistant":
                            for block in msg.get("content", []):
                                if block.get("type") == "tool_use":
                                    entries.append({
                                        "tool": block.get("name", "?"),
                                        "ts": entry.get("timestamp", ""),
                                        "ts_ago": iso_ago(entry.get("timestamp", "")),
                                        "input_preview": _preview_input(block.get("input", {})),
                                    })
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            continue
    return entries[-max_entries:]


def get_session_tool_stats(session_id: str) -> dict:
    """Analyze tool usage patterns for a session."""
    tool_counts: dict[str, int] = {}
    tool_targets: dict[str, list[str]] = {}
    call_sequence: list[dict] = []
    error_messages: list[dict] = []
    error_count = 0
    total_calls = 0
    last_tool_id = None

    for jsonl in PROJECTS_DIR.rglob(f"{session_id}.jsonl"):
        try:
            with open(jsonl, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 131072))
                tail = f.read().decode("utf-8", errors="replace")

            for line in tail.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    etype = entry.get("type", "")

                    if etype == "assistant" and "message" in entry:
                        msg = entry["message"]
                        if isinstance(msg, dict) and msg.get("role") == "assistant":
                            for block in msg.get("content", []):
                                if block.get("type") == "tool_use":
                                    tool = block.get("name", "?")
                                    tool_counts[tool] = tool_counts.get(tool, 0) + 1
                                    total_calls += 1
                                    ts = entry.get("timestamp", "")
                                    inp = block.get("input", {})
                                    target = _preview_input(inp) if isinstance(inp, dict) else ""
                                    tool_targets.setdefault(tool, []).append(target)
                                    last_tool_id = block.get("id", "")
                                    call_sequence.append({
                                        "tool": tool, "target": target, "ts": ts,
                                        "ts_ago": iso_ago(ts),
                                        "tool_id": last_tool_id,
                                    })

                    elif etype == "tool-result":
                        msg = entry.get("message", {})
                        error_text = _extract_error(msg)
                        if error_text:
                            error_count += 1
                            error_messages.append({
                                "ts": entry.get("timestamp", ""),
                                "ts_ago": iso_ago(entry.get("timestamp", "")),
                                "tool_id": msg.get("tool_use_id", last_tool_id or ""),
                                "message": error_text[:200],
                            })
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            continue

    repeated = []
    for tool, targets in tool_targets.items():
        counts = Counter(targets[-20:])
        for target, count in counts.most_common(3):
            if count >= 3 and target:
                matching_calls = [
                    c for c in call_sequence[-30:]
                    if c["tool"] == tool and c["target"] == target
                ]
                repeated.append({
                    "tool": tool, "target": target, "count": count,
                    "calls": matching_calls[-6:],
                })

    return {
        "tool_counts": tool_counts,
        "total_calls": total_calls,
        "error_count": error_count,
        "error_messages": error_messages[-10:],
        "repeated": repeated,
        "call_sequence": call_sequence[-20:],
    }


def get_created_files(session_id: str, max_files: int = 30) -> list[str]:
    """Get files created via the Write tool in a session."""
    files: list[str] = []
    seen: set[str] = set()
    for jsonl in PROJECTS_DIR.rglob(f"{session_id}.jsonl"):
        try:
            with open(jsonl, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 131072))
                tail = f.read().decode("utf-8", errors="replace")
            for line in tail.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "assistant":
                        msg = entry.get("message", {})
                        if isinstance(msg, dict):
                            for block in msg.get("content", []):
                                if (isinstance(block, dict)
                                        and block.get("type") == "tool_use"
                                        and block.get("name") == "Write"):
                                    fp = block.get("input", {}).get("file_path", "")
                                    if fp and fp not in seen:
                                        seen.add(fp)
                                        files.append(fp)
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            continue
    return files[-max_files:]


# ── Agent data ───────────────────────────────────────────────────


def get_subagents() -> list[dict]:
    """Find all subagent metadata across all projects."""
    agents = []
    for meta in PROJECTS_DIR.rglob("subagents/*.meta.json"):
        try:
            data = json.loads(meta.read_text())
            session_dir = meta.parent.parent.name
            jsonl = meta.with_suffix("").with_suffix(".jsonl")
            data["session"] = session_dir[:8]
            data["agent_id"] = meta.stem.replace(".meta", "")
            data["has_log"] = jsonl.exists()
            if jsonl.exists():
                data["log_size"] = jsonl.stat().st_size
                mtime = datetime.datetime.fromtimestamp(jsonl.stat().st_mtime, tz=datetime.UTC)
                data["last_modified"] = iso_ago(mtime.isoformat())
            agents.append(data)
        except Exception:
            continue
    return agents


def get_agent_tree_events(max_events: int = 30) -> list[dict]:
    """Get recent agent spawn events from watchdog logs."""
    events = []
    if not AGENT_TREE_DIR.exists():
        return events
    for f in sorted(AGENT_TREE_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            for line in f.read_text().strip().split("\n"):
                if line.strip():
                    ev = json.loads(line)
                    ev["ts_ago"] = iso_ago(ev.get("ts", ""))
                    events.append(ev)
        except Exception:
            continue
    events.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return events[:max_events]


# ── Infrastructure ───────────────────────────────────────────────


def get_alerts(max_alerts: int = 15) -> list[dict]:
    """Read recent alerts from the shared alerts JSONL."""
    alerts = []
    if not ALERTS_FILE.exists():
        return alerts
    try:
        with open(ALERTS_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            tail = f.read().decode("utf-8", errors="replace")
        for line in tail.strip().split("\n"):
            if line.strip():
                try:
                    alerts.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    alerts.sort(key=lambda a: a.get("ts", ""), reverse=True)
    return alerts[:max_alerts]


def _worktree_dirs() -> list[Path]:
    """Collect all .claude/worktrees dirs: top-level ~/.claude and one level deep (subrepos)."""
    dirs = []
    top = CLAUDE_DIR / ".claude" / "worktrees"
    if top.exists():
        dirs.append(top)
    try:
        for sub in sorted(CLAUDE_DIR.iterdir()):
            if sub.is_dir() and not sub.name.startswith("."):
                candidate = sub / ".claude" / "worktrees"
                if candidate.exists():
                    dirs.append(candidate)
    except Exception:
        pass
    return dirs


def get_worktrees() -> list[dict]:
    """Find active git worktrees under ~/.claude/**/.claude/worktrees/."""
    worktrees = []
    for wt_dir in _worktree_dirs():
        for d in sorted(wt_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            wt = {"name": d.name, "path": str(d)}
            try:
                head_file = d / ".git"
                if head_file.exists():
                    result = subprocess.run(
                        ["git", "-C", str(d), "branch", "--show-current"],
                        capture_output=True, text=True, timeout=5,
                    )
                    wt["branch"] = result.stdout.strip() or "detached"
                else:
                    wt["branch"] = "?"
            except Exception:
                wt["branch"] = "?"
            try:
                result = subprocess.run(
                    ["git", "-C", str(d), "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5,
                )
                changes = [line for line in result.stdout.strip().split("\n") if line.strip()]
                wt["dirty"] = len(changes)
            except Exception:
                wt["dirty"] = -1
            try:
                newest = max(
                    (f.stat().st_mtime for f in d.rglob("*") if f.is_file() and ".git" not in f.parts),
                    default=0,
                )
                if newest:
                    wt["last_active"] = iso_ago(
                        datetime.datetime.fromtimestamp(newest, tz=datetime.UTC).isoformat()
                    )
                else:
                    wt["last_active"] = "?"
            except Exception:
                wt["last_active"] = "?"
            worktrees.append(wt)
    return worktrees


def get_session_estimates() -> dict[str, dict]:
    """Estimate token usage per active session from JSONL entry counts and sizes."""
    estimates = {}
    for s in get_active_sessions():
        sid = s.get("sessionId", "")
        if not sid:
            continue
        for jsonl in PROJECTS_DIR.rglob(f"{sid}.jsonl"):
            try:
                size_kb = jsonl.stat().st_size / 1024
                user_msgs = 0
                assistant_msgs = 0
                tool_calls = 0
                with open(jsonl, "rb") as f:
                    f.seek(0, 2)
                    total_size = f.tell()
                    sample_offset = max(0, total_size - 65536)
                    f.seek(sample_offset)
                    tail = f.read().decode("utf-8", errors="replace")
                    scale = total_size / max(1, total_size - sample_offset)

                for line in tail.strip().split("\n"):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        t = entry.get("type", "")
                        if t == "user":
                            user_msgs += 1
                        elif t == "assistant":
                            assistant_msgs += 1
                            msg = entry.get("message", {})
                            if isinstance(msg, dict):
                                for block in msg.get("content", []):
                                    if isinstance(block, dict) and block.get("type") == "tool_use":
                                        tool_calls += 1
                    except (json.JSONDecodeError, KeyError):
                        continue

                est_tokens = int(total_size / 4)
                subagent_dir = jsonl.parent / sid / "subagents"
                subagent_count = len(list(subagent_dir.glob("*.meta.json"))) if subagent_dir.exists() else 0

                estimates[sid] = {
                    "log_kb": round(size_kb, 1),
                    "est_tokens_k": round(est_tokens / 1000),
                    "user_msgs": int(user_msgs * scale),
                    "assistant_msgs": int(assistant_msgs * scale),
                    "tool_calls": int(tool_calls * scale),
                    "subagents": subagent_count,
                    "alive": s.get("alive", False),
                    "pid": s.get("pid", 0),
                }
            except Exception:
                continue
    return estimates


def get_stale_sessions() -> list[dict]:
    """Find session files whose PIDs are dead."""
    stale = []
    if not SESSIONS_DIR.exists():
        return stale
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if not pid_alive(data.get("pid", 0)):
                data["file"] = f.name
                data["age"] = time_ago(data.get("startedAt", 0))
                stale.append(data)
        except Exception:
            continue
    return stale


def detect_issues() -> list[dict]:
    """Proactively detect issues across all active sessions."""
    issues = []
    sessions = get_active_sessions()
    estimates = get_session_estimates()

    for s in sessions:
        sid = s.get("sessionId", "")
        pid = s.get("pid", 0)
        alive = s.get("alive", False)

        if not alive:
            issues.append({
                "severity": "info",
                "type": "dead-session",
                "detail": f"PID {pid} is dead but session file remains",
                "session": str(pid),
            })
            continue

        est = estimates.get(sid, {})
        tokens_k = est.get("est_tokens_k", 0)

        if tokens_k > 500:
            issues.append({
                "severity": "warn",
                "type": "high-tokens",
                "detail": f"~{tokens_k}k tokens — consider /compact or new session",
                "session": str(pid),
            })
        elif tokens_k > 300:
            issues.append({
                "severity": "info",
                "type": "token-usage",
                "detail": f"~{tokens_k}k tokens used — approaching high usage",
                "session": str(pid),
            })

        stats = get_session_tool_stats(sid)

        for rep in stats.get("repeated", []):
            evidence_lines = []
            for c in rep.get("calls", []):
                evidence_lines.append(f"  {iso_ago(c['ts']):>8}  {c['tool']:12} {c['target']}")
            issues.append({
                "severity": "warn",
                "type": "possible-loop",
                "detail": f"{rep['tool']} hit '{rep['target'][:40]}' {rep['count']}x in recent calls",
                "session": str(pid),
                "evidence": evidence_lines,
            })

        err = stats.get("error_count", 0)
        total = stats.get("total_calls", 0)
        if total > 5 and err / total > 0.3:
            err_evidence = []
            for em in stats.get("error_messages", [])[-5:]:
                err_evidence.append(f"  {iso_ago(em['ts']):>8}  {em['message'][:120]}")
            issues.append({
                "severity": "warn",
                "type": "high-error-rate",
                "detail": f"{err}/{total} tool calls had errors ({int(err/total*100)}%)",
                "session": str(pid),
                "evidence": err_evidence,
            })
        elif stats.get("error_messages"):
            for em in stats.get("error_messages", [])[-3:]:
                issues.append({
                    "severity": "info",
                    "type": "tool-error",
                    "detail": em["message"][:100],
                    "session": str(pid),
                    "evidence": [f"  {iso_ago(em['ts'])}  {em['message'][:200]}"],
                })

        if not s.get("has_git") and tokens_k > 100:
            issues.append({
                "severity": "info",
                "type": "no-git",
                "detail": f"No git repo — work may not be tracked ({tokens_k}k tokens used)",
                "session": str(pid),
            })

        agent_count = est.get("subagents", 0)
        if agent_count > 8:
            issues.append({
                "severity": "info",
                "type": "many-agents",
                "detail": f"{agent_count} subagents spawned — check if all are needed",
                "session": str(pid),
            })

        try:
            transcript = next(PROJECTS_DIR.rglob(f"{sid}.jsonl"), None)
            if transcript:
                sstats = _session_compute(transcript, state_dir=_MONITOR_STATE)
                th = _budget_load(cwd=sstats.cwd or s.get("cwd"))
                if is_opus_overkill(
                    sstats,
                    min_turns=th.sonnet_nudge_min_turns,
                    ratio=th.sonnet_nudge_tool_ratio,
                ):
                    total_mix = sum(sstats.tool_mix_last_20.values())
                    mech = sum(n for name, n in sstats.tool_mix_last_20.items()
                               if name in {"Bash", "Grep", "Read", "Edit"})
                    issues.append({
                        "severity": "info",
                        "type": "opus-overkill",
                        "detail": (f"Opus session: last {total_mix} tool calls are "
                                   f"{int(mech/total_mix*100)}% Bash/Grep/Read/Edit "
                                   f"(${sstats.cumulative_cost_usd:.2f} so far)"),
                        "session": str(pid),
                    })
        except Exception:
            pass

    stale = get_stale_sessions()
    if len(stale) > 2:
        issues.append({
            "severity": "info",
            "type": "stale-cleanup",
            "detail": f"{len(stale)} dead session files — consider cleanup",
            "session": "—",
        })

    hooks_dir = CLAUDE_DIR / "hooks"
    if hooks_dir.exists():
        for h in hooks_dir.glob("*.sh"):
            if not os.access(h, os.X_OK):
                issues.append({
                    "severity": "warn",
                    "type": "hook-not-executable",
                    "detail": f"{h.name} is not executable — hook won't fire",
                    "session": "—",
                })

    for wt in get_worktrees():
        if wt.get("dirty", 0) > 5:
            issues.append({
                "severity": "info",
                "type": "dirty-worktree",
                "detail": f"Worktree '{wt['name']}' has {wt['dirty']} uncommitted changes",
                "session": "—",
            })

    issues.sort(key=lambda i: (0 if i["severity"] == "warn" else 1, i["type"]))

    for a in get_alerts(max_alerts=5):
        issues.append({
            "severity": a.get("severity", "info"),
            "type": a.get("type", "hook-alert"),
            "detail": a.get("detail", ""),
            "session": a.get("session", "—")[:8],
        })

    return issues


def get_health() -> dict:
    """Quick health checks on the Claude Code config."""
    health = {}

    hooks_dir = CLAUDE_DIR / "hooks"
    if hooks_dir.exists():
        hooks = list(hooks_dir.glob("*.sh"))
        health["hooks"] = f"{len(hooks)} active"
        for h in hooks:
            if not os.access(h, os.X_OK):
                health[f"hook:{h.name}"] = "NOT EXECUTABLE"
    else:
        health["hooks"] = "none"

    memory_count = 0
    for mem_dir in PROJECTS_DIR.rglob("memory"):
        if mem_dir.is_dir():
            memory_count += len(list(mem_dir.glob("*.md")))
    health["memory_files"] = str(memory_count)

    stale = get_stale_sessions()
    health["stale_sessions"] = str(len(stale))

    wts = get_worktrees()
    dirty_wts = [w for w in wts if w.get("dirty", 0) > 0]
    health["worktrees"] = f"{len(wts)} ({len(dirty_wts)} dirty)" if wts else "0"

    if _CLAUDE_STATE.exists():
        state_files = list(_CLAUDE_STATE.rglob("*"))
        health["state_files"] = str(len([f for f in state_files if f.is_file()]))

    return health


# ── History / trends ─────────────────────────────────────────────

SESSION_LOG_FILE = _MONITOR_STATE / "session_log.jsonl"

_COST_TIERS = ("cost_soft", "cost_loud", "cost_alarm")
_CTX_TIERS = ("ctx_warn", "ctx_alarm")


def _iter_jsonl(path: Path):
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _day_key(iso: str) -> str:
    return iso[:10] if iso else ""


def get_history(window_days: int = 14) -> dict:
    """Aggregate session_log.jsonl + alerts.jsonl over the last N days.

    Returns per-day rollups, top projects by spend, model split, and
    threshold-fire counts by tier. Missing data degrades to zeros/None.
    """
    today = datetime.date.today()
    start = today - datetime.timedelta(days=window_days - 1)
    day_keys = [(start + datetime.timedelta(days=i)).isoformat() for i in range(window_days)]
    days_idx = {d: i for i, d in enumerate(day_keys)}

    empty_day = lambda: {"cost_usd": 0.0, "sessions": 0, "tool_calls": 0, "opus_cost": 0.0, "sonnet_cost": 0.0}
    days = [empty_day() for _ in day_keys]

    project_totals: dict[str, dict] = {}
    model_totals: dict[str, dict] = {}
    total_cost = 0.0
    total_sessions = 0
    total_tools = 0

    for entry in _iter_jsonl(SESSION_LOG_FILE):
        day = _day_key(entry.get("recorded_at", ""))
        if day not in days_idx:
            continue
        idx = days_idx[day]
        cost = float(entry.get("cumulative_cost_usd") or 0.0)
        mix = entry.get("tool_mix_last_20") or {}
        tool_calls = sum(mix.values()) if isinstance(mix, dict) else 0
        model = (entry.get("model") or "other").lower()
        cwd = entry.get("cwd") or ""

        days[idx]["cost_usd"] += cost
        days[idx]["sessions"] += 1
        days[idx]["tool_calls"] += tool_calls
        if model == "opus":
            days[idx]["opus_cost"] += cost
        elif model == "sonnet":
            days[idx]["sonnet_cost"] += cost

        if cwd:
            p = project_totals.setdefault(cwd, {"cost_usd": 0.0, "sessions": 0})
            p["cost_usd"] += cost
            p["sessions"] += 1

        m = model_totals.setdefault(model, {"cost_usd": 0.0, "sessions": 0})
        m["cost_usd"] += cost
        m["sessions"] += 1

        total_cost += cost
        total_sessions += 1
        total_tools += tool_calls

    # Threshold-fire counts from alerts.jsonl (full file; we filter by date).
    fires_by_day = [dict.fromkeys(_COST_TIERS + _CTX_TIERS + ("loop-detected",), 0) for _ in day_keys]
    fire_totals = dict.fromkeys(_COST_TIERS + _CTX_TIERS + ("loop-detected",), 0)

    for entry in _iter_jsonl(ALERTS_FILE):
        day = _day_key(entry.get("ts", ""))
        if day not in days_idx:
            continue
        tier = entry.get("type", "")
        if tier not in fire_totals:
            continue
        fires_by_day[days_idx[day]][tier] += 1
        fire_totals[tier] += 1

    # Round day values for JSON compactness.
    for d in days:
        d["cost_usd"] = round(d["cost_usd"], 2)
        d["opus_cost"] = round(d["opus_cost"], 2)
        d["sonnet_cost"] = round(d["sonnet_cost"], 2)

    top_projects = sorted(
        ({"cwd": k.replace(str(Path.home()), "~"), "cost_usd": round(v["cost_usd"], 2), "sessions": v["sessions"]}
         for k, v in project_totals.items()),
        key=lambda r: r["cost_usd"], reverse=True,
    )[:5]

    model_split = {
        k: {"cost_usd": round(v["cost_usd"], 2), "sessions": v["sessions"]}
        for k, v in sorted(model_totals.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)
    }

    return {
        "window_days": window_days,
        "days": [{"date": d, **days[i]} for i, d in enumerate(day_keys)],
        "fires_by_day": [{"date": d, **fires_by_day[i]} for i, d in enumerate(day_keys)],
        "fire_totals": fire_totals,
        "top_projects": top_projects,
        "model_split": model_split,
        "totals": {
            "cost_usd": round(total_cost, 2),
            "sessions": total_sessions,
            "tool_calls": total_tools,
        },
    }


# ── Web snapshot ─────────────────────────────────────────────────


def snapshot() -> dict:
    """Full state snapshot for the web dashboard. Called every 2s by the SSE stream."""
    sessions = get_active_sessions()
    estimates = get_session_estimates()

    session_data = []
    for s in sessions:
        sid = s.get("sessionId", "")
        est = estimates.get(sid, {})
        # Normalise the kind label for IDE sessions
        kind = s.get("kind", s.get("entrypoint", "cli"))
        entrypoint = s.get("entrypoint", "")
        if kind in ("interactive",):
            kind = "cli"
        if kind == "ide" and entrypoint:
            if "code" in entrypoint.lower():
                kind = "vscode"
            elif any(x in entrypoint.lower() for x in ("rider", "idea", "pycharm", "goland", "webstorm")):
                kind = "jetbrains"
        cwd = s.get("cwd", "")
        cwd_display = cwd.replace(str(Path.home()), "~")
        session_data.append({
            "pid": s.get("pid"),
            "session_id": sid,
            "kind": kind,
            "alive": s.get("alive", False),
            "runtime": s.get("runtime", "?"),
            "title": s.get("title", ""),
            "cwd": cwd_display,
            "has_git": s.get("has_git", False),
            "tokens_k": est.get("est_tokens_k", 0),
            "tool_calls": est.get("tool_calls", 0),
            "subagents": est.get("subagents", 0),
        })

    # Tool stats for alive sessions only
    tools: dict[str, dict] = {}
    for s in sessions:
        if not s.get("alive"):
            continue
        sid = s.get("sessionId", "")
        if not sid:
            continue
        stats = get_session_tool_stats(sid)
        tools[sid] = {
            "breakdown": stats.get("tool_counts", {}),
            "error_count": stats.get("error_count", 0),
            "recent": stats.get("call_sequence", [])[-8:],
            "created_files": get_created_files(sid),
        }

    return {
        "ts": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
        "sessions": session_data,
        "issues": detect_issues(),
        "agent_events": get_agent_tree_events(max_events=30),
        "subagents": get_subagents(),
        "tools": tools,
        "worktrees": get_worktrees(),
        "health": get_health(),
        "history": get_history(),
    }

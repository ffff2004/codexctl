"""Best-effort read-only rollout reader.

Rollout persistence is an internal Codex storage format. ``codexctl`` touches
it only for optional enrichment (context-window usage) and diagnostics, never
as a primary history source. The reader parses narrowly, ignores unknown
records, and returns ``None`` instead of raising on any format drift.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .model import ContextUsage


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)
    return Path.home() / ".codex"


def find_rollout(thread_id: str, home: Path | None = None) -> Path | None:
    """Locate the rollout file for a thread id, if present.

    Rollout files are named ``rollout-<timestamp>-<session_id>.jsonl`` under
    ``<CODEX_HOME>/sessions/YYYY/MM/DD``. Root threads use their own id as
    the session id; forked threads may not match and simply yield ``None``.
    """
    home = home or codex_home()
    sessions = home / "sessions"
    if not sessions.is_dir():
        return None
    try:
        candidates = sorted(
            sessions.glob(f"*/*/*/rollout-*-{thread_id}.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    return candidates[0] if candidates else None


def _used_tokens(total: dict) -> int | None:
    try:
        return int(total.get("input_tokens", 0)) + int(
            total.get("cached_input_tokens", 0)
        )
    except (TypeError, ValueError):
        return None


def lookup_context_usage(
    thread_id: str, home: Path | None = None
) -> ContextUsage | None:
    """Extract the latest token usage from the thread's rollout, if any.

    Scans for ``token_count`` event records and keeps the last one carrying
    both a total usage and a model context window. Unknown record types and
    malformed lines are ignored.
    """
    path = find_rollout(thread_id, home=home)
    if path is None:
        return None
    used: int | None = None
    window: int | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                total = info.get("total_token_usage")
                mw = info.get("model_context_window")
                if not isinstance(total, dict) or not isinstance(mw, int):
                    continue
                candidate = _used_tokens(total)
                if candidate is None:
                    continue
                used, window = candidate, mw
    except OSError:
        return None
    if used is None or window is None or window <= 0:
        return None
    return ContextUsage(
        used_tokens=used,
        window_tokens=window,
        ratio=round(used / window, 5),
        source="rollout",
    )


def sessions_dir_exists(home: Path | None = None) -> bool:
    """Diagnostic helper for ``doctor``: is rollout enrichment plausible?"""
    home = home or codex_home()
    return (home / "sessions").is_dir()

"""Rollout reader: best-effort, narrow parsing, never raises."""

from __future__ import annotations

import json
import os
from pathlib import Path

from codexctl import rollout


def _write_rollout(home: Path, thread_id: str, lines: list[str], name: str | None = None) -> Path:
    directory = home / "sessions" / "2026" / "08" / "15"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name or f"rollout-2026-08-15T00-00-00-{thread_id}.jsonl")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _token_count_line(input_tokens: int, cached: int, window: int) -> str:
    return json.dumps(
        {
            "timestamp": "2026-08-15T00:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 1},
                    "total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached,
                        "output_tokens": 99,
                    },
                    "model_context_window": window,
                },
            },
        }
    )


class TestFindRollout:
    def test_finds_rollout_by_thread_id(self, isolated_codex_home):
        path = _write_rollout(isolated_codex_home, "abc123", ["{}"])
        assert rollout.find_rollout("abc123") == path

    def test_missing_thread(self, isolated_codex_home):
        _write_rollout(isolated_codex_home, "abc123", ["{}"])
        assert rollout.find_rollout("other") is None

    def test_no_sessions_dir(self, isolated_codex_home):
        assert rollout.find_rollout("abc123") is None

    def test_picks_newest_by_mtime(self, isolated_codex_home):
        old = _write_rollout(
            isolated_codex_home, "abc123", ["{}"], name="rollout-2026-old-abc123.jsonl"
        )
        new = _write_rollout(
            isolated_codex_home, "abc123", ["{}"], name="rollout-2026-new-abc123.jsonl"
        )
        os.utime(old, (1_000_000_000, 1_000_000_000))
        os.utime(new, (1_900_000_000, 1_900_000_000))
        assert rollout.find_rollout("abc123") == new


class TestLookupContextUsage:
    def test_extracts_latest_token_count(self, isolated_codex_home):
        _write_rollout(
            isolated_codex_home,
            "abc123",
            [
                json.dumps({"type": "session_meta", "payload": {}}),
                _token_count_line(1000, 100, 200000),
                _token_count_line(80000, 3000, 200000),
            ],
        )
        usage = rollout.lookup_context_usage("abc123")
        assert usage is not None
        assert usage.used_tokens == 83000
        assert usage.window_tokens == 200000
        assert usage.ratio == 0.415
        assert usage.source == "rollout"

    def test_ignores_malformed_lines_and_unknown_records(self, isolated_codex_home):
        _write_rollout(
            isolated_codex_home,
            "abc123",
            [
                "not json at all",
                json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}),
                _token_count_line(50, 50, 1000),
            ],
        )
        usage = rollout.lookup_context_usage("abc123")
        assert usage is not None and usage.used_tokens == 100

    def test_none_without_usable_records(self, isolated_codex_home):
        _write_rollout(isolated_codex_home, "abc123", ["{}", "[]"])
        assert rollout.lookup_context_usage("abc123") is None
        assert rollout.lookup_context_usage("missing-thread") is None

    def test_none_for_nonpositive_window(self, isolated_codex_home):
        _write_rollout(isolated_codex_home, "abc123", [_token_count_line(10, 0, 0)])
        assert rollout.lookup_context_usage("abc123") is None


class TestSessionsDirExists:
    def test_present_and_absent(self, isolated_codex_home):
        assert rollout.sessions_dir_exists() is False
        (isolated_codex_home / "sessions").mkdir(parents=True)
        assert rollout.sessions_dir_exists() is True

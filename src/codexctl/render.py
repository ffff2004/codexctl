"""Renderers: text, json, jsonl.

Rendering is outside ``CodexCtl``. Renderers consume outcomes and projected
events; they never influence execution behavior.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .model import (
    CodexCtlError,
    ContextUsage,
    DetachedTurnStarted,
    DoctorSnapshot,
    HistorySnapshot,
    InterruptResult,
    ProjectedEvent,
    StatusSnapshot,
    SteerAcknowledged,
    ThreadListSnapshot,
    TurnTerminal,
)


# ---------------------------------------------------------------------------
# JSON documents (single source for structured shapes)
# ---------------------------------------------------------------------------


def _context_document(context: ContextUsage | None) -> dict | None:
    if context is None:
        return None
    return {
        "usedTokens": context.used_tokens,
        "windowTokens": context.window_tokens,
        "ratio": context.ratio,
        "source": context.source,
    }


def snapshot_document(outcome: Any) -> dict:
    if isinstance(outcome, DetachedTurnStarted):
        return {
            "threadId": outcome.thread_id,
            "turnId": outcome.turn_id,
            "detached": True,
        }
    if isinstance(outcome, SteerAcknowledged):
        return {"threadId": outcome.thread_id, "turnId": outcome.turn_id}
    if isinstance(outcome, InterruptResult):
        return {
            "threadId": outcome.thread_id,
            "turnId": outcome.turn_id,
            "status": outcome.status,
        }
    if isinstance(outcome, StatusSnapshot):
        doc: dict[str, Any] = {
            "threadId": outcome.thread_id,
            "status": outcome.status,
            "activeTurnId": outcome.active_turn_id,
            "context": _context_document(outcome.context),
        }
        if outcome.active_flags:
            doc["activeFlags"] = outcome.active_flags
        return doc
    if isinstance(outcome, HistorySnapshot):
        return {
            "threadId": outcome.thread_id,
            "turns": [
                {"id": turn.id, "index": turn.index, "items": turn.items}
                for turn in outcome.turns
            ],
        }
    if isinstance(outcome, ThreadListSnapshot):
        return {
            "threads": [
                {
                    "threadId": record.thread_id,
                    "status": record.status,
                    "preview": record.preview,
                    "updatedAt": record.updated_at,
                }
                for record in outcome.threads
            ]
        }
    if isinstance(outcome, DoctorSnapshot):
        return {
            "codexctlVersion": outcome.codexctl_version,
            "endpointMode": outcome.endpoint_mode,
            "codexCliVersion": outcome.codex_cli_version,
            "appServerVersion": outcome.app_server_version,
            "compatible": outcome.compatible,
            "checks": [
                {"name": check.name, "ok": check.ok, "detail": check.detail}
                for check in outcome.checks
            ],
        }
    raise TypeError(f"no JSON document for outcome {type(outcome).__name__}")


def error_document(error: CodexCtlError) -> dict:
    return {"error": error.to_document()}


def event_document(event: ProjectedEvent) -> dict:
    return event.to_document()


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def _format_tokens(count: int) -> str:
    if count >= 1000:
        return f"{round(count / 1000)}k"
    return str(count)


def format_context_line(context: ContextUsage | None) -> str | None:
    if context is None:
        return None
    percent = round(context.ratio * 100)
    return (
        f"Context: {_format_tokens(context.used_tokens)} / "
        f"{_format_tokens(context.window_tokens)} ({percent}%)"
    )


class TextRenderer:
    """Human-readable streaming and snapshot rendering."""

    def __init__(self, out: TextIO | None = None, err: TextIO | None = None) -> None:
        self._out = out if out is not None else sys.stdout
        self._err = err if err is not None else sys.stderr
        self._started_items: set[str] = set()

    def _write(self, text: str) -> None:
        self._out.write(text)
        self._out.flush()

    def stream_header(self, thread_id: str, turn_id: str) -> None:
        self._write(f"Thread: {thread_id}\nTurn:   {turn_id}\n\n")

    def event(self, event: ProjectedEvent) -> None:
        if event.type == "item/completed" and event.item is not None:
            self._render_item(event.item)
        elif event.type == "item/started" and event.item is not None:
            item = event.item
            if item.get("type") == "commandExecution":
                command = item.get("command")
                if command:
                    item_id = str(item.get("id") or "")
                    self._started_items.add(item_id)
                    self._write(f"$ {command}\n")
        elif event.type == "turn/completed":
            status = event.extra.get("status")
            label = {"completed": "Turn completed"}.get(
                str(status), f"Turn ended: {status}"
            )
            self._write(f"\n{label}\n")
        elif event.type == "error":
            error = event.extra.get("error") or {}
            self._err.write(
                f"codexctl: {error.get('code')}: {error.get('message')}\n"
            )
            self._err.flush()

    def _render_item(self, item: dict) -> None:
        kind = item.get("type")
        if kind == "agentMessage":
            text = item.get("text") or ""
            if text:
                self._write(f"\n[agent]\n{text}\n")
        elif kind == "userMessage":
            text = item.get("text") or ""
            if text:
                self._write(f"\n[user]\n{text}\n")
        elif kind == "commandExecution":
            item_id = str(item.get("id") or "")
            command = item.get("command")
            if command and item_id not in self._started_items:
                self._write(f"$ {command}\n")
            exit_code = item.get("exitCode")
            if exit_code is not None:
                self._write(f"exit {exit_code}\n")
        elif kind == "fileChange":
            for change in item.get("changes") or []:
                path = change.get("path")
                if path:
                    kind_letter = str(change.get("kind") or "M")[:1].upper() or "M"
                    self._write(f"{kind_letter} {path}\n")
        elif kind == "contextCompaction":
            self._write("[context compacted]\n")

    def stream_footer(self, terminal: TurnTerminal) -> None:
        line = format_context_line(terminal.context)
        if line:
            self._write(f"{line}\n")

    def snapshot(self, outcome: Any) -> None:
        if isinstance(outcome, DetachedTurnStarted):
            self._write(
                f"Thread: {outcome.thread_id}\nTurn:   {outcome.turn_id}\nDetached\n"
            )
        elif isinstance(outcome, SteerAcknowledged):
            self._write(
                f"Steered turn {outcome.turn_id} of thread {outcome.thread_id}\n"
            )
        elif isinstance(outcome, InterruptResult):
            self._write(
                f"Interrupted turn {outcome.turn_id} of thread "
                f"{outcome.thread_id} ({outcome.status})\n"
            )
        elif isinstance(outcome, StatusSnapshot):
            self._write(f"Thread: {outcome.thread_id}\nStatus: {outcome.status}\n")
            if outcome.active_flags:
                self._write(f"Flags:  {', '.join(outcome.active_flags)}\n")
            self._write(f"Active turn: {outcome.active_turn_id or '-'}\n")
            line = format_context_line(outcome.context)
            self._write(f"{line}\n" if line else "Context: -\n")
        elif isinstance(outcome, HistorySnapshot):
            for turn in outcome.turns:
                self._write(
                    f"Turn {turn.index} {turn.id} [{turn.status or '?'}]\n"
                )
                for item in turn.items:
                    self._write(_summarize_item(item, indent="  "))
        elif isinstance(outcome, ThreadListSnapshot):
            for record in outcome.threads:
                preview = f"  {record.preview}" if record.preview else ""
                self._write(f"{record.thread_id}  {record.status}{preview}\n")
        elif isinstance(outcome, DoctorSnapshot):
            self._write(f"codexctl version: {outcome.codexctl_version}\n")
            if outcome.codex_cli_version:
                self._write(f"codex CLI:        {outcome.codex_cli_version}\n")
            if outcome.app_server_version:
                self._write(f"app-server:       {outcome.app_server_version}\n")
            self._write(f"endpoint mode:    {outcome.endpoint_mode}\n")
            for check in outcome.checks:
                mark = "ok  " if check.ok else "FAIL"
                detail = f"  {check.detail}" if check.detail else ""
                self._write(f"  [{mark}] {check.name}{detail}\n")
            verdict = "compatible" if outcome.compatible else "not compatible"
            self._write(f"verdict: {verdict}\n")
        else:
            raise TypeError(f"no text rendering for {type(outcome).__name__}")

    def error(self, error: CodexCtlError) -> None:
        self._err.write(f"codexctl: {error.code.value}: {error.message}\n")
        self._err.flush()


def _summarize_item(item: dict, indent: str = "") -> str:
    kind = item.get("type")
    if kind == "agentMessage":
        text = (item.get("text") or "").strip()
        return f"{indent}[agent] {text}\n" if text else ""
    if kind == "userMessage":
        text = (item.get("text") or "").strip()
        return f"{indent}[user] {text}\n" if text else ""
    if kind == "commandExecution":
        exit_code = item.get("exitCode")
        suffix = f" (exit {exit_code})" if exit_code is not None else ""
        return f"{indent}$ {item.get('command')}{suffix}\n"
    if kind == "fileChange":
        lines = []
        for change in item.get("changes") or []:
            lines.append(f"{indent}~ {change.get('path')}\n")
        return "".join(lines)
    if kind == "contextCompaction":
        return f"{indent}[context compacted]\n"
    return ""


class JsonRenderer:
    """Exactly one complete JSON document on stdout."""

    def __init__(self, out: TextIO | None = None, err: TextIO | None = None) -> None:
        self._out = out if out is not None else sys.stdout
        self._err = err if err is not None else sys.stderr

    def snapshot(self, outcome: Any) -> None:
        self._out.write(json.dumps(snapshot_document(outcome), indent=2) + "\n")
        self._out.flush()

    def error(self, error: CodexCtlError) -> None:
        self._out.write(json.dumps(error_document(error)) + "\n")
        self._out.flush()


class JsonlRenderer:
    """One complete JSON object per stdout line."""

    def __init__(self, out: TextIO | None = None, err: TextIO | None = None) -> None:
        self._out = out if out is not None else sys.stdout
        self._err = err if err is not None else sys.stderr

    def stream_header(self, thread_id: str, turn_id: str) -> None:
        pass

    def event(self, event: ProjectedEvent) -> None:
        self._out.write(json.dumps(event_document(event)) + "\n")
        self._out.flush()

    def stream_footer(self, terminal: TurnTerminal) -> None:
        pass

    def snapshot_records(self, events: list[ProjectedEvent]) -> None:
        for event in events:
            self.event(event)

    def error(self, error: CodexCtlError) -> None:
        doc = {"type": "error", "error": error.to_document()}
        self._out.write(json.dumps(doc) + "\n")
        self._out.flush()

"""CodexCtl: one lifecycle-oriented entry point over the Codex runtime.

This module owns command dispatch, thread/turn orchestration, race handling,
the follow replay/live frontier, history selection, and stable error mapping.
It never exposes transport or protocol types through ``run``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Awaitable, Callable

from . import rollout
from .appserver import (
    TERMINAL_TURN_STATUSES,
    AppServerPort,
    JsonRpcError,
    UnixSocketAppServerAdapter,
    project_item,
    project_notification,
    project_thread_status,
)
from .endpoint import AppServerEndpoint, EndpointPort
from .model import (
    ContextUsage,
    CodexCtlError,
    Command,
    DetachedTurnStarted,
    Doctor,
    DoctorCheck,
    DoctorSnapshot,
    ErrorCode,
    EventStreamOutcome,
    Follow,
    History,
    HistorySnapshot,
    HistoryTurn,
    Interrupt,
    InterruptResult,
    ListThreads,
    ProjectedEvent,
    Resume,
    Start,
    Status,
    StatusSnapshot,
    Steer,
    SteerAcknowledged,
    ThreadListSnapshot,
    ThreadRecord,
    TurnTerminal,
    apply_turn_selector,
    select_replay_turns,
)

DEFAULT_APPROVAL_POLICY = "never"
DEFAULT_SANDBOX = "workspaceWrite"
INTERRUPT_WAIT_SECONDS = 120.0
INTERRUPT_POLL_INTERVAL = 0.5

ConnectFactory = Callable[[AppServerEndpoint], Awaitable[AppServerPort]]


class CodexCtl:
    """The external module: ``run(Command) -> Outcome``."""

    def __init__(
        self,
        endpoint: EndpointPort,
        connect: ConnectFactory | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._connect: ConnectFactory = connect or _default_connect

    async def run(self, command: Command) -> Any:
        if isinstance(command, Start):
            return await self._start(command)
        if isinstance(command, Resume):
            return await self._resume(command)
        if isinstance(command, Status):
            return await self._status(command)
        if isinstance(command, History):
            return await self._history(command)
        if isinstance(command, Follow):
            return await self._follow(command)
        if isinstance(command, Steer):
            return await self._steer(command)
        if isinstance(command, Interrupt):
            return await self._interrupt(command)
        if isinstance(command, ListThreads):
            return await self._list()
        if isinstance(command, Doctor):
            return await self._doctor()
        raise CodexCtlError(
            ErrorCode.APP_SERVER_PROTOCOL_ERROR,
            f"unknown command: {type(command).__name__}",
        )

    # -- plumbing -------------------------------------------------------------

    async def _open(self) -> AppServerPort:
        endpoint = await self._endpoint.resolve()
        return await self._connect(endpoint)

    @staticmethod
    def _find_active_turn(thread: dict) -> dict | None:
        for turn in reversed(thread.get("turns") or []):
            if isinstance(turn, dict) and turn.get("status") == "inProgress":
                return turn
        return None

    @staticmethod
    async def _read_thread(adapter: AppServerPort, thread_id: str) -> dict:
        try:
            resp = await adapter.request(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            )
        except JsonRpcError as exc:
            await adapter.close()
            raise _map_rpc_error(
                exc, default=ErrorCode.THREAD_NOT_FOUND, thread_id=thread_id
            ) from exc
        return resp.get("thread") or {}

    # -- start ----------------------------------------------------------------

    async def _start(self, command: Start) -> Any:
        adapter = await self._open()
        try:
            params: dict[str, Any] = {
                "approvalPolicy": DEFAULT_APPROVAL_POLICY,
                "sandbox": command.config.sandbox or DEFAULT_SANDBOX,
            }
            if command.config.cwd:
                params["cwd"] = command.config.cwd
            if command.config.model:
                params["model"] = command.config.model
            try:
                resp = await adapter.request("thread/start", params)
            except JsonRpcError as exc:
                raise _map_rpc_error(exc, default=ErrorCode.APP_SERVER_PROTOCOL_ERROR) from exc
            thread_id = (resp.get("thread") or {}).get("id")
            if not thread_id:
                raise CodexCtlError(
                    ErrorCode.APP_SERVER_PROTOCOL_ERROR,
                    "thread/start returned no thread id",
                )
            turn_id = await self._turn_start(
                adapter, thread_id, command.prompt, effort=command.config.effort
            )
        except Exception:
            await adapter.close()
            raise
        if command.detach:
            # Disconnecting never interrupts the turn.
            await adapter.close()
            return DetachedTurnStarted(thread_id=thread_id, turn_id=turn_id)
        return self._follow_live(adapter, thread_id, turn_id)

    async def _turn_start(
        self,
        adapter: AppServerPort,
        thread_id: str,
        prompt: str,
        effort: str | None = None,
    ) -> str:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if effort:
            params["effort"] = effort
        try:
            resp = await adapter.request("turn/start", params)
        except JsonRpcError as exc:
            # The actual turn/start result is authoritative for races.
            raise _map_rpc_error(
                exc, default=ErrorCode.THREAD_BUSY, thread_id=thread_id
            ) from exc
        turn_id = (resp.get("turn") or {}).get("id")
        if not turn_id:
            raise CodexCtlError(
                ErrorCode.APP_SERVER_PROTOCOL_ERROR,
                "turn/start returned no turn id",
                thread_id=thread_id,
            )
        return turn_id

    # -- resume ---------------------------------------------------------------

    async def _resume(self, command: Resume) -> Any:
        adapter = await self._open()
        try:
            try:
                resp = await adapter.request(
                    "thread/resume", {"threadId": command.thread_id}
                )
            except JsonRpcError as exc:
                raise _map_resume_error(exc, command.thread_id) from exc
            thread = resp.get("thread") or {}
            status, _ = project_thread_status(thread.get("status"))
            if status == "active" or self._find_active_turn(thread) is not None:
                raise CodexCtlError(
                    ErrorCode.THREAD_BUSY,
                    "thread has an active turn",
                    thread_id=command.thread_id,
                )
            # Resume never queues and never becomes steer: one turn/start, or fail.
            turn_id = await self._turn_start(adapter, command.thread_id, command.prompt)
        except Exception:
            await adapter.close()
            raise
        if command.detach:
            await adapter.close()
            return DetachedTurnStarted(thread_id=command.thread_id, turn_id=turn_id)
        return self._follow_live(adapter, command.thread_id, turn_id)

    # -- foreground streaming --------------------------------------------------

    def _follow_live(
        self, adapter: AppServerPort, thread_id: str, turn_id: str
    ) -> EventStreamOutcome:
        async def started(seen: set[tuple]) -> AsyncIterator[ProjectedEvent]:
            ev = ProjectedEvent(
                "turn/started", thread_id=thread_id, turn_id=turn_id, source="live"
            )
            seen.add(ev.dedup_key())
            yield ev

        return self._stream_turn(adapter, thread_id, turn_id, started)

    def _stream_turn(
        self,
        adapter: AppServerPort,
        thread_id: str,
        turn_id: str,
        prelude: Callable[[set[tuple]], AsyncIterator[ProjectedEvent]] | None = None,
    ) -> EventStreamOutcome:
        """Stream one turn: optional prelude, then live notifications.

        Shared live phase for start/resume and follow. The optional
        ``prelude`` yields its events first and registers their dedup keys in
        the shared ``seen`` set. Ends on the target turn's ``turn/completed``;
        a stream lost before that yields a deterministic error event and never
        interrupts. Always unsubscribes and closes, resolving ``result`` once.
        """
        loop = asyncio.get_running_loop()
        result: asyncio.Future[TurnTerminal] = loop.create_future()

        async def events() -> AsyncIterator[ProjectedEvent]:
            seen: set[tuple] = set()
            usage: dict | None = None
            terminal_status: str | None = None
            terminal_error: str | None = None
            try:
                if prelude is not None:
                    async for ev in prelude(seen):
                        yield ev
                async for raw in adapter.notifications():
                    params = raw.get("params") or {}
                    notif_thread = params.get("threadId")
                    if notif_thread is not None and notif_thread != thread_id:
                        continue
                    method = raw.get("method")
                    if method == "thread/tokenUsage/updated":
                        ev = project_notification(raw)
                        if ev is not None:
                            usage = ev.extra.get("usage")
                        continue
                    ev = project_notification(raw, source="live")
                    if ev is None:
                        continue
                    if ev.dedup_key() in seen:
                        continue
                    seen.add(ev.dedup_key())
                    yield ev
                    if ev.type == "turn/completed" and ev.turn_id == turn_id:
                        terminal_status = str(ev.extra.get("status") or "")
                        terminal_error = (ev.extra.get("error") or {}).get("message")
                        break
                if terminal_status is None:
                    # Stream ended before turn completion: deterministic error path.
                    yield ProjectedEvent(
                        "error",
                        thread_id=thread_id,
                        turn_id=turn_id,
                        source="live",
                        extra={
                            "error": {
                                "code": ErrorCode.APP_SERVER_PROTOCOL_ERROR.value,
                                "message": "connection closed before turn completion",
                            }
                        },
                    )
            finally:
                await adapter.unsubscribe(thread_id)
                await adapter.close()
                context = _usage_to_context(usage)
                if terminal_status is None:
                    if not result.done():
                        result.set_exception(
                            CodexCtlError(
                                ErrorCode.APP_SERVER_PROTOCOL_ERROR,
                                "connection closed before turn completion",
                                thread_id=thread_id,
                                turn_id=turn_id,
                            )
                        )
                elif not result.done():
                    result.set_result(
                        TurnTerminal(
                            thread_id=thread_id,
                            turn_id=turn_id,
                            status=terminal_status,
                            error=terminal_error,
                            context=context,
                        )
                    )

        return EventStreamOutcome(
            thread_id=thread_id, turn_id=turn_id, events=events(), result=result
        )

    # -- status ---------------------------------------------------------------

    async def _status(self, command: Status) -> StatusSnapshot:
        adapter = await self._open()
        try:
            # Strictly read-only: never thread/resume for status.
            thread = await self._read_thread(adapter, command.thread_id)
        finally:
            await adapter.close()
        status, flags = project_thread_status(thread.get("status"))
        active = self._find_active_turn(thread)
        context = rollout.lookup_context_usage(command.thread_id)
        return StatusSnapshot(
            thread_id=command.thread_id,
            status=status,
            active_flags=flags,
            active_turn_id=(active or {}).get("id"),
            context=context,
        )

    # -- history ----------------------------------------------------------------

    async def _history(self, command: History) -> HistorySnapshot:
        adapter = await self._open()
        try:
            thread = await self._read_thread(adapter, command.thread_id)
        finally:
            await adapter.close()
        turns = thread.get("turns") or []
        try:
            selected = apply_turn_selector(turns, command.selector)
        except IndexError as exc:
            # Out-of-range single index is a selector usage error (exit 2),
            # not an output-mode problem.
            raise CodexCtlError(
                ErrorCode.USAGE_ERROR,
                f"turn index out of range for thread {command.thread_id}",
                thread_id=command.thread_id,
            ) from exc
        history_turns = []
        for index, turn in selected:
            items = [
                projected
                for projected in (project_item(item) for item in turn.get("items") or [])
                if projected is not None
            ]
            history_turns.append(
                HistoryTurn(
                    id=turn.get("id") or "",
                    index=index,
                    status=str(turn.get("status") or ""),
                    items=items,
                )
            )
        return HistorySnapshot(thread_id=command.thread_id, turns=history_turns)

    # -- follow -----------------------------------------------------------------

    async def _follow(self, command: Follow) -> Any:
        adapter = await self._open()
        try:
            try:
                resp = await adapter.request(
                    "thread/resume", {"threadId": command.thread_id}
                )
            except JsonRpcError as exc:
                raise _map_resume_error(exc, command.thread_id) from exc
            thread = resp.get("thread") or {}
        except Exception:
            await adapter.close()
            raise
        active = self._find_active_turn(thread)
        if active is None:
            await adapter.unsubscribe(command.thread_id)
            await adapter.close()
            raise CodexCtlError(
                ErrorCode.NO_ACTIVE_TURN,
                "thread has no active turn to follow",
                thread_id=command.thread_id,
            )
        active_turn_id = active.get("id") or ""
        turns = thread.get("turns") or []
        replay_pairs = select_replay_turns(turns, command.replay)

        async def replay(seen: set[tuple]) -> AsyncIterator[ProjectedEvent]:
            # Replay the selected continuous suffix from the reconstructed
            # snapshot, then continue live. Events visible in both are
            # deduplicated by stable Codex identities.
            for index, turn in replay_pairs:
                turn_id = turn.get("id")
                for projected in (project_item(item) for item in turn.get("items") or []):
                    if projected is None:
                        continue
                    for phase in ("item/started", "item/completed"):
                        ev = ProjectedEvent(
                            phase,
                            thread_id=command.thread_id,
                            turn_id=turn_id,
                            item=projected,
                            source="replay",
                            turn_index=index,
                        )
                        if ev.dedup_key() in seen:
                            continue
                        if turn.get("status") != "inProgress" or phase == "item/completed":
                            # Only events actually emitted occupy ``seen``: a
                            # suppressed replay event (item/started of an
                            # in-progress turn) must not swallow its live
                            # delivery.
                            seen.add(ev.dedup_key())
                            yield ev
                if turn.get("status") != "inProgress":
                    completed = ProjectedEvent(
                        "turn/completed",
                        thread_id=command.thread_id,
                        turn_id=turn_id,
                        source="replay",
                        turn_index=index,
                        extra={"status": turn.get("status")},
                    )
                    if completed.dedup_key() not in seen:
                        seen.add(completed.dedup_key())
                        yield completed

        return self._stream_turn(adapter, command.thread_id, active_turn_id, replay)

    # -- steer ------------------------------------------------------------------

    async def _steer(self, command: Steer) -> SteerAcknowledged:
        adapter = await self._open()
        try:
            thread = await self._read_thread(adapter, command.thread_id)
            active = self._find_active_turn(thread)
            if active is None:
                raise CodexCtlError(
                    ErrorCode.NO_ACTIVE_TURN,
                    "thread has no active turn to steer",
                    thread_id=command.thread_id,
                )
            expected_turn_id = active.get("id") or ""
            try:
                resp = await adapter.request(
                    "turn/steer",
                    {
                        "threadId": command.thread_id,
                        "input": [{"type": "text", "text": command.input}],
                        "expectedTurnId": expected_turn_id,
                    },
                )
            except JsonRpcError as exc:
                raise _map_steer_error(exc, command.thread_id) from exc
            return SteerAcknowledged(
                thread_id=command.thread_id,
                turn_id=resp.get("turnId") or expected_turn_id,
            )
        finally:
            await adapter.close()

    # -- interrupt ----------------------------------------------------------------

    async def _interrupt(self, command: Interrupt) -> InterruptResult:
        adapter = await self._open()
        try:
            thread = await self._read_thread(adapter, command.thread_id)
            active = self._find_active_turn(thread)
            if active is None:
                raise CodexCtlError(
                    ErrorCode.NO_ACTIVE_TURN,
                    "thread has no active turn to interrupt",
                    thread_id=command.thread_id,
                )
            turn_id = active.get("id") or ""
            try:
                await adapter.request(
                    "turn/interrupt",
                    {"threadId": command.thread_id, "turnId": turn_id},
                )
            except JsonRpcError as exc:
                # A rejected interrupt means the target turn changed or ended;
                # never retry against a different turn.
                raise _map_interrupt_error(
                    exc, command.thread_id
                ) from exc
            deadline = time.monotonic() + INTERRUPT_WAIT_SECONDS
            while True:
                current = await self._read_thread(adapter, command.thread_id)
                target = next(
                    (
                        turn
                        for turn in current.get("turns") or []
                        if isinstance(turn, dict) and turn.get("id") == turn_id
                    ),
                    None,
                )
                status = (target or {}).get("status")
                if status in TERMINAL_TURN_STATUSES:
                    return InterruptResult(
                        thread_id=command.thread_id, turn_id=turn_id, status=str(status)
                    )
                if time.monotonic() >= deadline:
                    raise CodexCtlError(
                        ErrorCode.APP_SERVER_PROTOCOL_ERROR,
                        "timed out waiting for the interrupted turn to terminate",
                        thread_id=command.thread_id,
                        turn_id=turn_id,
                    )
                await asyncio.sleep(INTERRUPT_POLL_INTERVAL)
        finally:
            await adapter.close()

    # -- list ---------------------------------------------------------------------

    async def _list(self) -> ThreadListSnapshot:
        adapter = await self._open()
        try:
            records: list[ThreadRecord] = []
            cursor: str | None = None
            for _ in range(25):  # pagination safety cap
                params: dict[str, Any] = {"limit": 100}
                if cursor is not None:
                    params["cursor"] = cursor
                try:
                    resp = await adapter.request("thread/list", params)
                except JsonRpcError as exc:
                    raise _map_rpc_error(
                        exc, default=ErrorCode.APP_SERVER_PROTOCOL_ERROR
                    ) from exc
                for entry in resp.get("data") or []:
                    if not isinstance(entry, dict):
                        continue
                    status, _ = project_thread_status(entry.get("status"))
                    records.append(
                        ThreadRecord(
                            thread_id=entry.get("id") or "",
                            status=status,
                            preview=entry.get("preview") or None,
                            updated_at=entry.get("updatedAt"),
                        )
                    )
                cursor = resp.get("nextCursor")
                if not cursor:
                    break
            return ThreadListSnapshot(threads=records)
        finally:
            await adapter.close()

    # -- doctor ---------------------------------------------------------------------

    async def _doctor(self) -> DoctorSnapshot:
        from .appserver import CLIENT_VERSION

        checks: list[DoctorCheck] = []
        compatible = False
        app_server_version: str | None = None
        endpoint_mode = getattr(self._endpoint, "mode", "managed")
        try:
            endpoint = await self._endpoint.resolve()
            checks.append(
                DoctorCheck("endpoint reachable", True, str(endpoint.socket_path))
            )
            if endpoint.runtime_pid is not None:
                checks.append(DoctorCheck("runtime pid", True, str(endpoint.runtime_pid)))
        except CodexCtlError as exc:
            checks.append(DoctorCheck("endpoint reachable", False, exc.message))
            return DoctorSnapshot(
                codexctl_version=CLIENT_VERSION,
                endpoint_mode=endpoint_mode,
                checks=checks,
                compatible=False,
            )
        try:
            adapter = await self._connect(endpoint)
            checks.append(DoctorCheck("initialize handshake", True, None))
            app_server_version = getattr(adapter, "app_server_version", None)
            compatible = True
            await adapter.close()
        except CodexCtlError as exc:
            checks.append(DoctorCheck("initialize handshake", False, exc.message))
        codex_cli_version = self._endpoint.probe_cli_version()
        if endpoint_mode == "managed":
            checks.append(
                DoctorCheck(
                    "codex cli version",
                    codex_cli_version is not None,
                    codex_cli_version,
                )
            )
        checks.append(
            DoctorCheck(
                "context usage enrichment",
                rollout.sessions_dir_exists(),
                "rollout sessions directory present"
                if rollout.sessions_dir_exists()
                else "no rollout sessions directory",
            )
        )
        return DoctorSnapshot(
            codexctl_version=CLIENT_VERSION,
            endpoint_mode=endpoint_mode,
            checks=checks,
            codex_cli_version=codex_cli_version,
            app_server_version=app_server_version or endpoint.runtime_version,
            compatible=compatible,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _default_connect(endpoint: AppServerEndpoint) -> AppServerPort:
    return await UnixSocketAppServerAdapter.connect(endpoint.socket_path)


def _usage_to_context(usage: dict | None) -> ContextUsage | None:
    if not usage:
        return None
    return ContextUsage(
        used_tokens=usage.get("usedTokens", 0),
        window_tokens=usage.get("windowTokens", 0),
        ratio=usage.get("ratio", 0.0),
        source="live",
    )


# Lowercase message markers that identify a missing thread. Shared by the
# generic RPC mapping and resume; interrupt deliberately avoids keywords.
_THREAD_NOT_FOUND_MARKERS = (
    "not found",
    "no thread",
    "unknown thread",
    "does not exist",
)


def _map_rpc_error(
    exc: JsonRpcError, *, default: ErrorCode, thread_id: str | None = None
) -> CodexCtlError:
    message = exc.message or ""
    lowered = message.lower()
    info = ""
    if isinstance(exc.data, dict):
        info = str(exc.data.get("codexErrorInfo") or "")
    code = default
    if "ActiveTurnNotSteerable" in info or "not steerable" in lowered:
        code = ErrorCode.TURN_NOT_STEERABLE
    elif exc.code == -32601:
        code = ErrorCode.INCOMPATIBLE_CODEX
    elif any(marker in lowered for marker in _THREAD_NOT_FOUND_MARKERS):
        code = ErrorCode.THREAD_NOT_FOUND
    elif any(marker in lowered for marker in ("active turn", "already active", "busy")):
        code = ErrorCode.THREAD_BUSY
    return CodexCtlError(code, message, thread_id=thread_id, cause=exc)


def _map_resume_error(exc: JsonRpcError, thread_id: str) -> CodexCtlError:
    message = exc.message or ""
    lowered = message.lower()
    if any(marker in lowered for marker in _THREAD_NOT_FOUND_MARKERS):
        code = ErrorCode.THREAD_NOT_FOUND
    else:
        # Recovery failure never silently creates a replacement thread.
        code = ErrorCode.THREAD_RECOVERY_FAILED
    return CodexCtlError(code, message, thread_id=thread_id, cause=exc)


def _map_interrupt_error(exc: JsonRpcError, thread_id: str) -> CodexCtlError:
    # A rejected interrupt is always a domain condition: the turn we targeted
    # is gone or un-interruptible. Message keywords must not reclassify it.
    code = ErrorCode.INCOMPATIBLE_CODEX if exc.code == -32601 else ErrorCode.NO_ACTIVE_TURN
    return CodexCtlError(code, exc.message or "", thread_id=thread_id, cause=exc)


def _map_steer_error(exc: JsonRpcError, thread_id: str) -> CodexCtlError:
    message = exc.message or ""
    lowered = message.lower()
    info = ""
    if isinstance(exc.data, dict):
        info = str(exc.data.get("codexErrorInfo") or "")
    if "ActiveTurnNotSteerable" in info or "not steerable" in lowered:
        code = ErrorCode.TURN_NOT_STEERABLE
    elif "expectedturn" in lowered or "no active turn" in lowered or "invalid" in lowered:
        code = ErrorCode.NO_ACTIVE_TURN
    else:
        code = ErrorCode.APP_SERVER_PROTOCOL_ERROR
    return CodexCtlError(code, message, thread_id=thread_id, cause=exc)


def history_to_events(snapshot: HistorySnapshot) -> list[ProjectedEvent]:
    """Project a history snapshot into finite JSONL records.

    One lifecycle-shaped record sequence per selected turn: ``turn/started``,
    one ``item/completed`` per projected item, then ``turn/completed`` — all
    with ``source: "replay"`` and ``turnIndex`` convenience metadata. A turn
    still ``inProgress`` keeps its item records but gets no ``turn/completed``,
    exactly like the follow replay prelude: completion is a terminal fact.
    """
    events: list[ProjectedEvent] = []
    for turn in snapshot.turns:
        events.append(
            ProjectedEvent(
                "turn/started",
                thread_id=snapshot.thread_id,
                turn_id=turn.id,
                source="replay",
                turn_index=turn.index,
            )
        )
        for item in turn.items:
            events.append(
                ProjectedEvent(
                    "item/completed",
                    thread_id=snapshot.thread_id,
                    turn_id=turn.id,
                    item=item,
                    source="replay",
                    turn_index=turn.index,
                )
            )
        if turn.status == "inProgress":
            continue
        events.append(
            ProjectedEvent(
                "turn/completed",
                thread_id=snapshot.thread_id,
                turn_id=turn.id,
                source="replay",
                turn_index=turn.index,
                extra={"status": turn.status},
            )
        )
    return events

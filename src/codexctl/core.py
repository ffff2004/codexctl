"""CodexCtl: one lifecycle-oriented entry point over the Codex runtime.

This module owns command dispatch, thread/turn orchestration, race handling,
the follow replay/live frontier, history selection, and stable error mapping.
It never exposes transport or protocol types through ``run``.
"""

import asyncio
import time
from dataclasses import replace
from typing import Any, AsyncIterator, Awaitable, Callable

from . import rollout
from .appserver import (
    AppServerClient,
    AppServerThread,
    AppServerTurn,
    JsonRpcError,
    connect_app_server,
)
from .endpoint import (
    AppServerEndpoint,
    LifecycleOwnership,
    RuntimeProvider,
)
from .model import (
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
    usage_to_context,
)

INTERRUPT_WAIT_SECONDS = 120.0
INTERRUPT_POLL_INTERVAL = 0.5

ConnectFactory = Callable[[AppServerEndpoint], Awaitable[AppServerClient]]


class CodexCtl:
    """The external module: ``run(Command) -> Outcome``."""

    def __init__(
        self,
        runtime: RuntimeProvider,
        connect: ConnectFactory | None = None,
    ) -> None:
        self._runtime = runtime
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
            return await self._list(command)
        if isinstance(command, Doctor):
            return await self._doctor()
        raise CodexCtlError(
            ErrorCode.APP_SERVER_PROTOCOL_ERROR,
            f"unknown command: {type(command).__name__}",
        )

    # -- plumbing -------------------------------------------------------------

    async def _open(self) -> AppServerClient:
        endpoint = await self._runtime.resolve_endpoint()
        return await self._connect(endpoint)

    @staticmethod
    def _find_active_turn(thread: AppServerThread) -> AppServerTurn | None:
        for turn in reversed(thread.turns):
            if turn.in_progress:
                return turn
        return None

    @staticmethod
    async def _read_thread(
        app_server: AppServerClient, thread_id: str
    ) -> AppServerThread:
        try:
            thread = await app_server.read_thread(thread_id)
        except JsonRpcError as exc:
            await app_server.close()
            raise _map_rpc_error(
                exc, default=ErrorCode.THREAD_NOT_FOUND, thread_id=thread_id
            ) from exc
        return thread or AppServerThread(thread_id, "notLoaded", [], [])

    # -- start ----------------------------------------------------------------

    async def _start(self, command: Start) -> Any:
        config = command.config
        cwd = self._runtime.policy.resolve_cwd(config.cwd)
        if cwd != config.cwd:
            config = replace(config, cwd=cwd)
        app_server = await self._open()
        try:
            try:
                thread = await app_server.start_thread(config)
            except JsonRpcError as exc:
                raise _map_rpc_error(
                    exc, default=ErrorCode.APP_SERVER_PROTOCOL_ERROR
                ) from exc
            thread_id = thread.id if thread is not None else None
            if not thread_id:
                raise CodexCtlError(
                    ErrorCode.APP_SERVER_PROTOCOL_ERROR,
                    "thread/start returned no thread id",
                )
            turn_id = await self._turn_start(
                app_server, thread_id, command.prompt, effort=config.effort
            )
        except Exception:
            await app_server.close()
            raise
        if command.detach:
            # Disconnecting never interrupts the turn.
            await app_server.close()
            return DetachedTurnStarted(thread_id=thread_id, turn_id=turn_id)
        return self._follow_live(app_server, thread_id, turn_id)

    async def _turn_start(
        self,
        app_server: AppServerClient,
        thread_id: str,
        prompt: str,
        effort: str | None = None,
    ) -> str:
        try:
            turn_id = await app_server.start_turn(thread_id, prompt, effort)
        except JsonRpcError as exc:
            # The actual start-turn result is authoritative for races.
            raise _map_rpc_error(
                exc, default=ErrorCode.THREAD_BUSY, thread_id=thread_id
            ) from exc
        if not turn_id:
            raise CodexCtlError(
                ErrorCode.APP_SERVER_PROTOCOL_ERROR,
                "turn/start returned no turn id",
                thread_id=thread_id,
            )
        return turn_id

    # -- resume ---------------------------------------------------------------

    async def _resume(self, command: Resume) -> Any:
        app_server = await self._open()
        try:
            try:
                thread = await app_server.resume_thread(command.thread_id)
            except JsonRpcError as exc:
                raise _map_resume_error(exc, command.thread_id) from exc
            thread = thread or AppServerThread(command.thread_id, "notLoaded", [], [])
            if thread.status == "active" or self._find_active_turn(thread) is not None:
                raise CodexCtlError(
                    ErrorCode.THREAD_BUSY,
                    "thread has an active turn",
                    thread_id=command.thread_id,
                )
            # Resume never queues and never becomes steer: one start-turn, or fail.
            turn_id = await self._turn_start(
                app_server, command.thread_id, command.prompt
            )
        except Exception:
            await app_server.close()
            raise
        if command.detach:
            await app_server.close()
            return DetachedTurnStarted(thread_id=command.thread_id, turn_id=turn_id)
        return self._follow_live(app_server, command.thread_id, turn_id)

    # -- foreground streaming --------------------------------------------------

    def _follow_live(
        self, app_server: AppServerClient, thread_id: str, turn_id: str
    ) -> EventStreamOutcome:
        async def started(seen: set[tuple]) -> AsyncIterator[ProjectedEvent]:
            ev = ProjectedEvent(
                "turn/started", thread_id=thread_id, turn_id=turn_id, source="live"
            )
            seen.add(ev.dedup_key())
            yield ev

        return self._stream_turn(app_server, thread_id, turn_id, started)

    def _stream_turn(
        self,
        app_server: AppServerClient,
        thread_id: str,
        turn_id: str | None,
        prelude: Callable[[set[tuple]], AsyncIterator[ProjectedEvent]] | None = None,
        persist: bool = False,
    ) -> EventStreamOutcome:
        """Stream one turn, or a whole thread session when ``persist``.

        Shared live phase for start/resume and follow. The optional
        ``prelude`` yields its events first and registers their dedup keys in
        the shared ``seen`` set.

        Non-persist ends on the target turn's ``turn/completed``. Persist
        spans turns: each ``turn/completed`` merely closes one turn's segment
        (recorded as the latest terminal observation), and the session ends
        only on connection loss or local cancellation. A stream lost before
        session end yields a deterministic error event and never interrupts.
        Always unsubscribes and closes, resolving ``result`` once: to the
        terminal turn (non-persist) or the last observed terminal turn /
        ``None`` on cancellation (persist).
        """
        loop = asyncio.get_running_loop()
        result: asyncio.Future[TurnTerminal | None] = loop.create_future()

        async def events() -> AsyncIterator[ProjectedEvent]:
            seen: set[tuple] = set()
            usage: dict | None = None
            terminal_status: str | None = None
            terminal_error: str | None = None
            last_terminal: TurnTerminal | None = None
            cancelled = False
            try:
                if prelude is not None:
                    async for ev in prelude(seen):
                        yield ev
                async for notification in app_server.notifications():
                    notif_thread = notification.thread_id
                    if notif_thread is not None and notif_thread != thread_id:
                        continue
                    if notification.type == "thread/tokenUsage/updated":
                        usage = notification.extra.get("usage")
                    ev = replace(notification, source="live")
                    if ev.type != "thread/tokenUsage/updated":
                        if ev.dedup_key() in seen:
                            continue
                        seen.add(ev.dedup_key())
                    yield ev
                    if ev.type == "turn/completed":
                        status = str(ev.extra.get("status") or "")
                        error = (ev.extra.get("error") or {}).get("message")
                        if persist:
                            # One turn's segment closes; the session continues
                            # regardless of the turn's outcome.
                            last_terminal = TurnTerminal(
                                thread_id=thread_id,
                                turn_id=ev.turn_id or "",
                                status=status,
                                error=error,
                                context=usage_to_context(usage),
                            )
                            continue
                        if ev.turn_id == turn_id:
                            terminal_status = status
                            terminal_error = error
                            break
                # The notification stream ended before session end.
                if persist or terminal_status is None:
                    yield ProjectedEvent(
                        "error",
                        thread_id=thread_id,
                        turn_id=turn_id,
                        source="live",
                        extra={
                            "error": {
                                "code": ErrorCode.APP_SERVER_PROTOCOL_ERROR.value,
                                "message": (
                                    "connection lost while following the thread"
                                    if persist
                                    else "connection closed before turn completion"
                                ),
                            }
                        },
                    )
            except asyncio.CancelledError:
                # Local cancellation ends a persist session cleanly; it never
                # emits a connection-loss error event and never interrupts.
                cancelled = True
                raise
            finally:
                await app_server.unsubscribe(thread_id)
                await app_server.close()
                if not result.done():
                    if persist:
                        if cancelled:
                            result.set_result(last_terminal)
                        else:
                            result.set_exception(
                                CodexCtlError(
                                    ErrorCode.APP_SERVER_PROTOCOL_ERROR,
                                    "connection lost while following the thread",
                                    thread_id=thread_id,
                                    turn_id=turn_id,
                                )
                            )
                    else:
                        context = usage_to_context(usage)
                        if terminal_status is None:
                            result.set_exception(
                                CodexCtlError(
                                    ErrorCode.APP_SERVER_PROTOCOL_ERROR,
                                    "connection closed before turn completion",
                                    thread_id=thread_id,
                                    turn_id=turn_id,
                                )
                            )
                        else:
                            result.set_result(
                                TurnTerminal(
                                    thread_id=thread_id,
                                    turn_id=turn_id or "",
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
        app_server = await self._open()
        try:
            # Strictly read-only: never recover or start a thread for status.
            thread = await self._read_thread(app_server, command.thread_id)
        finally:
            await app_server.close()
        status, flags = thread.status, thread.active_flags
        active = self._find_active_turn(thread)
        context = (
            rollout.lookup_context_usage(command.thread_id)
            if self._runtime.policy.supports_rollout_enrichment
            else None
        )
        return StatusSnapshot(
            thread_id=command.thread_id,
            status=status,
            active_flags=flags,
            active_turn_id=active.id if active is not None else None,
            context=context,
        )

    # -- history ----------------------------------------------------------------

    async def _history(self, command: History) -> HistorySnapshot:
        app_server = await self._open()
        try:
            thread = await self._read_thread(app_server, command.thread_id)
        finally:
            await app_server.close()
        turns = thread.turns
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
            items = list(turn.items)
            history_turns.append(
                HistoryTurn(
                    id=turn.id,
                    index=index,
                    status=turn.status,
                    items=items,
                )
            )
        return HistorySnapshot(thread_id=command.thread_id, turns=history_turns)

    # -- follow -----------------------------------------------------------------

    async def _follow(self, command: Follow) -> Any:
        app_server = await self._open()
        try:
            try:
                thread = await app_server.resume_thread(command.thread_id)
            except JsonRpcError as exc:
                raise _map_resume_error(exc, command.thread_id) from exc
            thread = thread or AppServerThread(command.thread_id, "notLoaded", [], [])
        except Exception:
            await app_server.close()
            raise
        active = self._find_active_turn(thread)
        if active is None and not command.persist:
            await app_server.unsubscribe(command.thread_id)
            await app_server.close()
            raise CodexCtlError(
                ErrorCode.NO_ACTIVE_TURN,
                "thread has no active turn to follow",
                thread_id=command.thread_id,
            )
        active_turn_id = active.id if active is not None else None
        turns = thread.turns
        # The replay anchor is the active turn when one exists, otherwise the
        # end of history (reachable only with persist).
        replay_pairs = select_replay_turns(turns, command.replay)

        async def replay(seen: set[tuple]) -> AsyncIterator[ProjectedEvent]:
            # Replay the selected continuous suffix from the reconstructed
            # snapshot, then continue live. Events visible in both are
            # deduplicated by stable Codex identities.
            for index, turn in replay_pairs:
                turn_id = turn.id
                for projected in turn.items:
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
                        if not turn.in_progress or phase == "item/completed":
                            # Only events actually emitted occupy ``seen``: a
                            # suppressed replay event (item/started of an
                            # in-progress turn) must not swallow its live
                            # delivery.
                            seen.add(ev.dedup_key())
                            yield ev
                if not turn.in_progress:
                    completed = ProjectedEvent(
                        "turn/completed",
                        thread_id=command.thread_id,
                        turn_id=turn_id,
                        source="replay",
                        turn_index=index,
                        extra={"status": turn.status},
                    )
                    if completed.dedup_key() not in seen:
                        seen.add(completed.dedup_key())
                        yield completed
            if active_turn_id is not None:
                # The attached turn started before subscription, so its
                # turn/started notification predates the live stream and is
                # never delivered. Synthesize it at the replay/live boundary
                # so the attached turn's marker is emitted exactly once;
                # occupying ``seen`` deduplicates any repeated live delivery
                # of the same start event.
                started = ProjectedEvent(
                    "turn/started",
                    thread_id=command.thread_id,
                    turn_id=active_turn_id,
                    source="live",
                )
                if started.dedup_key() not in seen:
                    seen.add(started.dedup_key())
                    yield started

        return self._stream_turn(
            app_server,
            command.thread_id,
            active_turn_id,
            replay,
            persist=command.persist,
        )

    # -- steer ------------------------------------------------------------------

    async def _steer(self, command: Steer) -> SteerAcknowledged:
        app_server = await self._open()
        try:
            thread = await self._read_thread(app_server, command.thread_id)
            active = self._find_active_turn(thread)
            if active is None:
                raise CodexCtlError(
                    ErrorCode.NO_ACTIVE_TURN,
                    "thread has no active turn to steer",
                    thread_id=command.thread_id,
                )
            expected_turn_id = active.id
            try:
                turn_id = await app_server.steer_turn(
                    command.thread_id, command.input, expected_turn_id
                )
            except JsonRpcError as exc:
                raise _map_steer_error(exc, command.thread_id) from exc
            return SteerAcknowledged(
                thread_id=command.thread_id,
                turn_id=turn_id or expected_turn_id,
            )
        finally:
            await app_server.close()

    # -- interrupt ----------------------------------------------------------------

    async def _interrupt(self, command: Interrupt) -> InterruptResult:
        app_server = await self._open()
        try:
            thread = await self._read_thread(app_server, command.thread_id)
            active = self._find_active_turn(thread)
            if active is None:
                raise CodexCtlError(
                    ErrorCode.NO_ACTIVE_TURN,
                    "thread has no active turn to interrupt",
                    thread_id=command.thread_id,
                )
            turn_id = active.id
            try:
                await app_server.interrupt_turn(command.thread_id, turn_id)
            except JsonRpcError as exc:
                # A rejected interrupt means the target turn changed or ended;
                # never retry against a different turn.
                raise _map_interrupt_error(exc, command.thread_id) from exc
            deadline = time.monotonic() + INTERRUPT_WAIT_SECONDS
            while True:
                current = await self._read_thread(app_server, command.thread_id)
                target = next(
                    (turn for turn in current.turns if turn.id == turn_id), None
                )
                if target is not None and target.is_terminal:
                    return InterruptResult(
                        thread_id=command.thread_id,
                        turn_id=turn_id,
                        status=target.status,
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
            await app_server.close()

    # -- list ---------------------------------------------------------------------

    async def _list(self, command: ListThreads) -> ThreadListSnapshot:
        app_server = await self._open()
        try:
            records: list[ThreadRecord] = []
            cursor: str | None = None
            cwd = (
                None
                if command.all_threads
                else self._runtime.policy.resolve_cwd(command.cwd)
            )
            for _ in range(25):  # pagination safety cap
                try:
                    page = await app_server.list_threads(cursor, cwd=cwd)
                except JsonRpcError as exc:
                    raise _map_rpc_error(
                        exc, default=ErrorCode.APP_SERVER_PROTOCOL_ERROR
                    ) from exc
                for entry in page.threads:
                    records.append(
                        ThreadRecord(
                            thread_id=entry.id,
                            status=entry.status,
                            preview=entry.preview,
                            updated_at=entry.updated_at,
                        )
                    )
                cursor = page.next_cursor
                if not cursor:
                    break
            return ThreadListSnapshot(threads=records)
        finally:
            await app_server.close()

    # -- doctor ---------------------------------------------------------------------

    async def _doctor(self) -> DoctorSnapshot:
        from .appserver import CLIENT_VERSION

        checks: list[DoctorCheck] = []
        compatible = False
        app_server_version: str | None = None
        endpoint_mode = getattr(self._runtime, "mode", "managed")
        policy = self._runtime.policy
        try:
            endpoint = await self._runtime.resolve_endpoint()
            checks.append(DoctorCheck("endpoint reachable", True, endpoint.display))
            if endpoint.runtime_pid is not None:
                checks.append(
                    DoctorCheck("runtime pid", True, str(endpoint.runtime_pid))
                )
        except CodexCtlError as exc:
            checks.append(DoctorCheck("endpoint reachable", False, exc.message))
            return DoctorSnapshot(
                codexctl_version=CLIENT_VERSION,
                endpoint_mode=endpoint_mode,
                checks=checks,
                compatible=False,
            )
        try:
            app_server = await self._connect(endpoint)
        except CodexCtlError as exc:
            checks.append(DoctorCheck("initialize handshake", False, exc.message))
        else:
            checks.append(DoctorCheck("initialize handshake", True, None))
            app_server_version = getattr(app_server, "app_server_version", None)
            try:
                missing = await app_server.check_lifecycle_operations()
            except JsonRpcError as exc:
                checks.append(
                    DoctorCheck("required lifecycle operations", False, exc.message)
                )
            else:
                lifecycle_ok = not missing
                checks.append(
                    DoctorCheck(
                        "required lifecycle operations",
                        lifecycle_ok,
                        "all available"
                        if lifecycle_ok
                        else "unavailable: " + ", ".join(missing),
                    )
                )
                compatible = lifecycle_ok
            finally:
                await app_server.close()
        codex_cli_version: str | None = None
        if policy.lifecycle == LifecycleOwnership.MANAGED:
            codex_cli_version = endpoint.cli_version
            if codex_cli_version is None:
                codex_cli_version = await self._runtime.probe_cli_version()
            checks.append(
                DoctorCheck(
                    "codex cli version",
                    codex_cli_version is not None,
                    codex_cli_version,
                )
            )
        if policy.supports_rollout_enrichment:
            rollout_available = rollout.sessions_dir_exists()
            checks.append(
                DoctorCheck(
                    "context usage enrichment",
                    rollout_available,
                    "rollout sessions directory present"
                    if rollout_available
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


async def _default_connect(endpoint: AppServerEndpoint) -> AppServerClient:
    return await connect_app_server(endpoint)


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
    info = exc.codex_error_info or ""
    code = default
    if exc.code == -32601:
        code = ErrorCode.INCOMPATIBLE_CODEX
    elif "ActiveTurnNotSteerable" in info or "not steerable" in lowered:
        code = ErrorCode.TURN_NOT_STEERABLE
    elif any(marker in lowered for marker in _THREAD_NOT_FOUND_MARKERS):
        code = ErrorCode.THREAD_NOT_FOUND
    elif any(marker in lowered for marker in ("active turn", "already active", "busy")):
        code = ErrorCode.THREAD_BUSY
    return CodexCtlError(code, message, thread_id=thread_id, cause=exc)


def _map_resume_error(exc: JsonRpcError, thread_id: str) -> CodexCtlError:
    message = exc.message or ""
    lowered = message.lower()
    if exc.code == -32601:
        code = ErrorCode.INCOMPATIBLE_CODEX
    elif any(marker in lowered for marker in _THREAD_NOT_FOUND_MARKERS):
        code = ErrorCode.THREAD_NOT_FOUND
    else:
        # Recovery failure never silently creates a replacement thread.
        code = ErrorCode.THREAD_RECOVERY_FAILED
    return CodexCtlError(code, message, thread_id=thread_id, cause=exc)


def _map_interrupt_error(exc: JsonRpcError, thread_id: str) -> CodexCtlError:
    # A rejected interrupt is always a domain condition: the turn we targeted
    # is gone or un-interruptible. Message keywords must not reclassify it.
    return CodexCtlError(
        ErrorCode.NO_ACTIVE_TURN,
        exc.message or "",
        thread_id=thread_id,
        cause=exc,
    )


def _map_steer_error(exc: JsonRpcError, thread_id: str) -> CodexCtlError:
    message = exc.message or ""
    lowered = message.lower()
    info = ""
    info = exc.codex_error_info or ""
    if exc.code == -32601:
        code = ErrorCode.INCOMPATIBLE_CODEX
    elif "ActiveTurnNotSteerable" in info or "not steerable" in lowered:
        code = ErrorCode.TURN_NOT_STEERABLE
    elif (
        "expectedturn" in lowered or "no active turn" in lowered or "invalid" in lowered
    ):
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

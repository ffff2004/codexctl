"""Codex app-server adapter: transport, JSON-RPC routing, and projection.

This is the compatibility firewall between the Codex wire protocol and the
rest of ``codexctl``. Raw protocol messages stay inside this module; callers
only see request results, :class:`~codexctl.model.ProjectedEvent` values, and
:class:`JsonRpcError` failures.
"""

import asyncio
import contextlib
import itertools
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Literal,
    NoReturn,
    Protocol,
    runtime_checkable,
)

from .model import (
    DEFAULT_SANDBOX_POLICY,
    ApprovalPolicy,
    ApprovalsReviewer,
    CodexCtlError,
    ErrorCode,
    ProjectedEvent,
    SandboxPolicy,
    StartConfig,
    UsageError,
    context_usage_ratio,
)

if TYPE_CHECKING:
    from .endpoint import AppServerEndpoint

CLIENT_NAME = "codexctl"
CLIENT_VERSION = "0.1.0"
THREAD_LIST_PAGE_SIZE = 100
_STDIO_STREAM_LIMIT = 64 * 1024

# Synthetic notification method used to surface server-initiated interaction
# requests that v1 cannot broker. Internal to codexctl; never on the wire.
UNSUPPORTED_INTERACTION_METHOD = "codexctl/unsupportedInteraction"

TERMINAL_TURN_STATUSES = {"completed", "interrupted", "failed"}

_SANDBOX_POLICY_TO_WIRE: dict[SandboxPolicy, str] = {
    SandboxPolicy.readOnly: "read-only",
    SandboxPolicy.workspaceWrite: "workspace-write",
    SandboxPolicy.dangerFullAccess: "danger-full-access",
}

_APPROVAL_POLICY_TO_WIRE: dict[ApprovalPolicy, str] = {
    ApprovalPolicy.untrusted: "untrusted",
    ApprovalPolicy.onRequest: "on-request",
    ApprovalPolicy.never: "never",
}

_APPROVALS_REVIEWER_TO_WIRE: dict[ApprovalsReviewer, str] = {
    ApprovalsReviewer.user: "user",
    ApprovalsReviewer.autoReview: "auto_review",
}


def _serialize_sandbox_policy(policy: SandboxPolicy | None) -> str:
    """Translate the public sandbox policy to the app-server wire enum."""
    if policy is None:
        policy = DEFAULT_SANDBOX_POLICY

    try:
        return _SANDBOX_POLICY_TO_WIRE[policy]
    except (KeyError, TypeError) as exc:
        choices = ", ".join(_SANDBOX_POLICY_TO_WIRE.values())
        raise UsageError(
            f"unsupported sandbox policy {policy!r}; expected one of: {choices}"
        ) from exc


# Each probe is the sole definition of a required operation: its projected
# label and its private wire method and parameters. The report below derives
# its operation vocabulary from these entries.
@dataclass(frozen=True)
class _LifecycleProbe:
    label: str
    method: str
    params: dict[str, Any]


_LIFECYCLE_PROBE_THREAD_ID = "00000000-0000-0000-0000-000000000000"
_LIFECYCLE_PROBE_TURN_ID = "codexctl-doctor-probe"
_LIFECYCLE_PROBES = (
    _LifecycleProbe(
        "start thread",
        "thread/start",
        {"ephemeral": True, "historyMode": "paginated"},
    ),
    _LifecycleProbe(
        "resume thread",
        "thread/resume",
        {"threadId": _LIFECYCLE_PROBE_THREAD_ID},
    ),
    _LifecycleProbe(
        "read thread",
        "thread/read",
        {"threadId": _LIFECYCLE_PROBE_THREAD_ID, "includeTurns": False},
    ),
    _LifecycleProbe(
        "list threads",
        "thread/list",
        {"limit": 1},
    ),
    _LifecycleProbe(
        "start turn",
        "turn/start",
        {
            "threadId": _LIFECYCLE_PROBE_THREAD_ID,
            "input": [{"type": "text", "text": "codexctl doctor probe"}],
        },
    ),
    _LifecycleProbe(
        "steer turn",
        "turn/steer",
        {
            "threadId": _LIFECYCLE_PROBE_THREAD_ID,
            "input": [{"type": "text", "text": "codexctl doctor probe"}],
            "expectedTurnId": _LIFECYCLE_PROBE_TURN_ID,
        },
    ),
    _LifecycleProbe(
        "interrupt turn",
        "turn/interrupt",
        {
            "threadId": _LIFECYCLE_PROBE_THREAD_ID,
            "turnId": _LIFECYCLE_PROBE_TURN_ID,
        },
    ),
)
REQUIRED_LIFECYCLE_OPERATIONS = tuple(probe.label for probe in _LIFECYCLE_PROBES)


class JsonRpcError(Exception):
    """A JSON-RPC error response from the app-server."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.codex_error_info = _project_codex_error_info(data)


@dataclass(frozen=True)
class AppServerTurn:
    """Projected turn state used by the orchestration layer."""

    id: str
    status: str
    items: list[dict[str, Any]]

    @property
    def in_progress(self) -> bool:
        return self.status == "inProgress"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TURN_STATUSES


@dataclass(frozen=True)
class AppServerThread:
    """Projected thread state used by the orchestration layer."""

    id: str
    status: str
    active_flags: list[str]
    turns: list[AppServerTurn]


@dataclass(frozen=True)
class ThreadResponse:
    thread: AppServerThread | None


@dataclass(frozen=True)
class TurnResponse:
    turn_id: str | None


@dataclass(frozen=True)
class InitializeResponse:
    user_agent: str | None
    codex_home: str | None


@dataclass(frozen=True)
class ThreadSummary:
    id: str
    status: str
    preview: str | None
    updated_at: int | None


@dataclass(frozen=True)
class ThreadListResponse:
    threads: list[ThreadSummary]
    next_cursor: str | None


@dataclass(frozen=True)
class EmptyResponse:
    pass


type AppServerResponse = (
    ThreadResponse
    | TurnResponse
    | InitializeResponse
    | ThreadListResponse
    | EmptyResponse
)


@runtime_checkable
class AppServerPort(Protocol):
    """Typed app-server operations used by the orchestration layer."""

    user_agent: str | None
    app_server_version: str | None
    server_codex_home: str | None

    async def read_thread(self, thread_id: str) -> AppServerThread | None: ...

    async def start_thread(self, config: StartConfig) -> AppServerThread | None: ...

    async def start_turn(
        self, thread_id: str, prompt: str, effort: str | None = None
    ) -> str | None: ...

    async def resume_thread(self, thread_id: str) -> AppServerThread | None: ...

    async def steer_turn(
        self, thread_id: str, input_text: str, expected_turn_id: str
    ) -> str | None: ...

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None: ...

    async def list_threads(
        self, cursor: str | None = None, cwd: str | None = None
    ) -> ThreadListResponse: ...

    async def check_lifecycle_operations(self) -> tuple[str, ...]:
        """Return required lifecycle operation labels unavailable on the runtime."""
        ...

    def notifications(self) -> AsyncIterator[ProjectedEvent]: ...

    async def unsubscribe(self, thread_id: str) -> None:
        """Best-effort unsubscribe; never raises."""
        ...

    async def close(self) -> None: ...


type AppServerMessage = str | bytes


class _AppServerMessageTransport(Protocol):
    """Raw message transport below JSON decoding."""

    async def send_text(self, payload: str) -> None: ...

    def messages(self) -> AsyncIterator[AppServerMessage]: ...

    async def close(self) -> None: ...


class _WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...

    def __aiter__(self) -> AsyncIterator[AppServerMessage]: ...

    async def close(self) -> None: ...


class _StdioLaunchState:
    """Keep the child handle reachable while subprocess setup is pending."""

    def __init__(self) -> None:
        self.process: _OwnedStdioProcess | None = None

    def capture(
        self,
        raw_transport: asyncio.BaseTransport,
        protocol: asyncio.SubprocessProtocol,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        process = asyncio.subprocess.Process(raw_transport, protocol, loop)
        self.process = _OwnedStdioProcess(process)


class _CapturingSubprocessStreamProtocol(asyncio.subprocess.SubprocessStreamProtocol):
    """Capture the process before low-level subprocess setup returns."""

    def __init__(self, state: _StdioLaunchState, loop: asyncio.AbstractEventLoop):
        super().__init__(limit=_STDIO_STREAM_LIMIT, loop=loop)
        self._launch_state = state
        self._launch_loop = loop

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        self._launch_state.capture(transport, self, self._launch_loop)


class WebSocketMessageTransport:
    """Adapt a websockets connection to the app-server message seam."""

    def __init__(self, conn: _WebSocketConnection) -> None:
        self._conn = conn

    async def send_text(self, payload: str) -> None:
        await self._conn.send(payload)

    async def _messages(self) -> AsyncIterator[AppServerMessage]:
        async for message in self._conn:
            yield message

    def messages(self) -> AsyncIterator[AppServerMessage]:
        return self._messages()

    async def close(self) -> None:
        await self._conn.close()


class _OwnedStdioProcess:
    """Own one child process and its bounded process-group cleanup."""

    _GRACEFUL_WAIT_SECONDS = 1.0
    _TERMINATE_WAIT_SECONDS = 1.0

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._closed = False

    @classmethod
    async def launch(
        cls, argv: tuple[str, ...], state: _StdioLaunchState | None = None
    ) -> "_OwnedStdioProcess":
        launch_state = state or _StdioLaunchState()
        loop = asyncio.get_running_loop()

        def protocol_factory() -> _CapturingSubprocessStreamProtocol:
            return _CapturingSubprocessStreamProtocol(launch_state, loop)

        try:
            await loop.subprocess_exec(
                protocol_factory,
                argv[0],
                *argv[1:],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # The child protocol uses its stdin/stdout pipes. Inherit
                # stderr so child diagnostics remain visible on codexctl's
                # stderr.
                stderr=None,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise CodexCtlError(
                ErrorCode.APP_SERVER_UNAVAILABLE,
                "cannot start stdio app-server process",
                cause=exc,
            ) from exc
        process = launch_state.process
        if process is None:  # pragma: no cover - loop contract fallback
            raise RuntimeError("stdio subprocess setup returned without a process")
        return process

    async def read(self, size: int) -> bytes:
        stdout = self._process.stdout
        if stdout is None:
            raise RuntimeError("stdio app-server stdout is unavailable")
        return await stdout.read(size)

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def read_until(self, separator: bytes) -> bytes:
        stdout = self._process.stdout
        if stdout is None:
            raise RuntimeError("stdio app-server stdout is unavailable")
        return await stdout.readuntil(separator)

    async def read_lines(self) -> AsyncIterator[bytes]:
        stdout = self._process.stdout
        if stdout is None:
            raise RuntimeError("stdio app-server stdout is unavailable")
        async for line in stdout:
            yield line

    async def write(self, data: bytes) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise RuntimeError("stdio app-server stdin is unavailable")
        stdin.write(data)
        await stdin.drain()

    def write_nowait(self, data: bytes) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise RuntimeError("stdio app-server stdin is unavailable")
        stdin.write(data)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        stdin = self._process.stdin
        if stdin is not None:
            with contextlib.suppress(Exception):
                stdin.close()

        await self._wait(self._GRACEFUL_WAIT_SECONDS)
        # The parent may already have exited while a descendant still holds
        # the protocol pipes open. Always clean the dedicated process group,
        # then use SIGKILL only when the group remains alive.
        self._signal_group(signal.SIGTERM)
        await self._wait(self._TERMINATE_WAIT_SECONDS)
        if self._group_exists():
            self._signal_group(signal.SIGKILL)
            await self._wait(self._TERMINATE_WAIT_SECONDS)

    async def _wait(self, timeout: float) -> bool:
        if self._process.returncode is not None:
            return True
        try:
            await asyncio.wait_for(self._process.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    def _signal_group(self, signum: int) -> None:
        try:
            os.killpg(self._process.pid, signum)
        except ProcessLookupError, PermissionError, OSError:
            pass

    def _group_exists(self) -> bool:
        try:
            os.killpg(self._process.pid, 0)
        except ProcessLookupError, PermissionError, OSError:
            return False
        return True


class JsonlStdioMessageTransport:
    """Newline-delimited JSON transport backed by one owned child process."""

    def __init__(self, process: _OwnedStdioProcess) -> None:
        self._process = process
        self._stream = _RawByteStream(process)

    async def send_text(self, payload: str) -> None:
        await self._stream.write(payload.encode("utf-8") + b"\n")

    async def _messages(self) -> AsyncIterator[AppServerMessage]:
        async for line in self._stream.lines():
            line = line.rstrip(b"\r\n")
            if not line.strip():
                continue
            # Keep decoding below the framing seam: the shared decoder rejects
            # malformed UTF-8 as a protocol error without resynchronizing.
            yield line.decode("utf-8")

    def messages(self) -> AsyncIterator[AppServerMessage]:
        return self._messages()

    async def close(self) -> None:
        await self._process.close()


class _RawByteStream:
    """Raw byte access to a child process's private stdin/stdout pipes."""

    _READ_SIZE = 64 * 1024

    def __init__(self, process: _OwnedStdioProcess) -> None:
        self._process = process

    async def read(self, size: int = _READ_SIZE) -> bytes:
        return await self._process.read(size)

    async def read_until(self, separator: bytes) -> bytes:
        return await self._process.read_until(separator)

    async def lines(self) -> AsyncIterator[bytes]:
        async for line in self._process.read_lines():
            yield line

    async def write(self, data: bytes) -> None:
        await self._process.write(data)

    def write_nowait(self, data: bytes) -> None:
        self._process.write_nowait(data)


class StdioWebSocketConnection:
    """Sans-I/O WebSocket client running over an owned stdio byte stream."""

    _HTTP_RESPONSE_LIMIT = 64 * 1024

    def __init__(self, process: _OwnedStdioProcess) -> None:
        from websockets.uri import parse_uri

        try:
            from websockets.client import ClientProtocol
        except ImportError:  # websockets 13.x named this Sans-I/O class differently
            from websockets.client import ClientConnection as ClientProtocol

        self._process = process
        self._stream = _RawByteStream(process)
        self._protocol = ClientProtocol(
            parse_uri("ws://localhost/"),
            extensions=None,
            subprotocols=None,
            max_size=None,
        )
        self._handshake_complete = False
        self._closed = False

    @classmethod
    async def connect(cls, process: _OwnedStdioProcess) -> "StdioWebSocketConnection":
        connection = cls(process)
        try:
            await connection._handshake()
        except BaseException:
            await _close_owned_process(process)
            raise
        return connection

    async def _handshake(self) -> None:
        request = self._protocol.connect()
        await self._stream.write(request.serialize())
        raw_response = await self._stream.read_until(b"\r\n\r\n")
        if len(raw_response) > self._HTTP_RESPONSE_LIMIT:
            raise ValueError("WebSocket handshake response is too large")
        response = _parse_stdio_websocket_response(raw_response)
        self._protocol.process_response(response)
        # The Sans-I/O client normally transitions this state from its parser;
        # the raw HTTP response is parsed by _RawByteStream instead.
        from websockets.protocol import OPEN

        self._protocol.state = OPEN
        # The regular client parser consumes the HTTP response itself. Since
        # the byte stream parses that response, restart it at the WebSocket
        # frame parser before accepting application traffic.
        self._protocol.parser = self._protocol.parse()
        next(self._protocol.parser)
        self._handshake_complete = True

    async def send(self, message: str) -> None:
        if self._closed:
            raise RuntimeError("stdio WebSocket connection is closed")
        self._protocol.send_text(message.encode("utf-8"))
        await self._flush()

    async def _flush(self) -> None:
        for data in self._protocol.data_to_send():
            # websockets uses b"" as a transport-level EOF marker. Closing
            # the owned process is the bounded stdio equivalent.
            if data:
                await self._stream.write(data)

    def _flush_nowait(self) -> None:
        for data in self._protocol.data_to_send():
            if data:
                self._stream.write_nowait(data)

    async def _messages(self) -> AsyncIterator[AppServerMessage]:
        message_opcode: object | None = None
        message_data = bytearray()
        while True:
            data = await self._stream.read()
            if not data:
                self._protocol.receive_eof()
                self._flush_nowait()
                if self._protocol.parser_exc is not None:
                    raise self._protocol.parser_exc
                raise EOFError("stdio WebSocket stream closed")

            self._protocol.receive_data(data)
            if self._protocol.parser_exc is not None:
                self._flush_nowait()
                raise self._protocol.parser_exc
            await self._flush()

            for event in self._protocol.events_received():
                # The handshake is parsed outside the Sans-I/O parser, so
                # post-upgrade events are WebSocket frames. Keep the runtime
                # narrowing local to this compatibility module.
                frame: Any = event
                if frame.opcode == 8:  # CLOSE
                    return
                if frame.opcode == 1 or frame.opcode == 2:  # TEXT or BINARY
                    message_opcode = frame.opcode
                    message_data = bytearray(frame.data)
                elif frame.opcode == 0 and message_opcode is not None:  # CONT
                    message_data.extend(frame.data)
                else:
                    continue

                if not frame.fin:
                    continue
                if message_opcode == 1:  # TEXT
                    yield bytes(message_data).decode("utf-8")
                else:
                    yield bytes(message_data)
                message_opcode = None
                message_data.clear()

    def __aiter__(self) -> AsyncIterator[AppServerMessage]:
        return self._messages()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._handshake_complete:
            with contextlib.suppress(Exception):
                self._protocol.send_close()
                self._flush_nowait()
        await self._process.close()


class JsonRpcAppServerSession:
    """Shared JSON-RPC session over a transport-specific message stream.

    Transport setup is intentionally outside this session implementation so
    Unix and TCP endpoints share reader routing, projection, and the
    unattended interaction policy.
    """

    def __init__(
        self, transport: _AppServerMessageTransport | None, display: str
    ) -> None:
        self._transport = transport
        self._display = display
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._ids = itertools.count(1)
        self._reader_task: asyncio.Task[None] | None = None
        self._lifecycle: Literal["OPEN", "CLOSING", "CLOSED"] = "OPEN"
        self._cleanup_task: asyncio.Task[None] | None = None
        self._cleanup_owner: asyncio.Task[None] | None = None
        self.user_agent: str | None = None
        self.app_server_version: str | None = None
        self.server_codex_home: str | None = None
        self.interaction_count = 0
        self._initialized = False

    # -- lifecycle -----------------------------------------------------------

    async def _initialize(self) -> None:
        result = await self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "title": CLIENT_NAME,
                    "version": CLIENT_VERSION,
                },
                "capabilities": {"experimentalApi": False},
            },
        )
        assert isinstance(result, InitializeResponse)
        self.user_agent = result.user_agent
        self.server_codex_home = result.codex_home
        self.app_server_version = parse_user_agent_version(self.user_agent)
        await self._notify("initialized")
        self._initialized = True

    async def close(self) -> None:
        """Close the session and wait for its shared transport cleanup."""
        cleanup_task = self._begin_closing()
        await self._await_cleanup(cleanup_task)

    def _begin_closing(self) -> asyncio.Task[None]:
        """Enter ``CLOSING`` and return the one shared cleanup task."""
        if self._lifecycle == "OPEN":
            self._lifecycle = "CLOSING"
        if self._cleanup_task is None:
            self._cleanup_owner = asyncio.current_task()
            self._cleanup_task = asyncio.create_task(self._cleanup_transport())
        return self._cleanup_task

    async def _cleanup_transport(self) -> None:
        """Cancel the reader, then close the transport exactly once."""
        try:
            reader_task = self._reader_task
            owner = self._cleanup_owner
            if (
                reader_task is not None
                and reader_task is not owner
                and not reader_task.done()
            ):
                # Do not await the reader here: a reader that observed the
                # failure may itself be waiting for this cleanup task.
                reader_task.cancel()
            if self._transport is not None:
                with contextlib.suppress(Exception):
                    await self._transport.close()
        finally:
            self._lifecycle = "CLOSED"

    async def _await_cleanup(self, cleanup_task: asyncio.Task[None]) -> None:
        """Await cleanup without allowing caller cancellation to cancel it."""
        cancelled = False
        try:
            while True:
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    # A second cancellation can arrive while cleanup is
                    # blocked. Keep waiting for the shared task rather than
                    # allowing this waiter to complete early.
                    cancelled = True
                    continue
                break
        finally:
            if cancelled:
                # Preserve the caller's cancellation only after the shared
                # cleanup task has completed.
                raise asyncio.CancelledError

    async def unsubscribe(self, thread_id: str) -> None:
        """Best-effort unsubscribe; never raises."""
        try:
            await asyncio.wait_for(
                self._request("thread/unsubscribe", {"threadId": thread_id}), 5.0
            )
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            pass

    # -- transport -----------------------------------------------------------

    async def _send(self, message: dict) -> None:
        if self._transport is None:
            raise _transport_failure(
                self._display,
                self._initialized,
                RuntimeError("app-server transport is unavailable"),
            )
        try:
            await self._transport.send_text(json.dumps(message))
        except CodexCtlError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport failure
            raise _transport_failure(self._display, self._initialized, exc) from exc

    async def _notify(self, method: str, params: dict | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    # -- typed app-server operations -----------------------------------------

    async def read_thread(self, thread_id: str) -> AppServerThread | None:
        response = await self._request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        assert isinstance(response, ThreadResponse)
        return response.thread

    async def start_thread(self, config: StartConfig) -> AppServerThread | None:
        params: dict[str, Any] = {
            "approvalPolicy": _APPROVAL_POLICY_TO_WIRE[config.approval_policy],
            "sandbox": _serialize_sandbox_policy(config.sandbox),
        }
        if config.approvals_reviewer is not None:
            params["approvalsReviewer"] = _APPROVALS_REVIEWER_TO_WIRE[
                config.approvals_reviewer
            ]
        if config.cwd:
            params["cwd"] = config.cwd
        if config.model:
            params["model"] = config.model
        response = await self._request("thread/start", params)
        assert isinstance(response, ThreadResponse)
        return response.thread

    async def start_turn(
        self, thread_id: str, prompt: str, effort: str | None = None
    ) -> str | None:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if effort:
            params["effort"] = effort
        response = await self._request("turn/start", params)
        assert isinstance(response, TurnResponse)
        return response.turn_id

    async def resume_thread(self, thread_id: str) -> AppServerThread | None:
        response = await self._request("thread/resume", {"threadId": thread_id})
        assert isinstance(response, ThreadResponse)
        return response.thread

    async def steer_turn(
        self, thread_id: str, input_text: str, expected_turn_id: str
    ) -> str | None:
        response = await self._request(
            "turn/steer",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": input_text}],
                "expectedTurnId": expected_turn_id,
            },
        )
        assert isinstance(response, TurnResponse)
        return response.turn_id

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        response = await self._request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )
        assert isinstance(response, EmptyResponse)

    async def list_threads(
        self, cursor: str | None = None, cwd: str | None = None
    ) -> ThreadListResponse:
        params: dict[str, Any] = {"limit": THREAD_LIST_PAGE_SIZE}
        if cursor is not None:
            params["cursor"] = cursor
        if cwd is not None:
            params["cwd"] = cwd
        response = await self._request("thread/list", params)
        assert isinstance(response, ThreadListResponse)
        return response

    async def check_lifecycle_operations(self) -> tuple[str, ...]:
        """Probe the lifecycle RPC surface without starting useful work.

        A JSON-RPC domain error still proves that the method was dispatched;
        only explicit method-not-found responses count as incompatibility.
        Older app-server versions report an unknown request variant as
        ``-32600`` instead of ``-32601``, so that form is recognized too.
        """
        missing: list[str] = []
        for probe in _LIFECYCLE_PROBES:
            try:
                await self._request(probe.method, probe.params)
            except JsonRpcError as exc:
                if _is_missing_method_error(exc):
                    missing.append(probe.label)
                elif exc.code == -32000 and "connection closed" in exc.message.lower():
                    raise
        return tuple(missing)

    # -- transport -----------------------------------------------------------

    async def _request(
        self, method: str, params: dict | None = None
    ) -> AppServerResponse:
        if self._lifecycle != "OPEN":
            raise _transport_failure(
                self._display,
                self._initialized,
                RuntimeError("connection closed"),
            )
        request_id = next(self._ids)
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        try:
            await self._send(message)
            return project_response(method, await future)
        finally:
            self._pending.pop(request_id, None)

    def notifications(self) -> AsyncIterator[ProjectedEvent]:
        return self._iter_notifications()

    async def _iter_notifications(self) -> AsyncIterator[ProjectedEvent]:
        while True:
            try:
                message = await self._queue.get()
            except asyncio.QueueShutDown:
                return
            event = project_notification(message)
            if event is not None:
                yield event

    async def _reader(self) -> None:
        failure: CodexCtlError | None = None
        cancelled = False
        try:
            if self._transport is None:
                raise RuntimeError("app-server transport is unavailable")
            async for raw_message in self._transport.messages():
                message = _decode_message(raw_message)
                if "method" in message:
                    if "id" in message:
                        await self._handle_server_request(message)
                    else:
                        await self._queue.put(message)
                elif "id" in message:
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            err = message["error"] or {}
                            future.set_exception(
                                JsonRpcError(
                                    err.get("code", -1),
                                    err.get("message", ""),
                                    err.get("data"),
                                )
                            )
                        else:
                            future.set_result(message.get("result") or {})
        except asyncio.CancelledError:
            cancelled = True
        except UnicodeDecodeError as exc:
            failure = CodexCtlError(
                ErrorCode.APP_SERVER_PROTOCOL_ERROR,
                "app-server protocol error: invalid UTF-8 frame",
                cause=exc,
            )
        except CodexCtlError as exc:
            failure = exc
        except Exception as exc:  # noqa: BLE001 - any transport loss ends the stream
            failure = _transport_failure(self._display, self._initialized, exc)
        finally:
            if not cancelled and failure is None and self._lifecycle == "OPEN":
                failure = _transport_failure(
                    self._display,
                    self._initialized,
                    RuntimeError("connection closed"),
                )
            if failure is not None:
                cleanup_task = self._begin_closing()
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(failure)
            elif cancelled:
                cleanup_task = self._begin_closing()
            else:
                cleanup_task = self._cleanup_task
            if cleanup_task is not None:
                await self._await_cleanup(cleanup_task)
            self._pending.clear()
            self._queue.shutdown()

    # -- server-initiated requests (unattended policy) ------------------------

    async def _handle_server_request(self, message: dict) -> None:
        """Answer server requests without ever waiting for a human.

        v1 brokers no interactive approvals: decline/cancel when a safe
        negative response exists, otherwise answer with a JSON-RPC error, and
        always surface UNSUPPORTED_INTERACTION on the notification stream.
        """
        method = message.get("method", "")
        request_id = message.get("id")
        params = message.get("params") or {}
        self.interaction_count += 1

        response: dict[str, Any] = {"id": request_id}
        if "requestApproval" in method:
            response["result"] = {"decision": "decline"}
        elif method == "mcpServer/elicitation/request":
            response["result"] = {"action": "decline", "content": None}
        else:
            response["error"] = {
                "code": -32601,
                "message": "codexctl does not support this interaction",
            }
        try:
            await self._send(response)
        except Exception:  # noqa: BLE001 - connection loss handled by reader
            pass
        await self._queue.put(
            {
                "method": UNSUPPORTED_INTERACTION_METHOD,
                "params": {
                    "method": method,
                    "threadId": params.get("threadId"),
                    "turnId": params.get("turnId"),
                },
            }
        )


async def connect_endpoint(
    endpoint: "AppServerEndpoint", timeout: float = 15.0
) -> AppServerPort:
    """Connect one resolved endpoint without exposing transport to callers."""
    # Delayed import avoids an endpoint -> appserver import cycle during
    # managed-runtime probing.
    from .endpoint import StdioProtocol, StdioTarget, TcpTarget, UnixTarget

    target = endpoint.target
    adapter: JsonRpcAppServerSession | None = None
    try:
        async with asyncio.timeout(timeout):
            if isinstance(target, UnixTarget):
                from websockets.asyncio.client import unix_connect

                # Codex's Unix transport rejects the client's default
                # permessage-deflate offer.
                conn = await unix_connect(
                    str(target.path), max_size=None, compression=None
                )
                transport: _AppServerMessageTransport = WebSocketMessageTransport(conn)
            elif isinstance(target, TcpTarget):
                from websockets.asyncio.client import connect

                headers: dict[str, str] | None = None
                if target.token_file is not None:
                    token = _read_endpoint_token(target.token_file)
                    headers = {"Authorization": f"Bearer {token}"}
                conn = await connect(
                    target.url,
                    additional_headers=headers,
                    max_size=None,
                    compression=None,
                )
                transport = WebSocketMessageTransport(conn)
            elif isinstance(target, StdioTarget):
                process = await _launch_stdio_process(target.argv)
                if target.protocol is StdioProtocol.WEBSOCKET:
                    transport = WebSocketMessageTransport(
                        await StdioWebSocketConnection.connect(process)
                    )
                else:
                    transport = JsonlStdioMessageTransport(process)
            else:  # pragma: no cover - targets are a closed endpoint vocabulary
                raise RuntimeError("unknown app-server endpoint target")

            adapter = JsonRpcAppServerSession(transport, endpoint.display)
            adapter._reader_task = asyncio.create_task(adapter._reader())
            await adapter._initialize()
    except asyncio.CancelledError:
        if adapter is not None:
            await adapter.close()
        raise
    except asyncio.TimeoutError as exc:
        if adapter is not None:
            await adapter.close()
        raise CodexCtlError(
            ErrorCode.APP_SERVER_UNAVAILABLE,
            f"app-server startup or initialize timed out after {timeout:g}s",
            cause=exc,
        ) from exc
    except CodexCtlError:
        if adapter is not None:
            await adapter.close()
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to application error
        if adapter is not None:
            await adapter.close()
        raise _unavailable(endpoint.display, exc) from exc
    assert adapter is not None
    return adapter


async def _launch_stdio_process(argv: tuple[str, ...]) -> _OwnedStdioProcess:
    """Launch stdio without making cancellation wait for process creation."""
    state = _StdioLaunchState()
    launch_task = asyncio.create_task(_OwnedStdioProcess.launch(argv, state))
    try:
        return await asyncio.shield(launch_task)
    except BaseException:
        # A cancelled launch may still finish after subprocess setup has
        # published its process handle. Drain it briefly when possible, but do
        # not let stalled process setup extend the startup deadline.
        launch_task.cancel()
        try:
            process = await asyncio.wait_for(
                asyncio.shield(launch_task), _STDIO_LAUNCH_DRAIN_SECONDS
            )
        except asyncio.TimeoutError:
            launch_task.add_done_callback(
                lambda task: _cleanup_stdio_launch(task, state)
            )
        except BaseException:
            await _close_stdio_launch(state)
        else:
            await _close_owned_process(process)
        raise


_STDIO_LAUNCH_DRAIN_SECONDS = 0.1


async def _close_owned_process(process: _OwnedStdioProcess) -> None:
    """Close a transport from a cancelled launch without leaking exceptions."""
    with contextlib.suppress(BaseException):
        await process.close()


async def _close_stdio_launch(state: _StdioLaunchState) -> None:
    """Close a child captured before its transport wrapper was returned."""
    if state.process is not None:
        await _close_owned_process(state.process)


def _cleanup_stdio_launch(
    launch_task: asyncio.Task[_OwnedStdioProcess], state: _StdioLaunchState
) -> None:
    """Finish cleanup if a cancelled launch eventually captures a process."""
    with contextlib.suppress(BaseException):
        launch_task.result()
    cleanup_task = asyncio.create_task(_close_stdio_launch(state))
    cleanup_task.add_done_callback(_consume_task)


def _consume_task(task: asyncio.Task[object]) -> None:
    with contextlib.suppress(BaseException):
        task.result()


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant: {value}")


def _decode_message(message: AppServerMessage) -> dict:
    """Decode exactly one text message/line into one JSON object."""
    if isinstance(message, bytes):
        raise CodexCtlError(
            ErrorCode.APP_SERVER_PROTOCOL_ERROR,
            "app-server protocol error: binary frame is not allowed",
        )
    try:
        decoded = json.loads(message, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise CodexCtlError(
            ErrorCode.APP_SERVER_PROTOCOL_ERROR,
            "app-server protocol error: malformed JSON frame",
            cause=exc,
        ) from exc
    if not isinstance(decoded, dict):
        raise CodexCtlError(
            ErrorCode.APP_SERVER_PROTOCOL_ERROR,
            "app-server protocol error: frame must contain a JSON object",
        )
    return decoded


def _parse_stdio_websocket_response(raw_response: bytes) -> Any:
    """Parse the HTTP/1.1 response to the fixed stdio WebSocket upgrade."""
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    if not raw_response.endswith(b"\r\n\r\n"):
        raise ValueError("incomplete WebSocket handshake response")
    lines = raw_response[:-4].split(b"\r\n")
    if not lines or len(lines[0].split(b" ", 2)) != 3:
        raise ValueError("malformed WebSocket handshake status line")
    protocol, raw_status, raw_reason = lines[0].split(b" ", 2)
    if protocol != b"HTTP/1.1":
        raise ValueError("WebSocket handshake must use HTTP/1.1")
    try:
        status = int(raw_status)
    except ValueError as exc:
        raise ValueError("malformed WebSocket handshake status code") from exc

    headers = Headers()
    for line in lines[1:]:
        if not line:
            raise ValueError("malformed WebSocket handshake header")
        try:
            name, value = line.split(b":", 1)
            header_name = name.decode("ascii")
            header_value = value.strip().decode("iso-8859-1")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("malformed WebSocket handshake header") from exc
        headers[header_name] = header_value
    return Response(status, raw_reason.decode("iso-8859-1"), headers)


def _transport_failure(
    endpoint: str, initialized: bool, cause: Exception
) -> CodexCtlError:
    if initialized:
        return CodexCtlError(
            ErrorCode.APP_SERVER_PROTOCOL_ERROR,
            f"app-server connection failed at {endpoint}: {cause}",
            cause=cause,
        )
    return _unavailable(endpoint, cause)


def _read_endpoint_token(token_file: Path) -> str:
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CodexCtlError(
            ErrorCode.APP_SERVER_UNAVAILABLE,
            "cannot read app-server endpoint token file",
            cause=exc,
        ) from exc
    if not token:
        raise CodexCtlError(
            ErrorCode.APP_SERVER_UNAVAILABLE,
            "app-server endpoint token file is empty",
        )
    return token


def _is_missing_method_error(exc: JsonRpcError) -> bool:
    if exc.code == -32601:
        return True
    if exc.code != -32600:
        return False
    message = exc.message.lower()
    return "unknown variant" in message or "unknown method" in message


def project_response(method: str, response: Any) -> AppServerResponse:
    """Project one JSON-RPC result into the adapter's typed interface."""
    payload = response if isinstance(response, dict) else {}
    if method == "initialize":
        return InitializeResponse(
            user_agent=_project_string(payload.get("userAgent")),
            codex_home=_project_string(payload.get("codexHome")),
        )
    if method in ("thread/start", "thread/read", "thread/resume"):
        return ThreadResponse(thread=_project_thread(payload.get("thread")))
    if method in ("turn/start", "turn/steer"):
        if method == "turn/start":
            turn = _project_turn(payload.get("turn"))
            return TurnResponse(turn_id=turn.id if turn is not None else None)
        return TurnResponse(turn_id=_project_string(payload.get("turnId")))
    if method == "thread/list":
        entries = payload.get("data")
        threads = [
            projected
            for entry in (entries if isinstance(entries, list) else [])
            if (projected := _project_thread_summary(entry)) is not None
        ]
        return ThreadListResponse(
            threads=threads,
            next_cursor=_project_string(payload.get("nextCursor")),
        )
    return EmptyResponse()


def _project_thread(raw: Any) -> AppServerThread | None:
    if not isinstance(raw, dict):
        return None
    status, flags = project_thread_status(raw.get("status"))
    turns = [
        projected
        for turn in raw.get("turns") or []
        if (projected := _project_turn(turn)) is not None
    ]
    return AppServerThread(
        id=_project_string(raw.get("id")) or "",
        status=status,
        active_flags=flags,
        turns=turns,
    )


def _project_turn(raw: Any) -> AppServerTurn | None:
    if not isinstance(raw, dict):
        return None
    items = [
        projected
        for item in raw.get("items") or []
        if (projected := project_item(item)) is not None
    ]
    return AppServerTurn(
        id=_project_string(raw.get("id")) or "",
        status=_project_string(raw.get("status")) or "",
        items=items,
    )


def _project_thread_summary(raw: Any) -> ThreadSummary | None:
    if not isinstance(raw, dict):
        return None
    status, _ = project_thread_status(raw.get("status"))
    updated_at = raw.get("updatedAt")
    return ThreadSummary(
        id=_project_string(raw.get("id")) or "",
        status=status,
        preview=_project_string(raw.get("preview")),
        updated_at=updated_at if isinstance(updated_at, int) else None,
    )


def _project_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _project_codex_error_info(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get("codexErrorInfo")
    return str(value) if value is not None else None


def _unavailable(endpoint: str, cause: Exception) -> CodexCtlError:
    return CodexCtlError(
        ErrorCode.APP_SERVER_UNAVAILABLE,
        f"cannot reach app-server at {endpoint}: {cause}",
        cause=cause,
    )


def parse_user_agent_version(user_agent: str | None) -> str | None:
    """Extract a version from an app-server user agent, tolerantly."""
    if not user_agent or "/" not in user_agent:
        return None
    tail = user_agent.rsplit("/", 1)[-1].strip()
    return tail.split()[0] if tail else None


# ---------------------------------------------------------------------------
# Projection (compatibility firewall)
# ---------------------------------------------------------------------------


def project_thread_status(status: Any) -> tuple[str, list[str]]:
    """Project a Codex ``ThreadStatus`` tagged union into the public vocabulary."""
    if isinstance(status, str):  # tolerate a plain string form
        return status, []
    if not isinstance(status, dict):
        return "notLoaded", []
    kind = status.get("type", "notLoaded")
    flags = status.get("activeFlags") or []
    if not isinstance(flags, list):
        flags = []
    return kind, [str(f) for f in flags]


def project_item(raw: Any) -> dict | None:
    """Project a Codex ``ThreadItem`` into the stable codexctl item shape.

    Unknown item types and unknown fields are dropped; additive fields are
    tolerated. Output content is metadata-only in v1.
    """
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    item_id = raw.get("id")
    if kind == "userMessage":
        parts = []
        for entry in raw.get("content") or []:
            if isinstance(entry, dict) and entry.get("type") == "text":
                parts.append(entry.get("text", ""))
        return {"id": item_id, "type": "userMessage", "text": "".join(parts)}
    if kind == "agentMessage":
        return {"id": item_id, "type": "agentMessage", "text": raw.get("text", "")}
    if kind == "commandExecution":
        projected: dict[str, Any] = {
            "id": item_id,
            "type": "commandExecution",
            "command": raw.get("command"),
        }
        if raw.get("cwd") is not None:
            projected["cwd"] = raw.get("cwd")
        if raw.get("exitCode") is not None:
            projected["exitCode"] = raw.get("exitCode")
        if raw.get("status") is not None:
            projected["status"] = raw.get("status")
        return projected
    if kind == "fileChange":
        changes = []
        for change in raw.get("changes") or []:
            if isinstance(change, dict):
                entry: dict[str, Any] = {"path": change.get("path")}
                if change.get("kind") is not None:
                    entry["kind"] = change.get("kind")
                changes.append(entry)
        projected = {"id": item_id, "type": "fileChange", "changes": changes}
        if raw.get("status") is not None:
            projected["status"] = raw.get("status")
        return projected
    if kind == "contextCompaction":
        return {"id": item_id, "type": "contextCompaction"}
    return None


def _project_usage(token_usage: Any) -> dict | None:
    if not isinstance(token_usage, dict):
        return None
    last = token_usage.get("last") or {}
    if not isinstance(last, dict):
        return None
    window = token_usage.get("modelContextWindow")
    if not isinstance(window, int) or window <= 0:
        return None
    try:
        used = int(last["totalTokens"])
    except KeyError, TypeError, ValueError:
        return None
    return {
        "usedTokens": used,
        "windowTokens": window,
        "ratio": context_usage_ratio(used, window),
    }


def project_notification(
    message: dict, source: Literal["live", "replay"] | None = None
) -> ProjectedEvent | None:
    """Project one raw notification into a stable projected event, or drop it."""
    method = message.get("method", "")
    params = message.get("params") or {}

    if method == UNSUPPORTED_INTERACTION_METHOD:
        return ProjectedEvent(
            "error",
            thread_id=params.get("threadId"),
            turn_id=params.get("turnId"),
            source=source,
            extra={
                "error": {
                    "code": ErrorCode.UNSUPPORTED_INTERACTION.value,
                    "message": (
                        "server requested an interaction codexctl cannot broker: "
                        f"{params.get('method')}"
                    ),
                }
            },
        )

    if method == "thread/started":
        thread = params.get("thread") or {}
        return ProjectedEvent(
            "thread/started", thread_id=thread.get("id"), source=source
        )

    if method == "turn/started":
        turn = params.get("turn") or {}
        return ProjectedEvent(
            "turn/started",
            thread_id=params.get("threadId"),
            turn_id=turn.get("id"),
            source=source,
        )

    if method == "turn/completed":
        turn = params.get("turn") or {}
        extra: dict[str, Any] = {"status": turn.get("status")}
        error = turn.get("error")
        if isinstance(error, dict) and error.get("message"):
            extra["error"] = {"message": error.get("message")}
        return ProjectedEvent(
            "turn/completed",
            thread_id=params.get("threadId"),
            turn_id=turn.get("id"),
            source=source,
            extra=extra,
        )

    if method in ("item/started", "item/completed"):
        item = project_item(params.get("item"))
        if item is None:
            return None
        return ProjectedEvent(
            method,
            thread_id=params.get("threadId"),
            turn_id=params.get("turnId"),
            item=item,
            source=source,
        )

    if method == "thread/tokenUsage/updated":
        usage = _project_usage(params.get("tokenUsage"))
        if usage is None:
            return None
        return ProjectedEvent(
            "thread/tokenUsage/updated",
            thread_id=params.get("threadId"),
            turn_id=params.get("turnId"),
            source=source,
            extra={"usage": usage},
        )

    if method == "error":
        error = params.get("error") or {}
        extra = {
            "error": {
                "code": ErrorCode.TURN_FAILED.value,
                "message": error.get("message", "turn error"),
            }
        }
        if params.get("willRetry"):
            extra["willRetry"] = True
        return ProjectedEvent(
            "error",
            thread_id=params.get("threadId"),
            turn_id=params.get("turnId"),
            source=source,
            extra=extra,
        )

    return None

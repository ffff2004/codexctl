"""Shared test doubles: scripted fake app-server and fake runtime provider."""

import asyncio
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import pytest

from codexctl.appserver import (
    _APPROVAL_POLICY_TO_WIRE,
    _APPROVALS_REVIEWER_TO_WIRE,
    REQUIRED_LIFECYCLE_OPERATIONS,
    AppServerResponse,
    AppServerThread,
    EmptyResponse,
    JsonRpcError,
    ThreadListResponse,
    ThreadResponse,
    TurnResponse,
    _serialize_sandbox_policy,
    project_notification,
    project_response,
)
from codexctl.endpoint import (
    AppServerEndpoint,
    StdioFraming,
    StdioRuntimeProvider,
    StdioTarget,
    UnixSocketTarget,
)
from codexctl.model import ProjectedEvent, StartConfig


class FakeAppServer:
    """Scripted ``AppServerClient`` implementation.

    Handlers are registered per method; unregistered methods fail with
    ``-32601`` like a real server would. Notifications are queued and served
    in order; ``end_stream`` terminates the notification iterator.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict | None]] = []
        self.handlers: dict[str, Callable[[dict | None], dict]] = {}
        self._queue: asyncio.Queue[ProjectedEvent] = asyncio.Queue()
        self.closed = False
        self.unsubscribed: list[str] = []
        self.missing_lifecycle_operations: set[str] = set()

    # -- scripting -----------------------------------------------------------

    def on(
        self, method: str, handler: Callable[[dict | None], dict]
    ) -> "FakeAppServer":
        self.handlers[method] = handler
        return self

    def result(self, method: str, value: dict) -> "FakeAppServer":
        self.handlers[method] = lambda params: value
        return self

    def sequence(self, method: str, values: list[dict]) -> "FakeAppServer":
        """Serve successive canned responses for repeated calls."""
        iterator = iter(values)

        def handler(params: dict | None) -> dict:
            return next(iterator)

        self.handlers[method] = handler
        return self

    def fail(
        self, method: str, code: int, message: str, data: Any = None
    ) -> "FakeAppServer":
        def handler(params: dict | None) -> dict:
            raise JsonRpcError(code, message, data)

        self.handlers[method] = handler
        return self

    def emit(self, method: str, params: dict | None) -> None:
        event = project_notification({"method": method, "params": params})
        if event is not None:
            self._queue.put_nowait(event)

    def end_stream(self) -> None:
        self._queue.shutdown()

    @property
    def methods_requested(self) -> list[str]:
        return [method for method, _ in self.requests]

    def params_of(self, method: str) -> dict | None:
        for name, params in self.requests:
            if name == method:
                return params
        return None

    # -- AppServerClient ---------------------------------------------------------

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
        return response.turn_id if response.turn_id else None

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
        params: dict[str, Any] = {"limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        if cwd is not None:
            params["cwd"] = cwd
        response = await self._request("thread/list", params)
        assert isinstance(response, ThreadListResponse)
        return response

    async def check_lifecycle_operations(self) -> tuple[str, ...]:
        return tuple(
            operation
            for operation in REQUIRED_LIFECYCLE_OPERATIONS
            if operation in self.missing_lifecycle_operations
        )

    async def _request(
        self, method: str, params: dict | None = None
    ) -> AppServerResponse:
        self.requests.append((method, params))
        handler = self.handlers.get(method)
        if handler is None:
            raise JsonRpcError(-32601, f"method not found: {method}")
        return project_response(method, handler(params))

    def notifications(self) -> AsyncIterator[ProjectedEvent]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[ProjectedEvent]:
        while True:
            try:
                yield await self._queue.get()
            except asyncio.QueueShutDown:
                return

    async def unsubscribe(self, thread_id: str) -> None:
        self.unsubscribed.append(thread_id)

    async def close(self) -> None:
        self.closed = True


class FakeRuntimeProvider:
    """Runtime provider returning a fixed fake endpoint."""

    mode = "fake"

    def __init__(
        self,
        resolve_error: Exception | None = None,
        cli_version: str | None = None,
        mode: str | None = None,
    ) -> None:
        self._resolve_error = resolve_error
        self._cli_version = cli_version
        if mode is not None:
            self.mode = mode

    async def resolve_endpoint(self) -> AppServerEndpoint:
        if self._resolve_error is not None:
            raise self._resolve_error
        return AppServerEndpoint(
            display="/fake.sock", target=UnixSocketTarget(Path("/fake.sock"))
        )

    def probe_cli_version(self) -> str | None:
        return self._cli_version


def make_ctl(server: FakeAppServer, runtime: FakeRuntimeProvider | None = None):
    """Build a CodexCtl wired to the fake server (no sockets involved)."""
    from codexctl.core import CodexCtl

    async def connect(endpoint_: AppServerEndpoint):
        return server

    return CodexCtl(runtime or FakeRuntimeProvider(), connect=connect)


async def collect(outcome) -> tuple[list, Any]:
    """Drain a streaming outcome into (events, terminal)."""
    events = [event async for event in outcome.events]
    terminal = await outcome.result
    return events, terminal


@pytest.fixture(autouse=True)
def isolated_codex_home(tmp_path, monkeypatch) -> Path:
    """Never touch the real ~/.codex from tests."""
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def stdio_endpoint(tmp_path):
    """Write a temporary stdio server and return its endpoint factory."""

    def make_endpoint(
        source: str,
        *args: str,
        filename: str = "stdio-server.py",
        framing: StdioFraming = StdioFraming.JSONL,
    ) -> AppServerEndpoint:
        script = tmp_path / filename
        script.write_text(source, encoding="utf-8")
        return AppServerEndpoint(
            "stdio",
            StdioTarget((sys.executable, str(script), *args), framing),
        )

    return make_endpoint


@pytest.fixture
def stdio_runtime_provider(stdio_endpoint):
    """Build a runtime provider for a temporary stdio server."""

    def make_provider(
        source: str,
        *args: str,
        filename: str = "stdio-server.py",
    ) -> StdioRuntimeProvider:
        endpoint = stdio_endpoint(source, *args, filename=filename)
        target = endpoint.target
        assert isinstance(target, StdioTarget)
        return StdioRuntimeProvider(target.argv[0], target.argv[1:])

    return make_provider

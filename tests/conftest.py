"""Shared test doubles: scripted fake app-server and fake endpoint."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import pytest

from codexctl.appserver import (
    AppServerThread,
    AppServerResponse,
    EmptyResponse,
    JsonRpcError,
    REQUIRED_LIFECYCLE_OPERATIONS,
    ThreadListResponse,
    ThreadResponse,
    TurnResponse,
    _serialize_sandbox_policy,
    project_notification,
    project_response,
)
from codexctl.endpoint import AppServerEndpoint
from codexctl.model import ProjectedEvent, StartConfig


class FakeAppServer:
    """Scripted ``AppServerPort`` implementation.

    Handlers are registered per method; unregistered methods fail with
    ``-32601`` like a real server would. Notifications are queued and served
    in order; ``end_stream`` terminates the notification iterator.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict | None]] = []
        self.handlers: dict[str, Callable[[dict | None], dict]] = {}
        self._queue: asyncio.Queue[ProjectedEvent | None] = asyncio.Queue()
        self.closed = False
        self.unsubscribed: list[str] = []
        self.missing_lifecycle_operations: set[str] = set()

    # -- scripting -----------------------------------------------------------

    def on(self, method: str, handler: Callable[[dict | None], dict]) -> "FakeAppServer":
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
        self._queue.put_nowait(None)

    @property
    def methods_requested(self) -> list[str]:
        return [method for method, _ in self.requests]

    def params_of(self, method: str) -> dict | None:
        for name, params in self.requests:
            if name == method:
                return params
        return None

    # -- AppServerPort ---------------------------------------------------------

    async def read_thread(self, thread_id: str) -> AppServerThread | None:
        response = await self._request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        assert isinstance(response, ThreadResponse)
        return response.thread

    async def start_thread(self, config: StartConfig) -> AppServerThread | None:
        params: dict[str, Any] = {
            "approvalPolicy": "never",
            "sandbox": _serialize_sandbox_policy(config.sandbox),
        }
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

    async def list_threads(self, cursor: str | None = None) -> ThreadListResponse:
        params: dict[str, Any] = {"limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
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
            message = await self._queue.get()
            if message is None:
                return
            yield message

    async def unsubscribe(self, thread_id: str) -> None:
        self.unsubscribed.append(thread_id)

    async def close(self) -> None:
        self.closed = True


class FakeEndpoint:
    """Endpoint port returning a fixed fake endpoint."""

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

    async def resolve(self) -> AppServerEndpoint:
        if self._resolve_error is not None:
            raise self._resolve_error
        return AppServerEndpoint(socket_path=Path("/fake.sock"))

    def probe_cli_version(self) -> str | None:
        return self._cli_version


def make_ctl(server: FakeAppServer, endpoint: FakeEndpoint | None = None):
    """Build a CodexCtl wired to the fake server (no sockets involved)."""
    from codexctl.core import CodexCtl

    async def connect(endpoint_: AppServerEndpoint):
        return server

    return CodexCtl(endpoint or FakeEndpoint(), connect=connect)


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

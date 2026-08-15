"""Shared test doubles: scripted fake app-server and fake endpoint."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import pytest

from codexctl.appserver import JsonRpcError
from codexctl.endpoint import AppServerEndpoint


class FakeAppServer:
    """Scripted ``AppServerPort`` implementation.

    Handlers are registered per method; unregistered methods fail with
    ``-32601`` like a real server would. Notifications are queued and served
    in order; ``end_stream`` terminates the notification iterator.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict | None]] = []
        self.handlers: dict[str, Callable[[dict | None], dict]] = {}
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.closed = False
        self.unsubscribed: list[str] = []

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
        self._queue.put_nowait({"method": method, "params": params})

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

    async def request(self, method: str, params: dict | None = None) -> dict:
        self.requests.append((method, params))
        handler = self.handlers.get(method)
        if handler is None:
            raise JsonRpcError(-32601, f"method not found: {method}")
        return handler(params)

    def notifications(self) -> AsyncIterator[dict]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[dict]:
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

"""Projection: the compatibility firewall between Codex wire shapes and ours."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
from pathlib import Path

import pytest
from websockets.asyncio.server import serve, unix_serve

from codexctl.appserver import (
    REQUIRED_LIFECYCLE_OPERATIONS,
    UNSUPPORTED_INTERACTION_METHOD,
    AppServerTurn,
    JsonRpcError,
    ThreadListResponse,
    ThreadResponse,
    TurnResponse,
    WebSocketAppServerAdapter,
    connect_endpoint,
    parse_user_agent_version,
    project_item,
    project_notification,
    project_response,
    project_thread_status,
)
from codexctl.endpoint import AppServerEndpoint, TcpTarget, UnixTarget
from codexctl.model import CodexCtlError, ErrorCode, SandboxPolicy, StartConfig


class TestProjectThreadStatus:
    def test_tagged_union_forms(self):
        assert project_thread_status({"type": "notLoaded"}) == ("notLoaded", [])
        assert project_thread_status({"type": "idle"}) == ("idle", [])
        assert project_thread_status({"type": "systemError"}) == ("systemError", [])

    def test_active_with_flags(self):
        status = {"type": "active", "activeFlags": ["waitingOnApproval"]}
        assert project_thread_status(status) == ("active", ["waitingOnApproval"])

    def test_tolerant_forms(self):
        assert project_thread_status("idle") == ("idle", [])
        assert project_thread_status(None) == ("notLoaded", [])
        assert project_thread_status({"type": "active", "activeFlags": "x"}) == (
            "active",
            [],
        )


class TestProjectResponse:
    def test_thread_response_projects_nested_turns_and_items(self):
        response = project_response(
            "thread/read",
            {
                "thread": {
                    "id": "t1",
                    "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
                    "turns": [
                        {
                            "id": "u1",
                            "status": "inProgress",
                            "items": [
                                {"type": "agentMessage", "id": "i1", "text": "hello"},
                                {"type": "plan", "id": "i2"},
                            ],
                        }
                    ],
                }
            },
        )

        assert isinstance(response, ThreadResponse)
        assert response.thread is not None
        assert response.thread.id == "t1"
        assert response.thread.status == "active"
        assert response.thread.active_flags == ["waitingOnApproval"]
        turn = response.thread.turns[0]
        assert isinstance(turn, AppServerTurn)
        assert turn.in_progress
        assert turn.items == [{"id": "i1", "type": "agentMessage", "text": "hello"}]
        assert not isinstance(response, dict)

    def test_operation_specific_results_are_projected(self):
        assert project_response("turn/start", {"turn": {"id": "u1"}}) == TurnResponse(
            turn_id="u1"
        )
        assert project_response("turn/steer", {"turnId": "u1"}) == TurnResponse(
            turn_id="u1"
        )
        response = project_response(
            "thread/list",
            {
                "data": [{"id": "t1", "status": {"type": "idle"}}],
                "nextCursor": "next",
            },
        )
        assert isinstance(response, ThreadListResponse)
        assert response.next_cursor == "next"
        assert response.threads[0].status == "idle"


class TestTypedOperations:
    async def test_start_thread_serializes_upstream_sandbox_enum(self, monkeypatch):
        adapter = WebSocketAppServerAdapter(None, "/fake.sock")
        calls = []

        async def request(method, params=None):
            calls.append((method, params))
            return project_response(method, {"thread": {"id": "t1"}})

        monkeypatch.setattr(adapter, "_request", request)

        for policy, wire_value in (
            (None, "workspace-write"),
            (SandboxPolicy.readOnly, "read-only"),
            (SandboxPolicy.workspaceWrite, "workspace-write"),
            (SandboxPolicy.dangerFullAccess, "danger-full-access"),
        ):
            assert (await adapter.start_thread(StartConfig(sandbox=policy))).id == "t1"
            assert calls[-1] == (
                "thread/start",
                {"approvalPolicy": "never", "sandbox": wire_value},
            )

    async def test_start_thread_rejects_unsupported_sandbox_policy(self, monkeypatch):
        adapter = WebSocketAppServerAdapter(None, "/fake.sock")
        calls = []

        async def request(method, params=None):
            calls.append((method, params))
            return project_response(method, {"thread": {"id": "t1"}})

        monkeypatch.setattr(adapter, "_request", request)

        with pytest.raises(CodexCtlError) as excinfo:
            await adapter.start_thread(StartConfig(sandbox="not-a-policy"))  # type: ignore[arg-type]

        assert excinfo.value.code == ErrorCode.USAGE_ERROR
        assert "unsupported sandbox policy 'not-a-policy'" in excinfo.value.message
        assert calls == []

    async def test_operations_keep_wire_requests_inside_the_adapter(self, monkeypatch):
        adapter = WebSocketAppServerAdapter(None, "/fake.sock")
        calls = []
        responses = {
            "thread/start": {"thread": {"id": "t1"}},
            "turn/start": {"turn": {"id": "u1"}},
            "thread/read": {"thread": {"id": "t1", "status": "idle"}},
            "thread/resume": {"thread": {"id": "t1", "status": "idle"}},
            "turn/steer": {"turnId": "u1"},
            "turn/interrupt": {},
            "thread/list": {"data": [], "nextCursor": "next"},
        }

        async def request(method, params=None):
            calls.append((method, params))
            return project_response(method, responses[method])

        monkeypatch.setattr(adapter, "_request", request)

        assert (
            await adapter.start_thread(
                StartConfig(cwd="/tmp", model="o4-mini", sandbox=SandboxPolicy.readOnly)
            )
        ).id == "t1"
        assert await adapter.start_turn("t1", "hello", effort="high") == "u1"
        assert (await adapter.read_thread("t1")).id == "t1"
        assert (await adapter.resume_thread("t1")).id == "t1"
        assert await adapter.steer_turn("t1", "more", "u1") == "u1"
        await adapter.interrupt_turn("t1", "u1")
        assert (await adapter.list_threads("c1", cwd="/tmp")).next_cursor == "next"

        assert calls == [
            (
                "thread/start",
                {
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "cwd": "/tmp",
                    "model": "o4-mini",
                },
            ),
            (
                "turn/start",
                {
                    "threadId": "t1",
                    "input": [{"type": "text", "text": "hello"}],
                    "effort": "high",
                },
            ),
            ("thread/read", {"threadId": "t1", "includeTurns": True}),
            ("thread/resume", {"threadId": "t1"}),
            (
                "turn/steer",
                {
                    "threadId": "t1",
                    "input": [{"type": "text", "text": "more"}],
                    "expectedTurnId": "u1",
                },
            ),
            ("turn/interrupt", {"threadId": "t1", "turnId": "u1"}),
            ("thread/list", {"limit": 100, "cursor": "c1", "cwd": "/tmp"}),
        ]
        assert not hasattr(adapter, "request")

    async def test_lifecycle_probe_checks_each_required_operation(self, monkeypatch):
        adapter = WebSocketAppServerAdapter(None, "/fake.sock")
        responses = {
            "thread/start": {"thread": {"id": "probe"}},
            "thread/resume": {"thread": {"id": "probe", "status": "idle"}},
            "thread/read": {"thread": {"id": "probe", "status": "idle"}},
            "thread/list": {"data": []},
            "turn/start": {"turn": {"id": "probe"}},
            "turn/steer": {"turnId": "probe"},
            "turn/interrupt": {},
        }
        calls = []

        async def request(method, params=None):
            calls.append((method, params))
            return project_response(method, responses[method])

        monkeypatch.setattr(adapter, "_request", request)

        assert await adapter.check_lifecycle_operations() == ()
        assert len(calls) == len(REQUIRED_LIFECYCLE_OPERATIONS)
        assert [method for method, _ in calls] == [
            "thread/start",
            "thread/resume",
            "thread/read",
            "thread/list",
            "turn/start",
            "turn/steer",
            "turn/interrupt",
        ]
        assert calls[0][1] == {"ephemeral": True, "historyMode": "paginated"}

    async def test_lifecycle_probe_reports_method_not_found(self, monkeypatch):
        adapter = WebSocketAppServerAdapter(None, "/fake.sock")
        missing = "steer turn"

        async def request(method, params=None):
            if method == "turn/steer":
                raise JsonRpcError(-32601, "method not found")
            return project_response(method, {})

        monkeypatch.setattr(adapter, "_request", request)

        assert await adapter.check_lifecycle_operations() == (missing,)


class TestUnixSocketConnection:
    async def test_codex_socket_handshake_disables_permessage_deflate(self, tmp_path):
        socket_path = tmp_path / "app-server.sock"

        async def handle(reader, writer):
            request = await reader.readuntil(b"\r\n\r\n")

            # Codex v0.147's Unix transport uses tokio_tungstenite::accept_async,
            # which closes the socket when websockets advertises its default
            # permessage-deflate extension instead of completing the upgrade.
            if b"sec-websocket-extensions: permessage-deflate" in request.lower():
                writer.close()
                await writer.wait_closed()
                return

            headers = {
                line.split(b":", 1)[0].lower(): line.split(b":", 1)[1].strip()
                for line in request.split(b"\r\n")[1:]
                if b":" in line
            }
            accept = base64.b64encode(
                hashlib.sha1(
                    headers[b"sec-websocket-key"]
                    + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                ).digest()
            )
            response = (
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            payload = json.dumps(
                {
                    "id": 1,
                    "result": {
                        "userAgent": "codex-app-server/0.147.0",
                        "codexHome": str(tmp_path / "codex-home"),
                    },
                }
            ).encode()
            if len(payload) < 126:
                frame_header = bytes((0x81, len(payload)))
            else:
                frame_header = b"\x81\x7e" + struct.pack("!H", len(payload))
            writer.write(response + frame_header + payload)
            await writer.drain()

            # Consume the client's initialize and initialized frames, then
            # complete the close handshake when the adapter is cleaned up.
            while True:
                first, second = await reader.readexactly(2)
                frame_length = second & 0x7F
                if frame_length == 126:
                    frame_length = struct.unpack("!H", await reader.readexactly(2))[0]
                elif frame_length == 127:
                    frame_length = struct.unpack("!Q", await reader.readexactly(8))[0]
                if second & 0x80:
                    await reader.readexactly(4)
                await reader.readexactly(frame_length)
                if first & 0x0F == 0x08:
                    writer.write(b"\x88\x00")
                    await writer.drain()
                    break

        server = await asyncio.start_unix_server(handle, path=socket_path)
        adapter = None
        try:
            adapter = await connect_endpoint(
                AppServerEndpoint(str(socket_path), UnixTarget(socket_path))
            )
        finally:
            if adapter is not None:
                await adapter.close()
            server.close()
            await server.wait_closed()

        assert adapter.user_agent == "codex-app-server/0.147.0"

    async def test_connects_over_unix_socket_and_completes_initialization(
        self, tmp_path
    ):
        socket_path = tmp_path / "app-server.sock"
        received: list[dict] = []
        request_headers: list[dict[str, str]] = []
        initialized = asyncio.Event()

        async def handle(connection):
            request_headers.append(dict(connection.request.headers))
            async for frame in connection:
                message = json.loads(frame)
                received.append(message)
                if message.get("method") == "initialize":
                    await connection.send(
                        json.dumps(
                            {
                                "id": message["id"],
                                "result": {
                                    "userAgent": "codex-app-server/0.101.0",
                                    "codexHome": str(tmp_path / "codex-home"),
                                },
                            }
                        )
                    )
                elif message.get("method") == "initialized":
                    initialized.set()

        server = await unix_serve(handle, str(socket_path))
        adapter = None
        try:
            adapter = await connect_endpoint(
                AppServerEndpoint(str(socket_path), UnixTarget(socket_path))
            )
            await asyncio.wait_for(initialized.wait(), timeout=1.0)
        finally:
            if adapter is not None:
                await adapter.close()
            server.close()
            await server.wait_closed()

        assert adapter.user_agent == "codex-app-server/0.101.0"
        assert adapter.server_codex_home == str(tmp_path / "codex-home")
        assert [message["method"] for message in received] == [
            "initialize",
            "initialized",
        ]
        assert "sec-websocket-extensions" not in request_headers[0]

    async def test_tcp_connects_initializes_without_auth_or_compression(self):
        received: list[dict] = []
        requests: list[tuple[str, dict[str, str]]] = []
        initialized = asyncio.Event()

        async def handle(connection):
            requests.append((connection.request.path, dict(connection.request.headers)))
            async for frame in connection:
                message = json.loads(frame)
                received.append(message)
                if message.get("method") == "initialize":
                    await connection.send(
                        json.dumps(
                            {
                                "id": message["id"],
                                "result": {"userAgent": "codex-app-server/0.101.0"},
                            }
                        )
                    )
                elif message.get("method") == "initialized":
                    initialized.set()

        server = await serve(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        adapter = None
        try:
            adapter = await connect_endpoint(
                AppServerEndpoint(
                    f"ws://127.0.0.1:{port}/app-server?client=test",
                    TcpTarget(f"ws://127.0.0.1:{port}/app-server?client=test", None),
                )
            )
            await asyncio.wait_for(initialized.wait(), timeout=1.0)
        finally:
            if adapter is not None:
                await adapter.close()
            server.close()
            await server.wait_closed()

        assert adapter.user_agent == "codex-app-server/0.101.0"
        assert [message["method"] for message in received] == [
            "initialize",
            "initialized",
        ]
        assert requests[0][0] == "/app-server?client=test"
        assert "authorization" not in requests[0][1]
        assert "sec-websocket-extensions" not in requests[0][1]

    async def test_tcp_connect_passes_token_path_query_and_no_compression(
        self, tmp_path, monkeypatch
    ):
        captured: dict = {}

        class Connection:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def close(self):
                pass

        async def fake_connect(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Connection()

        async def initialize(self):
            pass

        import websockets.asyncio.client

        monkeypatch.setattr(websockets.asyncio.client, "connect", fake_connect)
        monkeypatch.setattr(WebSocketAppServerAdapter, "_initialize", initialize)
        token_file = tmp_path / "token"
        endpoint = AppServerEndpoint(
            "ws://127.0.0.1:7777/app-server?client=test",
            TcpTarget("ws://127.0.0.1:7777/app-server?client=test", token_file),
        )
        # The file intentionally does not exist until connection time.
        token_file.write_text("  secret-token\n", encoding="utf-8")
        adapter = await connect_endpoint(endpoint)
        await adapter.close()

        assert captured["url"] == "ws://127.0.0.1:7777/app-server?client=test"
        assert captured["additional_headers"] == {
            "Authorization": "Bearer secret-token"
        }
        assert captured["compression"] is None

    @pytest.mark.parametrize("contents", ["", " \n"])
    async def test_empty_tcp_token_is_unavailable(self, tmp_path, contents):
        token_file = tmp_path / "token"
        token_file.write_text(contents, encoding="utf-8")
        endpoint = AppServerEndpoint(
            "ws://127.0.0.1:1", TcpTarget("ws://127.0.0.1:1", token_file)
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await connect_endpoint(endpoint)
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_missing_tcp_token_is_unavailable(self, tmp_path):
        token_file = tmp_path / "missing-token"
        endpoint = AppServerEndpoint(
            "ws://127.0.0.1:1", TcpTarget("ws://127.0.0.1:1", token_file)
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await connect_endpoint(endpoint)
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_unreadable_tcp_token_is_unavailable_without_exposing_path_or_contents(
        self, tmp_path, monkeypatch
    ):
        token_file = tmp_path / "token"
        endpoint = AppServerEndpoint(
            "ws://127.0.0.1:1", TcpTarget("ws://127.0.0.1:1", token_file)
        )
        original_read_text = Path.read_text

        def unreadable_read_text(path, *args, **kwargs):
            if path == token_file:
                raise PermissionError("secret-token")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unreadable_read_text)
        with pytest.raises(CodexCtlError) as excinfo:
            await connect_endpoint(endpoint)
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE
        assert "secret-token" not in excinfo.value.message
        assert str(token_file) not in excinfo.value.message


class TestProjectItem:
    def test_user_message_joins_text_parts(self):
        raw = {
            "type": "userMessage",
            "id": "i1",
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "image"},  # dropped: v1 is text-only metadata
                {"type": "text", "text": "world"},
            ],
        }
        assert project_item(raw) == {
            "id": "i1",
            "type": "userMessage",
            "text": "hello world",
        }

    def test_agent_message(self):
        raw = {"type": "agentMessage", "id": "i2", "text": "done", "extraField": 1}
        assert project_item(raw) == {"id": "i2", "type": "agentMessage", "text": "done"}

    def test_command_execution_keeps_metadata_only(self):
        raw = {
            "type": "commandExecution",
            "id": "i3",
            "command": "ls",
            "cwd": "/tmp",
            "status": "completed",
            "exitCode": 0,
            "aggregatedOutput": "secret output",  # never projected in v1
        }
        projected = project_item(raw)
        assert projected == {
            "id": "i3",
            "type": "commandExecution",
            "command": "ls",
            "cwd": "/tmp",
            "status": "completed",
            "exitCode": 0,
        }
        assert "aggregatedOutput" not in projected

    def test_file_change(self):
        raw = {
            "type": "fileChange",
            "id": "i4",
            "changes": [{"path": "a.py", "kind": "added"}, {"path": "b.py"}],
        }
        assert project_item(raw) == {
            "id": "i4",
            "type": "fileChange",
            "changes": [{"path": "a.py", "kind": "added"}, {"path": "b.py"}],
        }

    def test_context_compaction_and_unknown(self):
        assert project_item({"type": "contextCompaction", "id": "i5"}) == {
            "id": "i5",
            "type": "contextCompaction",
        }
        assert project_item({"type": "mcpToolCall", "id": "i6"}) is None
        assert project_item("not a dict") is None


class TestProjectNotification:
    def test_turn_completed_carries_status_and_error(self):
        message = {
            "method": "turn/completed",
            "params": {
                "threadId": "t1",
                "turn": {"id": "u1", "status": "failed", "error": {"message": "boom"}},
            },
        }
        event = project_notification(message, source="live")
        assert event.type == "turn/completed"
        assert event.turn_id == "u1"
        assert event.extra["status"] == "failed"
        assert event.extra["error"] == {"message": "boom"}

    def test_item_notifications_project_and_drop(self):
        started = {
            "method": "item/started",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {"type": "agentMessage", "id": "i1", "text": "hi"},
            },
        }
        event = project_notification(started)
        assert event is not None and event.item["type"] == "agentMessage"

        unknown = {
            "method": "item/completed",
            "params": {"threadId": "t1", "turnId": "u1", "item": {"type": "plan"}},
        }
        assert project_notification(unknown) is None

    def test_token_usage_projects_into_usage_extra(self):
        message = {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "tokenUsage": {
                    "total": {
                        "inputTokens": 1000000,
                        "cachedInputTokens": 500000,
                    },
                    "last": {"totalTokens": 83000},
                    "modelContextWindow": 200000,
                },
            },
        }
        event = project_notification(message)
        assert event is not None
        assert event.extra["usage"] == {
            "usedTokens": 83000,
            "windowTokens": 200000,
            "ratio": 0.38,
        }

    def test_token_usage_dropped_without_window(self):
        message = {
            "method": "thread/tokenUsage/updated",
            "params": {"threadId": "t1", "tokenUsage": {"total": {"inputTokens": 5}}},
        }
        assert project_notification(message) is None

    def test_token_usage_dropped_when_last_is_malformed(self):
        message = {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "t1",
                "tokenUsage": {
                    "total": {"inputTokens": 5},
                    "last": "unavailable",
                    "modelContextWindow": 200000,
                },
            },
        }
        assert project_notification(message) is None

    def test_unsupported_interaction_becomes_error_event(self):
        message = {
            "method": UNSUPPORTED_INTERACTION_METHOD,
            "params": {
                "method": "item/commandExecution/requestApproval",
                "threadId": "t1",
                "turnId": "u1",
            },
        }
        event = project_notification(message)
        assert event is not None
        assert event.type == "error"
        assert event.extra["error"]["code"] == ErrorCode.UNSUPPORTED_INTERACTION.value

    def test_error_notification(self):
        message = {
            "method": "error",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "error": {"message": "x"},
                "willRetry": True,
            },
        }
        event = project_notification(message)
        assert event is not None
        assert event.extra["error"]["message"] == "x"
        assert event.extra["willRetry"] is True

    def test_unknown_notification_dropped(self):
        assert project_notification({"method": "thread/progress", "params": {}}) is None

    def test_thread_started(self):
        message = {"method": "thread/started", "params": {"thread": {"id": "t9"}}}
        event = project_notification(message)
        assert event is not None
        assert event.type == "thread/started" and event.thread_id == "t9"


class TestParseUserAgentVersion:
    def test_versions(self):
        assert parse_user_agent_version("codex-cli/0.101.0") == "0.101.0"
        assert parse_user_agent_version("codex app-server/0.99.1 (linux)") == "0.99.1"
        assert parse_user_agent_version("noversion") is None
        assert parse_user_agent_version(None) is None
        assert parse_user_agent_version("name/") is None

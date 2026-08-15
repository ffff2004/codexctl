"""Projection: the compatibility firewall between Codex wire shapes and ours."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from websockets.asyncio.server import unix_serve

from codexctl.appserver import (
    AppServerTurn,
    JsonRpcError,
    REQUIRED_LIFECYCLE_OPERATIONS,
    ThreadListResponse,
    ThreadResponse,
    TurnResponse,
    UNSUPPORTED_INTERACTION_METHOD,
    UnixSocketAppServerAdapter,
    parse_user_agent_version,
    project_item,
    project_notification,
    project_response,
    project_thread_status,
)
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
        adapter = UnixSocketAppServerAdapter(None, Path("/fake.sock"))
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
            assert (
                await adapter.start_thread(StartConfig(sandbox=policy))
            ).id == "t1"
            assert calls[-1] == (
                "thread/start",
                {"approvalPolicy": "never", "sandbox": wire_value},
            )

    async def test_start_thread_rejects_unsupported_sandbox_policy(self, monkeypatch):
        adapter = UnixSocketAppServerAdapter(None, Path("/fake.sock"))
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
        adapter = UnixSocketAppServerAdapter(None, Path("/fake.sock"))
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

        assert (await adapter.start_thread(
            StartConfig(
                cwd="/tmp", model="o4-mini", sandbox=SandboxPolicy.readOnly
            )
        )).id == "t1"
        assert await adapter.start_turn("t1", "hello", effort="high") == "u1"
        assert (await adapter.read_thread("t1")).id == "t1"
        assert (await adapter.resume_thread("t1")).id == "t1"
        assert await adapter.steer_turn("t1", "more", "u1") == "u1"
        await adapter.interrupt_turn("t1", "u1")
        assert (await adapter.list_threads("c1")).next_cursor == "next"

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
            ("thread/list", {"limit": 100, "cursor": "c1"}),
        ]
        assert not hasattr(adapter, "request")

    async def test_lifecycle_probe_checks_each_required_operation(self, monkeypatch):
        adapter = UnixSocketAppServerAdapter(None, Path("/fake.sock"))
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
        adapter = UnixSocketAppServerAdapter(None, Path("/fake.sock"))
        missing = "steer turn"

        async def request(method, params=None):
            if method == "turn/steer":
                raise JsonRpcError(-32601, "method not found")
            return project_response(method, {})

        monkeypatch.setattr(adapter, "_request", request)

        assert await adapter.check_lifecycle_operations() == (missing,)


class TestUnixSocketConnection:
    async def test_connects_over_unix_socket_and_completes_initialization(self, tmp_path):
        socket_path = tmp_path / "app-server.sock"
        received: list[dict] = []
        initialized = asyncio.Event()

        async def handle(connection):
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
            adapter = await UnixSocketAppServerAdapter.connect(socket_path)
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
        assert project_item(raw) == {"id": "i1", "type": "userMessage", "text": "hello world"}

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
                    "total": {"inputTokens": 80000, "cachedInputTokens": 3000},
                    "modelContextWindow": 200000,
                },
            },
        }
        event = project_notification(message)
        assert event is not None
        assert event.extra["usage"] == {
            "usedTokens": 83000,
            "windowTokens": 200000,
            "ratio": 0.415,
        }

    def test_token_usage_dropped_without_window(self):
        message = {
            "method": "thread/tokenUsage/updated",
            "params": {"threadId": "t1", "tokenUsage": {"total": {"inputTokens": 5}}},
        }
        assert project_notification(message) is None

    def test_token_usage_dropped_when_total_is_malformed(self):
        message = {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "t1",
                "tokenUsage": {
                    "total": "unavailable",
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
            "params": {"threadId": "t1", "turnId": "u1", "error": {"message": "x"}, "willRetry": True},
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

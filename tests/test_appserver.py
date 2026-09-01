"""Projection: the compatibility firewall between Codex wire shapes and ours."""

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import signal
import struct
import sys
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
    connect_app_server,
    parse_user_agent_version,
    project_item,
    project_notification,
    project_response,
    project_thread_status,
)
from codexctl.endpoint import (
    AppServerEndpoint,
    SshRuntimeProvider,
    StdioFraming,
    StdioTarget,
    UnixSocketTarget,
    WebSocketTarget,
)
from codexctl.model import (
    ApprovalPolicy,
    ApprovalsReviewer,
    CodexCtlError,
    ErrorCode,
    IsolationOptions,
    SandboxPolicy,
    StartConfig,
)


def _stdio_websocket_proxy_source(
    *,
    initialize_action: str = (
        "send_text(json.dumps({'id': value['id'], 'result': "
        "{'userAgent': 'proxy/2.0'}}))"
    ),
    initialized_action: str = "pass",
    thread_list_action: str | None = None,
    after_handshake: str = "pass",
    termination_marker: bool = False,
    termination_action: str = "raise SystemExit(0)",
) -> str:
    signal_setup = ""
    if termination_marker:
        indented_termination_action = termination_action.replace("\n", "\n    ")
        signal_setup = (
            "def stop(signum, frame):\n"
            "    pathlib.Path(sys.argv[-1]).write_text('terminated')\n"
            f"    {indented_termination_action}\n"
            "signal.signal(signal.SIGTERM, stop)\n"
        )
    return (
        "import base64, hashlib, json, pathlib, signal, struct, subprocess, sys, time\n"
        f"{signal_setup}"
        "def read_exact(size):\n"
        "    value = b''\n"
        "    while len(value) < size:\n"
        "        chunk = sys.stdin.buffer.read(size - len(value))\n"
        "        if not chunk:\n"
        "            raise EOFError\n"
        "        value += chunk\n"
        "    return value\n"
        "def read_frame():\n"
        "    first, second = read_exact(2)\n"
        "    length = second & 127\n"
        "    if length == 126:\n"
        "        length = struct.unpack('!H', read_exact(2))[0]\n"
        "    elif length == 127:\n"
        "        length = struct.unpack('!Q', read_exact(8))[0]\n"
        "    mask = read_exact(4) if second & 128 else None\n"
        "    data = read_exact(length)\n"
        "    if mask:\n"
        "        data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))\n"
        "    return first & 15, bool(first & 128), data\n"
        "def send_frame(opcode, data, final=True):\n"
        "    length = len(data)\n"
        "    header = bytes([(128 if final else 0) | opcode])\n"
        "    if length < 126:\n"
        "        header += bytes([length])\n"
        "    elif length < 65536:\n"
        "        header += bytes([126]) + struct.pack('!H', length)\n"
        "    else:\n"
        "        header += bytes([127]) + struct.pack('!Q', length)\n"
        "    sys.stdout.buffer.write(header + data)\n"
        "    sys.stdout.buffer.flush()\n"
        "def send_text(data):\n"
        "    send_frame(1, data.encode())\n"
        "request = b''\n"
        "while not request.endswith(b'\\r\\n\\r\\n'):\n"
        "    request += read_exact(1)\n"
        "headers = {line.split(b':', 1)[0].lower(): line.split(b':', 1)[1].strip()\n"
        "           for line in request.split(b'\\r\\n')[1:] if b':' in line}\n"
        "assert request.startswith(b'GET / HTTP/1.1\\r\\n')\n"
        "assert b'sec-websocket-extensions' not in headers\n"
        "accept = base64.b64encode(hashlib.sha1(\n"
        "    headers[b'sec-websocket-key'] + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'\n"
        ").digest())\n"
        "sys.stdout.buffer.write(\n"
        "    b'HTTP/1.1 101 Switching Protocols\\r\\n'\n"
        "    b'Upgrade: websocket\\r\\nConnection: Upgrade\\r\\n'\n"
        "    b'Sec-WebSocket-Accept: ' + accept + b'\\r\\n\\r\\n'\n"
        ")\n"
        "sys.stdout.buffer.flush()\n"
        f"{after_handshake}\n"
        "message = b''\n"
        "while True:\n"
        "    opcode, final, data = read_frame()\n"
        "    if opcode == 8:\n"
        "        send_frame(8, data)\n"
        "        break\n"
        "    if opcode == 1:\n"
        "        message = data\n"
        "    elif opcode == 0:\n"
        "        message += data\n"
        "    else:\n"
        "        continue\n"
        "    if not final:\n"
        "        continue\n"
        "    value = json.loads(message)\n"
        "    if value.get('method') == 'initialize':\n"
        f"        {initialize_action}\n"
        "    elif value.get('method') == 'initialized':\n"
        f"        {initialized_action}\n"
        + (
            "    elif value.get('method') == 'thread/list':\n"
            f"        {thread_list_action}\n"
            if thread_list_action is not None
            else ""
        )
        + "    message = b''\n"
    )


@contextlib.asynccontextmanager
async def _wire_app_server(handlers):
    """Run a minimal JSON-RPC peer over the public WebSocket boundary."""
    received: list[dict] = []

    async def handle(connection):
        async for frame in connection:
            message = json.loads(frame)
            received.append(message)
            method = message.get("method")
            if method == "initialize":
                reply: dict = {
                    "id": message["id"],
                    "result": {"userAgent": "fake-app-server/1.0"},
                }
            elif "id" not in message:
                continue
            else:
                handler = handlers.get(method)
                result = (
                    JsonRpcError(-32601, f"method not found: {method}")
                    if handler is None
                    else handler(message.get("params"))
                )
                if isinstance(result, JsonRpcError):
                    reply = {
                        "id": message["id"],
                        "error": {"code": result.code, "message": result.message},
                    }
                else:
                    reply = {"id": message["id"], "result": result}
            await connection.send(json.dumps(reply))

    server = await serve(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    endpoint = AppServerEndpoint(
        f"ws://127.0.0.1:{port}", WebSocketTarget(f"ws://127.0.0.1:{port}", None)
    )
    app_server = None
    try:
        app_server = await connect_app_server(endpoint)
        yield app_server, received
    finally:
        if app_server is not None:
            await app_server.close()
        server.close()
        await server.wait_closed()


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
    async def test_thread_loading_serializes_only_closed_isolation_overrides(self):
        responses = {
            "thread/start": lambda _params: {"thread": {"id": "t1"}},
            "thread/resume": lambda _params: {"thread": {"id": "t1", "status": "idle"}},
        }
        async with _wire_app_server(responses) as (app_server, received):
            await app_server.start_thread(
                StartConfig(isolation=IsolationOptions(no_goals=True, no_agents=True))
            )
            assert received[-1]["params"]["config"] == {
                "features.goals": False,
                "agents.enabled": False,
            }

            await app_server.resume_thread(
                "t1", isolation=IsolationOptions(no_goals=True, no_agents=True)
            )
            assert received[-1]["params"] == {
                "threadId": "t1",
                "config": {
                    "features.goals": False,
                    "agents.enabled": False,
                },
            }

    async def test_start_thread_serializes_upstream_sandbox_enum(self):
        async with _wire_app_server(
            {"thread/start": lambda _params: {"thread": {"id": "t1"}}}
        ) as (app_server, received):
            for policy, wire_value in (
                (None, "workspace-write"),
                (SandboxPolicy.readOnly, "read-only"),
                (SandboxPolicy.workspaceWrite, "workspace-write"),
                (SandboxPolicy.dangerFullAccess, "danger-full-access"),
            ):
                assert (
                    await app_server.start_thread(StartConfig(sandbox=policy))
                ).id == "t1"
                assert received[-1] == {
                    "method": "thread/start",
                    "id": received[-1]["id"],
                    "params": {"approvalPolicy": "never", "sandbox": wire_value},
                }

    async def test_start_thread_rejects_unsupported_sandbox_policy(self):
        async with _wire_app_server(
            {"thread/start": lambda _params: {"thread": {"id": "t1"}}}
        ) as (app_server, received):
            with pytest.raises(CodexCtlError) as excinfo:
                await app_server.start_thread(
                    StartConfig(sandbox="not-a-policy")  # type: ignore[arg-type]
                )

        assert excinfo.value.code == ErrorCode.USAGE_ERROR
        assert "unsupported sandbox policy 'not-a-policy'" in excinfo.value.message
        assert all(message.get("method") != "thread/start" for message in received)

    async def test_start_thread_serializes_approval_policy_and_reviewer(self):
        async with _wire_app_server(
            {"thread/start": lambda _params: {"thread": {"id": "t1"}}}
        ) as (app_server, received):
            await app_server.start_thread(StartConfig())
            assert received[-1]["params"] == {
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
            }

            await app_server.start_thread(
                StartConfig(
                    approval_policy=ApprovalPolicy.onRequest,
                    approvals_reviewer=ApprovalsReviewer.autoReview,
                )
            )
            assert received[-1]["params"] == {
                "approvalPolicy": "on-request",
                "approvalsReviewer": "auto_review",
                "sandbox": "workspace-write",
            }

    async def test_start_thread_serializes_each_approval_policy(self):
        async with _wire_app_server(
            {"thread/start": lambda _params: {"thread": {"id": "t1"}}}
        ) as (app_server, received):
            for policy, wire_value in (
                (ApprovalPolicy.untrusted, "untrusted"),
                (ApprovalPolicy.onRequest, "on-request"),
                (ApprovalPolicy.never, "never"),
            ):
                await app_server.start_thread(StartConfig(approval_policy=policy))
                assert received[-1]["params"]["approvalPolicy"] == wire_value

    async def test_resume_thread_serializes_approval_and_sandbox_overrides(self):
        async with _wire_app_server(
            {"thread/resume": lambda _params: {"thread": {"id": "t1"}}}
        ) as (app_server, received):
            await app_server.resume_thread(
                "t1",
                approval_policy=ApprovalPolicy.onRequest,
                approvals_reviewer=ApprovalsReviewer.autoReview,
                sandbox=SandboxPolicy.readOnly,
                isolation=IsolationOptions(no_goals=True, no_agents=True),
            )

        assert received[-1]["params"] == {
            "threadId": "t1",
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
            "sandbox": "read-only",
            "config": {
                "features.goals": False,
                "agents.enabled": False,
            },
        }

    async def test_operations_keep_wire_requests_inside_the_app_server(self):
        responses = {
            "thread/start": {"thread": {"id": "t1"}},
            "turn/start": {"turn": {"id": "u1"}},
            "thread/read": {"thread": {"id": "t1", "status": "idle"}},
            "thread/resume": {"thread": {"id": "t1", "status": "idle"}},
            "turn/steer": {"turnId": "u1"},
            "turn/interrupt": {},
            "thread/list": {"data": [], "nextCursor": "next"},
        }
        async with _wire_app_server(
            {
                method: lambda _params, value=value: value
                for method, value in responses.items()
            }
        ) as (app_server, received):
            assert (
                await app_server.start_thread(
                    StartConfig(
                        cwd="/tmp", model="o4-mini", sandbox=SandboxPolicy.readOnly
                    )
                )
            ).id == "t1"
            assert await app_server.start_turn("t1", "hello", effort="high") == "u1"
            assert (await app_server.read_thread("t1")).id == "t1"
            assert (await app_server.resume_thread("t1")).id == "t1"
            assert await app_server.steer_turn("t1", "more", "u1") == "u1"
            await app_server.interrupt_turn("t1", "u1")
            assert (
                await app_server.list_threads("c1", cwd="/tmp")
            ).next_cursor == "next"

        calls = [
            (message["method"], message.get("params"))
            for message in received
            if message.get("method") not in {"initialize", "initialized"}
        ]
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

    async def test_lifecycle_probe_checks_each_required_operation(self):
        responses = {
            "thread/start": {"thread": {"id": "probe"}},
            "thread/resume": {"thread": {"id": "probe", "status": "idle"}},
            "thread/read": {"thread": {"id": "probe", "status": "idle"}},
            "thread/list": {"data": []},
            "turn/start": {"turn": {"id": "probe"}},
            "turn/steer": {"turnId": "probe"},
            "turn/interrupt": {},
        }
        async with _wire_app_server(
            {
                method: lambda _params, value=value: value
                for method, value in responses.items()
            }
        ) as (app_server, received):
            assert await app_server.check_lifecycle_operations() == ()

        calls = [
            (message["method"], message.get("params"))
            for message in received
            if message.get("method") not in {"initialize", "initialized"}
        ]
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

    async def test_lifecycle_probe_reports_method_not_found(self):
        responses = {
            "thread/start": {"thread": {"id": "probe"}},
            "thread/resume": {"thread": {"id": "probe", "status": "idle"}},
            "thread/read": {"thread": {"id": "probe", "status": "idle"}},
            "thread/list": {"data": []},
            "turn/start": {"turn": {"id": "probe"}},
            "turn/steer": JsonRpcError(-32601, "method not found"),
            "turn/interrupt": {},
        }
        async with _wire_app_server(
            {
                method: lambda _params, value=value: value
                for method, value in responses.items()
            }
        ) as (app_server, _received):
            assert await app_server.check_lifecycle_operations() == ("steer turn",)


class TestStrictFraming:
    async def test_stdio_framing_skips_blank_lines_and_accepts_crlf_and_eof(
        self, stdio_endpoint
    ):
        endpoint = stdio_endpoint(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message.get('method') == 'initialize':\n"
            "        sys.stdout.write('\\n  \\r\\n')\n"
            "        sys.stdout.write(json.dumps({'id': message['id'], 'result': "
            "{'userAgent': 'stdio/1.0'}}) + '\\r\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif message.get('method') == 'thread/list':\n"
            "        sys.stdout.write(json.dumps({'id': message['id'], 'result': {'data': []}}))\n"
            "        sys.stdout.flush()\n"
            "        raise SystemExit(0)\n",
            filename="stdio-framing.py",
        )

        app_server = await connect_app_server(endpoint)
        try:
            assert (await app_server.list_threads()).threads == []
        finally:
            await app_server.close()

    async def test_runtime_failure_and_concurrent_close_finish_cleanup(
        self, tmp_path, stdio_endpoint
    ):
        cleanup_started = tmp_path / "runtime-failure.cleanup-started"
        release_cleanup = tmp_path / "runtime-failure.release-cleanup"
        terminated = tmp_path / "runtime-failure.terminated"
        endpoint = stdio_endpoint(
            "import json, pathlib, signal, sys, time\n"
            "cleanup_started = pathlib.Path(sys.argv[1])\n"
            "release_cleanup = pathlib.Path(sys.argv[2])\n"
            "terminated = pathlib.Path(sys.argv[3])\n"
            "def stop(signum, frame):\n"
            "    cleanup_started.write_text('started')\n"
            "    while not release_cleanup.exists():\n"
            "        time.sleep(0.001)\n"
            "    terminated.write_text('terminated')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "requests = 0\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message.get('method') == 'initialize':\n"
            "        print(json.dumps({'id': message['id'], 'result': "
            "{'userAgent': 'stdio/1.0'}}), flush=True)\n"
            "    elif message.get('method') == 'thread/list':\n"
            "        requests += 1\n"
            "        if requests == 2:\n"
            "            print('{malformed', flush=True)\n"
            "            time.sleep(60)\n",
            str(cleanup_started),
            str(release_cleanup),
            str(terminated),
            filename="stdio-runtime-failure.py",
        )

        app_server = await connect_app_server(endpoint)
        close_tasks: list[asyncio.Task[None]] = []
        try:
            results = await asyncio.gather(
                app_server.list_threads(),
                app_server.list_threads(),
                return_exceptions=True,
            )
            assert len(results) == 2
            assert all(isinstance(result, CodexCtlError) for result in results)
            assert all(
                result.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
                for result in results
                if isinstance(result, CodexCtlError)
            )

            # Public callers must join cleanup. Whether that is one task or
            # several is deliberately not an observable transport contract.
            close_tasks = [asyncio.create_task(app_server.close()) for _ in range(3)]
            for _ in range(300):
                if cleanup_started.exists():
                    break
                await asyncio.sleep(0.01)
            assert cleanup_started.read_text(encoding="utf-8") == "started"
            assert all(not task.done() for task in close_tasks)

            release_cleanup.write_text("release", encoding="utf-8")
            await asyncio.gather(*close_tasks)
        finally:
            release_cleanup.touch()
            await app_server.close()

        for _ in range(300):
            if terminated.exists():
                break
            await asyncio.sleep(0.01)
        assert terminated.read_text(encoding="utf-8") == "terminated"

    async def test_close_is_idempotent_and_cancellation_waits_for_cleanup(
        self, tmp_path, stdio_endpoint
    ):
        cleanup_started = tmp_path / "close-cancelled.cleanup-started"
        release_cleanup = tmp_path / "close-cancelled.release-cleanup"
        terminated = tmp_path / "close-cancelled.terminated"
        endpoint = stdio_endpoint(
            "import json, pathlib, signal, sys, time\n"
            "cleanup_started = pathlib.Path(sys.argv[1])\n"
            "release_cleanup = pathlib.Path(sys.argv[2])\n"
            "terminated = pathlib.Path(sys.argv[3])\n"
            "def stop(signum, frame):\n"
            "    cleanup_started.write_text('started')\n"
            "    while not release_cleanup.exists():\n"
            "        time.sleep(0.001)\n"
            "    terminated.write_text('terminated')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message.get('method') == 'initialize':\n"
            "        print(json.dumps({'id': message['id'], 'result': "
            "{'userAgent': 'stdio/1.0'}}), flush=True)\n"
            "    elif message.get('method') == 'initialized':\n"
            "        time.sleep(60)\n",
            str(cleanup_started),
            str(release_cleanup),
            str(terminated),
            filename="stdio-close-cancelled.py",
        )

        app_server = await connect_app_server(endpoint)
        close_task = asyncio.create_task(app_server.close())
        try:
            for _ in range(300):
                if cleanup_started.exists():
                    break
                await asyncio.sleep(0.01)
            assert cleanup_started.read_text(encoding="utf-8") == "started"

            close_task.cancel()
            await asyncio.sleep(0)
            assert not close_task.done()
            close_task.cancel()
            await asyncio.sleep(0)
            assert not close_task.done()

            release_cleanup.write_text("release", encoding="utf-8")
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(close_task, timeout=3.0)
        finally:
            release_cleanup.touch()
            await app_server.close()

        for _ in range(300):
            if terminated.exists():
                break
            await asyncio.sleep(0.01)
        assert terminated.read_text(encoding="utf-8") == "terminated"

    async def test_stdio_connects_with_ndjson_and_inherits_process_setup(
        self, stdio_endpoint
    ):
        endpoint = stdio_endpoint(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message.get('method') == 'initialize':\n"
            "        sys.stdout.write(json.dumps({'id': message['id'], 'result': "
            "{'userAgent': 'stdio/1.0'}}) + '\\r\\n')\n"
            "        sys.stdout.flush()\n",
            filename="stdio-initialize.py",
        )

        app_server = await connect_app_server(endpoint)
        try:
            assert app_server.app_server_version == "1.0"
        finally:
            await app_server.close()

    async def test_stdio_websocket_proxy_upgrades_and_routes_messages(
        self, tmp_path, stdio_endpoint
    ):
        pong = tmp_path / "pong"
        endpoint = stdio_endpoint(
            """
import base64, hashlib, json, pathlib, struct, sys

pong = pathlib.Path(sys.argv[-1])

def read_exact(size):
    value = b""
    while len(value) < size:
        chunk = sys.stdin.buffer.read(size - len(value))
        if not chunk:
            raise EOFError
        value += chunk
    return value

def read_frame():
    first, second = read_exact(2)
    length = second & 127
    if length == 126:
        length = struct.unpack("!H", read_exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(8))[0]
    mask = read_exact(4) if second & 128 else None
    data = read_exact(length)
    if mask:
        data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
    return first & 15, bool(first & 128), data

def send_frame(opcode, data, final=True):
    length = len(data)
    header = bytes([(128 if final else 0) | opcode])
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    sys.stdout.buffer.write(header + data)
    sys.stdout.buffer.flush()

def send_text(data):
    payload = data.encode()
    split = max(1, len(payload) // 2)
    send_frame(1, payload[:split], False)
    send_frame(0, payload[split:])

request = b""
while not request.endswith(b"\\r\\n\\r\\n"):
    request += read_exact(1)
headers = {
    line.split(b":", 1)[0].lower(): line.split(b":", 1)[1].strip()
    for line in request.split(b"\\r\\n")[1:]
    if b":" in line
}
assert request.startswith(b"GET / HTTP/1.1\\r\\n")
assert b"sec-websocket-extensions" not in headers
accept = base64.b64encode(hashlib.sha1(
    headers[b"sec-websocket-key"] + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
).digest())
sys.stdout.buffer.write(
    b"HTTP/1.1 101 Switching Protocols\\r\\n"
    b"Upgrade: websocket\\r\\n"
    b"Connection: Upgrade\\r\\n"
    b"Sec-WebSocket-Accept: " + accept + b"\\r\\n\\r\\n"
)
sys.stdout.buffer.flush()

message = b""
message_opcode = None
while True:
    opcode, final, data = read_frame()
    if opcode == 10:
        if data == b"ping-check":
            pong.write_text("seen", encoding="utf-8")
        continue
    if opcode == 8:
        send_frame(8, data)
        break
    if opcode == 1:
        message_opcode = opcode
        message = data
    elif opcode == 0:
        message += data
    else:
        continue
    if not final:
        continue
    value = json.loads(message)
    if value.get("method") == "initialize":
        send_text(json.dumps({"id": value["id"], "result": {
            "userAgent": "proxy/2.0"
        }}))
        send_frame(9, b"ping-check")
    elif value.get("method") == "thread/list":
        send_text(json.dumps({"id": value["id"], "result": {"data": []}}))
    message = b""
""",
            str(pong),
            filename="stdio-websocket-proxy.py",
            framing=StdioFraming.WEBSOCKET,
        )

        app_server = await connect_app_server(endpoint)
        try:
            assert app_server.app_server_version == "2.0"
            assert (await app_server.list_threads()).threads == []
        finally:
            await app_server.close()

        assert pong.read_text(encoding="utf-8") == "seen"

    async def test_ssh_runtime_connects_websocket_proxy_and_isolates_stderr(
        self, tmp_path, capfd
    ):
        proxy = tmp_path / "ssh-proxy.py"
        proxy.write_text(
            _stdio_websocket_proxy_source(
                after_handshake=(
                    "print('remote proxy diagnostic', file=sys.stderr, flush=True)"
                ),
                thread_list_action=(
                    "send_text(json.dumps({'id': value['id'], 'result': {'data': []}}))"
                ),
            ),
            encoding="utf-8",
        )
        ssh = tmp_path / "fake-ssh.py"
        ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "print('ssh diagnostic', file=sys.stderr, flush=True)\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, {str(proxy)!r}])\n",
            encoding="utf-8",
        )
        ssh.chmod(0o755)

        provider = SshRuntimeProvider(
            "opaque destination",
            remote_socket="/run/codex.sock",
            ssh_bin=str(ssh),
        )
        endpoint = await provider.resolve_endpoint()
        app_server = await connect_app_server(endpoint)
        try:
            assert (await app_server.list_threads()).threads == []
        finally:
            await app_server.close()

        captured = capfd.readouterr()
        assert "ssh diagnostic" in captured.err
        assert "remote proxy diagnostic" in captured.err
        assert "diagnostic" not in captured.out

    async def test_ssh_runtime_cancellation_cleans_up_process_group(self, tmp_path):
        ready = tmp_path / "ssh-websocket.ready"
        terminated = tmp_path / "ssh-websocket.terminated"
        proxy = tmp_path / "ssh-proxy-cancel.py"
        proxy.write_text(
            _stdio_websocket_proxy_source(
                initialize_action="pass",
                after_handshake=(
                    "pathlib.Path(sys.argv[-2]).write_text('ready')\ntime.sleep(60)"
                ),
                termination_marker=True,
            ),
            encoding="utf-8",
        )
        ssh = tmp_path / "fake-ssh-cancel.py"
        ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, {str(proxy)!r}, "
            f"{str(ready)!r}, {str(terminated)!r}])\n",
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        provider = SshRuntimeProvider(
            "devbox", remote_socket="/run/codex.sock", ssh_bin=str(ssh)
        )
        endpoint = await provider.resolve_endpoint()
        task = asyncio.create_task(connect_app_server(endpoint, timeout=5.0))
        for _ in range(300):
            if ready.exists():
                break
            await asyncio.sleep(0.01)
        assert ready.read_text(encoding="utf-8") == "ready"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(100):
            if terminated.exists():
                break
            await asyncio.sleep(0.01)
        assert terminated.read_text(encoding="utf-8") == "terminated"

    async def test_stdio_websocket_malformed_upgrade_is_unavailable(
        self, stdio_endpoint
    ):
        endpoint = stdio_endpoint(
            "import sys\n"
            "sys.stdin.buffer.read(1)\n"
            "sys.stdout.buffer.write(b'not an HTTP response')\n"
            "sys.stdout.buffer.flush()\n",
            filename="stdio-websocket-bad-upgrade.py",
            framing=StdioFraming.WEBSOCKET,
        )

        with pytest.raises(CodexCtlError) as excinfo:
            await connect_app_server(endpoint, timeout=0.5)

        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    @pytest.mark.parametrize(
        ("opcode", "payload"),
        [(2, b"{}"), (1, b"{malformed"), (1, b"\xff")],
    )
    async def test_stdio_websocket_invalid_messages_are_protocol_errors(
        self, stdio_endpoint, opcode, payload
    ):
        endpoint = stdio_endpoint(
            _stdio_websocket_proxy_source(
                initialized_action=f"send_frame({opcode}, {payload!r})"
            ),
            filename="stdio-websocket-invalid-message.py",
            framing=StdioFraming.WEBSOCKET,
        )

        app_server = await connect_app_server(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await app_server.list_threads()
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        finally:
            await app_server.close()

    async def test_stdio_websocket_eof_is_runtime_protocol_error(self, stdio_endpoint):
        endpoint = stdio_endpoint(
            _stdio_websocket_proxy_source(initialized_action="raise SystemExit(0)"),
            filename="stdio-websocket-eof.py",
            framing=StdioFraming.WEBSOCKET,
        )

        app_server = await connect_app_server(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await app_server.list_threads()
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        finally:
            await app_server.close()

    async def test_stdio_websocket_runtime_failure_cleans_up_descendants(
        self, tmp_path, stdio_endpoint
    ):
        descendant_ready = tmp_path / "websocket-runtime-descendant.ready"
        descendant_terminated = tmp_path / "websocket-runtime-descendant.terminated"
        descendant_pid = tmp_path / "websocket-runtime-descendant.pid"
        descendant_source = (
            "import os, pathlib, signal, sys, time\n"
            "ready = pathlib.Path(sys.argv[1])\n"
            "terminated = pathlib.Path(sys.argv[2])\n"
            "pid_path = pathlib.Path(sys.argv[3])\n"
            "def stop(signum, frame):\n"
            "    terminated.write_text('terminated')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "pid_path.write_text(str(os.getpid()))\n"
            "ready.write_text('ready')\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        endpoint = stdio_endpoint(
            _stdio_websocket_proxy_source(
                after_handshake=(
                    "subprocess.Popen([sys.executable, '-c', "
                    f"{descendant_source!r}, sys.argv[-4], sys.argv[-3], sys.argv[-2]])\n"
                    "while not pathlib.Path(sys.argv[-4]).exists():\n"
                    "    time.sleep(0.001)"
                ),
                thread_list_action="send_frame(1, b'{malformed}')",
                termination_marker=True,
            ),
            str(descendant_ready),
            str(descendant_terminated),
            str(descendant_pid),
            str(tmp_path / "websocket-runtime-parent.terminated"),
            filename="stdio-websocket-runtime-descendant.py",
            framing=StdioFraming.WEBSOCKET,
        )
        app_server = await connect_app_server(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await app_server.list_threads()
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
            pid = int(descendant_pid.read_text(encoding="utf-8"))
            for _ in range(300):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("WebSocket stdio descendant survived process-group cleanup")
        finally:
            await app_server.close()

    async def test_stdio_websocket_close_cancellation_cleans_up_established_process_group(
        self, tmp_path, stdio_endpoint
    ):
        descendant_ready = tmp_path / "websocket-cancel-descendant.ready"
        descendant_terminated = tmp_path / "websocket-cancel-descendant.terminated"
        descendant_pid = tmp_path / "websocket-cancel-descendant.pid"
        cleanup_started = tmp_path / "websocket-cancel.cleanup-started"
        release_cleanup = tmp_path / "websocket-cancel.release-cleanup"
        parent_terminated = tmp_path / "websocket-cancel-parent.terminated"
        descendant_source = (
            "import os, pathlib, signal, sys, time\n"
            "ready = pathlib.Path(sys.argv[1])\n"
            "terminated = pathlib.Path(sys.argv[2])\n"
            "pid_path = pathlib.Path(sys.argv[3])\n"
            "def stop(signum, frame):\n"
            "    terminated.write_text('terminated')\n"
            "pid_path.write_text(str(os.getpid()))\n"
            "ready.write_text('ready')\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        endpoint = stdio_endpoint(
            _stdio_websocket_proxy_source(
                after_handshake=(
                    "subprocess.Popen([sys.executable, '-c', "
                    f"{descendant_source!r}, sys.argv[-6], sys.argv[-5], sys.argv[-4]])\n"
                    "while not pathlib.Path(sys.argv[-6]).exists():\n"
                    "    time.sleep(0.001)"
                ),
                initialized_action="time.sleep(60)",
                termination_marker=True,
                termination_action=(
                    "pathlib.Path(sys.argv[-3]).write_text('started')\n"
                    "while not pathlib.Path(sys.argv[-2]).exists():\n"
                    "    time.sleep(0.001)\n"
                    "raise SystemExit(0)"
                ),
            ),
            str(descendant_ready),
            str(descendant_terminated),
            str(descendant_pid),
            str(cleanup_started),
            str(release_cleanup),
            str(parent_terminated),
            filename="stdio-websocket-cancel-established.py",
            framing=StdioFraming.WEBSOCKET,
        )

        app_server = await connect_app_server(endpoint)
        close_task = asyncio.create_task(app_server.close())
        try:
            for _ in range(300):
                if cleanup_started.exists():
                    break
                await asyncio.sleep(0.01)
            assert cleanup_started.read_text(encoding="utf-8") == "started"

            close_task.cancel()
            await asyncio.sleep(0)
            assert not close_task.done()
            close_task.cancel()
            await asyncio.sleep(0)
            assert not close_task.done()

            release_cleanup.write_text("release", encoding="utf-8")
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(close_task, timeout=3.0)

            pid = int(descendant_pid.read_text(encoding="utf-8"))
            for _ in range(300):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("WebSocket stdio descendant survived process-group cleanup")
        finally:
            release_cleanup.touch()
            if not close_task.done():
                await app_server.close()

    async def test_stdio_websocket_framing_failure_is_protocol_error(
        self, stdio_endpoint
    ):
        endpoint = stdio_endpoint(
            _stdio_websocket_proxy_source(
                initialized_action=(
                    "sys.stdout.buffer.write(b'\\xc1\\x00'); sys.stdout.buffer.flush()"
                )
            ),
            filename="stdio-websocket-framing-failure.py",
            framing=StdioFraming.WEBSOCKET,
        )

        app_server = await connect_app_server(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await asyncio.wait_for(app_server.list_threads(), timeout=0.5)
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        finally:
            await app_server.close()

    async def test_stdio_websocket_startup_timeout_closes_child(
        self, tmp_path, stdio_endpoint
    ):
        terminated = tmp_path / "websocket-timeout.terminated"
        endpoint = stdio_endpoint(
            _stdio_websocket_proxy_source(
                initialize_action="pass",
                after_handshake="time.sleep(60)",
                termination_marker=True,
            ),
            str(terminated),
            filename="stdio-websocket-timeout.py",
            framing=StdioFraming.WEBSOCKET,
        )

        with pytest.raises(CodexCtlError) as excinfo:
            await connect_app_server(endpoint, timeout=0.5)

        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE
        for _ in range(300):
            if terminated.exists():
                break
            await asyncio.sleep(0.01)
        assert terminated.read_text(encoding="utf-8") == "terminated"

    async def test_stdio_websocket_cancellation_cleans_up_child(
        self, tmp_path, stdio_endpoint
    ):
        ready = tmp_path / "websocket-cancel.ready"
        terminated = tmp_path / "websocket-cancel.terminated"
        endpoint = stdio_endpoint(
            _stdio_websocket_proxy_source(
                initialize_action="pass",
                after_handshake=(
                    "pathlib.Path(sys.argv[-2]).write_text('ready')\ntime.sleep(60)"
                ),
                termination_marker=True,
            ),
            str(ready),
            str(terminated),
            filename="stdio-websocket-cancel.py",
            framing=StdioFraming.WEBSOCKET,
        )
        task = asyncio.create_task(connect_app_server(endpoint, timeout=5.0))
        for _ in range(300):
            if ready.exists():
                break
            await asyncio.sleep(0.01)
        assert ready.read_text(encoding="utf-8") == "ready"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(100):
            if terminated.exists():
                break
            await asyncio.sleep(0.01)
        assert terminated.read_text(encoding="utf-8") == "terminated"

    async def test_stdio_websocket_cleanup_does_not_wait_for_close_handshake(
        self, tmp_path, stdio_endpoint
    ):
        terminated = tmp_path / "websocket-cleanup.terminated"
        endpoint = stdio_endpoint(
            _stdio_websocket_proxy_source(
                initialized_action="time.sleep(60)",
                termination_marker=True,
            ),
            str(tmp_path / "unused"),
            str(terminated),
            filename="stdio-websocket-cleanup.py",
            framing=StdioFraming.WEBSOCKET,
        )
        app_server = await connect_app_server(endpoint)
        await asyncio.wait_for(app_server.close(), timeout=3.0)

        for _ in range(300):
            if terminated.exists():
                break
            await asyncio.sleep(0.01)
        assert terminated.read_text(encoding="utf-8") == "terminated"

    async def test_stdio_preinitialize_exit_is_unavailable(self, stdio_endpoint):
        endpoint = stdio_endpoint("raise SystemExit(0)\n", filename="stdio-exit.py")

        with pytest.raises(CodexCtlError) as excinfo:
            await connect_app_server(endpoint)
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_stdio_executable_failure_is_unavailable(self, tmp_path):
        endpoint = AppServerEndpoint(
            "stdio", StdioTarget((str(tmp_path / "missing-app-server"),))
        )

        with pytest.raises(CodexCtlError) as excinfo:
            await connect_app_server(endpoint)
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    @pytest.mark.parametrize("payload", ["{malformed", "[]", '{"value": NaN}'])
    async def test_stdio_invalid_frame_is_protocol_error(self, stdio_endpoint, payload):
        endpoint = stdio_endpoint(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message.get('method') == 'initialize':\n"
            "        sys.stdout.write('\\n')\n"
            "        sys.stdout.write(json.dumps({'id': message['id'], 'result': "
            "{'userAgent': 'stdio/1.0'}}) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif message.get('method') == 'initialized':\n"
            f"        sys.stdout.write({payload!r} + '\\n')\n"
            "        sys.stdout.flush()\n",
            filename="stdio-invalid.py",
        )

        app_server = await connect_app_server(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await app_server.start_thread(StartConfig())
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        finally:
            await app_server.close()

    async def test_stdio_ignores_valid_unknown_json_rpc_messages(self, stdio_endpoint):
        endpoint = stdio_endpoint(
            "import json, sys\n"
            "def send(value):\n"
            "    print(json.dumps(value), flush=True)\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message.get('method') == 'initialize':\n"
            "        send({'id': message['id'], 'result': {'userAgent': 'stdio/1.0'}})\n"
            "    elif message.get('method') == 'initialized':\n"
            "        send({'jsonrpc': '2.0', 'method': 'future/notification', 'params': {}})\n"
            "        send({'jsonrpc': '2.0', 'futureField': True})\n"
            "    elif message.get('method') == 'thread/list':\n"
            "        send({'id': message['id'], 'result': {'data': []}})\n",
            filename="stdio-unknown.py",
        )

        app_server = await connect_app_server(endpoint)
        try:
            assert (await app_server.list_threads()).threads == []
            assert app_server.interaction_count == 0
        finally:
            await app_server.close()

    async def test_stdio_routes_operations_notifications_and_interactions(
        self, tmp_path, stdio_endpoint, monkeypatch, capfd
    ):
        record = tmp_path / "record.jsonl"
        monkeypatch.setenv("CODEXCTL_TEST_ENV", "inherited")
        shell_marker = tmp_path / "shell-marker"
        child_args = (
            "--child-flag",
            "value with spaces",
            f"$(touch {shell_marker})",
            str(record),
        )
        endpoint = stdio_endpoint(
            "import json, os, pathlib, sys\n"
            "record_path = pathlib.Path(sys.argv[-1])\n"
            "def record(value):\n"
            "    with record_path.open('a', encoding='utf-8') as stream:\n"
            "        stream.write(json.dumps(value) + '\\n')\n"
            "def send(value, ending='\\n'):\n"
            "    sys.stdout.write(json.dumps(value) + ending)\n"
            "    sys.stdout.flush()\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    method = message.get('method')\n"
            "    if method == 'initialize':\n"
            "        record({'argv': sys.argv[1:-1], 'cwd': os.getcwd(), "
            "'env': os.environ.get('CODEXCTL_TEST_ENV')})\n"
            "        print('stdio child diagnostic', file=sys.stderr, flush=True)\n"
            "        sys.stdout.write('\\n  \\r\\n')\n"
            "        send({'id': message['id'], 'result': {'userAgent': 'stdio/1.0'}}, '\\r\\n')\n"
            "    elif method == 'initialized':\n"
            "        send({'method': 'item/started', 'params': {'threadId': 't1', 'turnId': 'u1', "
            "'item': {'type': 'agentMessage', 'id': 'i0', 'text': 'hello'}}})\n"
            "        send({'id': 700, 'method': 'item/commandExecution/requestApproval', "
            "'params': {'threadId': 't1', 'turnId': 'u1'}})\n"
            "    elif method == 'thread/start':\n"
            "        send({'id': message['id'], 'result': {'thread': {'id': 't1'}}})\n"
            "        send({'method': 'thread/started', 'params': {'thread': {'id': 't1'}}})\n"
            "    elif method == 'turn/start':\n"
            "        send({'id': message['id'], 'result': {'turn': {'id': 'u1'}}})\n"
            "        send({'method': 'turn/started', 'params': {'threadId': 't1', 'turn': {'id': 'u1'}}})\n"
            "        send({'method': 'item/completed', 'params': {'threadId': 't1', 'turnId': 'u1', "
            "'item': {'type': 'agentMessage', 'id': 'i1', 'text': 'done'}}})\n"
            "        send({'method': 'turn/completed', 'params': {'threadId': 't1', "
            "'turn': {'id': 'u1', 'status': 'completed'}}})\n"
            "    elif method == 'thread/list':\n"
            "        send({'id': message['id'], 'result': {'data': []}}, '')\n"
            "        raise SystemExit(0)\n"
            "    elif method == 'thread/unsubscribe':\n"
            "        send({'id': message['id'], 'result': {}})\n"
            "    elif message.get('id') == 700:\n"
            "        record({'approval': message})\n",
            *child_args,
            filename="stdio-routes.py",
        )

        app_server = await connect_app_server(endpoint)
        notifications = app_server.notifications()
        try:
            thread = await app_server.start_thread(StartConfig())
            assert thread is not None and thread.id == "t1"
            assert await app_server.start_turn("t1", "hello") == "u1"
            assert (await app_server.list_threads()).threads == []

            events = [
                await asyncio.wait_for(anext(notifications), timeout=1.0)
                for _ in range(6)
            ]
        finally:
            await app_server.close()

        assert [event.type for event in events] == [
            "item/started",
            "error",
            "thread/started",
            "turn/started",
            "item/completed",
            "turn/completed",
        ]
        assert (
            events[1].extra["error"]["code"] == ErrorCode.UNSUPPORTED_INTERACTION.value
        )
        records = [json.loads(line) for line in record.read_text().splitlines()]
        assert records[0] == {
            "argv": list(child_args[:-1]),
            "cwd": str(Path.cwd()),
            "env": "inherited",
        }
        assert records[1]["approval"]["result"] == {"decision": "decline"}
        assert "stdio child diagnostic" in capfd.readouterr().err
        assert not shell_marker.exists()

    async def test_stdio_runtime_exit_after_initialize_is_protocol_error(
        self, stdio_endpoint
    ):
        endpoint = stdio_endpoint(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message.get('method') == 'initialize':\n"
            "        print(json.dumps({'id': message['id'], 'result': "
            "{'userAgent': 'stdio/1.0'}}), flush=True)\n"
            "    elif message.get('method') == 'initialized':\n"
            "        raise SystemExit(0)\n",
            filename="stdio-runtime-exit.py",
        )

        app_server = await connect_app_server(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await asyncio.wait_for(app_server.list_threads(), timeout=1.0)
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        finally:
            await app_server.close()

    async def test_stdio_startup_timeout_cleans_up_child(
        self, tmp_path, stdio_endpoint
    ):
        terminated = tmp_path / "stdio-startup-timeout.terminated"
        endpoint = stdio_endpoint(
            "import pathlib, signal, sys, time\n"
            "terminated = pathlib.Path(sys.argv[1])\n"
            "def stop(signum, frame):\n"
            "    terminated.write_text('terminated')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "time.sleep(60)\n",
            str(terminated),
            filename="stdio-hang.py",
        )

        with pytest.raises(CodexCtlError) as excinfo:
            await connect_app_server(endpoint, timeout=0.05)
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE
        for _ in range(300):
            if terminated.exists():
                break
            await asyncio.sleep(0.01)
        assert terminated.read_text(encoding="utf-8") == "terminated"

    async def test_stdio_cleanup_waits_for_process_exit(self, tmp_path, stdio_endpoint):
        terminated = tmp_path / "stdio-close.terminated"
        endpoint = stdio_endpoint(
            "import json, pathlib, signal, sys, time\n"
            "terminated = pathlib.Path(sys.argv[1])\n"
            "def stop(signum, frame):\n"
            "    terminated.write_text('terminated')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message.get('method') == 'initialize':\n"
            "        print(json.dumps({'id': message['id'], 'result': "
            "{'userAgent': 'stdio/1.0'}}), flush=True)\n"
            "        time.sleep(60)\n",
            str(terminated),
            filename="stdio-running.py",
        )

        app_server = await connect_app_server(endpoint)
        await app_server.close()

        assert terminated.read_text(encoding="utf-8") == "terminated"

    async def test_stdio_cleanup_terminates_descendant_process_group(
        self, tmp_path, stdio_endpoint
    ):
        marker = tmp_path / "descendant-terminated"
        endpoint = stdio_endpoint(
            "import json, pathlib, subprocess, sys, time\n"
            "marker = pathlib.Path(sys.argv[1])\n"
            "ready = marker.with_suffix('.ready')\n"
            "descendant = (\n"
            "    'import pathlib, signal, sys, time\\n'\n"
            "    'marker = pathlib.Path(sys.argv[1])\\n'\n"
            "    'def stop(signum, frame):\\n'\n"
            "    '    marker.write_text(\\\"terminated\\\")\\n'\n"
            "    '    raise SystemExit(0)\\n'\n"
            "    'signal.signal(signal.SIGTERM, stop)\\n'\n"
            '    \'marker.with_suffix(\\".ready\\").write_text(\\"ready\\")\\n\'\n'
            "    'while True:\\n'\n"
            "    '    time.sleep(1)\\n'\n"
            ")\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message.get('method') == 'initialize':\n"
            "        subprocess.Popen([sys.executable, '-c', descendant, str(marker)])\n"
            "        while not ready.exists():\n"
            "            time.sleep(0.001)\n"
            "        print(json.dumps({'id': message['id'], 'result': "
            "{'userAgent': 'stdio/1.0'}}), flush=True)\n"
            "        time.sleep(60)\n",
            str(marker),
            filename="stdio-descendant.py",
        )
        app_server = await connect_app_server(endpoint)
        await app_server.close()

        for _ in range(300):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.read_text(encoding="utf-8") == "terminated"

    async def test_stdio_connect_cancellation_does_not_wait_for_stalled_launch(
        self, monkeypatch
    ):
        started = asyncio.Event()
        release = asyncio.Event()

        async def stalled_subprocess_exec(*_args, **_kwargs):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            raise OSError("stalled subprocess launch")

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "subprocess_exec", stalled_subprocess_exec)
        endpoint = AppServerEndpoint("stdio", StdioTarget(("app-server",)))
        task = asyncio.create_task(connect_app_server(endpoint))
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)

        release.set()
        await asyncio.sleep(0)

    async def test_stdio_connect_cancellation_reaps_child_created_during_launch(
        self, tmp_path, monkeypatch
    ):
        ready = tmp_path / "real-child.ready"
        terminated = tmp_path / "real-child.terminated"
        source = (
            "import os, pathlib, signal, sys, time\n"
            "ready = pathlib.Path(sys.argv[1])\n"
            "terminated = pathlib.Path(sys.argv[2])\n"
            "def stop(signum, frame):\n"
            "    terminated.write_text('terminated')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "ready.write_text(str(os.getpid()))\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        loop = asyncio.get_running_loop()
        original_subprocess_exec = loop.subprocess_exec
        subprocess_created = asyncio.Event()
        release = asyncio.Event()

        async def delayed_subprocess_exec(protocol_factory, *args, **kwargs):
            raw_transport, protocol = await original_subprocess_exec(
                protocol_factory, *args, **kwargs
            )
            subprocess_created.set()
            await release.wait()
            return raw_transport, protocol

        monkeypatch.setattr(loop, "subprocess_exec", delayed_subprocess_exec)
        endpoint = AppServerEndpoint(
            "stdio",
            StdioTarget((sys.executable, "-c", source, str(ready), str(terminated))),
        )
        task = asyncio.create_task(connect_app_server(endpoint))
        pid: int | None = None
        try:
            await subprocess_created.wait()
            for _ in range(300):
                if ready.exists():
                    break
                await asyncio.sleep(0.01)
            assert ready.exists()
            pid = int(ready.read_text(encoding="utf-8"))

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            for _ in range(300):
                if terminated.exists():
                    break
                await asyncio.sleep(0.01)
            assert terminated.read_text(encoding="utf-8") == "terminated"
            for _ in range(200):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("cancelled stdio child remained in the process table")
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            if pid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)

    async def test_stdio_connect_cancellation_closes_child_during_initialize(
        self, tmp_path, stdio_endpoint
    ):
        ready = tmp_path / "initialize-cancel.ready"
        terminated = tmp_path / "initialize-cancel.terminated"
        endpoint = stdio_endpoint(
            "import json, pathlib, signal, sys, time\n"
            "ready = pathlib.Path(sys.argv[1])\n"
            "terminated = pathlib.Path(sys.argv[2])\n"
            "def stop(signum, frame):\n"
            "    terminated.write_text('terminated')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message.get('method') == 'initialize':\n"
            "        ready.write_text('ready')\n"
            "        time.sleep(60)\n",
            str(ready),
            str(terminated),
            filename="stdio-initialize-cancel.py",
        )
        task = asyncio.create_task(connect_app_server(endpoint))
        for _ in range(300):
            if ready.exists():
                break
            await asyncio.sleep(0.01)
        assert ready.read_text(encoding="utf-8") == "ready"
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(300):
            if terminated.exists():
                break
            await asyncio.sleep(0.01)
        assert terminated.read_text(encoding="utf-8") == "terminated"


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
            # complete the close handshake when the app_server is cleaned up.
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
        app_server = None
        try:
            app_server = await connect_app_server(
                AppServerEndpoint(str(socket_path), UnixSocketTarget(socket_path))
            )
        finally:
            if app_server is not None:
                await app_server.close()
            server.close()
            await server.wait_closed()

        assert app_server.user_agent == "codex-app-server/0.147.0"

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
        app_server = None
        try:
            app_server = await connect_app_server(
                AppServerEndpoint(str(socket_path), UnixSocketTarget(socket_path))
            )
            await asyncio.wait_for(initialized.wait(), timeout=1.0)
        finally:
            if app_server is not None:
                await app_server.close()
            server.close()
            await server.wait_closed()

        assert app_server.user_agent == "codex-app-server/0.101.0"
        assert app_server.server_codex_home == str(tmp_path / "codex-home")
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
                    await connection.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "future/notification",
                                "params": {},
                            }
                        )
                    )
                    await connection.send(
                        json.dumps({"jsonrpc": "2.0", "futureField": True})
                    )
                elif message.get("method") == "thread/list":
                    await connection.send(
                        json.dumps({"id": message["id"], "result": {"data": []}})
                    )

        server = await serve(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        app_server = None
        try:
            app_server = await connect_app_server(
                AppServerEndpoint(
                    f"ws://127.0.0.1:{port}/app-server?client=test",
                    WebSocketTarget(
                        f"ws://127.0.0.1:{port}/app-server?client=test", None
                    ),
                )
            )
            await asyncio.wait_for(initialized.wait(), timeout=1.0)
            assert (await app_server.list_threads()).threads == []
        finally:
            if app_server is not None:
                await app_server.close()
            server.close()
            await server.wait_closed()

        assert app_server.user_agent == "codex-app-server/0.101.0"
        assert [message["method"] for message in received] == [
            "initialize",
            "initialized",
            "thread/list",
        ]
        assert requests[0][0] == "/app-server?client=test"
        assert "authorization" not in requests[0][1]
        assert "sec-websocket-extensions" not in requests[0][1]

    @pytest.mark.parametrize(
        "payload",
        [
            b'{"binary": true}',
            "{malformed",
            "[]",
            '{"value": NaN}',
            '{"value": Infinity}',
            '{"value": -Infinity}',
        ],
    )
    async def test_tcp_rejects_binary_malformed_non_object_and_nonstandard_json(
        self, payload
    ):
        async def handle(connection):
            async for frame in connection:
                message = json.loads(frame)
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
                    await connection.send(payload)

        server = await serve(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        endpoint = AppServerEndpoint(
            f"ws://127.0.0.1:{port}",
            WebSocketTarget(f"ws://127.0.0.1:{port}", None),
        )
        app_server = await connect_app_server(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await asyncio.wait_for(app_server.list_threads(), timeout=1.0)
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        finally:
            await app_server.close()
            server.close()
            await server.wait_closed()

    async def test_tcp_immediate_connection_failure_is_unavailable(self):
        endpoint = AppServerEndpoint(
            "ws://127.0.0.1:1",
            WebSocketTarget("ws://127.0.0.1:1", None),
        )

        with pytest.raises(CodexCtlError) as excinfo:
            await connect_app_server(endpoint, timeout=0.2)
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_tcp_connect_passes_token_path_query_and_no_compression(
        self, tmp_path
    ):
        captured: dict = {}

        async def handle(connection):
            captured["path"] = connection.request.path
            captured["headers"] = dict(connection.request.headers)
            async for frame in connection:
                message = json.loads(frame)
                if message.get("method") == "initialize":
                    await connection.send(
                        json.dumps(
                            {
                                "id": message["id"],
                                "result": {"userAgent": "fake-app-server/1.0"},
                            }
                        )
                    )

        server = await serve(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        token_file = tmp_path / "token"
        endpoint = AppServerEndpoint(
            f"ws://127.0.0.1:{port}/app-server?client=test",
            WebSocketTarget(
                f"ws://127.0.0.1:{port}/app-server?client=test", token_file
            ),
        )
        # The file intentionally does not exist until connection time.
        token_file.write_text("  secret-token\n", encoding="utf-8")
        app_server = None
        try:
            app_server = await connect_app_server(endpoint)
        finally:
            if app_server is not None:
                await app_server.close()
            server.close()
            await server.wait_closed()

        assert captured["path"] == "/app-server?client=test"
        assert captured["headers"]["authorization"] == "Bearer secret-token"
        assert "sec-websocket-extensions" not in captured["headers"]

    @pytest.mark.parametrize("contents", ["", " \n"])
    async def test_empty_tcp_token_is_unavailable(self, tmp_path, contents):
        token_file = tmp_path / "token"
        token_file.write_text(contents, encoding="utf-8")
        endpoint = AppServerEndpoint(
            "ws://127.0.0.1:1", WebSocketTarget("ws://127.0.0.1:1", token_file)
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await connect_app_server(endpoint)
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_missing_tcp_token_is_unavailable(self, tmp_path):
        token_file = tmp_path / "missing-token"
        endpoint = AppServerEndpoint(
            "ws://127.0.0.1:1", WebSocketTarget("ws://127.0.0.1:1", token_file)
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await connect_app_server(endpoint)
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_unreadable_tcp_token_is_unavailable_without_exposing_path_or_contents(
        self, tmp_path, monkeypatch
    ):
        token_file = tmp_path / "token"
        endpoint = AppServerEndpoint(
            "ws://127.0.0.1:1", WebSocketTarget("ws://127.0.0.1:1", token_file)
        )
        original_read_text = Path.read_text

        def unreadable_read_text(path, *args, **kwargs):
            if path == token_file:
                raise PermissionError("secret-token")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unreadable_read_text)
        with pytest.raises(CodexCtlError) as excinfo:
            await connect_app_server(endpoint)
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

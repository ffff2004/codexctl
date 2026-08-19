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
from types import SimpleNamespace
from typing import cast

import pytest
from websockets.asyncio.server import serve, unix_serve

from codexctl.appserver import (
    REQUIRED_LIFECYCLE_OPERATIONS,
    UNSUPPORTED_INTERACTION_METHOD,
    AppServerTurn,
    JsonlStdioMessageTransport,
    JsonRpcAppServerSession,
    JsonRpcError,
    ThreadListResponse,
    ThreadResponse,
    TurnResponse,
    _decode_message,
    _launch_stdio_process,
    _OwnedStdioProcess,
    connect_endpoint,
    parse_user_agent_version,
    project_item,
    project_notification,
    project_response,
    project_thread_status,
)
from codexctl.endpoint import (
    AppServerEndpoint,
    StdioProtocol,
    StdioTarget,
    TcpTarget,
    UnixTarget,
)
from codexctl.model import (
    ApprovalPolicy,
    ApprovalsReviewer,
    CodexCtlError,
    ErrorCode,
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
) -> str:
    signal_setup = ""
    if termination_marker:
        signal_setup = (
            "def stop(signum, frame):\n"
            "    pathlib.Path(sys.argv[-1]).write_text('terminated')\n"
            "    raise SystemExit(0)\n"
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
        adapter = JsonRpcAppServerSession(None, "/fake.sock")
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
        adapter = JsonRpcAppServerSession(None, "/fake.sock")
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

    async def test_start_thread_serializes_approval_policy_and_reviewer(
        self, monkeypatch
    ):
        adapter = JsonRpcAppServerSession(None, "/fake.sock")
        calls = []

        async def request(method, params=None):
            calls.append((method, params))
            return project_response(method, {"thread": {"id": "t1"}})

        monkeypatch.setattr(adapter, "_request", request)

        # Default unattended start: never, reviewer omitted.
        await adapter.start_thread(StartConfig())
        assert calls[-1] == (
            "thread/start",
            {"approvalPolicy": "never", "sandbox": "workspace-write"},
        )
        assert "approvalsReviewer" not in calls[-1][1]

        # Auto review: on-request + auto_review reviewer.
        await adapter.start_thread(
            StartConfig(
                approval_policy=ApprovalPolicy.onRequest,
                approvals_reviewer=ApprovalsReviewer.autoReview,
            )
        )
        assert calls[-1] == (
            "thread/start",
            {
                "approvalPolicy": "on-request",
                "approvalsReviewer": "auto_review",
                "sandbox": "workspace-write",
            },
        )

    async def test_start_thread_serializes_each_approval_policy(self, monkeypatch):
        adapter = JsonRpcAppServerSession(None, "/fake.sock")
        calls = []

        async def request(method, params=None):
            calls.append((method, params))
            return project_response(method, {"thread": {"id": "t1"}})

        monkeypatch.setattr(adapter, "_request", request)

        for policy, wire_value in (
            (ApprovalPolicy.untrusted, "untrusted"),
            (ApprovalPolicy.onRequest, "on-request"),
            (ApprovalPolicy.never, "never"),
        ):
            await adapter.start_thread(StartConfig(approval_policy=policy))
            assert calls[-1][1]["approvalPolicy"] == wire_value

    async def test_operations_keep_wire_requests_inside_the_adapter(self, monkeypatch):
        adapter = JsonRpcAppServerSession(None, "/fake.sock")
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
        adapter = JsonRpcAppServerSession(None, "/fake.sock")
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
        adapter = JsonRpcAppServerSession(None, "/fake.sock")
        missing = "steer turn"

        async def request(method, params=None):
            if method == "turn/steer":
                raise JsonRpcError(-32601, "method not found")
            return project_response(method, {})

        monkeypatch.setattr(adapter, "_request", request)

        assert await adapter.check_lifecycle_operations() == (missing,)


class TestStrictFraming:
    @pytest.mark.parametrize(
        "frame",
        [
            b'{"ok": true}',
            "[1, 2]",
            "broken",
            '{"value": NaN}',
            '{"value": Infinity}',
            '{"value": -Infinity}',
        ],
    )
    def test_invalid_frames_are_protocol_errors(self, frame):
        with pytest.raises(CodexCtlError) as excinfo:
            _decode_message(frame)
        assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR

    def test_valid_json_object_does_not_require_jsonrpc_header(self):
        assert _decode_message('{"method":"initialize","id":1}') == {
            "method": "initialize",
            "id": 1,
        }

    async def test_stdio_framing_skips_blank_lines_and_accepts_crlf_and_eof(self):
        stdout = asyncio.StreamReader()
        stdout.feed_data(b'\n  \r\n{"first": 1}\r\n{"last": 2}')
        stdout.feed_eof()
        process = _OwnedStdioProcess(
            cast(asyncio.subprocess.Process, SimpleNamespace(stdout=stdout))
        )

        transport = JsonlStdioMessageTransport(process)

        assert [message async for message in transport.messages()] == [
            '{"first": 1}',
            '{"last": 2}',
        ]

    async def test_invalid_frame_fails_pending_request_and_closes_transport(self):
        class Transport:
            def __init__(self):
                self.closed = False

            async def send_text(self, payload):
                pass

            async def messages(self):
                yield "{malformed"

            async def close(self):
                self.closed = True

        transport = Transport()
        adapter = JsonRpcAppServerSession(transport, "stdio")
        adapter._reader_task = asyncio.create_task(adapter._reader())

        with pytest.raises(CodexCtlError) as excinfo:
            await adapter._request("unknown")
        await adapter._reader_task

        assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        assert transport.closed

    async def test_runtime_failure_and_concurrent_close_share_blocking_cleanup(self):
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        class Transport:
            close_calls = 0

            async def send_text(self, _payload):
                pass

            async def messages(self):
                yield "{malformed"

            async def close(self):
                self.close_calls += 1
                cleanup_started.set()
                await release_cleanup.wait()

        transport = Transport()
        adapter = JsonRpcAppServerSession(transport, "stdio")
        adapter._reader_task = asyncio.create_task(adapter._reader())

        await cleanup_started.wait()
        close_task = asyncio.create_task(adapter.close())
        await asyncio.sleep(0)

        assert not close_task.done()
        assert not adapter._reader_task.done()
        assert adapter._lifecycle == "CLOSING"

        release_cleanup.set()
        await adapter._reader_task
        await close_task

        assert transport.close_calls == 1
        assert adapter._lifecycle == "CLOSED"

    async def test_concurrent_and_repeated_close_calls_share_cleanup(self):
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        class Transport:
            close_calls = 0

            async def close(self):
                self.close_calls += 1
                cleanup_started.set()
                await release_cleanup.wait()

        transport = Transport()
        adapter = JsonRpcAppServerSession(transport, "stdio")
        close_tasks = [asyncio.create_task(adapter.close()) for _ in range(3)]

        await cleanup_started.wait()
        await asyncio.sleep(0)
        assert all(not task.done() for task in close_tasks)

        release_cleanup.set()
        await asyncio.gather(*close_tasks)
        await adapter.close()

        assert transport.close_calls == 1
        assert adapter._lifecycle == "CLOSED"

    async def test_close_cancellation_waits_for_cleanup_completion(self):
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        class Transport:
            async def close(self):
                cleanup_started.set()
                await release_cleanup.wait()

        adapter = JsonRpcAppServerSession(Transport(), "stdio")
        close_task = asyncio.create_task(adapter.close())
        await cleanup_started.wait()

        close_task.cancel()
        await asyncio.sleep(0)
        assert not close_task.done()

        close_task.cancel()
        await asyncio.sleep(0)
        assert not close_task.done()

        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await close_task
        assert adapter._lifecycle == "CLOSED"

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

        adapter = await connect_endpoint(endpoint)
        try:
            assert adapter.app_server_version == "1.0"
        finally:
            await adapter.close()

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
            protocol=StdioProtocol.WEBSOCKET,
        )

        adapter = await connect_endpoint(endpoint)
        try:
            assert adapter.app_server_version == "2.0"
            assert (await adapter.list_threads()).threads == []
        finally:
            await adapter.close()

        assert pong.read_text(encoding="utf-8") == "seen"

    async def test_stdio_websocket_malformed_upgrade_is_unavailable(
        self, stdio_endpoint
    ):
        endpoint = stdio_endpoint(
            "import sys\n"
            "sys.stdin.buffer.read(1)\n"
            "sys.stdout.buffer.write(b'not an HTTP response')\n"
            "sys.stdout.buffer.flush()\n",
            filename="stdio-websocket-bad-upgrade.py",
            protocol=StdioProtocol.WEBSOCKET,
        )

        with pytest.raises(CodexCtlError) as excinfo:
            await connect_endpoint(endpoint, timeout=0.5)

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
            protocol=StdioProtocol.WEBSOCKET,
        )

        adapter = await connect_endpoint(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await adapter.list_threads()
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        finally:
            await adapter.close()

    async def test_stdio_websocket_eof_is_runtime_protocol_error(self, stdio_endpoint):
        endpoint = stdio_endpoint(
            _stdio_websocket_proxy_source(initialized_action="raise SystemExit(0)"),
            filename="stdio-websocket-eof.py",
            protocol=StdioProtocol.WEBSOCKET,
        )

        adapter = await connect_endpoint(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await adapter.list_threads()
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        finally:
            await adapter.close()

    async def test_stdio_websocket_runtime_failure_cleans_up_descendants(
        self, tmp_path, stdio_endpoint, monkeypatch
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
            protocol=StdioProtocol.WEBSOCKET,
        )
        monkeypatch.setattr(_OwnedStdioProcess, "_GRACEFUL_WAIT_SECONDS", 0.01)
        monkeypatch.setattr(_OwnedStdioProcess, "_TERMINATE_WAIT_SECONDS", 0.01)

        adapter = await connect_endpoint(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await adapter.list_threads()
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
            await adapter._reader_task

            pid = int(descendant_pid.read_text(encoding="utf-8"))
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("WebSocket stdio descendant survived process-group cleanup")
        finally:
            await adapter.close()

    async def test_stdio_websocket_close_cancellation_cleans_up_established_process_group(
        self, tmp_path, stdio_endpoint, monkeypatch
    ):
        descendant_ready = tmp_path / "websocket-cancel-descendant.ready"
        descendant_terminated = tmp_path / "websocket-cancel-descendant.terminated"
        descendant_pid = tmp_path / "websocket-cancel-descendant.pid"
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
                    f"{descendant_source!r}, sys.argv[-4], sys.argv[-3], sys.argv[-2]])\n"
                    "while not pathlib.Path(sys.argv[-4]).exists():\n"
                    "    time.sleep(0.001)"
                ),
                termination_marker=True,
            ),
            str(descendant_ready),
            str(descendant_terminated),
            str(descendant_pid),
            str(tmp_path / "websocket-cancel-parent.terminated"),
            filename="stdio-websocket-cancel-established.py",
            protocol=StdioProtocol.WEBSOCKET,
        )
        monkeypatch.setattr(_OwnedStdioProcess, "_GRACEFUL_WAIT_SECONDS", 0.01)

        adapter = await connect_endpoint(endpoint)
        transport = adapter._transport
        assert transport is not None
        close_started = asyncio.Event()
        release_close = asyncio.Event()
        original_close = transport.close

        async def delayed_close():
            close_started.set()
            await release_close.wait()
            await original_close()

        transport.close = delayed_close
        close_task = asyncio.create_task(adapter.close())
        try:
            await close_started.wait()
            close_task.cancel()
            await asyncio.sleep(0)
            assert not close_task.done()

            close_task.cancel()
            await asyncio.sleep(0)
            assert not close_task.done()

            release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await close_task

            pid = int(descendant_pid.read_text(encoding="utf-8"))
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("WebSocket stdio descendant survived process-group cleanup")
        finally:
            if not close_task.done():
                await adapter.close()

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
            protocol=StdioProtocol.WEBSOCKET,
        )

        adapter = await connect_endpoint(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await asyncio.wait_for(adapter.list_threads(), timeout=0.5)
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        finally:
            await adapter.close()

    async def test_stdio_websocket_startup_timeout_cleans_up_child(
        self, tmp_path, stdio_endpoint, monkeypatch
    ):
        ready = tmp_path / "websocket-timeout.ready"
        terminated = tmp_path / "websocket-timeout.terminated"
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
            filename="stdio-websocket-timeout.py",
            protocol=StdioProtocol.WEBSOCKET,
        )
        monkeypatch.setattr(_OwnedStdioProcess, "_GRACEFUL_WAIT_SECONDS", 0.01)
        monkeypatch.setattr(_OwnedStdioProcess, "_TERMINATE_WAIT_SECONDS", 0.01)

        with pytest.raises(CodexCtlError) as excinfo:
            await connect_endpoint(endpoint, timeout=0.5)

        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE
        assert ready.read_text(encoding="utf-8") == "ready"
        for _ in range(100):
            if terminated.exists():
                break
            await asyncio.sleep(0.01)
        assert terminated.read_text(encoding="utf-8") == "terminated"

    async def test_stdio_websocket_cancellation_cleans_up_child(
        self, tmp_path, stdio_endpoint, monkeypatch
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
            protocol=StdioProtocol.WEBSOCKET,
        )
        monkeypatch.setattr(_OwnedStdioProcess, "_GRACEFUL_WAIT_SECONDS", 0.01)
        monkeypatch.setattr(_OwnedStdioProcess, "_TERMINATE_WAIT_SECONDS", 0.01)

        task = asyncio.create_task(connect_endpoint(endpoint, timeout=5.0))
        for _ in range(100):
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
        self, tmp_path, stdio_endpoint, monkeypatch
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
            protocol=StdioProtocol.WEBSOCKET,
        )
        monkeypatch.setattr(_OwnedStdioProcess, "_GRACEFUL_WAIT_SECONDS", 0.01)
        monkeypatch.setattr(_OwnedStdioProcess, "_TERMINATE_WAIT_SECONDS", 0.01)

        adapter = await connect_endpoint(endpoint)
        await asyncio.wait_for(adapter.close(), timeout=0.5)

        for _ in range(100):
            if terminated.exists():
                break
            await asyncio.sleep(0.01)
        assert terminated.read_text(encoding="utf-8") == "terminated"

    async def test_stdio_preinitialize_exit_is_unavailable(self, stdio_endpoint):
        endpoint = stdio_endpoint("raise SystemExit(0)\n", filename="stdio-exit.py")

        with pytest.raises(CodexCtlError) as excinfo:
            await connect_endpoint(endpoint)
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_stdio_executable_failure_is_unavailable(self, tmp_path):
        endpoint = AppServerEndpoint(
            "stdio", StdioTarget((str(tmp_path / "missing-app-server"),))
        )

        with pytest.raises(CodexCtlError) as excinfo:
            await connect_endpoint(endpoint)
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

        adapter = await connect_endpoint(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await adapter.start_thread(StartConfig())
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        finally:
            await adapter.close()

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

        adapter = await connect_endpoint(endpoint)
        try:
            assert (await adapter.list_threads()).threads == []
            assert adapter.interaction_count == 0
        finally:
            await adapter.close()

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

        adapter = await connect_endpoint(endpoint)
        notifications = adapter.notifications()
        try:
            thread = await adapter.start_thread(StartConfig())
            assert thread is not None and thread.id == "t1"
            assert await adapter.start_turn("t1", "hello") == "u1"
            assert (await adapter.list_threads()).threads == []

            events = [
                await asyncio.wait_for(anext(notifications), timeout=1.0)
                for _ in range(6)
            ]
        finally:
            await adapter.close()

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

        adapter = await connect_endpoint(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await asyncio.wait_for(adapter.list_threads(), timeout=1.0)
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        finally:
            await adapter.close()

    async def test_stdio_startup_timeout_cleans_up_child(
        self, stdio_endpoint, monkeypatch
    ):
        endpoint = stdio_endpoint(
            "import time\ntime.sleep(60)\n", filename="stdio-hang.py"
        )
        monkeypatch.setattr(_OwnedStdioProcess, "_GRACEFUL_WAIT_SECONDS", 0.01)
        monkeypatch.setattr(_OwnedStdioProcess, "_TERMINATE_WAIT_SECONDS", 0.01)

        with pytest.raises(CodexCtlError) as excinfo:
            await connect_endpoint(endpoint, timeout=0.05)
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_stdio_cleanup_waits_for_process_exit(
        self, stdio_endpoint, monkeypatch
    ):
        endpoint = stdio_endpoint(
            "import json, sys, time\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message.get('method') == 'initialize':\n"
            "        print(json.dumps({'id': message['id'], 'result': "
            "{'userAgent': 'stdio/1.0'}}), flush=True)\n"
            "        time.sleep(60)\n",
            filename="stdio-running.py",
        )
        monkeypatch.setattr(_OwnedStdioProcess, "_GRACEFUL_WAIT_SECONDS", 0.01)
        monkeypatch.setattr(_OwnedStdioProcess, "_TERMINATE_WAIT_SECONDS", 0.01)

        adapter = await connect_endpoint(endpoint)
        transport = adapter._transport
        assert isinstance(transport, JsonlStdioMessageTransport)
        process = transport._process
        await adapter.close()

        assert process.returncode is not None

    async def test_stdio_cleanup_terminates_descendant_process_group(
        self, tmp_path, stdio_endpoint, monkeypatch
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
        monkeypatch.setattr(_OwnedStdioProcess, "_GRACEFUL_WAIT_SECONDS", 0.01)
        monkeypatch.setattr(_OwnedStdioProcess, "_TERMINATE_WAIT_SECONDS", 0.01)

        adapter = await connect_endpoint(endpoint)
        await adapter.close()

        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.read_text(encoding="utf-8") == "terminated"

    async def test_stdio_launch_cancellation_closes_transport(self, monkeypatch):
        started = asyncio.Event()
        release = asyncio.Event()

        class Transport:
            closed = False

            async def close(self):
                self.closed = True

        transport = Transport()

        async def launch(_argv, _state):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return transport

        monkeypatch.setattr(_OwnedStdioProcess, "launch", launch)
        task = asyncio.create_task(_launch_stdio_process(("app-server",)))
        await started.wait()
        task.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.closed

    async def test_stdio_launch_cancellation_does_not_wait_for_stalled_launch(
        self, monkeypatch
    ):
        started = asyncio.Event()
        release = asyncio.Event()

        async def launch(_argv, _state):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return SimpleNamespace(close=lambda: None)

        monkeypatch.setattr(_OwnedStdioProcess, "launch", launch)
        task = asyncio.create_task(_launch_stdio_process(("app-server",)))
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)

        release.set()
        await asyncio.sleep(0)

    async def test_stdio_launch_cancellation_kills_real_child_before_wrapper(
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
        monkeypatch.setattr(_OwnedStdioProcess, "_GRACEFUL_WAIT_SECONDS", 0.01)
        monkeypatch.setattr(_OwnedStdioProcess, "_TERMINATE_WAIT_SECONDS", 0.01)
        task = asyncio.create_task(
            _launch_stdio_process(
                (sys.executable, "-c", source, str(ready), str(terminated))
            )
        )
        pid: int | None = None
        try:
            await subprocess_created.wait()
            for _ in range(100):
                if ready.exists():
                    break
                await asyncio.sleep(0.01)
            assert ready.exists()
            pid = int(ready.read_text(encoding="utf-8"))

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            for _ in range(100):
                if terminated.exists():
                    break
                await asyncio.sleep(0.01)
            assert terminated.read_text(encoding="utf-8") == "terminated"
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            if pid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)

    async def test_stdio_initialize_cancellation_closes_assigned_transport(
        self, monkeypatch
    ):
        initialized = asyncio.Event()

        class Transport:
            closed = False

            async def send_text(self, _payload):
                pass

            async def messages(self):
                await asyncio.Future()
                yield "{}"

            async def close(self):
                self.closed = True

        transport = Transport()

        async def launch(_argv, _state):
            return transport

        async def initialize(_adapter):
            initialized.set()
            await asyncio.Future()

        monkeypatch.setattr(_OwnedStdioProcess, "launch", launch)
        monkeypatch.setattr(JsonRpcAppServerSession, "_initialize", initialize)
        endpoint = AppServerEndpoint("stdio", StdioTarget(("app-server",)))
        task = asyncio.create_task(connect_endpoint(endpoint))
        await initialized.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.closed


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
        adapter = None
        try:
            adapter = await connect_endpoint(
                AppServerEndpoint(
                    f"ws://127.0.0.1:{port}/app-server?client=test",
                    TcpTarget(f"ws://127.0.0.1:{port}/app-server?client=test", None),
                )
            )
            await asyncio.wait_for(initialized.wait(), timeout=1.0)
            assert (await adapter.list_threads()).threads == []
        finally:
            if adapter is not None:
                await adapter.close()
            server.close()
            await server.wait_closed()

        assert adapter.user_agent == "codex-app-server/0.101.0"
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
        [b'{"binary": true}', "{malformed", "[]", '{"value": NaN}'],
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
            TcpTarget(f"ws://127.0.0.1:{port}", None),
        )
        adapter = await connect_endpoint(endpoint)
        try:
            with pytest.raises(CodexCtlError) as excinfo:
                await asyncio.wait_for(adapter.list_threads(), timeout=1.0)
            assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        finally:
            await adapter.close()
            server.close()
            await server.wait_closed()

    async def test_tcp_immediate_connection_failure_is_unavailable(self):
        endpoint = AppServerEndpoint(
            "ws://127.0.0.1:1",
            TcpTarget("ws://127.0.0.1:1", None),
        )

        with pytest.raises(CodexCtlError) as excinfo:
            await connect_endpoint(endpoint, timeout=0.2)
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

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
        monkeypatch.setattr(JsonRpcAppServerSession, "_initialize", initialize)
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

"""Codex app-server adapter: transport, JSON-RPC routing, and projection.

This is the compatibility firewall between the Codex wire protocol and the
rest of ``codexctl``. Raw protocol messages stay inside this module; callers
only see request results, :class:`~codexctl.model.ProjectedEvent` values, and
:class:`JsonRpcError` failures.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from .model import ErrorCode, ProjectedEvent

CLIENT_NAME = "codexctl"
CLIENT_VERSION = "0.1.0"

# Synthetic notification method used to surface server-initiated interaction
# requests that v1 cannot broker. Internal to codexctl; never on the wire.
UNSUPPORTED_INTERACTION_METHOD = "codexctl/unsupportedInteraction"

TERMINAL_TURN_STATUSES = {"completed", "interrupted", "failed"}


class JsonRpcError(Exception):
    """A JSON-RPC error response from the app-server."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@runtime_checkable
class AppServerPort(Protocol):
    """Internal low-level seam shaped around what CodexCtl needs."""

    async def request(self, method: str, params: dict | None = None) -> dict: ...

    def notifications(self) -> AsyncIterator[dict]: ...

    async def unsubscribe(self, thread_id: str) -> None:
        """Best-effort unsubscribe; never raises."""
        ...

    async def close(self) -> None: ...


class UnixSocketAppServerAdapter:
    """Production adapter: JSON-RPC over websocket on a Unix control socket.

    The Codex app-server Unix endpoint speaks websocket framing over the
    socket (standard HTTP Upgrade handshake), so the connection URI uses the
    ``ws+unix://`` scheme.
    """

    def __init__(self, conn: Any, socket_path: Path) -> None:
        self._conn = conn
        self.socket_path = socket_path
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._ids = itertools.count(1)
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        self.user_agent: str | None = None
        self.app_server_version: str | None = None
        self.server_codex_home: str | None = None
        self.interaction_count = 0

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    async def connect(cls, socket_path: Path, timeout: float = 15.0) -> "UnixSocketAppServerAdapter":
        from websockets.asyncio.client import connect as ws_connect

        uri = "ws+unix://" + str(Path(socket_path))
        try:
            conn = await asyncio.wait_for(ws_connect(uri, max_size=None), timeout)
        except Exception as exc:  # noqa: BLE001 - mapped to application error
            raise _unavailable(socket_path, exc) from exc
        adapter = cls(conn, socket_path)
        adapter._reader_task = asyncio.create_task(adapter._reader())
        try:
            await adapter._initialize()
        except Exception:
            await adapter.close()
            raise
        return adapter

    async def _initialize(self) -> None:
        result = await self.request(
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
        self.user_agent = result.get("userAgent")
        self.server_codex_home = result.get("codexHome")
        self.app_server_version = parse_user_agent_version(self.user_agent)
        await self._notify("initialized")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
        try:
            await self._conn.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass

    async def unsubscribe(self, thread_id: str) -> None:
        """Best-effort unsubscribe; never raises."""
        try:
            await asyncio.wait_for(
                self.request("thread/unsubscribe", {"threadId": thread_id}), 5.0
            )
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            pass

    # -- transport -----------------------------------------------------------

    async def _send(self, message: dict) -> None:
        await self._conn.send(json.dumps(message))

    async def _notify(self, method: str, params: dict | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def request(self, method: str, params: dict | None = None) -> dict:
        if self._closed:
            raise _unavailable(self.socket_path, RuntimeError("connection closed"))
        request_id = next(self._ids)
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        try:
            await self._send(message)
            return await future
        finally:
            self._pending.pop(request_id, None)

    def notifications(self) -> AsyncIterator[dict]:
        return self._iter_notifications()

    async def _iter_notifications(self) -> AsyncIterator[dict]:
        while True:
            message = await self._queue.get()
            if message is None:
                return
            yield message

    async def _reader(self) -> None:
        try:
            async for frame in self._conn:
                try:
                    message = json.loads(frame)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(message, dict):
                    continue
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
            return
        except Exception:  # noqa: BLE001 - any transport loss ends the stream
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        JsonRpcError(-32000, "app-server connection closed")
                    )
            self._pending.clear()
            await self._queue.put(None)

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


def _unavailable(socket_path: Path, cause: Exception) -> Exception:
    from .model import CodexCtlError

    return CodexCtlError(
        ErrorCode.APP_SERVER_UNAVAILABLE,
        f"cannot reach app-server at {socket_path}: {cause}",
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
    total = token_usage.get("total") or {}
    window = token_usage.get("modelContextWindow")
    if not isinstance(window, int) or window <= 0:
        return None
    try:
        used = int(total.get("inputTokens", 0)) + int(total.get("cachedInputTokens", 0))
    except (TypeError, ValueError):
        return None
    return {"usedTokens": used, "windowTokens": window, "ratio": round(used / window, 5)}


def project_notification(message: dict, source: str | None = None) -> ProjectedEvent | None:
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
        return ProjectedEvent("thread/started", thread_id=thread.get("id"), source=source)

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

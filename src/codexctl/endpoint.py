"""Runtime endpoint resolution.

Three real production behaviors justify this seam:

- ``ManagedRuntimeProvider`` may start the experimental Codex daemon lifecycle
  to make a compatible shared app-server available.
- ``ExternalRuntimeProvider`` honors ``--endpoint`` and performs no lifecycle
  mutation at all.
- ``StdioRuntimeProvider`` records one caller-supplied process invocation;
  process ownership begins when the app-server transport connects.

Core execution only ever sees an :class:`AppServerEndpoint`.
"""

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qsl, unquote, urlsplit

from .model import CodexCtlError, ErrorCode, UsageError
from .rollout import codex_home


@dataclass(frozen=True)
class AppServerEndpoint:
    """A resolved app-server location, opaque outside transport code."""

    display: str
    target: "UnixSocketTarget | WebSocketTarget | StdioTarget" = field(repr=False)
    runtime_pid: int | None = None
    runtime_version: str | None = None


@dataclass(frozen=True)
class UnixSocketTarget:
    path: Path


@dataclass(frozen=True)
class WebSocketTarget:
    url: str
    token_file: Path | None


class StdioFraming(StrEnum):
    """Message framing carried over a child process's stdin/stdout pipes."""

    JSONL = "jsonl"
    WEBSOCKET = "websocket"


@dataclass(frozen=True)
class StdioTarget:
    """Exact argv for a one-shot stdio app-server process."""

    argv: tuple[str, ...]
    framing: StdioFraming = StdioFraming.JSONL


# Endpoint URLs are locations, never credential carriers. Keep this closed
# list deliberately specific so ordinary application query parameters remain
# opaque and are forwarded unchanged.
_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "bearer_token",
        "authorization",
    }
)


@runtime_checkable
class RuntimeProvider(Protocol):
    mode: str

    async def resolve_endpoint(self) -> AppServerEndpoint: ...

    def probe_cli_version(self) -> str | None:
        """Best-effort codex CLI version probe; never raises."""
        ...


def default_control_socket_path(home: Path | None = None) -> Path:
    return (home or codex_home()) / "app-server-control" / "app-server-control.sock"


def _pid_from_pidfile(home: Path | None = None) -> int | None:
    pidfile = (home or codex_home()) / "app-server-daemon" / "app-server.pid"
    try:
        record = json.loads(pidfile.read_text(encoding="utf-8"))
        pid = record.get("pid") if isinstance(record, dict) else None
        return int(pid) if pid is not None else None
    except OSError, ValueError, TypeError, json.JSONDecodeError:
        return None


class ManagedRuntimeProvider:
    """Resolves the managed shared app-server, starting the daemon if needed.

    All daemon lifecycle knowledge (command spelling, pidfile layout, socket
    discovery) is contained here. The probe path avoids touching the daemon
    when a compatible runtime is already reachable.
    """

    mode = "managed"

    def __init__(self, codex_bin: str | None = None, home: Path | None = None) -> None:
        self._codex_bin = codex_bin or os.environ.get("CODEXCTL_CODEX_BIN", "codex")
        self._home = home

    async def resolve_endpoint(self) -> AppServerEndpoint:
        socket_path = default_control_socket_path(self._home)
        probed = await self._probe(socket_path)
        if probed is not None:
            return probed
        return await self._daemon_start(socket_path)

    async def _probe(self, socket_path: Path) -> AppServerEndpoint | None:
        if not socket_path.exists():
            return None
        from .appserver import connect_app_server

        try:
            app_server = await connect_app_server(
                AppServerEndpoint(str(socket_path), UnixSocketTarget(socket_path)),
                timeout=5.0,
            )
        except CodexCtlError:
            return None
        version = app_server.app_server_version
        await app_server.close()
        return AppServerEndpoint(
            display=str(socket_path),
            target=UnixSocketTarget(socket_path),
            runtime_pid=_pid_from_pidfile(self._home),
            runtime_version=version,
        )

    async def _daemon_start(self, fallback_socket: Path) -> AppServerEndpoint:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._codex_bin,
                "app-server",
                "daemon",
                "start",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CodexCtlError(
                ErrorCode.APP_SERVER_UNAVAILABLE,
                f"codex binary not found: {self._codex_bin!r}",
                cause=exc,
            ) from exc
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise CodexCtlError(
                ErrorCode.APP_SERVER_UNAVAILABLE,
                f"codex app-server daemon start failed: {detail or 'no output'}",
            )
        payload = _last_json_object(stdout.decode(errors="replace"))
        if payload is None:
            raise CodexCtlError(
                ErrorCode.INCOMPATIBLE_CODEX,
                "codex app-server daemon start produced no parseable JSON lifecycle response",
            )
        socket_path = payload.get("socketPath")
        if not socket_path:
            raise CodexCtlError(
                ErrorCode.INCOMPATIBLE_CODEX,
                "codex daemon lifecycle response omitted socketPath",
            )
        pid = payload.get("pid")
        return AppServerEndpoint(
            display=str(socket_path),
            target=UnixSocketTarget(Path(socket_path)),
            runtime_pid=int(pid) if pid is not None else None,
            runtime_version=payload.get("appServerVersion"),
        )

    def probe_cli_version(self) -> str | None:
        """Best-effort ``codex --version`` probe against the managed binary."""
        try:
            proc = subprocess.run(
                [self._codex_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except OSError, subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        output = (proc.stdout or proc.stderr or "").strip()
        return output.splitlines()[0] if output else None


def _last_json_object(text: str) -> dict | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


class ExternalRuntimeProvider:
    """Resolves a caller-supplied endpoint; never manages its lifecycle."""

    mode = "external"

    def __init__(self, endpoint: str, token_file: Path | None = None) -> None:
        self._endpoint = parse_external_endpoint(endpoint, token_file)

    async def resolve_endpoint(self) -> AppServerEndpoint:
        target = self._endpoint.target
        if isinstance(target, UnixSocketTarget) and not target.path.exists():
            raise CodexCtlError(
                ErrorCode.APP_SERVER_UNAVAILABLE,
                f"external app-server socket does not exist: {target.path}",
            )
        return self._endpoint

    def probe_cli_version(self) -> str | None:
        # External endpoints own no codex binary lifecycle.
        return None


class StdioRuntimeProvider:
    """Resolves a caller-configured, one-shot stdio app-server process."""

    mode = "stdio"

    def __init__(
        self,
        executable: str,
        args: tuple[str, ...] = (),
        framing: StdioFraming = StdioFraming.JSONL,
    ) -> None:
        self._target = StdioTarget((executable, *args), framing)

    async def resolve_endpoint(self) -> AppServerEndpoint:
        # Resolution is deliberately side-effect free. The connection layer
        # owns spawning and cleanup so every operation gets one fresh process.
        return AppServerEndpoint(display="stdio", target=self._target)

    def probe_cli_version(self) -> str | None:
        # Stdio mode does not expose or probe a separate Codex CLI lifecycle.
        return None


def parse_external_endpoint(
    endpoint: str, token_file: Path | None = None
) -> AppServerEndpoint:
    """Parse the deliberately small external endpoint vocabulary.

    Tokens are represented only by their file path and are read by the WebSocket
    transport immediately before it opens a connection.
    """
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        # Do not reflect a caller-provided URI: it could contain a credential.
        raise UsageError("invalid --endpoint") from exc

    if parsed.scheme == "unix":
        if (
            not endpoint.startswith("unix:///")
            or endpoint.startswith("unix:////")
            or parsed.netloc
            # urlsplit() cannot distinguish an absent delimiter from an empty
            # query or fragment, but Unix endpoints permit neither.
            or "?" in endpoint
            or "#" in endpoint
            or not parsed.path.startswith("/")
        ):
            raise UsageError("--endpoint unix URI must be unix:///absolute/path")
        if token_file is not None:
            raise UsageError(
                "--endpoint-token-file is supported only for ws:// endpoints"
            )
        path = Path(unquote(parsed.path))
        if not path.is_absolute():  # Defensive: keep URI validation explicit.
            raise UsageError("--endpoint unix URI must be unix:///absolute/path")
        return AppServerEndpoint(display=endpoint, target=UnixSocketTarget(path))

    if parsed.scheme != "ws":
        raise UsageError("--endpoint must use unix:///absolute/path or ws://host:port")
    if (
        not parsed.hostname
        or port is None
        # Preserve the URL path/query verbatim, but never accept a fragment
        # delimiter (including an empty fragment) as part of the endpoint.
        or "#" in endpoint
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise UsageError("--endpoint ws URI must be ws://host:port[/path][?query]")
    if any(
        key.casefold() in _CREDENTIAL_QUERY_KEYS
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise UsageError(
            "--endpoint must not carry credentials; use --endpoint-token-file"
        )
    return AppServerEndpoint(
        display=endpoint, target=WebSocketTarget(endpoint, token_file)
    )

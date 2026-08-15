"""Runtime endpoint resolution.

Two real production behaviors justify this seam:

- ``ManagedDaemonAdapter`` may start the experimental Codex daemon lifecycle
  to make a compatible shared app-server available.
- ``ExternalEndpointAdapter`` honors ``--endpoint`` and performs no lifecycle
  mutation at all.

Core execution only ever sees an :class:`AppServerEndpoint`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qsl, unquote, urlsplit

from .model import CodexCtlError, ErrorCode, UsageError
from .rollout import codex_home


@dataclass(frozen=True)
class AppServerEndpoint:
    """A resolved app-server location, opaque outside transport code."""

    display: str
    target: "UnixTarget | TcpTarget" = field(repr=False)
    runtime_pid: int | None = None
    runtime_version: str | None = None


@dataclass(frozen=True)
class UnixTarget:
    path: Path


@dataclass(frozen=True)
class TcpTarget:
    url: str
    token_file: Path | None


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
class EndpointPort(Protocol):
    mode: str

    async def resolve(self) -> AppServerEndpoint: ...

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
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


class ManagedDaemonAdapter:
    """Resolves the managed shared app-server, starting the daemon if needed.

    All daemon lifecycle knowledge (command spelling, pidfile layout, socket
    discovery) is contained here. The probe path avoids touching the daemon
    when a compatible runtime is already reachable.
    """

    mode = "managed"

    def __init__(self, codex_bin: str | None = None, home: Path | None = None) -> None:
        self._codex_bin = codex_bin or os.environ.get("CODEXCTL_CODEX_BIN", "codex")
        self._home = home

    async def resolve(self) -> AppServerEndpoint:
        socket_path = default_control_socket_path(self._home)
        probed = await self._probe(socket_path)
        if probed is not None:
            return probed
        return await self._daemon_start(socket_path)

    async def _probe(self, socket_path: Path) -> AppServerEndpoint | None:
        if not socket_path.exists():
            return None
        from .appserver import connect_endpoint

        try:
            adapter = await connect_endpoint(
                AppServerEndpoint(str(socket_path), UnixTarget(socket_path)),
                timeout=5.0,
            )
        except CodexCtlError:
            return None
        version = adapter.app_server_version
        await adapter.close()
        return AppServerEndpoint(
            display=str(socket_path),
            target=UnixTarget(socket_path),
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
            target=UnixTarget(Path(socket_path)),
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
        except (OSError, subprocess.TimeoutExpired):
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


class ExternalEndpointAdapter:
    """Resolves a caller-supplied endpoint; never manages its lifecycle."""

    mode = "external"

    def __init__(self, endpoint: str, token_file: Path | None = None) -> None:
        self._endpoint = parse_external_endpoint(endpoint, token_file)

    async def resolve(self) -> AppServerEndpoint:
        target = self._endpoint.target
        if isinstance(target, UnixTarget) and not target.path.exists():
            raise CodexCtlError(
                ErrorCode.APP_SERVER_UNAVAILABLE,
                f"external app-server socket does not exist: {target.path}",
            )
        return self._endpoint

    def probe_cli_version(self) -> str | None:
        # External endpoints own no codex binary lifecycle.
        return None


def parse_external_endpoint(
    endpoint: str, token_file: Path | None = None
) -> AppServerEndpoint:
    """Parse the deliberately small external endpoint vocabulary.

    Tokens are represented only by their file path and are read by the TCP
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
        return AppServerEndpoint(display=endpoint, target=UnixTarget(path))

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
    return AppServerEndpoint(display=endpoint, target=TcpTarget(endpoint, token_file))

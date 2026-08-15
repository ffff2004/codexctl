"""Runtime endpoint resolution.

Two real production behaviors justify this seam:

- ``ManagedDaemonAdapter`` may start the experimental Codex daemon lifecycle
  to make a compatible shared app-server available.
- ``ExternalSocketAdapter`` honors ``--socket`` and performs no lifecycle
  mutation at all.

Core execution only ever sees an :class:`AppServerEndpoint`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .model import CodexCtlError, ErrorCode
from .rollout import codex_home


@dataclass(frozen=True)
class AppServerEndpoint:
    socket_path: Path
    runtime_pid: int | None = None
    runtime_version: str | None = None


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
        from .appserver import UnixSocketAppServerAdapter

        try:
            adapter = await UnixSocketAppServerAdapter.connect(socket_path, timeout=5.0)
        except CodexCtlError:
            return None
        version = adapter.app_server_version
        await adapter.close()
        return AppServerEndpoint(
            socket_path=socket_path,
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
            socket_path=Path(socket_path),
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


class ExternalSocketAdapter:
    """Resolves a caller-supplied endpoint; never manages its lifecycle."""

    mode = "external"

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path

    async def resolve(self) -> AppServerEndpoint:
        if not self._socket_path.exists():
            raise CodexCtlError(
                ErrorCode.APP_SERVER_UNAVAILABLE,
                f"external app-server socket does not exist: {self._socket_path}",
            )
        return AppServerEndpoint(socket_path=self._socket_path)

    def probe_cli_version(self) -> str | None:
        # External endpoints own no codex binary lifecycle.
        return None

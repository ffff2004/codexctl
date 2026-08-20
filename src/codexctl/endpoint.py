"""Runtime endpoint resolution.

Four real production behaviors justify this seam:

- ``ManagedRuntimeProvider`` may start the experimental Codex daemon lifecycle
  to make a compatible shared app-server available.
- ``ExternalRuntimeProvider`` honors ``--endpoint`` and performs no lifecycle
  mutation at all.
- ``StdioRuntimeProvider`` records one caller-supplied process invocation;
  process ownership begins when the app-server transport connects.
- ``SshRuntimeProvider`` manages a remote daemon lifecycle or connects to an
  externally managed remote socket, reusing WebSocket-over-stdio transport.

Core execution sees an :class:`AppServerEndpoint` and an immutable
:class:`RuntimePolicy`; transport targets remain opaque outside this module.
"""

import asyncio
import json
import os
import shlex
import signal
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable
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
    cli_version: str | None = None
    socket_path: Path | None = None


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


SSH_SUBPROCESS_TIMEOUT = 15.0


def validate_absolute_posix_path(value: str, option: str = "path") -> str:
    """Validate a path that will be interpreted by a POSIX remote."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise UsageError(f"--{option} must be a non-empty absolute POSIX path")
    if not value.startswith("/"):
        raise UsageError(f"--{option} must be an absolute POSIX path")
    return value


def validate_ssh_destination(destination: str) -> str:
    """Apply only the local safety checks required for an OpenSSH destination."""
    if not isinstance(destination, str) or not destination:
        raise UsageError("--ssh requires a non-empty destination")
    if "\x00" in destination:
        raise UsageError("--ssh destination must not contain NUL")
    if destination.startswith("-"):
        raise UsageError("--ssh destination must not start with '-'")
    return destination


_SSH_VALUE_OPTIONS = frozenset("B b c D e E F I i J L l m O o p Q R S W w".split())
_SSH_SESSION_OPTIONS = frozenset(
    {"batchmode", "requesttty", "stdinnull", "remotecommand", "sessiontype"}
)
_SSH_FORBIDDEN_SHORT_OPTIONS = frozenset("tnNsfW")


def _ssh_option_setting(token: str) -> tuple[str, str] | None:
    if not token.startswith("-o"):
        return None
    setting = token[2:]
    if setting.startswith("="):
        setting = setting[1:]
    if not setting or "=" not in setting:
        raise UsageError(
            "--ssh-arg options requiring a value must attach it to the same token"
        )
    key, value = setting.split("=", 1)
    return key.casefold(), value


def validate_ssh_arg(token: str) -> str:
    """Validate one complete, non-session-shaping OpenSSH option token."""
    if (
        not isinstance(token, str)
        or not token.startswith("-")
        or token.startswith("--")
        or token == "-"
    ):
        raise UsageError("each --ssh-arg must be one complete OpenSSH option token")
    if "\x00" in token:
        raise UsageError("--ssh-arg must not contain NUL")

    setting = _ssh_option_setting(token)
    if setting is not None:
        key, _value = setting
        if key in _SSH_SESSION_OPTIONS:
            raise UsageError(f"--ssh-arg cannot set SSH session option {key}")
        return token

    short = token[1:]
    if short and short[0] in _SSH_FORBIDDEN_SHORT_OPTIONS:
        raise UsageError("--ssh-arg cannot alter the SSH session or data channel")
    if short and short[0] in _SSH_VALUE_OPTIONS:
        if len(short) == 1:
            raise UsageError(
                "--ssh-arg options requiring a value must attach it to the same token"
            )
        return token
    if any(option in _SSH_FORBIDDEN_SHORT_OPTIONS for option in short):
        raise UsageError("--ssh-arg cannot alter the SSH session or data channel")
    return token


def validate_remote_codex(executable: str) -> str:
    """Validate the single remote executable token supported by SSH v1."""
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise UsageError("--remote-codex must be one executable name or absolute path")
    if executable.startswith("~"):
        raise UsageError("--remote-codex must not use '~'")
    if "/" in executable and not executable.startswith("/"):
        raise UsageError("--remote-codex must be an executable name or absolute path")
    if not executable.startswith("/"):
        if any(character.isspace() for character in executable):
            raise UsageError("--remote-codex must not contain extra arguments")
        return executable

    # An absolute POSIX path is one opaque value, so spaces and shell
    # metacharacters in its components are valid and quote_remote_command()
    # protects them.  An option-looking token after whitespace is instead an
    # unambiguous command-string suffix (for example ``/opt/codex --version``).
    if any(
        executable[index + 1 :].lstrip().startswith("-")
        for index, character in enumerate(executable)
        if character.isspace()
    ):
        raise UsageError("--remote-codex must not contain extra arguments")
    return executable


def quote_remote_command(*tokens: str) -> str:
    """Build one POSIX-shell command for OpenSSH's remote command argument."""
    return " ".join(shlex.quote(token) for token in tokens)


class LifecycleOwnership(StrEnum):
    """Whether a runtime owns the app-server lifecycle."""

    MANAGED = "managed"
    EXTERNAL = "external"


@dataclass(frozen=True)
class RuntimePolicy:
    """Immutable behavioral capabilities of a runtime provider.

    ``default_cwd`` is captured by local providers when they are created. A
    remote provider can leave it unset (or provide a remote-specific value)
    without making core infer a local working directory. ``lifecycle`` is
    intentionally independent from the provider's public ``mode`` identity;
    the capability fields likewise describe behavior without naming a mode.
    """

    default_cwd: str | None
    lifecycle: LifecycleOwnership
    supports_rollout_enrichment: bool
    require_explicit_cwd: bool = False
    supports_remote_socket_metadata: bool = False
    cwd_validator: Callable[[str], str] | None = field(
        default=None, compare=False, repr=False
    )

    def resolve_cwd(self, requested: str | None) -> str | None:
        """Resolve an optional command cwd using this runtime's policy."""
        if requested is None and self.require_explicit_cwd:
            raise UsageError("this runtime requires an explicit cwd")
        resolved = self.default_cwd if requested is None else requested
        if resolved is not None and self.cwd_validator is not None:
            return self.cwd_validator(resolved)
        return resolved

    @property
    def lifecycle_ownership(self) -> LifecycleOwnership:
        """Descriptive alias for callers that prefer the full field name."""
        return self.lifecycle

    @property
    def supports_context_usage_enrichment(self) -> bool:
        """Alias for the rollout capability's observable purpose."""
        return self.supports_rollout_enrichment


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

    @property
    def policy(self) -> RuntimePolicy: ...

    async def resolve_endpoint(self) -> AppServerEndpoint: ...

    async def probe_cli_version(self) -> str | None:
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


def _local_runtime_policy(
    lifecycle: LifecycleOwnership, *, supports_rollout_enrichment: bool
) -> RuntimePolicy:
    return RuntimePolicy(
        default_cwd=str(Path.cwd()),
        lifecycle=lifecycle,
        supports_rollout_enrichment=supports_rollout_enrichment,
    )


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
        self._policy = _local_runtime_policy(
            LifecycleOwnership.MANAGED, supports_rollout_enrichment=True
        )

    @property
    def policy(self) -> RuntimePolicy:
        return self._policy

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
            socket_path=socket_path,
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
            cli_version=payload.get("cliVersion"),
            socket_path=Path(socket_path),
        )

    async def probe_cli_version(self) -> str | None:
        """Best-effort ``codex --version`` probe against the managed binary."""
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._codex_bin,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.communicate()
            return None
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        output = (stdout or stderr).decode(errors="replace").strip()
        return output.splitlines()[0] if output else None


async def _terminate_subprocess(proc: asyncio.subprocess.Process) -> None:
    """Terminate a dedicated subprocess group within a finite deadline."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError, PermissionError, OSError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), 1.0)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError, PermissionError, OSError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), 1.0)
    except asyncio.TimeoutError, ProcessLookupError, OSError:
        pass


async def _launch_ssh_process(
    argv: tuple[str, ...],
) -> asyncio.subprocess.Process:
    """Launch SSH while retaining a bounded cancellation cleanup path."""
    launch_task = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    )
    try:
        return await asyncio.shield(launch_task)
    except asyncio.CancelledError:
        try:
            proc = await asyncio.wait_for(asyncio.shield(launch_task), 0.1)
        except asyncio.TimeoutError:
            launch_task.add_done_callback(_cleanup_late_ssh_process)
        except BaseException:
            pass
        else:
            await asyncio.shield(_terminate_subprocess(proc))
        raise


def _cleanup_late_ssh_process(
    launch_task: asyncio.Task[asyncio.subprocess.Process],
) -> None:
    try:
        proc = launch_task.result()
    except BaseException:
        return
    cleanup_task = asyncio.create_task(_terminate_subprocess(proc))
    cleanup_task.add_done_callback(_consume_ssh_task)


def _consume_ssh_task(task: asyncio.Task[object]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _run_bounded_ssh(
    argv: tuple[str, ...], *, timeout: float | None = None
) -> tuple[int, bytes, bytes]:
    """Run a finite SSH command with stdout/stderr kept as separate streams."""
    timeout = SSH_SUBPROCESS_TIMEOUT if timeout is None else timeout
    proc: asyncio.subprocess.Process | None = None
    try:
        async with asyncio.timeout(timeout):
            proc = await _launch_ssh_process(argv)
            stdout, stderr = await proc.communicate()
    except asyncio.TimeoutError as exc:
        if proc is not None:
            await _terminate_subprocess(proc)
        raise CodexCtlError(
            ErrorCode.APP_SERVER_UNAVAILABLE,
            "SSH command timed out",
            cause=exc,
        ) from exc
    except asyncio.CancelledError:
        if proc is not None:
            await asyncio.shield(_terminate_subprocess(proc))
        raise
    except (OSError, ValueError) as exc:
        if proc is not None and proc.returncode is None:
            await _terminate_subprocess(proc)
        raise CodexCtlError(
            ErrorCode.APP_SERVER_UNAVAILABLE,
            "cannot start SSH command",
            cause=exc,
        ) from exc
    assert proc is not None
    return proc.returncode or 0, stdout, stderr


class SshRuntimeProvider:
    """Resolves a remote shared runtime through OpenSSH and stdio WebSockets."""

    mode = "ssh"

    def __init__(
        self,
        destination: str,
        ssh_args: tuple[str, ...] = (),
        remote_codex: str | None = None,
        remote_socket: str | None = None,
        ssh_bin: str = "ssh",
    ) -> None:
        self._destination = validate_ssh_destination(destination)
        self._ssh_args = tuple(validate_ssh_arg(arg) for arg in ssh_args)
        if remote_socket is not None and remote_codex is not None:
            raise UsageError("--remote-codex cannot be used with --remote-socket")
        self._remote_codex = validate_remote_codex(
            "codex" if remote_codex is None else remote_codex
        )
        self._remote_socket = (
            validate_absolute_posix_path(remote_socket, "remote-socket")
            if remote_socket is not None
            else None
        )
        if not ssh_bin or "\x00" in ssh_bin:
            raise UsageError(
                "SSH executable must be non-empty and must not contain NUL"
            )
        self._ssh_bin = ssh_bin
        self._policy = RuntimePolicy(
            default_cwd=None,
            lifecycle=(
                LifecycleOwnership.EXTERNAL
                if self._remote_socket is not None
                else LifecycleOwnership.MANAGED
            ),
            supports_rollout_enrichment=False,
            require_explicit_cwd=True,
            supports_remote_socket_metadata=True,
            cwd_validator=lambda value: validate_absolute_posix_path(value, "cwd"),
        )

    @property
    def policy(self) -> RuntimePolicy:
        return self._policy

    @property
    def destination(self) -> str:
        return self._destination

    @property
    def remote_socket(self) -> str | None:
        return self._remote_socket

    def _ssh_argv(self, remote_command: str) -> tuple[str, ...]:
        # Keep codexctl's invariants first. Duplicate explicit forms are
        # harmless, but removing them keeps the generated invocation exact.
        user_args = tuple(
            arg
            for arg in self._ssh_args
            if arg != "-T" and arg.casefold() != "-obatchmode=yes"
        )
        return (
            self._ssh_bin,
            "-T",
            "-oBatchMode=yes",
            *user_args,
            self._destination,
            remote_command,
        )

    async def _run_remote(self, *remote_tokens: str) -> tuple[int, bytes, bytes]:
        return await _run_bounded_ssh(
            self._ssh_argv(quote_remote_command(*remote_tokens))
        )

    async def resolve_endpoint(self) -> AppServerEndpoint:
        lifecycle_pid: int | None = None
        runtime_version: str | None = None
        cli_version: str | None = None
        if self._remote_socket is None:
            return_code, stdout, stderr = await self._run_remote(
                self._remote_codex, "app-server", "daemon", "start"
            )
            if return_code != 0:
                detail = stderr.decode(errors="replace").strip()
                raise CodexCtlError(
                    ErrorCode.APP_SERVER_UNAVAILABLE,
                    "remote codex app-server daemon start failed"
                    + (f": {detail}" if detail else ""),
                )
            payload = _parse_ssh_lifecycle_json(stdout)
            socket_text = payload["socketPath"]
            socket_path = Path(socket_text)
            raw_pid = payload.get("pid")
            if isinstance(raw_pid, int) and not isinstance(raw_pid, bool):
                lifecycle_pid = raw_pid
            raw_runtime_version = payload.get("appServerVersion")
            if isinstance(raw_runtime_version, str) and raw_runtime_version:
                runtime_version = raw_runtime_version
            raw_cli_version = payload.get("cliVersion")
            if isinstance(raw_cli_version, str) and raw_cli_version:
                cli_version = raw_cli_version
        else:
            socket_text = self._remote_socket
            socket_path = Path(socket_text)

        proxy_command = quote_remote_command(
            self._remote_codex,
            "app-server",
            "proxy",
            "--sock",
            socket_text,
        )
        return AppServerEndpoint(
            display=f"ssh:{self._destination}",
            target=StdioTarget(self._ssh_argv(proxy_command), StdioFraming.WEBSOCKET),
            runtime_pid=lifecycle_pid,
            runtime_version=runtime_version,
            cli_version=cli_version,
            socket_path=socket_path,
        )

    async def probe_cli_version(self) -> str | None:
        """Best-effort version probe for managed SSH lifecycle ownership."""
        if self._remote_socket is not None:
            return None
        try:
            return_code, stdout, stderr = await self._run_remote(
                self._remote_codex, "--version"
            )
        except CodexCtlError, OSError:
            return None
        if return_code != 0:
            return None
        output = (stdout or stderr).decode(errors="replace").strip()
        return output.splitlines()[0] if output else None


def _parse_ssh_lifecycle_json(stdout: bytes) -> dict[str, Any]:
    """Parse the complete, single-object SSH daemon lifecycle response."""

    def reject_json_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        parsed = json.loads(
            stdout.decode(errors="strict"), parse_constant=reject_json_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CodexCtlError(
            ErrorCode.INCOMPATIBLE_CODEX,
            "remote codex daemon start did not produce one JSON object",
            cause=exc,
        ) from exc
    if not isinstance(parsed, dict):
        raise CodexCtlError(
            ErrorCode.INCOMPATIBLE_CODEX,
            "remote codex daemon start did not produce one JSON object",
        )
    status = parsed.get("status")
    if status not in {"started", "alreadyRunning"}:
        raise CodexCtlError(
            ErrorCode.INCOMPATIBLE_CODEX,
            "remote codex daemon lifecycle response has an unsupported status",
        )
    socket_path = parsed.get("socketPath")
    if not isinstance(socket_path, str):
        raise CodexCtlError(
            ErrorCode.INCOMPATIBLE_CODEX,
            "remote codex daemon lifecycle response has an invalid socketPath",
        )
    try:
        validate_absolute_posix_path(socket_path, "socketPath")
    except UsageError as exc:
        raise CodexCtlError(
            ErrorCode.INCOMPATIBLE_CODEX,
            "remote codex daemon lifecycle response has an invalid socketPath",
            cause=exc,
        ) from exc
    return parsed


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
        self._policy = _local_runtime_policy(
            LifecycleOwnership.EXTERNAL, supports_rollout_enrichment=False
        )

    @property
    def policy(self) -> RuntimePolicy:
        return self._policy

    async def resolve_endpoint(self) -> AppServerEndpoint:
        target = self._endpoint.target
        if isinstance(target, UnixSocketTarget) and not target.path.exists():
            raise CodexCtlError(
                ErrorCode.APP_SERVER_UNAVAILABLE,
                f"external app-server socket does not exist: {target.path}",
            )
        return self._endpoint

    async def probe_cli_version(self) -> str | None:
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
        self._policy = _local_runtime_policy(
            LifecycleOwnership.EXTERNAL, supports_rollout_enrichment=False
        )

    @property
    def policy(self) -> RuntimePolicy:
        return self._policy

    async def resolve_endpoint(self) -> AppServerEndpoint:
        # Resolution is deliberately side-effect free. The connection layer
        # owns spawning and cleanup so every operation gets one fresh process.
        return AppServerEndpoint(display="stdio", target=self._target)

    async def probe_cli_version(self) -> str | None:
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
        return AppServerEndpoint(
            display=endpoint,
            target=UnixSocketTarget(path),
            socket_path=path,
        )

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

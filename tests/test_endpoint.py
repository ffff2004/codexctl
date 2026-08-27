"""Endpoint resolution: daemon lifecycle JSON parsing and external endpoints."""

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from codexctl.endpoint import (
    ExternalRuntimeProvider,
    LifecycleOwnership,
    ManagedRuntimeProvider,
    RuntimePolicy,
    SshRuntimeProvider,
    StdioFraming,
    StdioRuntimeProvider,
    StdioTarget,
    UnixSocketTarget,
    WebSocketTarget,
    default_control_socket_path,
)
from codexctl.model import CodexCtlError, ErrorCode


def _write_script(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-codex.sh"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


class TestManagedRuntimeLifecycle:
    async def test_picks_last_json_line(self, tmp_path):
        script = _write_script(
            tmp_path,
            "echo 'starting daemon... '\n"
            'echo \'{"status":"ignored"}\'\n'
            "echo 'some log noise'\n"
            'echo \'{"status":"started","socketPath":"/x.sock","pid":42}\'',
        )

        endpoint = await ManagedRuntimeProvider(
            codex_bin=script, home=tmp_path / "home"
        ).resolve_endpoint()

        assert endpoint.target == UnixSocketTarget(Path("/x.sock"))
        assert endpoint.runtime_pid == 42

    async def test_none_when_no_json(self, tmp_path):
        script = _write_script(tmp_path, "echo 'just logs'")
        provider = ManagedRuntimeProvider(codex_bin=script, home=tmp_path / "home")

        with pytest.raises(CodexCtlError) as excinfo:
            await provider.resolve_endpoint()

        assert excinfo.value.code == ErrorCode.INCOMPATIBLE_CODEX

    async def test_skips_malformed_json(self, tmp_path):
        script = _write_script(
            tmp_path,
            'echo \'{broken\'\necho \'{"status":"started","socketPath":"/x.sock"}\'',
        )

        endpoint = await ManagedRuntimeProvider(
            codex_bin=script, home=tmp_path / "home"
        ).resolve_endpoint()

        assert endpoint.target == UnixSocketTarget(Path("/x.sock"))


class TestManagedRuntimeProvider:
    def test_policy_is_immutable(self):
        provider = ManagedRuntimeProvider()

        with pytest.raises(FrozenInstanceError):
            provider.policy.default_cwd = "/changed"  # type: ignore[misc]

    async def test_daemon_start_parses_lifecycle_json(self, tmp_path):
        home = tmp_path / "home"
        script = _write_script(
            tmp_path,
            "echo 'starting'\n"
            'echo \'{"status":"started","socketPath":"/tmp/fake.sock",'
            '"pid":4242,"appServerVersion":"0.101.0",'
            '"cliVersion":"codex-cli 0.101.0"}\'',
        )
        provider = ManagedRuntimeProvider(codex_bin=script, home=home)
        endpoint = await provider.resolve_endpoint()
        assert endpoint.target == UnixSocketTarget(Path("/tmp/fake.sock"))
        assert endpoint.runtime_pid == 4242
        assert endpoint.runtime_version == "0.101.0"
        assert endpoint.cli_version == "codex-cli 0.101.0"
        assert endpoint.socket_path == Path("/tmp/fake.sock")
        assert provider.policy == RuntimePolicy(
            default_cwd=str(Path.cwd()),
            lifecycle=LifecycleOwnership.MANAGED,
            supports_rollout_enrichment=True,
        )

    async def test_already_running_status_also_yields_endpoint(self, tmp_path):
        home = tmp_path / "home"
        payload = json.dumps(
            {"status": "alreadyRunning", "socketPath": "/tmp/r.sock", "pid": 7}
        )
        script = _write_script(tmp_path, f"echo '{payload}'")
        provider = ManagedRuntimeProvider(codex_bin=script, home=home)
        endpoint = await provider.resolve_endpoint()
        assert endpoint.target == UnixSocketTarget(Path("/tmp/r.sock"))
        assert endpoint.runtime_pid == 7

    async def test_no_json_means_incompatible_codex(self, tmp_path):
        script = _write_script(tmp_path, "echo 'ancient codex without lifecycle json'")
        provider = ManagedRuntimeProvider(codex_bin=script, home=tmp_path / "home")
        with pytest.raises(CodexCtlError) as excinfo:
            await provider.resolve_endpoint()
        assert excinfo.value.code == ErrorCode.INCOMPATIBLE_CODEX

    async def test_missing_socket_path_means_incompatible_codex(self, tmp_path):
        script = _write_script(tmp_path, 'echo \'{"status":"started"}\'')
        provider = ManagedRuntimeProvider(codex_bin=script, home=tmp_path / "home")
        with pytest.raises(CodexCtlError) as excinfo:
            await provider.resolve_endpoint()
        assert excinfo.value.code == ErrorCode.INCOMPATIBLE_CODEX

    async def test_nonzero_exit_means_unavailable(self, tmp_path):
        script = _write_script(tmp_path, "echo 'boom' 1>&2\nexit 1")
        provider = ManagedRuntimeProvider(codex_bin=script, home=tmp_path / "home")
        with pytest.raises(CodexCtlError) as excinfo:
            await provider.resolve_endpoint()
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_missing_binary_means_unavailable(self, tmp_path):
        provider = ManagedRuntimeProvider(
            codex_bin=str(tmp_path / "does-not-exist"), home=tmp_path / "home"
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await provider.resolve_endpoint()
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_codex_bin_from_environment(self, monkeypatch, tmp_path):
        script = _write_script(tmp_path, "echo 'codex-cli from environment'")
        monkeypatch.setenv("CODEXCTL_CODEX_BIN", script)
        provider = ManagedRuntimeProvider()
        assert await provider.probe_cli_version() == "codex-cli from environment"

    async def test_probe_cli_version_reads_first_output_line(self, tmp_path):
        script = _write_script(tmp_path, "echo 'codex-cli 0.101.0'\necho 'noise'")
        provider = ManagedRuntimeProvider(codex_bin=script, home=tmp_path / "home")
        assert await provider.probe_cli_version() == "codex-cli 0.101.0"

    async def test_probe_cli_version_none_when_binary_missing(self, tmp_path):
        provider = ManagedRuntimeProvider(
            codex_bin=str(tmp_path / "does-not-exist"), home=tmp_path / "home"
        )
        assert await provider.probe_cli_version() is None

    async def test_probe_cli_version_none_on_nonzero_exit(self, tmp_path):
        script = _write_script(tmp_path, "exit 1")
        provider = ManagedRuntimeProvider(codex_bin=script, home=tmp_path / "home")
        assert await provider.probe_cli_version() is None


class TestExternalRuntimeProvider:
    async def test_missing_socket_is_unavailable(self, tmp_path):
        provider = ExternalRuntimeProvider(f"unix://{tmp_path / 'missing.sock'}")
        with pytest.raises(CodexCtlError) as excinfo:
            await provider.resolve_endpoint()
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_existing_socket_resolves_without_lifecycle(self, tmp_path):
        socket_path = tmp_path / "external.sock"
        socket_path.touch()
        provider = ExternalRuntimeProvider(f"unix://{socket_path}")
        endpoint = await provider.resolve_endpoint()
        assert endpoint.target == UnixSocketTarget(socket_path)
        assert endpoint.runtime_pid is None
        assert provider.mode == "external"
        assert provider.policy.lifecycle is LifecycleOwnership.EXTERNAL
        assert provider.policy.supports_rollout_enrichment is False

    async def test_probe_cli_version_is_none(self, tmp_path):
        provider = ExternalRuntimeProvider(f"unix://{tmp_path / 'external.sock'}")
        assert await provider.probe_cli_version() is None

    async def test_tcp_endpoint_preserves_path_query_and_defers_token_read(
        self, tmp_path
    ):
        token_file = tmp_path / "token"
        provider = ExternalRuntimeProvider(
            "ws://127.0.0.1:7777/app?version=1", token_file
        )
        endpoint = await provider.resolve_endpoint()
        assert endpoint.target == WebSocketTarget(
            "ws://127.0.0.1:7777/app?version=1", token_file
        )

    @pytest.mark.parametrize(
        "value",
        [
            "unix:/tmp/app-server.sock",
            "unix:////tmp/app-server.sock",
            "unix://relative/path",
            "unix://host/path",
            "unix:///tmp/app-server.sock?mode=test",
            "unix:///tmp/app-server.sock?",
            "unix:///tmp/app-server.sock#fragment",
            "unix:///tmp/app-server.sock#",
            "ws://host",
            "wss://host:443",
            "http://host:80",
            "ws://user@host:80",
            "ws://host:80/#fragment",
            "ws://host:80/#",
        ],
    )
    def test_invalid_endpoint_is_usage_error(self, value):
        with pytest.raises(CodexCtlError) as excinfo:
            ExternalRuntimeProvider(value)
        assert excinfo.value.code == ErrorCode.USAGE_ERROR

    @pytest.mark.parametrize(
        "query",
        [
            "token=secret",
            "token",
            "token=",
            "access_token=secret",
            "ACCESS_TOKEN=secret",
            "id_token=secret",
            "refresh_token=secret",
            "bearer_token=secret",
            "authorization=Bearer%20secret",
        ],
    )
    def test_ws_endpoint_rejects_url_credentials_without_reflecting_them(self, query):
        with pytest.raises(CodexCtlError) as excinfo:
            ExternalRuntimeProvider(f"ws://127.0.0.1:7777/app?{query}")
        assert excinfo.value.code == ErrorCode.USAGE_ERROR
        assert "secret" not in excinfo.value.message

    async def test_ws_endpoint_keeps_ordinary_query_parameters(self):
        provider = ExternalRuntimeProvider(
            "ws://127.0.0.1:7777/app?client=codexctl&trace=1"
        )
        endpoint = await provider.resolve_endpoint()
        assert endpoint.target == WebSocketTarget(
            "ws://127.0.0.1:7777/app?client=codexctl&trace=1", None
        )

    def test_token_file_is_rejected_for_unix_endpoint(self, tmp_path):
        with pytest.raises(CodexCtlError) as excinfo:
            ExternalRuntimeProvider("unix:///tmp/app-server.sock", tmp_path / "token")
        assert excinfo.value.code == ErrorCode.USAGE_ERROR


class TestStdioRuntimeProvider:
    async def test_resolves_exact_argv_without_starting_a_process(self):
        provider = StdioRuntimeProvider("app-server", ("--flag", "--", "value"))

        endpoint = await provider.resolve_endpoint()

        assert endpoint.display == "stdio"
        assert endpoint.target == StdioTarget(
            ("app-server", "--flag", "--", "value"), StdioFraming.JSONL
        )
        assert endpoint.runtime_pid is None
        assert provider.mode == "stdio"
        assert provider.policy.lifecycle is LifecycleOwnership.EXTERNAL
        assert provider.policy.supports_rollout_enrichment is False
        assert await provider.probe_cli_version() is None

    async def test_resolves_websocket_protocol_without_starting_a_process(self):
        provider = StdioRuntimeProvider(
            "codex", ("app-server", "proxy"), StdioFraming.WEBSOCKET
        )

        endpoint = await provider.resolve_endpoint()

        assert endpoint.target == StdioTarget(
            ("codex", "app-server", "proxy"), StdioFraming.WEBSOCKET
        )


class TestSshRuntimeProvider:
    async def test_managed_lifecycle_builds_quoted_proxy_argv(
        self, tmp_path, monkeypatch
    ):
        args_file = tmp_path / "args"
        script = _write_script(
            tmp_path,
            'printf \'%s\\n\' "$@" > "$SSH_ARGS_FILE"\n'
            'printf \'%s\' \'{"status":"started","socketPath":"/run/codex path/daemon;socket","pid":42,"cliVersion":"remote-cli","extra":true}\'',
        )
        provider = SshRuntimeProvider(
            "dev box",
            ("-Jbastion", "-p2222"),
            remote_codex="/opt/codex bin/codex",
            ssh_bin=script,
        )

        # The fake shell reads this through the inherited environment rather
        # than changing the provider's process contract.
        monkeypatch.setenv("SSH_ARGS_FILE", str(args_file))
        endpoint = await provider.resolve_endpoint()

        assert endpoint.display == "ssh:dev box"
        assert endpoint.target == StdioTarget(
            (
                script,
                "-T",
                "-oBatchMode=yes",
                "-Jbastion",
                "-p2222",
                "dev box",
                "'/opt/codex bin/codex' app-server proxy --sock '/run/codex path/daemon;socket'",
            ),
            StdioFraming.WEBSOCKET,
        )
        assert endpoint.runtime_pid == 42
        assert endpoint.cli_version == "remote-cli"
        assert endpoint.socket_path == Path("/run/codex path/daemon;socket")
        assert provider.policy.supports_remote_socket_metadata is True
        assert args_file.read_text(encoding="utf-8").splitlines() == [
            "-T",
            "-oBatchMode=yes",
            "-Jbastion",
            "-p2222",
            "dev box",
            "'/opt/codex bin/codex' app-server daemon start",
        ]

    async def test_lifecycle_stdout_is_one_strict_json_object(self, tmp_path):
        script = _write_script(
            tmp_path,
            'printf \'%s\\n\' \'{"status":"started","socketPath":"/one.sock"}\''
            '\'{"status":"started","socketPath":"/two.sock"}\'',
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await SshRuntimeProvider("host", ssh_bin=script).resolve_endpoint()
        assert excinfo.value.code == ErrorCode.INCOMPATIBLE_CODEX

    async def test_managed_cli_version_falls_back_to_remote_probe(self, tmp_path):
        script = _write_script(
            tmp_path,
            "last=''\n"
            'for value do last="$value"; done\n'
            'case "$last" in\n'
            "  *--version) echo 'codex-cli remote' ;;\n"
            '  *) echo \'{"status":"alreadyRunning","socketPath":"/run/codex.sock"}\' ;;\n'
            "esac",
        )
        provider = SshRuntimeProvider("host", ssh_bin=script)

        endpoint = await provider.resolve_endpoint()

        assert endpoint.cli_version is None
        assert await provider.probe_cli_version() == "codex-cli remote"
        assert provider.policy.lifecycle is LifecycleOwnership.MANAGED

    async def test_managed_lifecycle_failure_is_not_retried_or_bootstrapped(
        self, tmp_path, monkeypatch
    ):
        calls = tmp_path / "ssh-calls"
        script = _write_script(
            tmp_path,
            'printf "%s\\n" "$*" >> "$SSH_CALLS"\n'
            "echo 'remote daemon failed' >&2\n"
            "exit 1",
        )
        monkeypatch.setenv("SSH_CALLS", str(calls))

        with pytest.raises(CodexCtlError) as excinfo:
            await SshRuntimeProvider("host", ssh_bin=script).resolve_endpoint()

        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE
        recorded = calls.read_text(encoding="utf-8").splitlines()
        assert len(recorded) == 1
        assert "daemon start" in recorded[0]
        assert "bootstrap" not in recorded[0]

    async def test_ssh_lifecycle_timeout_is_bounded_and_unavailable(
        self, tmp_path, monkeypatch
    ):
        script = _write_script(tmp_path, "sleep 30")
        monkeypatch.setattr("codexctl.endpoint.SSH_SUBPROCESS_TIMEOUT", 0.05)

        with pytest.raises(CodexCtlError) as excinfo:
            await SshRuntimeProvider("host", ssh_bin=script).resolve_endpoint()

        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    @pytest.mark.parametrize(
        "arg",
        [
            "-t",
            "-tt",
            "-n",
            "-N",
            "-s",
            "-Wlocalhost:1",
            "-f",
            "-J",
            "-oRequestTTY=force",
            "-oStdinNull=yes",
            "-oRemoteCommand=echo nope",
            "-oSessionType=none",
            "-oBatchMode=no",
        ],
    )
    def test_rejects_session_shaping_or_split_ssh_args(self, arg):
        with pytest.raises(CodexCtlError) as excinfo:
            SshRuntimeProvider("host", (arg,))
        assert excinfo.value.code == ErrorCode.USAGE_ERROR

    @pytest.mark.parametrize("destination", ["", "-host", "host\x00name"])
    def test_destination_has_only_minimal_validation(self, destination):
        with pytest.raises(CodexCtlError) as excinfo:
            SshRuntimeProvider(destination)
        assert excinfo.value.code == ErrorCode.USAGE_ERROR

    @pytest.mark.parametrize(
        "executable", ["./codex", "bin/codex", "~/bin/codex", "codex --version"]
    )
    def test_remote_codex_accepts_only_one_name_or_absolute_path(self, executable):
        with pytest.raises(CodexCtlError) as excinfo:
            SshRuntimeProvider("host", remote_codex=executable)
        assert excinfo.value.code == ErrorCode.USAGE_ERROR

    @pytest.mark.parametrize(
        "executable",
        ["", "/opt/codex --version"],
    )
    def test_remote_codex_rejects_empty_and_absolute_command_strings(self, executable):
        with pytest.raises(CodexCtlError) as excinfo:
            SshRuntimeProvider("host", remote_codex=executable)
        assert excinfo.value.code == ErrorCode.USAGE_ERROR

    @pytest.mark.parametrize(
        "executable",
        ["codex", "/opt/codex bin/codex", "/opt/codex;v1/bin/codex"],
    )
    async def test_remote_codex_accepts_names_and_opaque_absolute_paths(
        self, executable, tmp_path, monkeypatch
    ):
        record = tmp_path / "ssh-args"
        script = _write_script(
            tmp_path,
            'printf "%s\\n" "$@" > "$SSH_ARGS_FILE"\necho \'codex-cli remote\'',
        )
        monkeypatch.setenv("SSH_ARGS_FILE", str(record))
        provider = SshRuntimeProvider("host", remote_codex=executable, ssh_bin=script)

        assert await provider.probe_cli_version() == "codex-cli remote"
        assert executable in record.read_text(encoding="utf-8")

    async def test_external_socket_skips_lifecycle_and_uses_socket(self, tmp_path):
        provider = SshRuntimeProvider(
            "host", remote_socket="/run/user/1000/codex.sock", ssh_bin="missing-ssh"
        )

        endpoint = await provider.resolve_endpoint()

        assert endpoint.target == StdioTarget(
            (
                "missing-ssh",
                "-T",
                "-oBatchMode=yes",
                "host",
                "codex app-server proxy --sock /run/user/1000/codex.sock",
            ),
            StdioFraming.WEBSOCKET,
        )
        assert provider.policy.lifecycle is LifecycleOwnership.EXTERNAL
        assert await provider.probe_cli_version() is None

    @pytest.mark.parametrize(
        "path", ["relative.sock", "~/codex.sock", "", "sock\x00path"]
    )
    def test_remote_socket_requires_absolute_posix_path(self, path):
        with pytest.raises(CodexCtlError) as excinfo:
            SshRuntimeProvider("host", remote_socket=path)
        assert excinfo.value.code == ErrorCode.USAGE_ERROR

    def test_remote_codex_and_socket_are_mutually_exclusive(self):
        with pytest.raises(CodexCtlError) as excinfo:
            SshRuntimeProvider(
                "host", remote_codex="/opt/codex", remote_socket="/run/codex.sock"
            )
        assert excinfo.value.code == ErrorCode.USAGE_ERROR


class TestDefaultControlSocketPath:
    def test_under_codex_home(self, tmp_path):
        path = default_control_socket_path(tmp_path)
        assert path == tmp_path / "app-server-control" / "app-server-control.sock"

    def test_honors_codex_home_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        path = default_control_socket_path()
        assert path == tmp_path / "app-server-control" / "app-server-control.sock"

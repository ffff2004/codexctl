"""Endpoint resolution: daemon lifecycle JSON parsing and external endpoints."""

import json
from pathlib import Path

import pytest

from codexctl.endpoint import (
    ExternalRuntimeProvider,
    ManagedRuntimeProvider,
    StdioFraming,
    StdioRuntimeProvider,
    StdioTarget,
    UnixSocketTarget,
    WebSocketTarget,
    _last_json_object,
    default_control_socket_path,
)
from codexctl.model import CodexCtlError, ErrorCode


def _write_script(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-codex.sh"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


class TestLastJsonObject:
    def test_picks_last_json_line(self):
        text = (
            "starting daemon...\n"
            '{"status":"ignored"}\n'
            "some log noise\n"
            '{"status":"started","socketPath":"/x.sock","pid":42}\n'
        )
        assert _last_json_object(text) == {
            "status": "started",
            "socketPath": "/x.sock",
            "pid": 42,
        }

    def test_none_when_no_json(self):
        assert _last_json_object("just logs\n") is None
        assert _last_json_object("") is None

    def test_skips_malformed_json(self):
        assert _last_json_object("{broken\n" + '{"ok": true}\n') == {"ok": True}


class TestManagedRuntimeProvider:
    async def test_daemon_start_parses_lifecycle_json(self, tmp_path):
        home = tmp_path / "home"
        script = _write_script(
            tmp_path,
            "echo 'starting'\n"
            'echo \'{"status":"started","socketPath":"/tmp/fake.sock",'
            '"pid":4242,"appServerVersion":"0.101.0"}\'',
        )
        provider = ManagedRuntimeProvider(codex_bin=script, home=home)
        endpoint = await provider.resolve_endpoint()
        assert endpoint.target == UnixSocketTarget(Path("/tmp/fake.sock"))
        assert endpoint.runtime_pid == 4242
        assert endpoint.runtime_version == "0.101.0"

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

    def test_codex_bin_from_environment(self, monkeypatch):
        monkeypatch.setenv("CODEXCTL_CODEX_BIN", "/opt/codex/bin/codex")
        provider = ManagedRuntimeProvider()
        assert provider._codex_bin == "/opt/codex/bin/codex"

    def test_probe_cli_version_reads_first_output_line(self, tmp_path):
        script = _write_script(tmp_path, "echo 'codex-cli 0.101.0'\necho 'noise'")
        provider = ManagedRuntimeProvider(codex_bin=script, home=tmp_path / "home")
        assert provider.probe_cli_version() == "codex-cli 0.101.0"

    def test_probe_cli_version_none_when_binary_missing(self, tmp_path):
        provider = ManagedRuntimeProvider(
            codex_bin=str(tmp_path / "does-not-exist"), home=tmp_path / "home"
        )
        assert provider.probe_cli_version() is None

    def test_probe_cli_version_none_on_nonzero_exit(self, tmp_path):
        script = _write_script(tmp_path, "exit 1")
        provider = ManagedRuntimeProvider(codex_bin=script, home=tmp_path / "home")
        assert provider.probe_cli_version() is None


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

    def test_probe_cli_version_is_none(self, tmp_path):
        provider = ExternalRuntimeProvider(f"unix://{tmp_path / 'external.sock'}")
        assert provider.probe_cli_version() is None

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

    def test_ws_endpoint_keeps_ordinary_query_parameters(self):
        endpoint = ExternalRuntimeProvider(
            "ws://127.0.0.1:7777/app?client=codexctl&trace=1"
        )._endpoint
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
        assert provider.probe_cli_version() is None

    async def test_resolves_websocket_protocol_without_starting_a_process(self):
        provider = StdioRuntimeProvider(
            "codex", ("app-server", "proxy"), StdioFraming.WEBSOCKET
        )

        endpoint = await provider.resolve_endpoint()

        assert endpoint.target == StdioTarget(
            ("codex", "app-server", "proxy"), StdioFraming.WEBSOCKET
        )


class TestDefaultControlSocketPath:
    def test_under_codex_home(self, tmp_path):
        path = default_control_socket_path(tmp_path)
        assert path == tmp_path / "app-server-control" / "app-server-control.sock"

    def test_honors_codex_home_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        path = default_control_socket_path()
        assert path == tmp_path / "app-server-control" / "app-server-control.sock"

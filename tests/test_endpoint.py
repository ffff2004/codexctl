"""Endpoint resolution: daemon lifecycle JSON parsing and external endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codexctl.endpoint import (
    ExternalEndpointAdapter,
    ManagedDaemonAdapter,
    TcpTarget,
    UnixTarget,
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


class TestManagedDaemonAdapter:
    async def test_daemon_start_parses_lifecycle_json(self, tmp_path):
        home = tmp_path / "home"
        script = _write_script(
            tmp_path,
            "echo 'starting'\n"
            'echo \'{"status":"started","socketPath":"/tmp/fake.sock",'
            '"pid":4242,"appServerVersion":"0.101.0"}\'',
        )
        adapter = ManagedDaemonAdapter(codex_bin=script, home=home)
        endpoint = await adapter.resolve()
        assert endpoint.target == UnixTarget(Path("/tmp/fake.sock"))
        assert endpoint.runtime_pid == 4242
        assert endpoint.runtime_version == "0.101.0"

    async def test_already_running_status_also_yields_endpoint(self, tmp_path):
        home = tmp_path / "home"
        payload = json.dumps(
            {"status": "alreadyRunning", "socketPath": "/tmp/r.sock", "pid": 7}
        )
        script = _write_script(tmp_path, f"echo '{payload}'")
        adapter = ManagedDaemonAdapter(codex_bin=script, home=home)
        endpoint = await adapter.resolve()
        assert endpoint.target == UnixTarget(Path("/tmp/r.sock"))
        assert endpoint.runtime_pid == 7

    async def test_no_json_means_incompatible_codex(self, tmp_path):
        script = _write_script(tmp_path, "echo 'ancient codex without lifecycle json'")
        adapter = ManagedDaemonAdapter(codex_bin=script, home=tmp_path / "home")
        with pytest.raises(CodexCtlError) as excinfo:
            await adapter.resolve()
        assert excinfo.value.code == ErrorCode.INCOMPATIBLE_CODEX

    async def test_missing_socket_path_means_incompatible_codex(self, tmp_path):
        script = _write_script(tmp_path, 'echo \'{"status":"started"}\'')
        adapter = ManagedDaemonAdapter(codex_bin=script, home=tmp_path / "home")
        with pytest.raises(CodexCtlError) as excinfo:
            await adapter.resolve()
        assert excinfo.value.code == ErrorCode.INCOMPATIBLE_CODEX

    async def test_nonzero_exit_means_unavailable(self, tmp_path):
        script = _write_script(tmp_path, "echo 'boom' 1>&2\nexit 1")
        adapter = ManagedDaemonAdapter(codex_bin=script, home=tmp_path / "home")
        with pytest.raises(CodexCtlError) as excinfo:
            await adapter.resolve()
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_missing_binary_means_unavailable(self, tmp_path):
        adapter = ManagedDaemonAdapter(
            codex_bin=str(tmp_path / "does-not-exist"), home=tmp_path / "home"
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await adapter.resolve()
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    def test_codex_bin_from_environment(self, monkeypatch):
        monkeypatch.setenv("CODEXCTL_CODEX_BIN", "/opt/codex/bin/codex")
        adapter = ManagedDaemonAdapter()
        assert adapter._codex_bin == "/opt/codex/bin/codex"

    def test_probe_cli_version_reads_first_output_line(self, tmp_path):
        script = _write_script(tmp_path, "echo 'codex-cli 0.101.0'\necho 'noise'")
        adapter = ManagedDaemonAdapter(codex_bin=script, home=tmp_path / "home")
        assert adapter.probe_cli_version() == "codex-cli 0.101.0"

    def test_probe_cli_version_none_when_binary_missing(self, tmp_path):
        adapter = ManagedDaemonAdapter(
            codex_bin=str(tmp_path / "does-not-exist"), home=tmp_path / "home"
        )
        assert adapter.probe_cli_version() is None

    def test_probe_cli_version_none_on_nonzero_exit(self, tmp_path):
        script = _write_script(tmp_path, "exit 1")
        adapter = ManagedDaemonAdapter(codex_bin=script, home=tmp_path / "home")
        assert adapter.probe_cli_version() is None


class TestExternalEndpointAdapter:
    async def test_missing_socket_is_unavailable(self, tmp_path):
        adapter = ExternalEndpointAdapter(f"unix://{tmp_path / 'missing.sock'}")
        with pytest.raises(CodexCtlError) as excinfo:
            await adapter.resolve()
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_existing_socket_resolves_without_lifecycle(self, tmp_path):
        socket_path = tmp_path / "external.sock"
        socket_path.touch()
        adapter = ExternalEndpointAdapter(f"unix://{socket_path}")
        endpoint = await adapter.resolve()
        assert endpoint.target == UnixTarget(socket_path)
        assert endpoint.runtime_pid is None
        assert adapter.mode == "external"

    def test_probe_cli_version_is_none(self, tmp_path):
        adapter = ExternalEndpointAdapter(f"unix://{tmp_path / 'external.sock'}")
        assert adapter.probe_cli_version() is None

    async def test_tcp_endpoint_preserves_path_query_and_defers_token_read(
        self, tmp_path
    ):
        token_file = tmp_path / "token"
        adapter = ExternalEndpointAdapter(
            "ws://127.0.0.1:7777/app?version=1", token_file
        )
        endpoint = await adapter.resolve()
        assert endpoint.target == TcpTarget(
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
            ExternalEndpointAdapter(value)
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
            ExternalEndpointAdapter(f"ws://127.0.0.1:7777/app?{query}")
        assert excinfo.value.code == ErrorCode.USAGE_ERROR
        assert "secret" not in excinfo.value.message

    def test_ws_endpoint_keeps_ordinary_query_parameters(self):
        endpoint = ExternalEndpointAdapter(
            "ws://127.0.0.1:7777/app?client=codexctl&trace=1"
        )._endpoint
        assert endpoint.target == TcpTarget(
            "ws://127.0.0.1:7777/app?client=codexctl&trace=1", None
        )

    def test_token_file_is_rejected_for_unix_endpoint(self, tmp_path):
        with pytest.raises(CodexCtlError) as excinfo:
            ExternalEndpointAdapter("unix:///tmp/app-server.sock", tmp_path / "token")
        assert excinfo.value.code == ErrorCode.USAGE_ERROR


class TestDefaultControlSocketPath:
    def test_under_codex_home(self, tmp_path):
        path = default_control_socket_path(tmp_path)
        assert path == tmp_path / "app-server-control" / "app-server-control.sock"

    def test_honors_codex_home_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        path = default_control_socket_path()
        assert path == tmp_path / "app-server-control" / "app-server-control.sock"

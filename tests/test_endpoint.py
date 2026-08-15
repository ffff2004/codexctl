"""Endpoint resolution: daemon lifecycle JSON parsing and external sockets."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codexctl.endpoint import (
    ExternalSocketAdapter,
    ManagedDaemonAdapter,
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
            "echo '{\"status\":\"started\",\"socketPath\":\"/tmp/fake.sock\","
            "\"pid\":4242,\"appServerVersion\":\"0.101.0\"}'",
        )
        adapter = ManagedDaemonAdapter(codex_bin=script, home=home)
        endpoint = await adapter.resolve()
        assert endpoint.socket_path == Path("/tmp/fake.sock")
        assert endpoint.runtime_pid == 4242
        assert endpoint.runtime_version == "0.101.0"

    async def test_already_running_status_also_yields_endpoint(self, tmp_path):
        home = tmp_path / "home"
        payload = json.dumps({"status": "alreadyRunning", "socketPath": "/tmp/r.sock", "pid": 7})
        script = _write_script(tmp_path, f"echo '{payload}'")
        adapter = ManagedDaemonAdapter(codex_bin=script, home=home)
        endpoint = await adapter.resolve()
        assert endpoint.socket_path == Path("/tmp/r.sock")
        assert endpoint.runtime_pid == 7

    async def test_no_json_means_incompatible_codex(self, tmp_path):
        script = _write_script(tmp_path, "echo 'ancient codex without lifecycle json'")
        adapter = ManagedDaemonAdapter(codex_bin=script, home=tmp_path / "home")
        with pytest.raises(CodexCtlError) as excinfo:
            await adapter.resolve()
        assert excinfo.value.code == ErrorCode.INCOMPATIBLE_CODEX

    async def test_missing_socket_path_means_incompatible_codex(self, tmp_path):
        script = _write_script(tmp_path, "echo '{\"status\":\"started\"}'")
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


class TestExternalSocketAdapter:
    async def test_missing_socket_is_unavailable(self, tmp_path):
        adapter = ExternalSocketAdapter(tmp_path / "missing.sock")
        with pytest.raises(CodexCtlError) as excinfo:
            await adapter.resolve()
        assert excinfo.value.code == ErrorCode.APP_SERVER_UNAVAILABLE

    async def test_existing_socket_resolves_without_lifecycle(self, tmp_path):
        socket_path = tmp_path / "external.sock"
        socket_path.touch()
        adapter = ExternalSocketAdapter(socket_path)
        endpoint = await adapter.resolve()
        assert endpoint.socket_path == socket_path
        assert endpoint.runtime_pid is None
        assert adapter.mode == "external"

    def test_probe_cli_version_is_none(self, tmp_path):
        adapter = ExternalSocketAdapter(tmp_path / "external.sock")
        assert adapter.probe_cli_version() is None


class TestDefaultControlSocketPath:
    def test_under_codex_home(self, tmp_path):
        path = default_control_socket_path(tmp_path)
        assert path == tmp_path / "app-server-control" / "app-server-control.sock"

    def test_honors_codex_home_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        path = default_control_socket_path()
        assert path == tmp_path / "app-server-control" / "app-server-control.sock"

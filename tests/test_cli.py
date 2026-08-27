"""CLI: public execution, output-mode matrix, usage errors, and exit mapping."""

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from codexctl.appserver import JsonlStdioMessageTransport
from codexctl.cli import (
    EXIT_DOMAIN,
    EXIT_OK,
    EXIT_RUNTIME,
    EXIT_TURN,
    EXIT_USAGE,
    build_parser,
    exit_code_for,
    main,
)
from codexctl.core import CodexCtl
from codexctl.endpoint import (
    StdioFraming,
    StdioTarget,
)
from codexctl.model import (
    ApprovalPolicy,
    ApprovalsReviewer,
    CodexCtlError,
    ErrorCode,
    Follow,
    HistorySnapshot,
    ListThreads,
    ReplayActiveTurn,
    Resume,
    SandboxPolicy,
    Start,
    Status,
    Steer,
)

_SUCCESSFUL_STDIO_SERVER = """\
import json
import sys


def send(value):
    print(json.dumps(value), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"userAgent": "stdio/1.0"}})
    elif method == "initialized":
        send({"method": "future/notification", "params": {}})
        send({"jsonrpc": "2.0", "futureField": True})
    elif method == "thread/start":
        send({"id": message["id"], "result": {"thread": {"id": "t1"}}})
    elif method == "turn/start":
        send({"id": message["id"], "result": {"turn": {"id": "u1"}}})
        send({
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "u1",
                "item": {"type": "agentMessage", "id": "i1", "text": "done"},
            },
        })
        send({
            "method": "turn/completed",
            "params": {"threadId": "t1", "turn": {"id": "u1", "status": "completed"}},
        })
    elif method == "thread/unsubscribe":
        send({"id": message["id"], "result": {}})
"""


def _argv_recording_stdio_server() -> str:
    return (
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[-1]).write_text(json.dumps(sys.argv[1:-1]))\n"
        + _SUCCESSFUL_STDIO_SERVER
    )


def stdio_cli_argv(endpoint, command_args, prompt=None):
    target = endpoint.target
    assert isinstance(target, StdioTarget)
    argv = [*command_args, "--stdio-exec", target.argv[0]]
    for value in target.argv[1:]:
        argv.extend(("--stdio-arg", value))
    if prompt is not None:
        argv.extend(("--", prompt))
    return argv


def capture_cli_command(monkeypatch):
    commands = []

    async def run(_ctl, command):
        commands.append(command)
        return HistorySnapshot(thread_id="t1")

    monkeypatch.setattr(CodexCtl, "run", run)
    return commands


class TestStdioExecution:
    def test_start_preserves_text_rendering(self, stdio_endpoint, capsys):
        endpoint = stdio_endpoint(_SUCCESSFUL_STDIO_SERVER, filename="render.py")

        code = main(stdio_cli_argv(endpoint, ["start"], "hello"))

        assert code == 0
        output = capsys.readouterr().out
        # Unified header: only the Thread line; the Turn marker is emitted
        # from the turn/started event (the first event for start/resume).
        assert output.startswith("Thread: t1\n\nTurn: u1\n")
        assert "[agent]\ndone\n" in output
        assert "Turn completed\n" in output

    def test_detach_returns_the_existing_json_document(self, stdio_endpoint, capsys):
        endpoint = stdio_endpoint(_SUCCESSFUL_STDIO_SERVER, filename="detach.py")

        code = main(
            stdio_cli_argv(endpoint, ["start", "--detach", "-o", "json"], "hello")
        )

        assert code == 0
        assert json.loads(capsys.readouterr().out) == {
            "threadId": "t1",
            "turnId": "u1",
            "detached": True,
        }

    def test_cleanup_failure_does_not_replace_successful_result(
        self, stdio_endpoint, capsys, monkeypatch
    ):
        endpoint = stdio_endpoint(_SUCCESSFUL_STDIO_SERVER, filename="cleanup.py")
        original_close = JsonlStdioMessageTransport.close

        async def close_then_fail(transport):
            await original_close(transport)
            raise RuntimeError("cleanup failed")

        monkeypatch.setattr(JsonlStdioMessageTransport, "close", close_then_fail)

        code = main(stdio_cli_argv(endpoint, ["start"], "hello"))

        assert code == 0
        assert "Turn completed" in capsys.readouterr().out

    async def test_live_ctrl_c_returns_130_without_turn_interrupt(self, tmp_path):
        server = tmp_path / "ctrl-c-server.py"
        marker = tmp_path / "messages.txt"
        server.write_text(
            "import json, pathlib, sys\n"
            "marker = pathlib.Path(sys.argv[1])\n"
            "def send(value):\n"
            "    print(json.dumps(value), flush=True)\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    method = message.get('method')\n"
            "    if method == 'initialize':\n"
            "        send({'id': message['id'], 'result': {'userAgent': 'stdio/1.0'}})\n"
            "    elif method == 'thread/start':\n"
            "        send({'id': message['id'], 'result': {'thread': {'id': 't1'}}})\n"
            "    elif method == 'turn/start':\n"
            "        marker.write_text('turn-start', encoding='utf-8')\n"
            "        send({'id': message['id'], 'result': {'turn': {'id': 'u1'}}})\n"
            "    elif method == 'thread/unsubscribe':\n"
            "        marker.write_text(marker.read_text() + '\\nunsubscribe', encoding='utf-8')\n"
            "        send({'id': message['id'], 'result': {}})\n"
            "    elif method == 'turn/interrupt':\n"
            "        marker.write_text(marker.read_text() + '\\ninterrupt', encoding='utf-8')\n"
            "        send({'id': message['id'], 'result': {}})\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        source_root = str(Path(__file__).parents[1] / "src")
        environment["PYTHONPATH"] = (
            source_root + os.pathsep + environment.get("PYTHONPATH", "")
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from codexctl.cli import main; raise SystemExit(main())",
                "start",
                "--stdio-exec",
                sys.executable,
                "--stdio-arg",
                str(server),
                "--stdio-arg",
                str(marker),
                "--",
                "hello",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            for _ in range(200):
                if marker.exists():
                    break
                await asyncio.sleep(0.01)
            else:
                process.kill()
                stdout, stderr = await asyncio.to_thread(process.communicate)
                pytest.fail(
                    "stdio command did not start its turn: "
                    f"returncode={process.returncode}, stdout={stdout!r}, "
                    f"stderr={stderr!r}"
                )

            process.send_signal(signal.SIGINT)
            stdout, stderr = await asyncio.to_thread(process.communicate, timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.communicate)
            raise

        assert process.returncode == 130, (stdout, stderr)
        assert "interrupt" not in marker.read_text(encoding="utf-8")


class TestPersistFollowExecution:
    def test_persist_flag_builds_follow_command(self, monkeypatch):
        commands = capture_cli_command(monkeypatch)

        assert main(["follow", "t1", "--persist", "--stdio-exec", "app"]) == EXIT_OK
        assert commands[-1] == Follow(
            thread_id="t1", replay=ReplayActiveTurn(), persist=True
        )

    def test_default_follow_is_not_persistent(self, monkeypatch):
        commands = capture_cli_command(monkeypatch)

        assert main(["follow", "t1", "--stdio-exec", "app"]) == EXIT_OK
        assert commands[-1] == Follow(thread_id="t1", replay=ReplayActiveTurn())

    def test_connection_loss_maps_to_protocol_error_exit_path(self, tmp_path, capsys):
        record = tmp_path / "methods"
        server = tmp_path / "connection-loss.py"
        server.write_text(
            "import json, pathlib, sys\n"
            "record = pathlib.Path(sys.argv[1])\n"
            "def send(value):\n"
            "    print(json.dumps(value), flush=True)\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    with record.open('a', encoding='utf-8') as stream:\n"
            "        stream.write(message.get('method', '') + '\\n')\n"
            "    if message.get('method') == 'initialize':\n"
            "        send({'id': message['id'], 'result': {'userAgent': 'stdio/1.0'}})\n"
            "    elif message.get('method') == 'thread/resume':\n"
            "        send({'id': message['id'], 'result': {'thread': {'id': 't1', "
            "'status': {'type': 'idle'}, 'turns': []}}})\n"
            "        break\n",
            encoding="utf-8",
        )

        code = main(
            [
                "follow",
                "t1",
                "--persist",
                "--stdio-exec",
                sys.executable,
                "--stdio-arg",
                str(server),
                "--stdio-arg",
                str(record),
            ]
        )

        assert code == EXIT_RUNTIME
        assert "APP_SERVER_PROTOCOL_ERROR" in capsys.readouterr().err
        assert "turn/interrupt" not in record.read_text(encoding="utf-8")


class TestOutputMatrixContract:
    """The matrix is the public contract from the interface specification."""

    def test_streaming_commands_accept_jsonl_foreground(self, monkeypatch):
        capture_cli_command(monkeypatch)
        for argv in (
            ["start", "--stdio-exec", "app", "-o", "jsonl", "--", "hello"],
            [
                "resume",
                "t1",
                "--stdio-exec",
                "app",
                "-o",
                "jsonl",
                "--",
                "hello",
            ],
            ["follow", "t1", "--stdio-exec", "app", "-o", "jsonl"],
        ):
            assert main(argv) == EXIT_OK
        for argv in (
            ["start", "--stdio-exec", "app", "-o", "json", "--", "hello"],
            [
                "resume",
                "t1",
                "--stdio-exec",
                "app",
                "-o",
                "json",
                "--",
                "hello",
            ],
            ["follow", "t1", "--stdio-exec", "app", "-o", "json"],
        ):
            assert main(argv) == EXIT_USAGE

    def test_detached_start_resume_accept_json_not_jsonl(self, monkeypatch):
        capture_cli_command(monkeypatch)
        for argv in (
            [
                "start",
                "--detach",
                "--stdio-exec",
                "app",
                "-o",
                "json",
                "--",
                "hello",
            ],
            [
                "resume",
                "t1",
                "--detach",
                "--stdio-exec",
                "app",
                "-o",
                "json",
                "--",
                "hello",
            ],
        ):
            assert main(argv) == EXIT_OK
        for argv in (
            [
                "start",
                "--detach",
                "--stdio-exec",
                "app",
                "-o",
                "jsonl",
                "--",
                "hello",
            ],
            [
                "resume",
                "t1",
                "--detach",
                "--stdio-exec",
                "app",
                "-o",
                "jsonl",
                "--",
                "hello",
            ],
        ):
            assert main(argv) == EXIT_USAGE

    def test_snapshot_commands_accept_json_not_jsonl(self, monkeypatch):
        capture_cli_command(monkeypatch)
        for argv in (
            ["status", "t1", "--stdio-exec", "app", "-o", "json"],
            ["steer", "t1", "--stdio-exec", "app", "-o", "json", "--", "hello"],
            ["interrupt", "t1", "--stdio-exec", "app", "-o", "json"],
            ["list", "--stdio-exec", "app", "-o", "json"],
            ["doctor", "--stdio-exec", "app", "-o", "json"],
        ):
            assert main(argv) == EXIT_OK
        for argv in (
            ["status", "t1", "--stdio-exec", "app", "-o", "jsonl"],
            ["steer", "t1", "--stdio-exec", "app", "-o", "jsonl", "--", "hello"],
            ["interrupt", "t1", "--stdio-exec", "app", "-o", "jsonl"],
            ["list", "--stdio-exec", "app", "-o", "jsonl"],
            ["doctor", "--stdio-exec", "app", "-o", "jsonl"],
        ):
            assert main(argv) == EXIT_USAGE

    def test_history_accepts_all_modes(self, monkeypatch):
        capture_cli_command(monkeypatch)
        for mode in ("text", "json", "jsonl"):
            assert main(["history", "t1", "--stdio-exec", "app", "-o", mode]) == EXIT_OK

    def test_text_always_allowed(self, monkeypatch):
        capture_cli_command(monkeypatch)
        commands = (
            ["start", "--stdio-exec", "app", "--", "hello"],
            ["resume", "t1", "--stdio-exec", "app", "--", "hello"],
            ["status", "t1", "--stdio-exec", "app"],
            ["history", "t1", "--stdio-exec", "app"],
            ["follow", "t1", "--stdio-exec", "app"],
            ["steer", "t1", "--stdio-exec", "app", "--", "hello"],
            ["interrupt", "t1", "--stdio-exec", "app"],
            ["list", "--stdio-exec", "app"],
            ["doctor", "--stdio-exec", "app"],
        )
        assert all(main(argv) == EXIT_OK for argv in commands)


class TestOutputModeRejection:
    @pytest.mark.parametrize(
        ("argv", "mode"),
        [
            (["start", "-o", "json", "--", "hello"], "json"),
            (["status", "t1", "--jsonl"], "jsonl"),
        ],
    )
    def test_structured_errors_use_stdout_for_requested_mode(self, argv, mode, capsys):
        assert main(argv) == EXIT_USAGE
        captured = capsys.readouterr()
        assert captured.err == ""

        document = json.loads(captured.out)
        if mode == "jsonl":
            assert document.pop("type") == "error"
        assert set(document) == {"error"}
        assert set(document["error"]) == {"code", "message"}
        assert document["error"]["code"] == "OUTPUT_MODE_NOT_SUPPORTED"

    def test_text_errors_stay_on_stderr(self, capsys):
        assert main(["start"]) == EXIT_USAGE
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "USAGE_ERROR" in captured.err

    def test_jsonl_for_detached_start_rejected(self, capsys):
        code = main(["start", "--detach", "--jsonl", "--", "hello"])
        assert code == EXIT_USAGE

    def test_json_error_document_for_jsonl_mode(self, capsys):
        # doctor -o jsonl is rejected before any runtime access.
        code = main(["doctor", "-o", "jsonl"])
        assert code == EXIT_USAGE
        captured = capsys.readouterr()
        assert captured.err == ""
        assert json.loads(captured.out)["error"]["code"] == (
            "OUTPUT_MODE_NOT_SUPPORTED"
        )


class TestUsageErrors:
    @pytest.mark.parametrize("argv", [["start"], ["resume", "t1"], ["steer", "t1"]])
    def test_missing_prompt(self, argv, capsys):
        assert main(argv) == EXIT_USAGE
        assert "USAGE_ERROR" in capsys.readouterr().err

    def test_invalid_turn_selector(self, capsys):
        assert main(["history", "t1", "--turns", "abc"]) == EXIT_USAGE
        assert "USAGE_ERROR" in capsys.readouterr().err

    def test_invalid_replay_selector(self, capsys):
        assert main(["follow", "t1", "--replay-turns", "5"]) == EXIT_USAGE
        assert "USAGE_ERROR" in capsys.readouterr().err

    def test_unknown_command(self, capsys):
        assert main(["frobnicate"]) == EXIT_USAGE

    def test_list_all_builds_global_list_command(self, monkeypatch):
        commands = capture_cli_command(monkeypatch)

        assert main(["list", "--all", "--stdio-exec", "app"]) == EXIT_OK
        assert commands[-1] == ListThreads(all_threads=True)

    def test_ssh_list_accepts_explicit_remote_cwd(self, monkeypatch):
        captured = {}

        class RecordingSshProvider:
            def __init__(self, destination, ssh_args, remote_codex, remote_socket):
                captured.update(
                    destination=destination,
                    ssh_args=ssh_args,
                    remote_codex=remote_codex,
                    remote_socket=remote_socket,
                )

        monkeypatch.setattr("codexctl.cli.SshRuntimeProvider", RecordingSshProvider)
        commands = capture_cli_command(monkeypatch)

        assert main(["list", "--ssh", "devbox", "--cwd", "/srv/repos/foo"]) == EXIT_OK
        assert commands[-1] == ListThreads(cwd="/srv/repos/foo")
        assert captured == {
            "destination": "devbox",
            "ssh_args": (),
            "remote_codex": None,
            "remote_socket": None,
        }

    @pytest.mark.parametrize(
        "argv",
        [
            ["start", "--ssh", "devbox", "--", "run tests"],
            ["list", "--ssh", "devbox"],
            ["start", "--ssh", "devbox", "--cwd", "relative", "--", "run"],
            [
                "list",
                "--ssh",
                "devbox",
                "--cwd",
                "~/repo",
                "--all",
            ],
        ],
    )
    def test_ssh_cwd_rules_are_usage_errors_before_connecting(self, argv, capsys):
        assert main(argv) == EXIT_USAGE
        assert "USAGE_ERROR" in capsys.readouterr().err

    def test_ssh_list_all_does_not_require_cwd(self, monkeypatch):
        captured = {}

        class RecordingSshProvider:
            def __init__(self, destination, ssh_args, remote_codex, remote_socket):
                captured.update(
                    destination=destination,
                    ssh_args=ssh_args,
                    remote_codex=remote_codex,
                    remote_socket=remote_socket,
                )

        monkeypatch.setattr("codexctl.cli.SshRuntimeProvider", RecordingSshProvider)
        commands = capture_cli_command(monkeypatch)

        assert main(["list", "--ssh", "devbox", "--all"]) == EXIT_OK
        assert commands[-1] == ListThreads(all_threads=True)
        assert captured["destination"] == "devbox"

    @pytest.mark.parametrize(
        "argv",
        [
            ["list", "--ssh-arg=-J"],
            ["list", "--remote-codex", "codex"],
            ["list", "--remote-socket", "/run/codex.sock"],
            ["list", "--ssh", "devbox", "--endpoint", "unix:///tmp/app.sock"],
            [
                "list",
                "--ssh",
                "devbox",
                "--remote-codex",
                "/opt/codex",
                "--remote-socket",
                "/run/codex.sock",
            ],
        ],
    )
    def test_ssh_selector_and_argument_combinations_are_usage_errors(
        self, argv, capsys
    ):
        assert main(argv) == EXIT_USAGE
        assert "USAGE_ERROR" in capsys.readouterr().err

    def test_explicit_empty_remote_codex_is_not_the_default(self, capsys):
        assert (
            main(
                [
                    "list",
                    "--ssh",
                    "devbox",
                    "--remote-codex",
                    "",
                    "--all",
                ]
            )
            == EXIT_USAGE
        )
        assert "USAGE_ERROR" in capsys.readouterr().err

    def test_ssh_args_preserve_one_token_order(self, monkeypatch):
        captured = {}

        class RecordingSshProvider:
            def __init__(self, destination, ssh_args, remote_codex, remote_socket):
                captured["destination"] = destination
                captured["ssh_args"] = ssh_args
                captured["remote_codex"] = remote_codex
                captured["remote_socket"] = remote_socket

        monkeypatch.setattr("codexctl.cli.SshRuntimeProvider", RecordingSshProvider)
        capture_cli_command(monkeypatch)

        assert (
            main(
                [
                    "list",
                    "--ssh",
                    "devbox",
                    "--ssh-arg=-Jbastion",
                    "--ssh-arg=-p2222",
                ]
            )
            == EXIT_OK
        )
        assert captured["destination"] == "devbox"
        assert captured["ssh_args"] == ("-Jbastion", "-p2222")

    def test_usage_errors_do_not_reuse_output_mode_code(self, capsys):
        # OUTPUT_MODE_NOT_SUPPORTED is reserved for the output-mode matrix;
        # general argument errors must carry USAGE_ERROR instead.
        assert main(["start"]) == EXIT_USAGE
        assert "OUTPUT_MODE_NOT_SUPPORTED" not in capsys.readouterr().err

    def test_json_usage_error_document_code(self, capsys):
        # steer in json mode without input: the usage error is rendered as
        # one JSON document whose code is USAGE_ERROR, exit stays 2.
        assert main(["steer", "t1", "-o", "json"]) == EXIT_USAGE
        captured = capsys.readouterr()
        assert '"code": "USAGE_ERROR"' in captured.out

    def test_version_flag(self, capsys):
        assert main(["--version"]) == EXIT_OK
        assert "codexctl" in capsys.readouterr().out

    def test_endpoint_token_file_requires_endpoint(self, capsys):
        assert main(["list", "--endpoint-token-file", "/tmp/token"]) == EXIT_USAGE
        assert "USAGE_ERROR" in capsys.readouterr().err

    def test_stdio_arg_accepts_dash_prefixed_values_and_preserves_order(
        self, monkeypatch
    ):
        captured = {}

        class RecordingStdioProvider:
            def __init__(self, executable, args, framing):
                captured["executable"] = executable
                captured["args"] = args
                captured["framing"] = framing

        monkeypatch.setattr("codexctl.cli.StdioRuntimeProvider", RecordingStdioProvider)
        capture_cli_command(monkeypatch)

        assert (
            main(
                [
                    "list",
                    "--stdio-exec",
                    "app-server",
                    "--stdio-arg",
                    "--child-flag",
                    "--stdio-arg",
                    "value",
                ]
            )
            == EXIT_OK
        )
        assert captured == {
            "executable": "app-server",
            "args": ("--child-flag", "value"),
            "framing": StdioFraming.JSONL,
        }

    def test_stdio_websocket_protocol_is_recorded_on_the_target(self, monkeypatch):
        captured = {}

        class RecordingStdioProvider:
            def __init__(self, executable, args, framing):
                captured["executable"] = executable
                captured["args"] = args
                captured["framing"] = framing

        monkeypatch.setattr("codexctl.cli.StdioRuntimeProvider", RecordingStdioProvider)
        capture_cli_command(monkeypatch)

        assert (
            main(
                [
                    "list",
                    "--stdio-exec",
                    "codex",
                    "--stdio-framing",
                    "websocket",
                    "--stdio-arg",
                    "app-server",
                    "--stdio-arg",
                    "proxy",
                ]
            )
            == EXIT_OK
        )
        assert captured == {
            "executable": "codex",
            "args": ("app-server", "proxy"),
            "framing": StdioFraming.WEBSOCKET,
        }

    def test_stdio_websocket_protocol_requires_an_executable(self, capsys):
        assert main(["list", "--stdio-framing", "websocket"]) == EXIT_USAGE
        assert "USAGE_ERROR" in capsys.readouterr().err

    def test_stdio_websocket_protocol_is_not_silently_external(self, capsys):
        assert (
            main(
                [
                    "list",
                    "--stdio-framing",
                    "websocket",
                    "--endpoint",
                    "ws://localhost:1",
                ]
            )
            == EXIT_USAGE
        )
        assert "USAGE_ERROR" in capsys.readouterr().err

    def test_stdio_exec_help_describes_both_protocols(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["list", "--help"])
        help_text = capsys.readouterr().out
        assert "selected stdio framing" in help_text
        assert "newline-delimited JSON" not in help_text

    def test_stdio_literal_double_dash_can_precede_prompt_delimiter(
        self, tmp_path, stdio_endpoint
    ):
        record = tmp_path / "stdio-argv.json"
        endpoint = stdio_endpoint(
            _argv_recording_stdio_server(),
            "--",
            str(record),
            filename="stdio-literal-double-dash.py",
        )

        assert main(stdio_cli_argv(endpoint, ["start"], "run")) == EXIT_OK
        assert json.loads(record.read_text(encoding="utf-8")) == ["--"]

    @pytest.mark.parametrize(
        "argv",
        [
            ["list", "--stdio-arg", "value"],
            ["list", "--stdio-exec", "app", "--endpoint", "unix:///tmp/x"],
            ["list", "--stdio-exec", "app", "--endpoint-token-file", "/tmp/t"],
        ],
    )
    def test_stdio_selection_errors_are_usage_errors(self, argv, capsys):
        assert main(argv) == EXIT_USAGE
        assert "USAGE_ERROR" in capsys.readouterr().err

    def test_stdio_options_are_available_on_every_command(self):
        command_argv = [
            ["start", "--stdio-exec", "app"],
            ["resume", "t1", "--stdio-exec", "app"],
            ["status", "t1", "--stdio-exec", "app"],
            ["history", "t1", "--stdio-exec", "app"],
            ["follow", "t1", "--stdio-exec", "app"],
            ["steer", "t1", "--stdio-exec", "app"],
            ["interrupt", "t1", "--stdio-exec", "app"],
            ["list", "--stdio-exec", "app"],
            ["doctor", "--stdio-exec", "app"],
        ]

        for argv in command_argv:
            args = build_parser().parse_args(argv)
            assert args.stdio_exec == "app"
            assert args.stdio_args == []

    def test_legacy_socket_option_is_removed(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["list", "--socket", "/tmp/app.sock"])

    @pytest.mark.parametrize(
        ("argv", "command_type", "input_field"),
        [
            (["start", "-"], Start, "prompt"),
            (["resume", "t1", "-"], Resume, "prompt"),
            (["steer", "t1", "-"], Steer, "input"),
        ],
    )
    def test_dash_reads_complete_stdin_prompt(
        self, argv, command_type, input_field, monkeypatch
    ):
        stdin = "\n leading line\ninternal line\n\n"
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        commands = capture_cli_command(monkeypatch)

        assert main([*argv, "--stdio-exec", "app"]) == EXIT_OK
        command = commands[-1]

        assert isinstance(command, command_type)
        assert getattr(command, input_field) == stdin

    @pytest.mark.parametrize(
        "argv", [["start", "-"], ["resume", "t1", "-"], ["steer", "t1", "-"]]
    )
    def test_empty_stdin_is_usage_error_before_endpoint_selection(
        self, argv, monkeypatch, capsys
    ):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        launched = False

        async def subprocess_exec(*_args, **_kwargs):
            nonlocal launched
            launched = True
            pytest.fail("empty stdin attempted to launch the stdio runtime")

        monkeypatch.setattr(asyncio.BaseEventLoop, "subprocess_exec", subprocess_exec)
        runtime_argv = [argv[0], "--stdio-exec", "app", *argv[1:]]

        assert main(runtime_argv) == EXIT_USAGE
        assert "USAGE_ERROR" in capsys.readouterr().err
        assert not launched

    def test_dash_is_not_stdin_input_for_non_prompt_commands(self, capsys, monkeypatch):
        class StdinMustNotBeRead:
            def read(self):
                pytest.fail("non-prompt command attempted to read stdin")

        monkeypatch.setattr(sys, "stdin", StdinMustNotBeRead())

        assert main(["status", "t1", "-"]) == EXIT_USAGE
        assert "usage:" in capsys.readouterr().err


class TestSandboxPolicy:
    @pytest.mark.parametrize(
        ("argument", "policy"),
        (
            ("read-only", SandboxPolicy.readOnly),
            ("workspace-write", SandboxPolicy.workspaceWrite),
            ("danger-full-access", SandboxPolicy.dangerFullAccess),
        ),
    )
    def test_parser_and_command_use_public_policy_vocabulary(
        self, argument, policy, monkeypatch
    ):
        commands = capture_cli_command(monkeypatch)

        assert (
            main(
                [
                    "start",
                    "--sandbox",
                    argument,
                    "--stdio-exec",
                    "app",
                    "--",
                    "hello",
                ]
            )
            == EXIT_OK
        )
        command = commands[-1]

        assert isinstance(command, Start)
        assert command.config.sandbox is policy

    def test_parser_rejects_legacy_camel_case_policy(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["start", "--sandbox", "readOnly"])


class TestApproveForMe:
    def test_flag_enables_auto_review(self, monkeypatch):
        commands = capture_cli_command(monkeypatch)

        assert (
            main(["start", "--approve-for-me", "--stdio-exec", "app", "--", "hello"])
            == EXIT_OK
        )
        command = commands[-1]

        assert isinstance(command, Start)
        assert command.config.approval_policy is ApprovalPolicy.onRequest
        assert command.config.approvals_reviewer is ApprovalsReviewer.autoReview

    def test_default_stays_unattended(self, monkeypatch):
        commands = capture_cli_command(monkeypatch)

        assert main(["start", "--stdio-exec", "app", "--", "hello"]) == EXIT_OK
        command = commands[-1]

        assert isinstance(command, Start)
        assert command.config.approval_policy is ApprovalPolicy.never
        assert command.config.approvals_reviewer is None


class TestSplitPrompt:
    def test_prompt_after_double_dash(self, monkeypatch):
        commands = capture_cli_command(monkeypatch)

        assert (
            main(
                [
                    "start",
                    "-o",
                    "jsonl",
                    "--stdio-exec",
                    "app",
                    "--",
                    "fix",
                    "the",
                    "bug",
                ]
            )
            == EXIT_OK
        )
        assert commands[-1] == Start(prompt="fix the bug")

    def test_no_double_dash(self, monkeypatch):
        commands = capture_cli_command(monkeypatch)

        assert main(["status", "t1", "--stdio-exec", "app"]) == EXIT_OK
        assert commands[-1] == Status(thread_id="t1")

    def test_flags_after_double_dash_are_prompt_content(self, monkeypatch):
        commands = capture_cli_command(monkeypatch)

        assert (
            main(["start", "--stdio-exec", "app", "--", "run", "--verbose"]) == EXIT_OK
        )
        assert commands[-1] == Start(prompt="run --verbose")

    def test_dash_after_double_dash_remains_prompt_content(self, monkeypatch):
        commands = capture_cli_command(monkeypatch)

        assert main(["start", "--stdio-exec", "app", "--", "-"]) == EXIT_OK
        assert commands[-1] == Start(prompt="-")

    def test_stdio_dash_value_does_not_become_prompt_delimiter(
        self, tmp_path, stdio_endpoint
    ):
        record = tmp_path / "stdio-argv.json"
        endpoint = stdio_endpoint(
            _argv_recording_stdio_server(),
            "--child-flag",
            str(record),
            filename="stdio-dash-value.py",
        )

        assert main(stdio_cli_argv(endpoint, ["start"], "run")) == EXIT_OK
        assert json.loads(record.read_text(encoding="utf-8")) == ["--child-flag"]

    def test_keyboard_interrupt_returns_130(self, monkeypatch):
        def interrupt(coroutine):
            coroutine.close()
            raise KeyboardInterrupt

        monkeypatch.setattr("codexctl.cli.asyncio.run", interrupt)

        assert main(["list", "--stdio-exec", "app"]) == 130


class TestExitCodeMapping:
    @pytest.mark.parametrize(
        ("error_code", "expected"),
        [
            (ErrorCode.THREAD_NOT_FOUND, EXIT_DOMAIN),
            (ErrorCode.THREAD_BUSY, EXIT_DOMAIN),
            (ErrorCode.NO_ACTIVE_TURN, EXIT_DOMAIN),
            (ErrorCode.TURN_NOT_STEERABLE, EXIT_DOMAIN),
            (ErrorCode.TURN_FAILED, EXIT_TURN),
            (ErrorCode.TURN_INTERRUPTED, EXIT_TURN),
            (ErrorCode.APP_SERVER_UNAVAILABLE, EXIT_RUNTIME),
            (ErrorCode.APP_SERVER_PROTOCOL_ERROR, EXIT_RUNTIME),
            (ErrorCode.THREAD_RECOVERY_FAILED, EXIT_RUNTIME),
            (ErrorCode.UNSUPPORTED_INTERACTION, EXIT_RUNTIME),
            (ErrorCode.INCOMPATIBLE_CODEX, EXIT_RUNTIME),
            (ErrorCode.OUTPUT_MODE_NOT_SUPPORTED, EXIT_USAGE),
            (ErrorCode.USAGE_ERROR, EXIT_USAGE),
        ],
    )
    def test_mapping(self, error_code, expected):
        error = CodexCtlError(error_code, "message")
        assert exit_code_for(error) == expected

    def test_exit_codes_are_distinct(self):
        assert len({EXIT_OK, EXIT_USAGE, EXIT_DOMAIN, EXIT_TURN, EXIT_RUNTIME}) == 5

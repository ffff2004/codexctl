"""CLI: output-mode matrix, usage errors, exit-code mapping.

All paths tested here return before any runtime connection is attempted.
"""

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FakeAppServer, make_ctl

from codexctl.appserver import StdioFrameTransport
from codexctl.cli import (
    _OUTPUT_MATRIX,
    EXIT_DOMAIN,
    EXIT_OK,
    EXIT_RUNTIME,
    EXIT_TURN,
    EXIT_USAGE,
    _build_command,
    _execute,
    _select_endpoint,
    _split_prompt,
    build_parser,
    exit_code_for,
    main,
)
from codexctl.core import CodexCtl
from codexctl.endpoint import StdioEndpointAdapter, StdioTarget
from codexctl.model import (
    ApprovalPolicy,
    ApprovalsReviewer,
    CodexCtlError,
    ErrorCode,
    Follow,
    ListThreads,
    ReplayActiveTurn,
    Resume,
    SandboxPolicy,
    Start,
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


class TestStdioExecution:
    async def test_start_preserves_text_rendering(self, stdio_adapter, capsys):
        endpoint = stdio_adapter(_SUCCESSFUL_STDIO_SERVER, filename="render.py")

        code = await _execute(CodexCtl(endpoint), Start(prompt="hello"), "text")

        assert code == 0
        output = capsys.readouterr().out
        # Unified header: only the Thread line; the Turn marker is emitted
        # from the turn/started event (the first event for start/resume).
        assert output.startswith("Thread: t1\n\nTurn: u1\n")
        assert "[agent]\ndone\n" in output
        assert "Turn completed\n" in output

    async def test_detach_returns_the_existing_json_document(
        self, stdio_adapter, capsys
    ):
        endpoint = stdio_adapter(_SUCCESSFUL_STDIO_SERVER, filename="detach.py")

        code = await _execute(
            CodexCtl(endpoint), Start(prompt="hello", detach=True), "json"
        )

        assert code == 0
        assert json.loads(capsys.readouterr().out) == {
            "threadId": "t1",
            "turnId": "u1",
            "detached": True,
        }

    async def test_cleanup_failure_does_not_replace_successful_result(
        self, stdio_adapter, capsys, monkeypatch
    ):
        endpoint = stdio_adapter(_SUCCESSFUL_STDIO_SERVER, filename="cleanup.py")
        original_close = StdioFrameTransport.close

        async def close_then_fail(transport):
            await original_close(transport)
            raise RuntimeError("cleanup failed")

        monkeypatch.setattr(StdioFrameTransport, "close", close_then_fail)

        code = await _execute(CodexCtl(endpoint), Start(prompt="hello"), "text")

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
    def test_persist_flag_builds_follow_command(self):
        args = build_parser().parse_args(["follow", "t1", "--persist"])

        command = _build_command(args, None)

        assert command == Follow(
            thread_id="t1", replay=ReplayActiveTurn(), persist=True
        )

    def test_default_follow_is_not_persistent(self):
        args = build_parser().parse_args(["follow", "t1"])

        command = _build_command(args, None)

        assert command == Follow(thread_id="t1", replay=ReplayActiveTurn())

    async def test_connection_loss_maps_to_protocol_error_exit_path(self):
        # Persist exits 5 on connection loss: _execute re-raises the
        # APP_SERVER_PROTOCOL_ERROR carried by the outcome result.
        server = FakeAppServer()
        server.result(
            "thread/resume",
            {"thread": {"id": "t1", "status": {"type": "idle"}, "turns": []}},
        )
        server.end_stream()

        with pytest.raises(CodexCtlError) as excinfo:
            await _execute(
                make_ctl(server), Follow(thread_id="t1", persist=True), "text"
            )

        assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        assert exit_code_for(excinfo.value) == EXIT_RUNTIME
        assert "turn/interrupt" not in server.methods_requested


class TestOutputMatrixContract:
    """The matrix is the public contract from the interface specification."""

    def test_streaming_commands_accept_jsonl_foreground(self):
        for command in ("start", "resume", "follow"):
            assert "jsonl" in _OUTPUT_MATRIX[(command, False)]
            assert "json" not in _OUTPUT_MATRIX[(command, False)]

    def test_detached_start_resume_accept_json_not_jsonl(self):
        for command in ("start", "resume"):
            assert "json" in _OUTPUT_MATRIX[(command, True)]
            assert "jsonl" not in _OUTPUT_MATRIX[(command, True)]

    def test_snapshot_commands_accept_json_not_jsonl(self):
        for command in ("status", "steer", "interrupt", "list", "doctor"):
            assert "json" in _OUTPUT_MATRIX[(command, False)]
            assert "jsonl" not in _OUTPUT_MATRIX[(command, False)]

    def test_history_accepts_all_modes(self):
        assert _OUTPUT_MATRIX[("history", False)] == {"text", "json", "jsonl"}

    def test_text_always_allowed(self):
        assert all("text" in modes for modes in _OUTPUT_MATRIX.values())


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

    def test_list_all_builds_global_list_command(self):
        args = build_parser().parse_args(["list", "--all"])

        assert _build_command(args, None) == ListThreads(all_threads=True)

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

    def test_stdio_arg_accepts_dash_prefixed_values_and_preserves_order(self):
        args = build_parser().parse_args(
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

        endpoint = _select_endpoint(args)

        assert isinstance(endpoint, StdioEndpointAdapter)
        assert endpoint.mode == "stdio"
        assert endpoint._target == StdioTarget(("app-server", "--child-flag", "value"))

    def test_stdio_literal_double_dash_can_precede_prompt_delimiter(self):
        args = build_parser().parse_args(
            ["start", "--stdio-exec", "app", "--stdio-arg=--"]
        )
        endpoint = _select_endpoint(args)
        assert endpoint._target == StdioTarget(("app", "--"))

        argv, prompt = _split_prompt(
            ["start", "--stdio-exec", "app", "--stdio-arg=--", "--", "run"]
        )
        assert argv[-1] == "--stdio-arg=--"
        assert prompt == "run"

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

        args = build_parser().parse_args(argv)
        command = _build_command(args, None)

        assert isinstance(command, command_type)
        assert getattr(command, input_field) == stdin

    @pytest.mark.parametrize(
        "argv", [["start", "-"], ["resume", "t1", "-"], ["steer", "t1", "-"]]
    )
    def test_empty_stdin_is_usage_error_before_endpoint_selection(
        self, argv, monkeypatch, capsys
    ):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))

        def endpoint_must_not_be_selected(_args):
            pytest.fail("empty stdin attempted to connect to the app-server")

        monkeypatch.setattr(
            "codexctl.cli._select_endpoint", endpoint_must_not_be_selected
        )

        assert main(argv) == EXIT_USAGE
        assert "USAGE_ERROR" in capsys.readouterr().err

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
    def test_parser_and_command_use_public_policy_vocabulary(self, argument, policy):
        args = build_parser().parse_args(["start", "--sandbox", argument])

        command = _build_command(args, "hello")

        assert isinstance(command, Start)
        assert command.config.sandbox is policy

    def test_parser_rejects_legacy_camel_case_policy(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["start", "--sandbox", "readOnly"])


class TestApproveForMe:
    def test_flag_enables_auto_review(self):
        args = build_parser().parse_args(["start", "--approve-for-me"])

        command = _build_command(args, "hello")

        assert isinstance(command, Start)
        assert command.config.approval_policy is ApprovalPolicy.onRequest
        assert command.config.approvals_reviewer is ApprovalsReviewer.autoReview

    def test_default_stays_unattended(self):
        args = build_parser().parse_args(["start"])

        command = _build_command(args, "hello")

        assert isinstance(command, Start)
        assert command.config.approval_policy is ApprovalPolicy.never
        assert command.config.approvals_reviewer is None


class TestSplitPrompt:
    def test_prompt_after_double_dash(self):
        argv, prompt = _split_prompt(
            ["start", "-o", "jsonl", "--", "fix", "the", "bug"]
        )
        assert argv == ["start", "-o", "jsonl"]
        assert prompt == "fix the bug"

    def test_no_double_dash(self):
        argv, prompt = _split_prompt(["status", "t1"])
        assert argv == ["status", "t1"] and prompt is None

    def test_flags_after_double_dash_are_prompt_content(self):
        argv, prompt = _split_prompt(["start", "--", "run", "--verbose"])
        assert argv == ["start"]
        assert prompt == "run --verbose"

    def test_dash_after_double_dash_remains_prompt_content(self):
        argv, prompt = _split_prompt(["start", "--", "-"])
        assert argv == ["start"]
        assert prompt == "-"

    def test_stdio_dash_value_does_not_become_prompt_delimiter(self):
        argv, prompt = _split_prompt(
            ["start", "--stdio-arg", "--child-flag", "--", "run"]
        )
        assert argv == ["start", "--stdio-arg", "--child-flag"]
        assert prompt == "run"

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

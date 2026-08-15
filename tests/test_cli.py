"""CLI: output-mode matrix, usage errors, exit-code mapping.

All paths tested here return before any runtime connection is attempted.
"""

from __future__ import annotations

import json

import pytest

from codexctl.cli import (
    EXIT_DOMAIN,
    EXIT_OK,
    EXIT_RUNTIME,
    EXIT_TURN,
    EXIT_USAGE,
    _OUTPUT_MATRIX,
    _build_command,
    _split_prompt,
    build_parser,
    exit_code_for,
    main,
)
from codexctl.model import CodexCtlError, ErrorCode, ListThreads, SandboxPolicy, Start


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
    def test_structured_errors_use_stdout_for_requested_mode(
        self, argv, mode, capsys
    ):
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
        args = build_parser().parse_args(
            ["start", "--sandbox", argument]
        )

        command = _build_command(args, "hello")

        assert isinstance(command, Start)
        assert command.config.sandbox is policy

    def test_parser_rejects_legacy_camel_case_policy(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["start", "--sandbox", "readOnly"])


class TestSplitPrompt:
    def test_prompt_after_double_dash(self):
        argv, prompt = _split_prompt(["start", "-o", "jsonl", "--", "fix", "the", "bug"])
        assert argv == ["start", "-o", "jsonl"]
        assert prompt == "fix the bug"

    def test_no_double_dash(self):
        argv, prompt = _split_prompt(["status", "t1"])
        assert argv == ["status", "t1"] and prompt is None

    def test_flags_after_double_dash_are_prompt_content(self):
        argv, prompt = _split_prompt(["start", "--", "run", "--verbose"])
        assert argv == ["start"]
        assert prompt == "run --verbose"


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

"""CLI entry point: argv parsing, output-mode validation, signals, exit codes.

Execution behavior lives in :mod:`codexctl.core`; rendering lives in
:mod:`codexctl.render`. This module only maps between the shell and those
two modules.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from .appserver import CLIENT_VERSION
from .core import CodexCtl, history_to_events
from .endpoint import ExternalSocketAdapter, ManagedDaemonAdapter
from .model import (
    CodexCtlError,
    Doctor,
    ErrorCode,
    EventStreamOutcome,
    Follow,
    History,
    Interrupt,
    ListThreads,
    Resume,
    Start,
    StartConfig,
    SandboxPolicy,
    Status,
    Steer,
    UsageError,
    parse_replay_selector,
    parse_turn_selector,
)
from .render import JsonlRenderer, JsonRenderer, TextRenderer

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DOMAIN = 3
EXIT_TURN = 4
EXIT_RUNTIME = 5
EXIT_SIGINT = 130

_CODE_TO_EXIT = {
    ErrorCode.THREAD_NOT_FOUND: EXIT_DOMAIN,
    ErrorCode.THREAD_BUSY: EXIT_DOMAIN,
    ErrorCode.NO_ACTIVE_TURN: EXIT_DOMAIN,
    ErrorCode.TURN_NOT_STEERABLE: EXIT_DOMAIN,
    ErrorCode.TURN_FAILED: EXIT_TURN,
    ErrorCode.TURN_INTERRUPTED: EXIT_TURN,
    ErrorCode.APP_SERVER_UNAVAILABLE: EXIT_RUNTIME,
    ErrorCode.APP_SERVER_PROTOCOL_ERROR: EXIT_RUNTIME,
    ErrorCode.THREAD_RECOVERY_FAILED: EXIT_RUNTIME,
    ErrorCode.UNSUPPORTED_INTERACTION: EXIT_RUNTIME,
    ErrorCode.INCOMPATIBLE_CODEX: EXIT_RUNTIME,
    ErrorCode.OUTPUT_MODE_NOT_SUPPORTED: EXIT_USAGE,
    ErrorCode.USAGE_ERROR: EXIT_USAGE,
}

# Public output-mode matrix (reference contract).
_OUTPUT_MATRIX: dict[tuple[str, bool], set[str]] = {
    ("start", False): {"text", "jsonl"},
    ("start", True): {"text", "json"},
    ("resume", False): {"text", "jsonl"},
    ("resume", True): {"text", "json"},
    ("status", False): {"text", "json"},
    ("history", False): {"text", "json", "jsonl"},
    ("follow", False): {"text", "jsonl"},
    ("steer", False): {"text", "json"},
    ("interrupt", False): {"text", "json"},
    ("list", False): {"text", "json"},
    ("doctor", False): {"text", "json"},
}

_SANDBOX_POLICY_BY_ARGUMENT = {
    "read-only": SandboxPolicy.readOnly,
    "workspace-write": SandboxPolicy.workspaceWrite,
    "danger-full-access": SandboxPolicy.dangerFullAccess,
}


def exit_code_for(error: CodexCtlError) -> int:
    return _CODE_TO_EXIT.get(error.code, EXIT_RUNTIME)


class _CliUsageError(Exception):
    pass


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-o", "--output", choices=("text", "json", "jsonl"), default="text"
    )
    parser.add_argument("--json", dest="output_json", action="store_true")
    parser.add_argument("--jsonl", dest="output_jsonl", action="store_true")
    parser.add_argument("--socket", type=Path, default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codexctl")
    parser.add_argument("--version", action="version", version=f"codexctl {CLIENT_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="create a new thread and start its first turn")
    _add_common(p)
    p.add_argument("--detach", action="store_true")
    p.add_argument("--cwd", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", default=None)
    p.add_argument("--sandbox", choices=_SANDBOX_POLICY_BY_ARGUMENT, default=None)

    p = sub.add_parser("resume", help="continue an existing thread with a new turn")
    _add_common(p)
    p.add_argument("thread_id")
    p.add_argument("--detach", action="store_true")

    p = sub.add_parser("status", help="read the current state of a thread")
    _add_common(p)
    p.add_argument("thread_id")

    p = sub.add_parser("history", help="read a finite snapshot of thread history")
    _add_common(p)
    p.add_argument("thread_id")
    p.add_argument("--turns", default=None)

    p = sub.add_parser("follow", help="attach to the current active turn")
    _add_common(p)
    p.add_argument("thread_id")
    p.add_argument("--replay-turns", default="-1")

    p = sub.add_parser("steer", help="add steering input to the active turn")
    _add_common(p)
    p.add_argument("thread_id")

    p = sub.add_parser("interrupt", help="interrupt the active turn")
    _add_common(p)
    p.add_argument("thread_id")

    p = sub.add_parser("list", help="list stored threads")
    _add_common(p)
    p.add_argument("--all", action="store_true", dest="all_threads")

    p = sub.add_parser("doctor", help="diagnose runtime compatibility")
    _add_common(p)

    return parser


def _resolve_output_mode(args: argparse.Namespace) -> str:
    mode = args.output
    if getattr(args, "output_json", False):
        mode = "json"
    if getattr(args, "output_jsonl", False):
        mode = "jsonl"
    return mode


def _split_prompt(argv: list[str]) -> tuple[list[str], str | None]:
    """Split argv at a bare ``--``; everything after it is the prompt."""
    if "--" in argv:
        index = argv.index("--")
        return argv[:index], " ".join(argv[index + 1 :])
    return argv, None


def _build_command(args: argparse.Namespace, prompt: str | None) -> Any:
    if args.command == "start":
        if not prompt:
            raise _CliUsageError("start requires a prompt after --")
        return Start(
            prompt=prompt,
            config=StartConfig(
                cwd=args.cwd,
                model=args.model,
                effort=args.effort,
                sandbox=(
                    _SANDBOX_POLICY_BY_ARGUMENT[args.sandbox]
                    if args.sandbox is not None
                    else None
                ),
            ),
            detach=args.detach,
        )
    if args.command == "resume":
        if not prompt:
            raise _CliUsageError("resume requires a prompt after --")
        return Resume(thread_id=args.thread_id, prompt=prompt, detach=args.detach)
    if args.command == "status":
        return Status(thread_id=args.thread_id)
    if args.command == "history":
        selector = None
        if args.turns is not None:
            try:
                selector = parse_turn_selector(args.turns)
            except ValueError as exc:
                raise _CliUsageError(f"invalid --turns selector: {exc}") from exc
        return History(thread_id=args.thread_id, selector=selector)
    if args.command == "follow":
        try:
            replay = parse_replay_selector(args.replay_turns)
        except ValueError as exc:
            raise _CliUsageError(f"invalid --replay-turns selector: {exc}") from exc
        return Follow(thread_id=args.thread_id, replay=replay)
    if args.command == "steer":
        if not prompt:
            raise _CliUsageError("steer requires input after --")
        return Steer(thread_id=args.thread_id, input=prompt)
    if args.command == "interrupt":
        return Interrupt(thread_id=args.thread_id)
    if args.command == "list":
        return ListThreads(all_threads=args.all_threads)
    if args.command == "doctor":
        return Doctor()
    raise _CliUsageError(f"unknown command: {args.command}")


async def _execute(ctl: CodexCtl, command: Any, mode: str) -> int:
    outcome = await ctl.run(command)

    if isinstance(outcome, EventStreamOutcome):
        renderer: Any = TextRenderer() if mode == "text" else JsonlRenderer()
        renderer.stream_header(outcome.thread_id, outcome.turn_id)
        async for event in outcome.events:
            renderer.event(event)
        terminal = await outcome.result  # may raise CodexCtlError
        renderer.stream_footer(terminal)
        if terminal.status == "completed":
            return EXIT_OK
        raise CodexCtlError(
            ErrorCode.TURN_FAILED
            if terminal.status == "failed"
            else ErrorCode.TURN_INTERRUPTED,
            terminal.error or f"turn ended {terminal.status}",
            thread_id=terminal.thread_id,
            turn_id=terminal.turn_id,
        )

    if mode == "json":
        JsonRenderer().snapshot(outcome)
    elif mode == "jsonl":
        # History is the only finite snapshot allowed in jsonl: emit it as a
        # finite sequence of canonical projected records.
        JsonlRenderer().snapshot_records(history_to_events(outcome))
    else:
        TextRenderer().snapshot(outcome)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, prompt = _split_prompt(argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 0 for --help/--version and 2 for usage errors.
        return EXIT_USAGE if exc.code is None else int(exc.code)

    mode = _resolve_output_mode(args)
    detach = getattr(args, "detach", False)
    allowed = _OUTPUT_MATRIX[(args.command, detach)]
    if mode not in allowed:
        error = CodexCtlError(
            ErrorCode.OUTPUT_MODE_NOT_SUPPORTED,
            f"output mode {mode!r} is not supported for {args.command}"
            + (" --detach" if detach else ""),
        )
        _emit_error(error, mode)
        return EXIT_USAGE

    try:
        command = _build_command(args, prompt)
    except _CliUsageError as exc:
        # General argument/usage failures carry USAGE_ERROR (exit 2).
        # OUTPUT_MODE_NOT_SUPPORTED is reserved for the output-mode
        # matrix rejection above.
        _emit_error(UsageError(str(exc)), mode)
        return EXIT_USAGE

    if args.socket is not None:
        endpoint: Any = ExternalSocketAdapter(args.socket)
    else:
        endpoint = ManagedDaemonAdapter()
    ctl = CodexCtl(endpoint)

    try:
        return asyncio.run(_execute(ctl, command, mode))
    except KeyboardInterrupt:
        # Local interruption never sends a Codex turn interrupt.
        return EXIT_SIGINT
    except CodexCtlError as exc:
        _emit_error(exc, mode)
        return exit_code_for(exc)


def _emit_error(error: CodexCtlError, mode: str) -> None:
    if mode == "json":
        JsonRenderer().error(error)
    elif mode == "jsonl":
        JsonlRenderer().error(error)
    else:
        TextRenderer().error(error)


if __name__ == "__main__":
    raise SystemExit(main())

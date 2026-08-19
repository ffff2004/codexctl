"""CLI entry point: argv parsing, output-mode validation, signals, exit codes.

Execution behavior lives in :mod:`codexctl.core`; rendering lives in
:mod:`codexctl.render`. This module only maps between the shell and those
two modules.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Iterable, cast

from .appserver import CLIENT_VERSION
from .core import CodexCtl, history_to_events
from .endpoint import (
    ExternalRuntimeProvider,
    ManagedRuntimeProvider,
    StdioFraming,
    StdioRuntimeProvider,
)
from .model import (
    ApprovalPolicy,
    ApprovalsReviewer,
    CodexCtlError,
    Doctor,
    ErrorCode,
    EventStreamOutcome,
    Follow,
    History,
    Interrupt,
    ListThreads,
    Resume,
    SandboxPolicy,
    Start,
    StartConfig,
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


def _protect_stdio_args(argv: list[str]) -> list[str]:
    """Make repeatable stdio values safe for argparse.

    ``argparse`` treats a value beginning with ``-`` as another option. The
    attached form preserves the exact value while leaving the first bare
    ``--`` available as the existing prompt delimiter. A literal ``--`` used
    as a stdio value can be written as ``--stdio-arg=--`` when a prompt also
    follows the options.
    """
    protected: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        protected.append(token)
        if token == "--":
            protected.extend(argv[index + 1 :])
            break
        if token == "--stdio-arg" and index + 1 < len(argv):
            protected[-1] = f"--stdio-arg={argv[index + 1]}"
            index += 1
        index += 1
    return protected


class _CodexArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that accepts dash-prefixed ``--stdio-arg`` values."""

    def parse_args(  # type: ignore[override]
        self,
        args: Iterable[str] | None = None,
        namespace: Any = None,
    ) -> argparse.Namespace:
        if args is not None:
            args = _protect_stdio_args(list(args))
        return cast(argparse.Namespace, super().parse_args(args, namespace))


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-o", "--output", choices=("text", "json", "jsonl"), default="text"
    )
    parser.add_argument("--json", dest="output_json", action="store_true")
    parser.add_argument("--jsonl", dest="output_jsonl", action="store_true")
    parser.add_argument(
        "--endpoint",
        default=None,
        metavar="URI",
        help="use an external unix:/// or ws:// app-server endpoint",
    )
    parser.add_argument(
        "--endpoint-token-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Bearer token file for a ws:// endpoint",
    )
    parser.add_argument(
        "--stdio-exec",
        default=None,
        metavar="EXECUTABLE",
        help=(
            "run a one-shot app-server using the child process's "
            "stdin/stdout for the selected stdio framing"
        ),
    )
    parser.add_argument(
        "--stdio-framing",
        choices=tuple(framing.value for framing in StdioFraming),
        default=StdioFraming.JSONL.value,
        help="framing carried by the stdio child pipes (default: jsonl)",
    )
    parser.add_argument(
        "--stdio-arg",
        dest="stdio_args",
        action="append",
        default=[],
        metavar="ARG",
        help="append one exact argument to --stdio-exec",
    )


def _add_stdin_prompt(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "_stdin_prompt",
        nargs="?",
        choices=("-",),
        help=argparse.SUPPRESS,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _CodexArgumentParser(prog="codexctl")
    parser.add_argument(
        "--version", action="version", version=f"codexctl {CLIENT_VERSION}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="create a new thread and start its first turn")
    _add_common(p)
    p.add_argument("--detach", action="store_true")
    p.add_argument("--cwd", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", default=None)
    p.add_argument("--sandbox", choices=_SANDBOX_POLICY_BY_ARGUMENT, default=None)
    p.add_argument(
        "--approve-for-me",
        dest="approve_for_me",
        action="store_true",
        help="let the runtime auto-review approval requests instead of declining them",
    )
    _add_stdin_prompt(p)

    p = sub.add_parser("resume", help="continue an existing thread with a new turn")
    _add_common(p)
    p.add_argument("thread_id")
    p.add_argument("--detach", action="store_true")
    _add_stdin_prompt(p)

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
    p.add_argument(
        "--persist",
        action="store_true",
        help="attach to the thread itself and keep streaming across turns",
    )

    p = sub.add_parser("steer", help="add steering input to the active turn")
    _add_common(p)
    p.add_argument("thread_id")
    _add_stdin_prompt(p)

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
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[:index], " ".join(argv[index + 1 :])
        if token == "--stdio-arg":
            index += 2
            continue
        index += 1
    return argv, None


def _select_runtime_provider(args: argparse.Namespace) -> Any:
    """Select the runtime provider implied by mutually exclusive CLI modes."""
    stdio_args = tuple(args.stdio_args)
    stdio_framing = StdioFraming(args.stdio_framing)
    has_stdio = (
        args.stdio_exec is not None
        or bool(stdio_args)
        or stdio_framing is not StdioFraming.JSONL
    )
    has_external = args.endpoint is not None or args.endpoint_token_file is not None
    if has_stdio and has_external:
        raise UsageError(
            "stdio options are mutually exclusive with --endpoint and "
            "--endpoint-token-file"
        )
    if stdio_args and args.stdio_exec is None:
        raise UsageError("--stdio-arg requires --stdio-exec")
    if stdio_framing is StdioFraming.WEBSOCKET and args.stdio_exec is None:
        raise UsageError("--stdio-framing websocket requires --stdio-exec")
    if args.stdio_exec is not None:
        return StdioRuntimeProvider(args.stdio_exec, stdio_args, stdio_framing)
    if args.endpoint is not None:
        return ExternalRuntimeProvider(args.endpoint, args.endpoint_token_file)
    if args.endpoint_token_file is not None:
        raise UsageError("--endpoint-token-file requires --endpoint")
    return ManagedRuntimeProvider()


def _require_prompt(prompt: str | None, message: str) -> str:
    if not prompt:
        raise _CliUsageError(message)
    return prompt


def _build_command(args: argparse.Namespace, prompt: str | None) -> Any:
    if prompt is None and getattr(args, "_stdin_prompt", None) == "-":
        prompt = sys.stdin.read()

    if args.command == "start":
        prompt = _require_prompt(
            prompt, "start requires prompt input after -- or from stdin"
        )
        if args.approve_for_me:
            approval_policy = ApprovalPolicy.onRequest
            approvals_reviewer = ApprovalsReviewer.autoReview
        else:
            approval_policy = ApprovalPolicy.never
            approvals_reviewer = None
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
                approval_policy=approval_policy,
                approvals_reviewer=approvals_reviewer,
            ),
            detach=args.detach,
        )
    if args.command == "resume":
        prompt = _require_prompt(
            prompt, "resume requires prompt input after -- or from stdin"
        )
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
        return Follow(thread_id=args.thread_id, replay=replay, persist=args.persist)
    if args.command == "steer":
        prompt = _require_prompt(prompt, "steer requires input after -- or from stdin")
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
        # A persist follow session can resolve with no terminal turn; exit
        # codes 0 and 4 are otherwise unreachable in persist mode.
        if terminal is None or terminal.status == "completed":
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
    argv = _protect_stdio_args(argv)
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

    try:
        runtime = _select_runtime_provider(args)
    except UsageError as exc:
        _emit_error(exc, mode)
        return EXIT_USAGE
    ctl = CodexCtl(runtime)

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

#!/usr/bin/env python3
"""Commit-checkpoint implementation review orchestrator.

The example intentionally keeps one deep public module: ``Workflow`` owns the
state machine and small adapters own Git, Codex, gates, and durable storage.
It stores no rendered patch as workflow state.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterator, NoReturn, Protocol, TypeVar

STATE_VERSION = 3
TERMINAL_STATES = {"READY_CERTIFIED", "READY_WITH_WAIVER"}
ACTIONS = {
    "START_NEXT_ROUND",
    "ACCEPT_FINDINGS",
    "REQUIRE_FRESH_AUDIT",
    "RETRY_GATES",
    "RETRY_REVIEWERS",
    "CONTINUE_WORKER",
    "ACCEPT_WORKER_RESULT",
}
VERDICT_RE = re.compile(r"^VERDICT: (PASS|FAIL)$")
BLOCKING_REVIEW_STATUSES = {"FAILED", "INCOMPLETE"}
WORKER_ACCEPT_STATUSES = {"failed", "interrupted"}
WORKER_CONTINUE_STATUSES = {
    "failed",
    "interrupted",
    "unknown",
    "protocol_error",
}
WORKER_RECOVERY_ACTIONS = {"CONTINUE_WORKER", "ACCEPT_WORKER_RESULT"}


def _worker_recovery_action_policy(
    status: Any, thread_id: Any, *, has_descendant: bool
) -> list[str]:
    actions: list[str] = []
    if (
        isinstance(status, str)
        and status in WORKER_CONTINUE_STATUSES
        and isinstance(thread_id, str)
        and thread_id
    ):
        actions.append("CONTINUE_WORKER")
    if isinstance(status, str) and status in WORKER_ACCEPT_STATUSES and has_descendant:
        actions.append("ACCEPT_WORKER_RESULT")
    return actions


FULL_WRAPPER = """Perform a cumulative FULL audit of the exact Git subject.
Compare {base_commit}..{candidate_head}. Inspect the checkout and derive the
patch with: git diff --no-ext-diff --no-textconv {base_commit} {candidate_head}
Do not anchor on findings from an older review."""

DELTA_WRAPPER = """Perform a DELTA review of repairs made since previous checkpoint.
Compare {previous_checkpoint}..{candidate_head}, re-check your prior findings,
and report any regression introduced by the repair."""

REVIEW_FOOTER = """The orchestrator already ran the configured gates. You may
inspect files and use read-only commands. Do not run gates, build, test, formatter,
or any command that can mutate the checkout. Report missing dynamic verification
as a finding. Do not modify the checkout.

Review yourself, do not delegate to sub-agents or `codexctl`.

The unique last line of your final response must be exactly one of:
VERDICT: PASS
VERDICT: FAIL"""

WORKER_FOOTER = """Workflow invariants: stay on the current branch; keep history
linear and append-only; do not amend, rebase, merge, reset, push, disable hooks,
or bypass verification. Finish with a clean checkout. Commit every implemented
change (one or more commits are allowed). If the requested work cannot be
implemented, make no changes and create no commit, then explain why."""


class OrchestratorError(Exception):
    """Stable user-facing workflow failure."""


class UsageError(OrchestratorError):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "run"


@dataclass(frozen=True)
class RunConfig:
    cwd: Path
    spec_path: Path
    worker_prompt_path: Path
    repair_prompt_path: Path
    reviewers: tuple[tuple[str, Path], ...]
    gates: tuple[str, ...] = ()
    branch: str | None = None
    worker_approve_for_me: bool = False
    max_auto_worker_rounds: int = 2
    gate_timeout_seconds: int = 1800
    model: str | None = None
    effort: str | None = None
    codexctl: str = "codexctl"
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state_dir: Path | None = None


@dataclass(frozen=True)
class Checkout:
    root: str
    branch: str | None
    head: str
    status: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.status


def _matches_checkout(
    actual: Checkout,
    *,
    branch: str | None,
    head: str,
    status: tuple[str, ...],
) -> bool:
    return actual.branch == branch and actual.head == head and actual.status == status


@dataclass(frozen=True)
class DetachReceipt:
    thread_id: str
    turn_id: str


@dataclass
class AgentResult:
    status: str
    raw_jsonl: bytes = b""
    messages: list[str] = field(default_factory=list)
    observed_turn_ids: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def final_message(self) -> str | None:
        return self.messages[-1] if self.messages else None


@dataclass
class _AgentAttempt:
    record: dict[str, Any]
    receipt: DetachReceipt | None
    result: AgentResult | None = None


@dataclass
class GateExecution:
    status: str
    returncode: int | None
    stdout: bytes
    stderr: bytes
    error: str | None = None


class GitPort(Protocol):
    def preflight(self, cwd: Path) -> Checkout: ...
    def snapshot(self, cwd: Path) -> Checkout: ...
    def branch_exists(self, cwd: Path, branch: str) -> bool: ...
    def create_branch(self, cwd: Path, branch: str, base: str) -> None: ...
    def is_ancestor(self, cwd: Path, older: str, newer: str) -> bool: ...
    def is_linear(self, cwd: Path, older: str, newer: str) -> bool: ...


class GitAdapter:
    @staticmethod
    def _run(
        cwd: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(cwd), *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise UsageError(f"cannot invoke Git: {exc}") from exc
        if check and result.returncode:
            raise UsageError(result.stderr.strip() or f"Git exited {result.returncode}")
        return result

    def snapshot(self, cwd: Path) -> Checkout:
        root = Path(
            self._run(cwd, "rev-parse", "--show-toplevel").stdout.strip()
        ).resolve()
        branch_result = self._run(
            cwd, "symbolic-ref", "--short", "-q", "HEAD", check=False
        )
        status = self._run(
            cwd,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=no",
        ).stdout
        return Checkout(
            str(root),
            branch_result.stdout.strip() or None,
            self._run(cwd, "rev-parse", "HEAD").stdout.strip(),
            tuple(line for line in status.splitlines() if line),
        )

    def preflight(self, cwd: Path) -> Checkout:
        checkout = self.snapshot(cwd)
        if Path(checkout.root) != cwd.resolve():
            raise UsageError(f"--cwd must be the checkout root: {checkout.root}")
        if not checkout.clean:
            raise UsageError("start requires a clean checkout")
        for key in ("user.name", "user.email"):
            if not self._run(cwd, "config", "--get", key, check=False).stdout.strip():
                raise UsageError(f"Git {key} is not configured")
        return checkout

    def branch_exists(self, cwd: Path, branch: str) -> bool:
        return (
            self._run(
                cwd,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
                check=False,
            ).returncode
            == 0
        )

    def create_branch(self, cwd: Path, branch: str, base: str) -> None:
        self._run(cwd, "switch", "-c", branch, base)

    def is_ancestor(self, cwd: Path, older: str, newer: str) -> bool:
        return (
            self._run(
                cwd, "merge-base", "--is-ancestor", older, newer, check=False
            ).returncode
            == 0
        )

    def is_linear(self, cwd: Path, older: str, newer: str) -> bool:
        result = self._run(cwd, "rev-list", "--merges", f"{older}..{newer}")
        return not result.stdout.strip()


class ArtifactStore:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.json"

    def write_state(self, state: dict[str, Any]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="state.", dir=self.run_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(_json_bytes(state))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)

    def read_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(f"cannot read {self.state_path}: {exc}") from exc
        try:
            return PersistedWorkflowState.from_json(value).payload
        except StateSchemaError as exc:
            raise UsageError(str(exc)) from exc
        except StateValidationError as exc:
            raise UsageError(f"invalid persisted workflow state: {exc}") from exc

    def artifact(
        self,
        state: dict[str, Any],
        relative: str,
        data: bytes,
        *,
        owner: dict[str, Any] | None = None,
    ) -> str:
        base = Path(relative)
        candidate = base
        serial = 2
        while (self.run_dir / candidate).exists():
            candidate = base.with_name(f"{base.stem}-{serial}{base.suffix}")
            serial += 1
        path = self.run_dir / candidate
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
        reference = candidate.as_posix()
        owner = owner or {}
        state.setdefault("artifacts", []).append(
            {
                "path": reference,
                "sha256": _digest(data),
                "size": len(data),
                "attempt_id": owner.get("attempt_id"),
                "review_session_id": owner.get("review_session_id"),
                "role": owner.get("role"),
                "thread_id": owner.get("thread_id"),
                "turn_id": owner.get("turn_id"),
                "observed_turn_ids": list(owner.get("observed_turn_ids", [])),
            }
        )
        return reference

    @contextlib.contextmanager
    def exclusive(self, *, blocking: bool = False) -> Iterator[None]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with (self.run_dir / "run.lock").open("a+b") as handle:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle, flags)
            except BlockingIOError as exc:
                raise UsageError("RUN_BUSY") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class CodexPort(Protocol):
    def start(
        self,
        *,
        prompt: str,
        cwd: Path,
        role: str,
        approve: bool,
        model: str | None,
        effort: str | None,
    ) -> DetachReceipt: ...
    def resume(self, *, thread_id: str, prompt: str) -> DetachReceipt: ...
    def follow(self, *, thread_id: str, turn_id: str) -> AgentResult: ...
    def history(self, *, thread_id: str, turn_id: str) -> AgentResult: ...


def _parse_detach(raw: bytes) -> DetachReceipt:
    try:
        value = json.loads(raw)
        return DetachReceipt(str(value["threadId"]), str(value["turnId"]))
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise OrchestratorError("codexctl returned an invalid detach receipt") from exc


def _event_turn_id(event: dict[str, Any]) -> str | None:
    value = event.get("turnId")
    return str(value) if value else None


def parse_agent_jsonl(
    raw: bytes, target_turn: str, *, reject_other_turns: bool = True
) -> AgentResult:
    messages: list[str] = []
    turns: list[str] = []
    status: str | None = None
    try:
        lines = raw.decode().splitlines()
    except UnicodeDecodeError as exc:
        return AgentResult("protocol_error", raw, error=str(exc))
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return AgentResult(
                "protocol_error", raw, error=f"JSONL line {number}: {exc.msg}"
            )
        if not isinstance(event, dict):
            return AgentResult(
                "protocol_error", raw, error=f"JSONL line {number} is not an object"
            )
        turn_id = _event_turn_id(event)
        if (
            turn_id
            and (reject_other_turns or turn_id == target_turn)
            and turn_id not in turns
        ):
            turns.append(turn_id)
        if turn_id != target_turn:
            continue
        if event.get("type") == "item/completed":
            item = event.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "agentMessage"
                and isinstance(item.get("text"), str)
            ):
                messages.append(item["text"])
        if event.get("type") == "turn/completed":
            status = str(event.get("status") or "completed")
    if reject_other_turns and any(turn != target_turn for turn in turns):
        return AgentResult(
            "unexpected_continuation", raw, messages, turns, "observed another turn"
        )
    return AgentResult(status or "unknown", raw, messages, turns)


class CodexctlAdapter:
    def __init__(
        self,
        executable: str = "codexctl",
        cwd: Path | None = None,
        subprocess_runner: Callable[..., subprocess.CompletedProcess[bytes]]
        | None = None,
    ):
        self.executable = executable
        self.cwd = cwd or Path.cwd()
        self._subprocess_runner = subprocess_runner or subprocess.run

    def _run(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
        try:
            return self._subprocess_runner(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise OrchestratorError(f"cannot invoke codexctl: {exc}") from exc

    def start(
        self,
        *,
        prompt: str,
        cwd: Path,
        role: str,
        approve: bool,
        model: str | None,
        effort: str | None,
    ) -> DetachReceipt:
        self.cwd = cwd
        argv = [
            self.executable,
            "start",
            "--detach",
            "-o",
            "json",
            "--cwd",
            str(cwd),
            "--sandbox",
            "workspace-write" if role == "worker" else "read-only",
            "--no-goals",
            "--no-agents",
        ]
        if approve and role == "worker":
            argv.append("--approve-for-me")
        if model:
            argv.extend(("--model", model))
        if effort:
            argv.extend(("--effort", effort))
        argv.extend(("--", prompt))
        result = self._run(argv, cwd)
        if result.returncode:
            raise OrchestratorError(
                result.stderr.decode("utf-8", "replace").strip()
                or "codexctl start failed"
            )
        return _parse_detach(result.stdout)

    def resume(self, *, thread_id: str, prompt: str) -> DetachReceipt:
        result = self._run(
            [
                self.executable,
                "resume",
                thread_id,
                "--detach",
                "-o",
                "json",
                "--no-goals",
                "--no-agents",
                "--",
                prompt,
            ],
            self.cwd,
        )
        if result.returncode:
            raise OrchestratorError(
                result.stderr.decode("utf-8", "replace").strip()
                or "codexctl resume failed"
            )
        return _parse_detach(result.stdout)

    def follow(self, *, thread_id: str, turn_id: str) -> AgentResult:
        status = self._run(
            [self.executable, "status", thread_id, "-o", "json"], self.cwd
        )
        active_turn_id: str | None = None
        if status.returncode == 0:
            try:
                document = json.loads(status.stdout)
                value = document.get("activeTurnId")
                active_turn_id = str(value) if value else None
            except AttributeError, UnicodeDecodeError, json.JSONDecodeError:
                pass

        if active_turn_id != turn_id:
            recovered = self.history(thread_id=thread_id, turn_id=turn_id)
            if active_turn_id:
                observed = list(recovered.observed_turn_ids)
                if active_turn_id not in observed:
                    observed.append(active_turn_id)
                return AgentResult(
                    "unexpected_continuation",
                    recovered.raw_jsonl,
                    recovered.messages,
                    observed,
                    f"active turn {active_turn_id} differs from target {turn_id}",
                )
            return recovered

        result = self._run(
            [
                self.executable,
                "follow",
                thread_id,
                "-o",
                "jsonl",
            ],
            self.cwd,
        )
        if result.returncode:
            try:
                recovered = self.history(thread_id=thread_id, turn_id=turn_id)
            except OrchestratorError as exc:
                recovered = AgentResult("unknown", error=str(exc))
            if recovered.status != "unknown":
                return recovered
            stderr = result.stderr.decode("utf-8", "replace").strip()
            error = (
                stderr or f"codexctl follow failed with exit code {result.returncode}"
            )
            if recovered.error:
                error = f"{error}; {recovered.error}"
            return AgentResult("unknown", result.stdout, error=error)
        parsed = parse_agent_jsonl(result.stdout, turn_id)
        if parsed.status == "unexpected_continuation":
            recovered = self.history(thread_id=thread_id, turn_id=turn_id)
            observed = list(recovered.observed_turn_ids)
            for observed_turn_id in parsed.observed_turn_ids:
                if observed_turn_id not in observed:
                    observed.append(observed_turn_id)
            return AgentResult(
                "unexpected_continuation",
                parsed.raw_jsonl,
                recovered.messages,
                observed,
                parsed.error,
            )
        if parsed.status == "unknown":
            recovered = self.history(thread_id=thread_id, turn_id=turn_id)
            if recovered.status != "unknown":
                return recovered
        return parsed

    def history(self, *, thread_id: str, turn_id: str) -> AgentResult:
        result = self._run(
            [self.executable, "history", thread_id, "-o", "jsonl"], self.cwd
        )
        if result.returncode:
            return AgentResult("unknown", error="target turn history unavailable")
        parsed = parse_agent_jsonl(result.stdout, turn_id, reject_other_turns=False)
        if parsed.status == "unknown":
            parsed.error = "target turn history unavailable"
        return parsed


class GatePort(Protocol):
    def run(self, command: str, cwd: Path, timeout: int) -> GateExecution: ...


class GateAdapter:
    def run(self, command: str, cwd: Path, timeout: int) -> GateExecution:
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate()
                return GateExecution(
                    "execution_error", None, stdout, stderr, "gate timed out"
                )
        except OSError as exc:
            return GateExecution("execution_error", None, b"", b"", str(exc))
        return GateExecution(
            "passed" if process.returncode == 0 else "failed",
            process.returncode,
            stdout,
            stderr,
        )


EnumValue = TypeVar("EnumValue", bound=StrEnum)


class WorkflowPhase(StrEnum):
    PREPARE_BRANCH = "PREPARE_BRANCH"
    WORKER = "WORKER"
    VERIFY_CHECKPOINT = "VERIFY_CHECKPOINT"
    REVIEW = "REVIEW"
    READY_CERTIFIED = "READY_CERTIFIED"
    READY_WITH_WAIVER = "READY_WITH_WAIVER"


class WorkflowStatus(StrEnum):
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    READY_CERTIFIED = "READY_CERTIFIED"
    READY_WITH_WAIVER = "READY_WITH_WAIVER"


class IntentKind(StrEnum):
    CREATE_BRANCH = "create_branch"
    GATE = "gate"
    REVIEW_FINALIZE = "review_finalize"
    AGENT_START = "agent_start"
    AGENT_TERMINAL = "agent_terminal"
    AGENT_FOLLOW = "agent_follow"


class AttemptStatus(StrEnum):
    START_INTENT = "START_INTENT"
    DETACHED = "DETACHED"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"
    UNEXPECTED_CONTINUATION = "unexpected_continuation"
    PROTOCOL_ERROR = "protocol_error"


class ReviewMode(StrEnum):
    FULL = "FULL"
    DELTA = "DELTA"


class ReviewSessionStatus(StrEnum):
    RUNNING = "RUNNING"
    INCOMPLETE = "INCOMPLETE"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SUPERSEDED_BY_FULL_AUDIT = "SUPERSEDED_BY_FULL_AUDIT"
    WAIVED = "WAIVED"


class ReviewRecordStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"
    UNEXPECTED_CONTINUATION = "unexpected_continuation"
    PROTOCOL_ERROR = "protocol_error"
    AMBIGUOUS = "ambiguous"


class StateValidationError(ValueError):
    """A persisted state value does not match the current state model."""


class StateSchemaError(StateValidationError):
    """The persisted state uses an unsupported schema version."""


def _state_error(path: str, message: str) -> NoReturn:
    raise StateValidationError(f"{path}: {message}")


def _state_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _state_error(path, "expected an object")
    return value


def _state_required(value: dict[str, Any], fields: set[str], path: str) -> None:
    missing = sorted(fields.difference(value))
    if missing:
        _state_error(path, f"missing required fields: {', '.join(missing)}")


def _state_field(value: dict[str, Any], field: str, path: str) -> Any:
    if field not in value:
        _state_error(path, f"missing required field: {field}")
    return value[field]


def _state_string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        _state_error(path, "expected a non-empty string")
    return value


def _state_optional_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _state_string(value, path)


def _state_strings(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        _state_error(path, "expected a list")
    return [_state_string(item, f"{path}[{index}]") for index, item in enumerate(value)]


def _state_enum(enum_type: type[EnumValue], value: Any, path: str) -> EnumValue:
    if not isinstance(value, str):
        _state_error(path, "expected a string enum value")
    try:
        return enum_type(value)
    except ValueError:
        _state_error(path, f"unknown value {value!r}")


@dataclass(frozen=True)
class PersistedIntent:
    kind: IntentKind
    data: dict[str, Any]


@dataclass(frozen=True)
class PersistedAttempt:
    id: str
    role: str
    status: AttemptStatus
    thread_id: str | None
    turn_id: str | None
    cohort: str | None
    review_session: str | None
    reviewer_role: str | None


@dataclass(frozen=True)
class PersistedRecord:
    role: str
    status: ReviewRecordStatus
    attempt_id: str
    thread_id: str | None
    turn_id: str | None
    verdict: str | None


@dataclass(frozen=True)
class PersistedWorkflowState:
    """Typed persistence boundary for workflow recovery state."""

    payload: dict[str, Any]
    phase: WorkflowPhase
    status: WorkflowStatus
    intent: PersistedIntent | None
    attempts: tuple[PersistedAttempt, ...]
    records: tuple[PersistedRecord, ...]

    @classmethod
    def from_json(cls, value: Any) -> "PersistedWorkflowState":
        try:
            return cls._parse(value)
        except StateValidationError:
            raise
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise StateValidationError(f"state: invalid structure ({exc})") from exc

    @classmethod
    def _parse(cls, value: Any) -> "PersistedWorkflowState":
        state = _state_object(value, "state")
        _state_required(
            state,
            {
                "version",
                "run_id",
                "created_at",
                "updated_at",
                "status",
                "phase",
                "continuation_phase",
                "waiting_reason",
                "allowed_actions",
                "cwd",
                "base_commit",
                "candidate_head",
                "branch",
                "initial_branch",
                "checkpoints",
                "config",
                "prompts",
                "reviewer_roles",
                "amendments",
                "attempts",
                "gate_results",
                "gate_attestation",
                "cohorts",
                "review_sessions",
                "development_cohort",
                "certificate",
                "waiver",
                "worker_rounds_used",
                "pending_worker",
                "operation_intent",
                "artifacts",
            },
            "state",
        )
        if (
            not isinstance(state["version"], int)
            or isinstance(state["version"], bool)
            or state["version"] != STATE_VERSION
        ):
            raise StateSchemaError("unsupported orchestrator state schema")
        phase = _state_enum(WorkflowPhase, state["phase"], "state.phase")
        status = _state_enum(WorkflowStatus, state["status"], "state.status")
        _state_enum(
            WorkflowPhase, state["continuation_phase"], "state.continuation_phase"
        )
        for key in (
            "run_id",
            "created_at",
            "updated_at",
            "cwd",
            "base_commit",
            "candidate_head",
            "branch",
        ):
            _state_string(state[key], f"state.{key}")
        _state_optional_string(state["initial_branch"], "state.initial_branch")
        checkpoints = _state_strings(state["checkpoints"], "state.checkpoints")
        if (
            not checkpoints
            or state["base_commit"] not in checkpoints
            or state["candidate_head"] not in checkpoints
        ):
            _state_error(
                "state.checkpoints", "must contain the base and candidate commits"
            )
        reviewer_roles = _state_strings(state["reviewer_roles"], "state.reviewer_roles")
        if len(reviewer_roles) != len(set(reviewer_roles)):
            _state_error("state.reviewer_roles", "contains duplicate roles")
        config = _state_object(state["config"], "state.config")
        _state_required(config, {"gates", "codexctl"}, "state.config")
        _state_strings(config["gates"], "state.config.gates")
        _state_string(config["codexctl"], "state.config.codexctl")
        _state_object(state["prompts"], "state.prompts")
        for key in ("amendments", "gate_results", "artifacts"):
            if not isinstance(state[key], list):
                _state_error(f"state.{key}", "expected a list")
        attempts = _parse_attempts(state["attempts"])
        attempt_by_id = {attempt.id: attempt for attempt in attempts}
        _validate_pending_worker(state["pending_worker"], attempt_by_id)
        cohort_ids = _parse_cohorts(state["cohorts"], reviewer_roles)
        records, session_ids = _parse_review_sessions(
            state["review_sessions"], reviewer_roles, attempt_by_id, cohort_ids
        )
        _validate_run_shape(
            state, phase, status, attempt_by_id, session_ids, cohort_ids
        )
        intent = _parse_intent(state["operation_intent"])
        _validate_intent(
            intent, state, phase, status, attempt_by_id, session_ids, cohort_ids
        )
        _validate_attestation(state["gate_attestation"], state["candidate_head"])
        return cls(state, phase, status, intent, tuple(attempts), tuple(records))


def _parse_intent(value: Any) -> PersistedIntent | None:
    if value is None:
        return None
    path = "state.operation_intent"
    data = _state_object(value, path)
    kind = _state_enum(IntentKind, _state_field(data, "kind", path), f"{path}.kind")
    required = {
        IntentKind.CREATE_BRANCH: {"branch", "base"},
        IntentKind.GATE: {"candidate_head", "index", "command_digest"},
        IntentKind.REVIEW_FINALIZE: {
            "review_session_id",
            "cohort_id",
            "mode",
            "candidate_head",
        },
        IntentKind.AGENT_START: {"attempt_id"},
        IntentKind.AGENT_TERMINAL: {
            "attempt_id",
            "role",
            "review_session_id",
            "reviewer_role",
            "thread_id",
            "turn_id",
            "observed_turn_ids",
            "result_status",
            "message",
            "messages",
            "raw_jsonl",
            "error",
        },
        IntentKind.AGENT_FOLLOW: {"attempt_id", "thread_id", "turn_id"},
    }[kind]
    _state_required(data, required, path)
    for key in required - {
        "index",
        "message",
        "review_session_id",
        "reviewer_role",
        "result_status",
        "observed_turn_ids",
        "messages",
        "raw_jsonl",
        "error",
        "mode",
    }:
        _state_string(data[key], f"{path}.{key}")
    if kind is IntentKind.GATE:
        if (
            not isinstance(data["index"], int)
            or isinstance(data["index"], bool)
            or data["index"] < 0
        ):
            _state_error(f"{path}.index", "expected a non-negative integer")
    elif kind is IntentKind.REVIEW_FINALIZE:
        _state_enum(ReviewMode, data["mode"], f"{path}.mode")
        if data.get("successor") is not None:
            _state_object(data["successor"], f"{path}.successor")
    elif kind is IntentKind.AGENT_TERMINAL:
        role = data["role"]
        if role not in {"worker", "reviewer"}:
            _state_error(f"{path}.role", "unknown role")
        _state_optional_string(data["review_session_id"], f"{path}.review_session_id")
        _state_optional_string(data["reviewer_role"], f"{path}.reviewer_role")
        _state_string(data["thread_id"], f"{path}.thread_id")
        _state_string(data["turn_id"], f"{path}.turn_id")
        _state_strings(data["observed_turn_ids"], f"{path}.observed_turn_ids")
        terminal_status = _state_enum(
            AttemptStatus, data["result_status"], f"{path}.result_status"
        )
        if terminal_status in {AttemptStatus.START_INTENT, AttemptStatus.DETACHED}:
            _state_error(f"{path}.result_status", "must be terminal")
        if data["message"] is not None and not isinstance(data["message"], str):
            _state_error(f"{path}.message", "expected a string or null")
        _state_strings(data["messages"], f"{path}.messages")
        raw = _state_string(data["raw_jsonl"], f"{path}.raw_jsonl", allow_empty=True)
        try:
            bytes.fromhex(raw)
        except ValueError:
            _state_error(f"{path}.raw_jsonl", "must contain hexadecimal data")
        _state_optional_string(data["error"], f"{path}.error")
        if role == "worker":
            _state_string(_state_field(data, "input_head", path), f"{path}.input_head")
            checkout = _state_object(
                _state_field(data, "checkout", path), f"{path}.checkout"
            )
            _state_optional_string(
                _state_field(checkout, "branch", f"{path}.checkout"),
                f"{path}.checkout.branch",
            )
            _state_string(
                _state_field(checkout, "head", f"{path}.checkout"),
                f"{path}.checkout.head",
            )
            _state_strings(
                _state_field(checkout, "status", f"{path}.checkout"),
                f"{path}.checkout.status",
            )
    return PersistedIntent(kind, data)


def _parse_attempts(value: Any) -> list[PersistedAttempt]:
    if not isinstance(value, list):
        _state_error("state.attempts", "expected a list")
    required = {
        "id",
        "role",
        "status",
        "thread_id",
        "turn_id",
        "cohort",
        "review_session",
        "reviewer_role",
        "completed_at",
        "observed_turn_ids",
    }
    result: list[PersistedAttempt] = []
    seen: set[str] = set()
    for index, value in enumerate(value):
        path = f"state.attempts[{index}]"
        data = _state_object(value, path)
        _state_required(data, required, path)
        attempt_id = _state_string(data["id"], f"{path}.id")
        if attempt_id in seen:
            _state_error(f"{path}.id", "is duplicated")
        seen.add(attempt_id)
        role = _state_string(data["role"], f"{path}.role")
        if role not in {"worker", "reviewer"}:
            _state_error(f"{path}.role", "unknown role")
        status = _state_enum(AttemptStatus, data["status"], f"{path}.status")
        thread_id = _state_optional_string(data["thread_id"], f"{path}.thread_id")
        turn_id = _state_optional_string(data["turn_id"], f"{path}.turn_id")
        if (thread_id is None) != (turn_id is None):
            _state_error(path, "thread_id and turn_id must be present together")
        completed_at = _state_optional_string(
            data["completed_at"], f"{path}.completed_at"
        )
        if status is AttemptStatus.START_INTENT and (
            thread_id is not None or completed_at is not None
        ):
            _state_error(path, "START_INTENT cannot have completion evidence")
        if status is AttemptStatus.DETACHED and (
            thread_id is None or completed_at is not None
        ):
            _state_error(path, "DETACHED requires an unfinished thread receipt")
        if (
            status not in {AttemptStatus.START_INTENT, AttemptStatus.DETACHED}
            and completed_at is None
        ):
            _state_error(path, "terminal attempts require completed_at")
        cohort = _state_optional_string(data["cohort"], f"{path}.cohort")
        review_session = _state_optional_string(
            data["review_session"], f"{path}.review_session"
        )
        reviewer_role = _state_optional_string(
            data["reviewer_role"], f"{path}.reviewer_role"
        )
        if role == "worker" and (
            cohort is not None
            or review_session is not None
            or reviewer_role is not None
        ):
            _state_error(path, "Worker cannot have reviewer ownership")
        if role == "reviewer" and (
            cohort is None or review_session is None or reviewer_role is None
        ):
            _state_error(path, "Reviewer ownership is incomplete")
        _state_strings(data["observed_turn_ids"], f"{path}.observed_turn_ids")
        result.append(
            PersistedAttempt(
                attempt_id,
                role,
                status,
                thread_id,
                turn_id,
                cohort,
                review_session,
                reviewer_role,
            )
        )
    return result


def _parse_cohorts(value: Any, reviewer_roles: list[str]) -> set[str]:
    if not isinstance(value, list):
        _state_error("state.cohorts", "expected a list")
    ids: set[str] = set()
    required = {"id", "fresh", "threads", "created_at", "last_checkpoint"}
    for index, value in enumerate(value):
        path = f"state.cohorts[{index}]"
        data = _state_object(value, path)
        _state_required(data, required, path)
        cohort_id = _state_string(data["id"], f"{path}.id")
        if cohort_id in ids:
            _state_error(f"{path}.id", "is duplicated")
        ids.add(cohort_id)
        if not isinstance(data["fresh"], bool):
            _state_error(f"{path}.fresh", "expected a boolean")
        threads = _state_object(data["threads"], f"{path}.threads")
        if not set(threads).issubset(reviewer_roles):
            _state_error(f"{path}.threads", "contains an unknown reviewer role")
        for role, thread_id in threads.items():
            _state_string(thread_id, f"{path}.threads.{role}")
    return ids


def _parse_record(value: Any, role: str, path: str) -> PersistedRecord:
    data = _state_object(value, path)
    _state_required(
        data,
        {
            "status",
            "attempt_id",
            "thread_id",
            "turn_id",
            "observed_turn_ids",
            "message_artifact",
            "message",
            "verdict",
            "error",
        },
        path,
    )
    status = _state_enum(ReviewRecordStatus, data["status"], f"{path}.status")
    verdict = data["verdict"]
    if verdict is not None and verdict not in {"PASS", "FAIL"}:
        _state_error(f"{path}.verdict", "must be PASS, FAIL, or null")
    message = _state_string(data["message"], f"{path}.message", allow_empty=True)
    if status is ReviewRecordStatus.COMPLETED and (not message or verdict is None):
        _state_error(path, "completed records require a message and verdict")
    if status is not ReviewRecordStatus.COMPLETED and verdict is not None:
        _state_error(f"{path}.verdict", "non-completed records cannot have a verdict")
    thread_id = _state_optional_string(data["thread_id"], f"{path}.thread_id")
    turn_id = _state_optional_string(data["turn_id"], f"{path}.turn_id")
    if (thread_id is None) != (turn_id is None):
        _state_error(path, "thread_id and turn_id must be present together")
    _state_strings(data["observed_turn_ids"], f"{path}.observed_turn_ids")
    _state_optional_string(data["message_artifact"], f"{path}.message_artifact")
    _state_optional_string(data["error"], f"{path}.error")
    return PersistedRecord(
        role,
        status,
        _state_string(data["attempt_id"], f"{path}.attempt_id"),
        thread_id,
        turn_id,
        verdict,
    )


def _parse_review_sessions(
    value: Any,
    reviewer_roles: list[str],
    attempts: dict[str, PersistedAttempt],
    cohort_ids: set[str],
) -> tuple[list[PersistedRecord], set[str]]:
    if not isinstance(value, list):
        _state_error("state.review_sessions", "expected a list")
    records: list[PersistedRecord] = []
    session_ids: set[str] = set()
    required = {
        "id",
        "mode",
        "cohort_id",
        "base_commit",
        "previous_checkpoint",
        "candidate_head",
        "policy_digest",
        "amendment_ids",
        "amendment_digest",
        "results",
        "status",
        "created_at",
    }
    for index, value in enumerate(value):
        path = f"state.review_sessions[{index}]"
        data = _state_object(value, path)
        _state_required(data, required, path)
        session_id = _state_string(data["id"], f"{path}.id")
        if session_id in session_ids:
            _state_error(f"{path}.id", "is duplicated")
        session_ids.add(session_id)
        _state_enum(ReviewMode, data["mode"], f"{path}.mode")
        cohort_id = _state_string(data["cohort_id"], f"{path}.cohort_id")
        if cohort_id not in cohort_ids:
            _state_error(f"{path}.cohort_id", "does not identify a cohort")
        _state_string(data["candidate_head"], f"{path}.candidate_head")
        _state_enum(ReviewSessionStatus, data["status"], f"{path}.status")
        result_map = _state_object(data["results"], f"{path}.results")
        if not set(result_map).issubset(reviewer_roles):
            _state_error(f"{path}.results", "contains an unknown reviewer role")
        for role, value in result_map.items():
            record = _parse_record(value, role, f"{path}.results.{role}")
            attempt = attempts.get(record.attempt_id)
            if attempt is None or attempt.role != "reviewer":
                _state_error(
                    f"{path}.results.{role}.attempt_id",
                    "does not identify a reviewer attempt",
                )
            if (
                attempt.review_session != session_id
                or attempt.reviewer_role != role
                or attempt.thread_id != record.thread_id
                or attempt.turn_id != record.turn_id
            ):
                _state_error(f"{path}.results.{role}", "does not match its attempt")
            records.append(record)
    return records, session_ids


def _validate_run_shape(
    state: dict[str, Any],
    phase: WorkflowPhase,
    status: WorkflowStatus,
    attempts: dict[str, PersistedAttempt],
    session_ids: set[str],
    cohort_ids: set[str],
) -> None:
    actions = _state_strings(state["allowed_actions"], "state.allowed_actions")
    if any(action not in ACTIONS for action in actions):
        _state_error("state.allowed_actions", "contains an unknown action")
    if status is WorkflowStatus.WAITING:
        _state_string(state["waiting_reason"], "state.waiting_reason")
    elif state["waiting_reason"] is not None or actions:
        _state_error("state", "waiting fields are inconsistent with status")
    if (phase is WorkflowPhase.READY_CERTIFIED) != (
        status is WorkflowStatus.READY_CERTIFIED
    ) or (phase is WorkflowPhase.READY_WITH_WAIVER) != (
        status is WorkflowStatus.READY_WITH_WAIVER
    ):
        _state_error("state", "phase and status do not match")
    development_cohort = _state_optional_string(
        state["development_cohort"], "state.development_cohort"
    )
    if development_cohort is not None and development_cohort not in cohort_ids:
        _state_error("state.development_cohort", "does not identify a cohort")
    for attempt in attempts.values():
        if (
            attempt.review_session is not None
            and attempt.review_session not in session_ids
        ):
            _state_error("state.attempts", "contains an attempt for an unknown session")
        if attempt.cohort is not None and attempt.cohort not in cohort_ids:
            _state_error("state.attempts", "contains an attempt for an unknown cohort")


def _validate_intent(
    intent: PersistedIntent | None,
    state: dict[str, Any],
    phase: WorkflowPhase,
    status: WorkflowStatus,
    attempts: dict[str, PersistedAttempt],
    session_ids: set[str],
    cohort_ids: set[str],
) -> None:
    if intent is None:
        if status is WorkflowStatus.RUNNING and phase is WorkflowPhase.PREPARE_BRANCH:
            _state_error(
                "state.operation_intent", "is required while preparing the branch"
            )
        return
    path = "state.operation_intent"
    if status is not WorkflowStatus.RUNNING:
        _state_error(path, "is only valid while status is RUNNING")
    data = intent.data
    if intent.kind is IntentKind.CREATE_BRANCH:
        if (
            phase is not WorkflowPhase.PREPARE_BRANCH
            or data["branch"] != state["branch"]
            or data["base"] != state["base_commit"]
        ):
            _state_error(path, "does not match branch preparation")
        return
    if intent.kind is IntentKind.GATE:
        gates = _state_strings(state["config"]["gates"], "state.config.gates")
        if (
            phase is not WorkflowPhase.VERIFY_CHECKPOINT
            or data["candidate_head"] != state["candidate_head"]
            or data["index"] >= len(gates)
            or data["command_digest"] != _digest(gates[data["index"]].encode())
        ):
            _state_error(path, "does not match a configured gate")
        return
    if intent.kind is IntentKind.REVIEW_FINALIZE:
        if (
            phase is not WorkflowPhase.REVIEW
            or data["review_session_id"] not in session_ids
            or data["cohort_id"] not in cohort_ids
            or data["candidate_head"] != state["candidate_head"]
        ):
            _state_error(path, "does not match the current review")
        mode = _state_enum(ReviewMode, data["mode"], f"{path}.mode")
        if mode is ReviewMode.DELTA and data.get("successor") is None:
            _state_error(path, "DELTA finalization requires a successor")
        if mode is ReviewMode.FULL and data.get("successor") is not None:
            _state_error(path, "FULL finalization cannot contain a successor")
        return
    attempt = attempts.get(data["attempt_id"])
    if attempt is None:
        _state_error(f"{path}.attempt_id", "does not identify an attempt")
    expected_phase = (
        WorkflowPhase.WORKER if attempt.role == "worker" else WorkflowPhase.REVIEW
    )
    if phase is not expected_phase:
        _state_error(path, "is outside the attempt's phase")
    if intent.kind is IntentKind.AGENT_START:
        if attempt.status not in {AttemptStatus.START_INTENT, AttemptStatus.UNKNOWN}:
            _state_error(path, "does not identify a pending attempt")
    elif intent.kind is IntentKind.AGENT_FOLLOW:
        if (
            attempt.status is not AttemptStatus.DETACHED
            or data["thread_id"] != attempt.thread_id
            or data["turn_id"] != attempt.turn_id
        ):
            _state_error(path, "does not match a detached attempt")
    elif (
        data["role"] != attempt.role
        or data["result_status"] != attempt.status.value
        or data["thread_id"] != attempt.thread_id
        or data["turn_id"] != attempt.turn_id
        or data["review_session_id"] != attempt.review_session
        or data["reviewer_role"] != attempt.reviewer_role
    ):
        _state_error(path, "does not match terminal attempt evidence")


def _validate_pending_worker(value: Any, attempts: dict[str, PersistedAttempt]) -> None:
    if value is None:
        return
    path = "state.pending_worker"
    data = _state_object(value, path)
    _state_required(data, {"input_head", "attempt_id"}, path)
    _state_string(data["input_head"], f"{path}.input_head")
    attempt_id = _state_string(data["attempt_id"], f"{path}.attempt_id")
    if attempt_id not in attempts or attempts[attempt_id].role != "worker":
        _state_error(f"{path}.attempt_id", "does not identify a Worker attempt")


def _validate_attestation(value: Any, candidate: Any) -> None:
    if value is None:
        return
    path = "state.gate_attestation"
    data = _state_object(value, path)
    _state_required(
        data, {"candidate_head", "policy_digest", "results", "created_at"}, path
    )
    if data["candidate_head"] != candidate:
        _state_error(path, "does not match candidate_head")
    _state_string(data["policy_digest"], f"{path}.policy_digest")
    if not isinstance(data["results"], list):
        _state_error(f"{path}.results", "expected a list")
    _state_string(data["created_at"], f"{path}.created_at")


def _default_state_root() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    base = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local" / "state"
    )
    return base / "codexctl" / "impl-review-orchestrator"


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read {label} {path}: {exc}") from exc


def _policy_digest(*parts: str) -> str:
    return _digest("\0".join(parts).encode())


def _verdict(message: str) -> str:
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    matches = [VERDICT_RE.fullmatch(line) for line in lines]
    verdicts = [match.group(1) for match in matches if match]
    if not lines or not VERDICT_RE.fullmatch(lines[-1]) or len(verdicts) != 1:
        raise UsageError("reviewer response has an ambiguous verdict")
    return verdicts[0]


class Workflow:
    """Small public workflow seam.

    Construct with adapters for deterministic tests, then call only ``start``,
    ``resume``, or ``inspect``. Reports intentionally reference large artifacts
    instead of embedding prompts, logs, or model messages.
    """

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        git: GitPort | None = None,
        codex: CodexPort | None = None,
        gates: GatePort | None = None,
        progress: Callable[[str], None] | None = None,
    ):
        self.state_root = (state_dir or _default_state_root()).expanduser().resolve()
        self.git = git or GitAdapter()
        self.codex = codex
        self.gates = gates or GateAdapter()
        self.progress = progress
        self.store: ArtifactStore | None = None
        self.state: dict[str, Any] = {}

    def _say(self, message: str) -> None:
        if not self.progress:
            return
        try:
            self.progress(message)
        except Exception:
            pass

    @staticmethod
    def _agent_role(record: dict[str, Any]) -> str:
        if record["role"] == "worker":
            return "Worker"
        return f"reviewer:{record['reviewer_role']}"

    def _say_agent_detached(self, record: dict[str, Any]) -> None:
        self._say(
            f"Agent detached: role={self._agent_role(record)} "
            f"attemptId={record['id']} threadId={record['thread_id']} "
            f"turnId={record['turn_id']}"
        )

    def _say_agent_completed(self, record: dict[str, Any]) -> None:
        self._say(
            f"Agent completed: role={self._agent_role(record)} "
            f"attemptId={record['id']} status={record['status']}"
        )

    def _save(self) -> None:
        assert self.store is not None
        self.state["updated_at"] = _now()
        self.store.write_state(self.state)

    def _artifact(
        self,
        relative: str,
        data: bytes,
        *,
        owner: dict[str, Any] | None = None,
    ) -> str:
        assert self.store is not None
        reference = self.store.artifact(self.state, relative, data, owner=owner)
        self._save()
        return reference

    def _artifact_once(
        self, relative: str, data: bytes, *, owner: dict[str, Any]
    ) -> str:
        assert self.store is not None
        digest = _digest(data)
        for artifact in self.state.get("artifacts", []):
            if (
                artifact.get("attempt_id") == owner.get("attempt_id")
                and artifact.get("path") == relative
                and artifact.get("sha256") == digest
                and artifact.get("size") == len(data)
            ):
                return relative
        if any(
            artifact.get("path") == relative
            for artifact in self.state.get("artifacts", [])
        ):
            return self._artifact(relative, data, owner=owner)
        path = self.store.run_dir / relative
        if path.exists() and path.is_file() and _digest(path.read_bytes()) == digest:
            self.state.setdefault("artifacts", []).append(
                {
                    "path": relative,
                    "sha256": digest,
                    "size": len(data),
                    "attempt_id": owner.get("attempt_id"),
                    "review_session_id": owner.get("review_session_id"),
                    "role": owner.get("role"),
                    "thread_id": owner.get("thread_id"),
                    "turn_id": owner.get("turn_id"),
                    "observed_turn_ids": list(owner.get("observed_turn_ids", [])),
                }
            )
            self._save()
            return relative
        return self._artifact(relative, data, owner=owner)

    def _load(self, run_id: str) -> None:
        matches = list(self.state_root.glob(f"*/{_slug(run_id)}/state.json"))
        if len(matches) != 1:
            raise UsageError(f"could not uniquely locate run {run_id!r}")
        self.store = ArtifactStore(matches[0].parent)
        self.state = self.store.read_state()
        if self.codex is None:
            self.codex = CodexctlAdapter(
                str(self.state["config"]["codexctl"]), Path(self.state["cwd"])
            )

    def _say_loaded_run(self) -> None:
        assert self.store is not None
        self._say(
            f"Run loaded: runId={self.state['run_id']} "
            f"statePath={self.store.state_path}"
        )

    def _reconcile_interrupted_operation(self) -> dict[str, Any] | None:
        intent = self.state.get("operation_intent")
        unknown_review: tuple[dict[str, Any], dict[str, Any], str, str] | None = None
        if not intent:
            detached = [
                item
                for item in self.state.get("attempts", [])
                if item["status"] == "DETACHED"
            ]
            if detached:
                pass
            elif (
                self.state.get("status") == "RUNNING"
                and self.state.get("phase") == "WORKER"
            ):
                return self._wait("AGENT_OUTCOME_UNKNOWN", [], "WORKER")
            elif (
                self.state.get("status") == "RUNNING"
                and self.state.get("phase") == "VERIFY_CHECKPOINT"
            ):
                try:
                    self._require_checkpoint()
                except UsageError:
                    return self._wait("CHECKOUT_DRIFT", [], "VERIFY_CHECKPOINT")
                return self._ensure_gates(force_full=bool(self.state.get("force_full")))
            elif (
                self.state.get("status") == "RUNNING"
                and self.state.get("phase") == "REVIEW"
            ):
                pass
            else:
                return None
        elif intent["kind"] == "create_branch":
            cwd = Path(self.state["cwd"])
            actual = self.git.snapshot(cwd)
            target_matches = (
                actual.branch == self.state["branch"]
                and actual.head == self.state["base_commit"]
                and actual.clean
            )
            initial_matches = _matches_checkout(
                actual,
                branch=self.state["initial_branch"],
                head=self.state["base_commit"],
                status=(),
            )
            if target_matches:
                # The branch mutation completed before the process crashed.
                pass
            elif (
                not self.git.branch_exists(cwd, self.state["branch"])
                and initial_matches
            ):
                self.git.create_branch(
                    cwd, self.state["branch"], self.state["base_commit"]
                )
            else:
                self.state["operation_intent"] = None
                return self._wait("CHECKOUT_DRIFT", [], "PREPARE_BRANCH")
            self.state["operation_intent"] = None
            self.state["phase"] = "WORKER"
            return self._run_worker(context="Initial implementation")
        elif intent["kind"] == "gate":
            try:
                self._require_checkpoint()
            except UsageError:
                self.state["operation_intent"] = None
                return self._wait("GATE_MUTATED_CHECKOUT", [], "VERIFY_CHECKPOINT")
            self.state["operation_intent"] = None
            return self._wait(
                "GATE_EXECUTION_ERROR",
                ["RETRY_GATES", "REQUIRE_FRESH_AUDIT"],
                "VERIFY_CHECKPOINT",
            )
        elif intent["kind"] == "review_finalize":
            session = next(
                item
                for item in self.state["review_sessions"]
                if item["id"] == intent["review_session_id"]
            )
            cohort = next(
                item
                for item in self.state["cohorts"]
                if item["id"] == intent["cohort_id"]
            )
            if (
                session["cohort_id"] != cohort["id"]
                or session["candidate_head"] != intent["candidate_head"]
                or session["mode"] != intent["mode"]
            ):
                raise UsageError("invalid Reviewer finalization evidence")
            return self._finalize_review(
                session,
                cohort,
                str(intent["mode"]),
                str(intent["candidate_head"]),
                rotation=intent.get("successor"),
            )
        elif intent["kind"] == "agent_start":
            attempt = next(
                item
                for item in self.state["attempts"]
                if item["id"] == intent["attempt_id"]
            )
            attempt["status"] = "unknown"
            attempt["completed_at"] = _now()
            attempt["error"] = "crash occurred before detach receipt was recorded"
            if attempt["role"] == "worker":
                self.state["operation_intent"] = None
                return self._wait(
                    "AGENT_OUTCOME_UNKNOWN",
                    [],
                    self.state["phase"],
                    completed_attempt=attempt,
                )
            session = next(
                item
                for item in self.state["review_sessions"]
                if item["id"] == attempt["review_session"]
            )
            self._store_reviewer_result(
                session,
                str(attempt["reviewer_role"]),
                attempt,
                None,
                AgentResult("unknown", error=attempt["error"]),
                agent_completion_pending=True,
            )
            self._mark_pending_reviewer_starts_unknown(session)
            cohort = next(
                item
                for item in self.state["cohorts"]
                if item["id"] == attempt["cohort"]
            )
            unknown_review = (
                session,
                cohort,
                str(session["mode"]),
                str(session["candidate_head"]),
            )
        elif intent["kind"] == "agent_terminal":
            attempt = next(
                item
                for item in self.state["attempts"]
                if item["id"] == intent["attempt_id"]
            )
            if (
                attempt.get("thread_id") != intent.get("thread_id")
                or attempt.get("turn_id") != intent.get("turn_id")
                or attempt.get("status") != intent.get("result_status")
                or attempt.get("role") != intent.get("role")
            ):
                raise UsageError("invalid terminal agent recovery evidence")
            result = AgentResult(
                str(intent["result_status"]),
                raw_jsonl=bytes.fromhex(str(intent.get("raw_jsonl", ""))),
                messages=(
                    list(intent["messages"])
                    if isinstance(intent.get("messages"), list)
                    else (
                        [intent["message"]]
                        if isinstance(intent.get("message"), str) and intent["message"]
                        else []
                    )
                ),
                observed_turn_ids=list(
                    intent.get("observed_turn_ids", attempt["observed_turn_ids"])
                ),
                error=intent.get("error"),
            )
            receipt = DetachReceipt(str(intent["thread_id"]), str(intent["turn_id"]))
            self._finish_attempt(
                attempt, receipt, result, preserve_operation_intent=True
            )
            if attempt["role"] == "worker":
                pending = self.state.get("pending_worker") or {}
                input_head = intent.get("input_head")
                if (
                    pending.get("attempt_id") != attempt["id"]
                    or pending.get("input_head") != input_head
                    or input_head != self.state["candidate_head"]
                    or not isinstance(input_head, str)
                    or not isinstance(intent.get("checkout"), dict)
                ):
                    raise UsageError("invalid terminal Worker recovery evidence")
                return self._handle_worker_result(
                    attempt,
                    result,
                    input_head,
                    expected_checkout=intent.get("checkout"),
                )
            if attempt["role"] != "reviewer":
                raise UsageError("invalid terminal agent recovery role")
            session = next(
                item
                for item in self.state["review_sessions"]
                if item["id"] == attempt["review_session"]
            )
            if attempt.get("review_session") != intent.get(
                "review_session_id"
            ) or attempt.get("reviewer_role") != intent.get("reviewer_role"):
                raise UsageError("invalid terminal Reviewer recovery evidence")
            cohort = next(
                item
                for item in self.state["cohorts"]
                if item["id"] == attempt["cohort"]
            )
            role = str(attempt["reviewer_role"])
            self._store_reviewer_result(session, role, attempt, receipt, result)
            self._mark_pending_reviewer_starts_unknown(session)
            unknown_review = (
                session,
                cohort,
                str(session["mode"]),
                str(session["candidate_head"]),
            )
        elif intent["kind"] == "agent_follow":
            attempt = next(
                item
                for item in self.state["attempts"]
                if item["id"] == intent["attempt_id"]
            )
            if attempt.get("thread_id") != intent.get("thread_id") or attempt.get(
                "turn_id"
            ) != intent.get("turn_id"):
                raise UsageError("invalid Reviewer follow recovery evidence")
            if attempt["role"] == "reviewer":
                session = next(
                    item
                    for item in self.state["review_sessions"]
                    if item["id"] == attempt["review_session"]
                )
                cohort = next(
                    item
                    for item in self.state["cohorts"]
                    if item["id"] == attempt["cohort"]
                )
                role = str(attempt["reviewer_role"])
                existing = session["results"].get(role, {})
                if existing.get("attempt_id") == attempt["id"]:
                    pass
                elif attempt["status"] != "DETACHED":
                    self._store_reviewer_result(
                        session,
                        role,
                        attempt,
                        (
                            DetachReceipt(
                                str(attempt["thread_id"]), str(attempt["turn_id"])
                            )
                            if attempt.get("thread_id") and attempt.get("turn_id")
                            else None
                        ),
                        AgentResult(str(attempt["status"]), error=attempt.get("error")),
                    )
                self._mark_pending_reviewer_starts_unknown(session)
                unknown_review = (
                    session,
                    cohort,
                    str(session["mode"]),
                    str(session["candidate_head"]),
                )

        detached = [
            item
            for item in self.state.get("attempts", [])
            if item["status"] == "DETACHED"
            and not (
                item["role"] == "reviewer"
                and next(
                    (
                        result
                        for session in self.state["review_sessions"]
                        if session["id"] == item.get("review_session")
                        for result in session["results"].values()
                        if result.get("attempt_id") == item["id"]
                    ),
                    None,
                )
                is not None
            )
        ]
        assert self.codex is not None
        recovered_review = unknown_review
        for attempt in detached:
            receipt = DetachReceipt(str(attempt["thread_id"]), str(attempt["turn_id"]))
            self._say_agent_detached(attempt)
            follow_failed = False
            try:
                result = self.codex.follow(
                    thread_id=receipt.thread_id, turn_id=receipt.turn_id
                )
            except Exception as exc:
                if attempt["role"] != "reviewer":
                    raise
                follow_failed = True
                result = AgentResult("unknown", error=str(exc))
            if result.status == "unknown" and not follow_failed:
                try:
                    recovered = self.codex.history(
                        thread_id=receipt.thread_id, turn_id=receipt.turn_id
                    )
                except Exception as exc:
                    if attempt["role"] != "reviewer":
                        raise
                    recovered = AgentResult("unknown", error=str(exc))
                if recovered.status != "unknown":
                    result = recovered
            self._finish_attempt(attempt, receipt, result)
            if attempt["role"] == "worker":
                pending = self.state.get("pending_worker") or {}
                return self._handle_worker_result(
                    attempt, result, str(pending["input_head"])
                )
            session = next(
                item
                for item in self.state["review_sessions"]
                if item["id"] == attempt["review_session"]
            )
            cohort = next(
                item
                for item in self.state["cohorts"]
                if item["id"] == attempt["cohort"]
            )
            role = str(attempt["reviewer_role"])
            self._store_reviewer_result(session, role, attempt, receipt, result)
            recovered_review = (
                session,
                cohort,
                str(session["mode"]),
                str(session["candidate_head"]),
            )
        if recovered_review:
            return self._prepare_review_finalization(*recovered_review)
        if (
            not intent
            and self.state.get("phase") == "REVIEW"
            and self.state.get("review_sessions")
        ):
            session = self.state["review_sessions"][-1]
            if session["status"] in {"RUNNING", "INCOMPLETE"}:
                cohort = next(
                    item
                    for item in self.state["cohorts"]
                    if item["id"] == session["cohort_id"]
                )
                return self._prepare_review_finalization(
                    session,
                    cohort,
                    str(session["mode"]),
                    str(session["candidate_head"]),
                )
        return None

    def _report(self) -> dict[str, Any]:
        state = self.state
        attempts = [
            {
                "attemptId": item["id"],
                "role": item["role"],
                "workerRound": item.get("worker_round"),
                "cohortId": item.get("cohort"),
                "reviewSessionId": item.get("review_session"),
                "reviewerRole": item.get("reviewer_role"),
                "threadId": item.get("thread_id"),
                "turnId": item.get("turn_id"),
                "observedTurnIds": list(item.get("observed_turn_ids", [])),
                "status": item.get("status"),
                "promptArtifact": item.get("prompt_artifact"),
                "promptPolicyDigest": item.get("prompt_policy_digest"),
                "amendmentIds": list(item.get("amendment_ids", [])),
                "amendmentDigest": item.get("amendment_digest"),
                "outputArtifacts": list(item.get("output_artifacts", [])),
            }
            for item in state.get("attempts", [])
        ]
        review_sessions = [
            {
                "reviewSessionId": session["id"],
                "mode": session["mode"],
                "cohortId": session["cohort_id"],
                "baseCommit": session["base_commit"],
                "previousCheckpoint": session.get("previous_checkpoint"),
                "candidateHead": session["candidate_head"],
                "policyDigest": session["policy_digest"],
                "amendmentIds": list(session["amendment_ids"]),
                "amendmentDigest": session["amendment_digest"],
                "status": session["status"],
                "results": {
                    role: {
                        "status": result.get("status"),
                        "attemptId": result.get("attempt_id"),
                        "threadId": result.get("thread_id"),
                        "turnId": result.get("turn_id"),
                        "observedTurnIds": list(result.get("observed_turn_ids", [])),
                        "messageArtifact": result.get("message_artifact"),
                        "verdict": result.get("verdict"),
                        "error": result.get("error"),
                    }
                    for role, result in session["results"].items()
                },
            }
            for session in state.get("review_sessions", [])
        ]
        return {
            "runId": state["run_id"],
            "status": state["status"],
            "phase": state["phase"],
            "exitCode": 0
            if state["status"] == "READY_CERTIFIED"
            else 3
            if state["status"] == "READY_WITH_WAIVER"
            else 2,
            "baseCommit": state["base_commit"],
            "candidateHead": state["candidate_head"],
            "branch": state["branch"],
            "waitingReason": state.get("waiting_reason"),
            "allowedActions": self._allowed_actions(),
            "gateAttestation": state.get("gate_attestation"),
            "certificate": state.get("certificate"),
            "waiver": state.get("waiver"),
            "attempts": attempts,
            "reviewSessions": review_sessions,
            "artifactManifest": list(state.get("artifacts", [])),
            "statePath": str(self.store.state_path) if self.store else None,
        }

    def inspect(self, run_id: str) -> dict[str, Any]:
        """Strictly read a run report without taking the run lock."""
        self._load(run_id)
        report = self._report()
        operation_intent = self.state.get("operation_intent") or {}
        if (
            self.state.get("status") == "RUNNING"
            and self.state.get("phase") == "WORKER"
            and operation_intent.get("kind")
            in {"agent_start", "agent_follow", "agent_terminal"}
        ):
            return report
        try:
            self._require_checkpoint(
                allow_descendant=any(
                    action in WORKER_RECOVERY_ACTIONS
                    for action in report["allowedActions"]
                )
            )
        except UsageError:
            report["status"] = "WAITING"
            report["exitCode"] = 2
            report["waitingReason"] = "CHECKOUT_DRIFT"
            report["allowedActions"] = []
            report["certificate"] = None
        return report

    def start(self, config: RunConfig) -> dict[str, Any]:
        if config.state_dir is not None:
            self.state_root = config.state_dir.expanduser().resolve()
        if config.max_auto_worker_rounds < 1:
            raise UsageError("--max-auto-worker-rounds must be at least 1")
        if config.gate_timeout_seconds < 1:
            raise UsageError("--gate-timeout-seconds must be positive")
        if not config.reviewers:
            raise UsageError("at least one reviewer is required")
        names = [name for name, _ in config.reviewers]
        if len(names) != len(set(names)) or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) for name in names
        ):
            raise UsageError("reviewer names must be unique and portable")

        cwd = config.cwd.expanduser().resolve()
        run_id = config.run_id
        repo_id = f"{_slug(cwd.name)}-{_digest(str(cwd).encode())[:12]}"
        self.store = ArtifactStore(self.state_root / repo_id / _slug(run_id))
        branch = config.branch or f"impl-review/{run_id}"
        with self.store.exclusive():
            initial = self.git.preflight(cwd)
            if self.store.state_path.exists():
                raise UsageError(f"run already exists: {run_id}")
            if self.git.branch_exists(cwd, branch):
                raise UsageError(f"branch already exists: {branch}")

            spec = _read_text(config.spec_path, "spec")
            worker = _read_text(config.worker_prompt_path, "Worker prompt")
            repair = _read_text(config.repair_prompt_path, "Repair prompt")
            rubrics = {
                name: _read_text(path, f"reviewer rubric {name}")
                for name, path in config.reviewers
            }
            created = _now()
            self.state = {
                "version": STATE_VERSION,
                "run_id": run_id,
                "created_at": created,
                "updated_at": created,
                "status": "RUNNING",
                "phase": "PREPARE_BRANCH",
                "continuation_phase": "WORKER",
                "waiting_reason": None,
                "allowed_actions": [],
                "cwd": str(cwd),
                "base_commit": initial.head,
                "candidate_head": initial.head,
                "branch": branch,
                "initial_branch": initial.branch,
                "checkpoints": [initial.head],
                "config": {
                    "gates": list(config.gates),
                    "gate_timeout_seconds": config.gate_timeout_seconds,
                    "gate_policy_digest": _policy_digest(
                        *config.gates, str(config.gate_timeout_seconds)
                    ),
                    "worker_approve_for_me": config.worker_approve_for_me,
                    "worker_sandbox": "workspace-write",
                    "reviewer_sandbox": "read-only",
                    "max_auto_worker_rounds": config.max_auto_worker_rounds,
                    "model": config.model,
                    "effort": config.effort,
                    "codexctl": config.codexctl,
                    "isolation": {
                        "features.goals": False,
                        "agents.enabled": False,
                    },
                    "full_wrapper": FULL_WRAPPER,
                    "delta_wrapper": DELTA_WRAPPER,
                },
                "prompts": {},
                "reviewer_roles": names,
                "amendments": [],
                "attempts": [],
                "gate_results": [],
                "gate_attestation": None,
                "cohorts": [],
                "review_sessions": [],
                "development_cohort": None,
                "certificate": None,
                "waiver": None,
                "worker_rounds_used": 0,
                "pending_worker": None,
                "operation_intent": {
                    "kind": "create_branch",
                    "branch": branch,
                    "base": initial.head,
                },
                "artifacts": [],
            }
            for key, text in (("spec", spec), ("worker", worker), ("repair", repair)):
                self.state["prompts"][key] = self.store.artifact(
                    self.state, f"inputs/{key}.md", text.encode()
                )
            for name, text in rubrics.items():
                self.state["prompts"].setdefault("reviewers", {})[name] = (
                    self.store.artifact(
                        self.state, f"inputs/reviewer-{_slug(name)}.md", text.encode()
                    )
                )
            self._save()
            self._say(
                f"Run initialized: runId={run_id} statePath={self.store.state_path}"
            )
            # Reconcile only the exact branch-create crash window owned by this run.
            current = self.git.snapshot(cwd)
            if not _matches_checkout(
                current,
                branch=initial.branch,
                head=initial.head,
                status=initial.status,
            ):
                raise UsageError("CHECKOUT_DRIFT: checkout changed after preflight")
            if self.git.branch_exists(cwd, branch):
                raise UsageError(f"branch collision while preparing run: {branch}")
            else:
                self.git.create_branch(cwd, branch, initial.head)
            if self.codex is None:
                self.codex = CodexctlAdapter(config.codexctl)
            return self._run_worker(context="Initial implementation")

    def resume(
        self,
        run_id: str,
        action: str | None = None,
        *,
        additional_prompt: str | None = None,
    ) -> dict[str, Any]:
        self._load(run_id)
        self._say_loaded_run()
        assert self.store is not None
        with self.store.exclusive():
            self.state = self.store.read_state()
            reconciled = self._reconcile_interrupted_operation()
            if reconciled is not None:
                return reconciled
            if self.state["status"] in TERMINAL_STATES:
                if action is not None:
                    raise UsageError("a terminal run accepts no actions")
                try:
                    self._require_checkpoint()
                except UsageError:
                    self.state["certificate"] = None
                    return self._wait("CHECKOUT_DRIFT", [], self.state["phase"])
                return self._report()
            if action is None:
                return self._report()
            allowed_actions = self._allowed_actions()
            if (
                action not in ACTIONS
                or (
                    action in WORKER_RECOVERY_ACTIONS
                    and action not in self._worker_recovery_actions()
                )
                or (
                    action not in WORKER_RECOVERY_ACTIONS
                    and action not in allowed_actions
                )
            ):
                raise UsageError(
                    f"action {action!r} is not allowed; choose from {allowed_actions}"
                )
            if additional_prompt is not None and action != "START_NEXT_ROUND":
                raise UsageError(
                    "prompt amendments are only valid with START_NEXT_ROUND"
                )
            if (
                action == "START_NEXT_ROUND"
                and additional_prompt
                and additional_prompt.strip()
            ):
                self._add_amendment(additional_prompt)
            allow_worker_recovery = action in {
                "CONTINUE_WORKER",
                "ACCEPT_WORKER_RESULT",
            }
            self._require_checkpoint(allow_descendant=allow_worker_recovery)
            self.state["status"] = "RUNNING"
            self.state["waiting_reason"] = None
            self.state["allowed_actions"] = []
            if action == "START_NEXT_ROUND":
                return self._run_worker(context=self._blocking_context())
            if action == "ACCEPT_FINDINGS":
                return self._waive()
            if action == "REQUIRE_FRESH_AUDIT":
                return self._ensure_gates(force_full=True)
            if action == "RETRY_GATES":
                return self._ensure_gates(force_full=bool(self.state.get("force_full")))
            if action == "RETRY_REVIEWERS":
                return self._review(retry=True)
            if action == "CONTINUE_WORKER":
                return self._continue_worker()
            if action == "ACCEPT_WORKER_RESULT":
                advanced_checkpoint = self._accept_descendant_checkpoint()
                return self._ensure_gates(advanced_checkpoint=advanced_checkpoint)
            raise AssertionError(action)

    def _pending_worker_attempt(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        pending = self.state.get("pending_worker")
        attempts = self.state.get("attempts")
        if not isinstance(pending, dict) or not isinstance(attempts, list):
            return None
        attempt_id = pending.get("attempt_id")
        if not isinstance(attempt_id, str):
            return None
        attempt = next(
            (
                item
                for item in attempts
                if isinstance(item, dict) and item.get("id") == attempt_id
            ),
            None,
        )
        if attempt is None or attempt.get("role") != "worker":
            return None
        return pending, attempt

    def _worker_recovery_actions(self) -> list[str]:
        if self.state.get("status") != "WAITING" or self.state.get("phase") != "WORKER":
            return []
        evidence = self._pending_worker_attempt()
        if evidence is None:
            return []
        pending, attempt = evidence
        has_descendant = (
            isinstance(pending.get("input_head"), str)
            and pending["input_head"] == self.state.get("candidate_head")
            and isinstance(pending.get("descendant_head"), str)
            and pending["descendant_head"] != pending["input_head"]
        )
        return _worker_recovery_action_policy(
            attempt.get("status"),
            attempt.get("thread_id"),
            has_descendant=has_descendant,
        )

    def _allowed_actions(self) -> list[str]:
        raw = self.state.get("allowed_actions", [])
        actions = (
            [action for action in raw if isinstance(action, str) and action in ACTIONS]
            if isinstance(raw, list)
            else []
        )
        if (
            self.state.get("status") == "WAITING"
            and self.state.get("phase") == "WORKER"
        ):
            if self.state.get("pending_worker") is not None:
                return self._worker_recovery_actions()
        return actions

    def _read_artifact_text(self, reference: str) -> str:
        assert self.store is not None
        return (self.store.run_dir / reference).read_text()

    def _amendment_text(self, amendment_ids: list[str] | None = None) -> str:
        amendments = self.state["amendments"]
        if amendment_ids is not None:
            by_id = {item["id"]: item for item in amendments}
            amendments = [by_id[amendment_id] for amendment_id in amendment_ids]
        if not amendments:
            return ""
        blocks = ["Persistent prompt amendments (in order):"]
        for amendment in amendments:
            blocks.append(
                f"\n[{amendment['id']}]\n{self._read_artifact_text(amendment['artifact'])}"
            )
        return "\n".join(blocks)

    def _amendment_snapshot(self) -> tuple[list[str], str]:
        return (
            [item["id"] for item in self.state["amendments"]],
            self._amendment_digest(),
        )

    def _amendment_digest(self) -> str:
        return _policy_digest(*(item["sha256"] for item in self.state["amendments"]))

    def _add_amendment(self, text: str) -> None:
        amendment_id = f"amendment-{len(self.state['amendments']) + 1}"
        data = text.encode()
        reference = self._artifact(f"amendments/{amendment_id}.md", data)
        self.state["amendments"].append(
            {
                "id": amendment_id,
                "artifact": reference,
                "sha256": _digest(data),
                "created_at": _now(),
            }
        )
        self._save()

    def _compose_worker(
        self,
        context: str,
        *,
        repair: bool,
        amendment_ids: list[str] | None = None,
    ) -> str:
        role = self._read_artifact_text(
            self.state["prompts"]["repair" if repair else "worker"]
        )
        spec = self._read_artifact_text(self.state["prompts"]["spec"])
        pieces = [role, f"Specification:\n{spec}", f"Current context:\n{context}"]
        amendments = self._amendment_text(amendment_ids)
        if amendments:
            pieces.append(amendments)
        pieces.append(WORKER_FOOTER)
        return "\n\n".join(pieces)

    def _compose_review(
        self,
        role: str,
        mode: str,
        previous: str | None,
        prior: str,
        *,
        amendment_ids: list[str] | None = None,
    ) -> str:
        rubric = self._read_artifact_text(self.state["prompts"]["reviewers"][role])
        wrapper_snapshot = self.state["config"][
            "full_wrapper" if mode == "FULL" else "delta_wrapper"
        ]
        wrapper = wrapper_snapshot.format(
            base_commit=self.state["base_commit"],
            candidate_head=self.state["candidate_head"],
            previous_checkpoint=previous or self.state["base_commit"],
        )
        gate_summary = self._gate_summary()
        spec = self._read_artifact_text(self.state["prompts"]["spec"])
        rubric = rubric.replace("{spec}", spec)
        pieces = [
            rubric,
            wrapper,
            f"Review subject: mode={mode}; base={self.state['base_commit']}; candidate={self.state['candidate_head']}"
            + (f"; previous={previous}" if previous else ""),
            gate_summary,
        ]
        if mode == "DELTA" and prior:
            pieces.append(f"Your cohort's prior review result:\n{prior}")
        amendments = self._amendment_text(amendment_ids)
        if amendments:
            pieces.append(amendments)
        pieces.append(REVIEW_FOOTER)
        return "\n\n".join(pieces)

    def _wait(
        self,
        reason: str,
        actions: list[str],
        continuation: str,
        *,
        completed_attempt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.state["status"] = "WAITING"
        self.state["waiting_reason"] = reason
        self.state["allowed_actions"] = actions
        self.state["continuation_phase"] = continuation
        self._save()
        if completed_attempt is not None:
            self._say_agent_completed(completed_attempt)
        rendered_actions = ",".join(actions) if actions else "(none)"
        self._say(f"Waiting: reason={reason} actions={rendered_actions}")
        return self._report()

    def _require_checkpoint(self, *, allow_descendant: bool = False) -> Checkout:
        cwd = Path(self.state["cwd"])
        actual = self.git.snapshot(cwd)
        if actual.branch != self.state["branch"]:
            raise UsageError("CHECKOUT_DRIFT: work branch changed")
        if not actual.clean:
            raise UsageError("CHECKOUT_DRIFT: checkout is dirty")
        if allow_descendant:
            pending = self.state.get("pending_worker") or {}
            input_head = pending.get("input_head")
            if not isinstance(input_head, str):
                raise UsageError("CHECKOUT_DRIFT: pending Worker input is missing")
            if input_head != self.state["candidate_head"]:
                raise UsageError("CHECKOUT_DRIFT: pending Worker input is stale")
            descendant_head = pending.get("descendant_head")
            if descendant_head is None:
                if actual.head != self.state["candidate_head"]:
                    raise UsageError("CHECKOUT_DRIFT: HEAD is not the saved checkpoint")
                return actual
            if not isinstance(descendant_head, str):
                raise UsageError(
                    "CHECKOUT_DRIFT: recorded Worker descendant is invalid"
                )
            if actual.head != descendant_head:
                raise UsageError(
                    "CHECKOUT_DRIFT: HEAD is not the recorded Worker descendant"
                )
            if (
                descendant_head == input_head
                or not self.git.is_ancestor(cwd, input_head, descendant_head)
                or not self.git.is_linear(cwd, input_head, descendant_head)
            ):
                raise UsageError(
                    "CHECKOUT_DRIFT: recorded Worker descendant is invalid"
                )
            return actual
        expected = self.state["candidate_head"]
        if actual.head == expected:
            return actual
        raise UsageError("CHECKOUT_DRIFT: HEAD is not the saved checkpoint")

    def _accept_descendant_checkpoint(self) -> str:
        actual = self._require_checkpoint(allow_descendant=True)
        if actual.head == self.state["candidate_head"]:
            raise UsageError("no descendant Worker result is available")
        self._freeze_checkpoint(actual.head)
        self.state["pending_worker"] = None
        return actual.head

    def _freeze_checkpoint(self, head: str) -> None:
        cwd = Path(self.state["cwd"])
        previous = self.state["candidate_head"]
        if (
            head == previous
            or not self.git.is_ancestor(cwd, previous, head)
            or not self.git.is_linear(cwd, previous, head)
        ):
            raise UsageError("Worker history is not a clean linear strict descendant")
        self.state["candidate_head"] = head
        self.state["checkpoints"].append(head)
        self.state["gate_attestation"] = None
        self.state["gate_results"] = []
        self.state["certificate"] = None

    @staticmethod
    def _attempt_owner(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "attempt_id": record["id"],
            "review_session_id": record.get("review_session"),
            "role": record["role"],
            "thread_id": record.get("thread_id"),
            "turn_id": record.get("turn_id"),
            "observed_turn_ids": list(record.get("observed_turn_ids", [])),
        }

    def _sync_attempt_artifacts(self, record: dict[str, Any]) -> None:
        owner = self._attempt_owner(record)
        for artifact in self.state.get("artifacts", []):
            if artifact.get("attempt_id") != record["id"]:
                continue
            artifact.update(
                {
                    "review_session_id": owner["review_session_id"],
                    "role": owner["role"],
                    "thread_id": owner["thread_id"],
                    "turn_id": owner["turn_id"],
                    "observed_turn_ids": owner["observed_turn_ids"],
                }
            )

    def _attempt_record(
        self,
        *,
        role: str,
        prompt: str,
        worker_round: int | None = None,
        cohort: str | None = None,
        session: str | None = None,
        reviewer_role: str | None = None,
        amendment_ids: list[str] | None = None,
        amendment_digest: str | None = None,
    ) -> dict[str, Any]:
        attempt_id = f"attempt-{len(self.state['attempts']) + 1}"
        prompt_data = prompt.encode()
        if amendment_ids is None:
            amendment_ids, amendment_digest = self._amendment_snapshot()
        elif amendment_digest is None:
            raise UsageError("amendment digest is required with an amendment snapshot")
        record = {
            "id": attempt_id,
            "role": role,
            "worker_round": worker_round,
            "cohort": cohort,
            "review_session": session,
            "reviewer_role": reviewer_role,
            "thread_id": None,
            "turn_id": None,
            "observed_turn_ids": [],
            "status": "START_INTENT",
            "started_at": _now(),
            "completed_at": None,
            "prompt_artifact": None,
            "prompt_policy_digest": _policy_digest(prompt, amendment_digest),
            "amendment_ids": list(amendment_ids),
            "amendment_digest": amendment_digest,
            "output_artifacts": [],
        }
        assert self.store is not None
        record["prompt_artifact"] = self.store.artifact(
            self.state,
            f"attempts/{attempt_id}/prompt.md",
            prompt_data,
            owner=self._attempt_owner(record),
        )
        if role == "worker":
            pending = dict(self.state.get("pending_worker") or {})
            pending.setdefault("input_head", self.state["candidate_head"])
            pending["attempt_id"] = attempt_id
            self.state["pending_worker"] = pending
        self.state["attempts"].append(record)
        self.state["operation_intent"] = {
            "kind": "agent_start",
            "attempt_id": attempt_id,
        }
        self._save()
        return record

    def _finish_attempt(
        self,
        record: dict[str, Any],
        receipt: DetachReceipt,
        result: AgentResult,
        *,
        preserve_operation_intent: bool = False,
    ) -> None:
        checkout: Checkout | None = None
        if record["role"] == "worker":
            checkout = self.git.snapshot(Path(self.state["cwd"]))
        record["thread_id"] = receipt.thread_id
        record["turn_id"] = receipt.turn_id
        record["observed_turn_ids"] = result.observed_turn_ids
        record["status"] = result.status
        record["completed_at"] = _now()
        self._sync_attempt_artifacts(record)
        terminal_intent: dict[str, Any] = {
            "kind": "agent_terminal",
            "attempt_id": record["id"],
            "role": record["role"],
            "review_session_id": record.get("review_session"),
            "reviewer_role": record.get("reviewer_role"),
            "thread_id": receipt.thread_id,
            "turn_id": receipt.turn_id,
            "observed_turn_ids": list(result.observed_turn_ids),
            "result_status": result.status,
            "message": result.final_message,
            "messages": list(result.messages),
            "raw_jsonl": result.raw_jsonl.hex(),
            "error": result.error,
        }
        if checkout is not None:
            pending = self.state.get("pending_worker") or {}
            terminal_intent.update(
                {
                    "input_head": pending.get("input_head"),
                    "checkout": {
                        "branch": checkout.branch,
                        "head": checkout.head,
                        "status": list(checkout.status),
                    },
                }
            )
        if not preserve_operation_intent:
            self.state["operation_intent"] = terminal_intent
        if result.raw_jsonl:
            reference = self._artifact_once(
                f"attempts/{record['id']}/events.jsonl",
                result.raw_jsonl,
                owner=self._attempt_owner(record),
            )
            if reference not in record["output_artifacts"]:
                record["output_artifacts"].append(reference)
        for index, message in enumerate(result.messages, 1):
            reference = self._artifact_once(
                f"attempts/{record['id']}/agent-message-{index}.md",
                message.encode(),
                owner=self._attempt_owner(record),
            )
            if reference not in record["output_artifacts"]:
                record["output_artifacts"].append(reference)
        if result.error:
            record["error"] = result.error
        self._save()
        self._say_agent_completed(record)

    def _start_agent_attempt(
        self,
        record: dict[str, Any],
        prompt: str,
        *,
        resume_thread: str | None,
        role: str,
        approve: bool,
    ) -> _AgentAttempt:
        """Launch one attempt after durably recording its start intent."""
        assert self.codex is not None
        self.state["operation_intent"] = {
            "kind": "agent_start",
            "attempt_id": record["id"],
        }
        self._save()
        self._say(
            f"Agent starting: role={self._agent_role(record)} attemptId={record['id']}"
        )
        try:
            if resume_thread:
                receipt = self.codex.resume(thread_id=resume_thread, prompt=prompt)
            else:
                receipt = self.codex.start(
                    prompt=prompt,
                    cwd=Path(self.state["cwd"]),
                    role=role,
                    approve=approve,
                    model=self.state["config"]["model"],
                    effort=self.state["config"]["effort"],
                )
        except Exception as exc:
            error = str(exc)
            record["status"] = "unknown"
            record["error"] = error
            record["completed_at"] = _now()
            return _AgentAttempt(record, None, AgentResult("unknown", error=error))
        return _AgentAttempt(record, receipt)

    def _detach_agent_attempt(self, attempt: _AgentAttempt) -> None:
        """Persist the receipt and make its exact turn the recovery target."""
        if attempt.receipt is None:
            raise AssertionError("cannot detach an attempt without a receipt")
        record = attempt.record
        receipt = attempt.receipt
        record["thread_id"] = receipt.thread_id
        record["turn_id"] = receipt.turn_id
        record["status"] = "DETACHED"
        self._sync_attempt_artifacts(record)
        self.state["operation_intent"] = {
            "kind": "agent_follow",
            "attempt_id": record["id"],
            "thread_id": receipt.thread_id,
            "turn_id": receipt.turn_id,
        }
        self._save()
        self._say_agent_detached(record)

    def _follow_agent_attempt(self, attempt: _AgentAttempt) -> AgentResult:
        """Follow the recorded turn and normalize runtime exceptions."""
        if attempt.result is not None:
            return attempt.result
        if attempt.receipt is None:
            raise AssertionError("cannot follow an attempt without a receipt")
        assert self.codex is not None
        try:
            result = self.codex.follow(
                thread_id=attempt.receipt.thread_id,
                turn_id=attempt.receipt.turn_id,
            )
        except Exception as exc:
            result = AgentResult("unknown", error=str(exc))
        attempt.result = result
        return result

    def _finalize_agent_attempt(
        self, attempt: _AgentAttempt, result: AgentResult
    ) -> None:
        """Finalize a detached attempt through the durable attempt seam."""
        if attempt.receipt is not None:
            self._finish_attempt(attempt.record, attempt.receipt, result)

    def _prepare_review_finalization(
        self,
        session: dict[str, Any],
        cohort: dict[str, Any],
        mode: str,
        candidate: str,
    ) -> dict[str, Any]:
        intent: dict[str, Any] = {
            "kind": "review_finalize",
            "review_session_id": session["id"],
            "cohort_id": cohort["id"],
            "mode": mode,
            "candidate_head": candidate,
        }
        if mode == "DELTA":
            intent["successor"] = self._full_rotation_target(candidate)
        self.state["operation_intent"] = intent
        self._save()
        return self._finalize_review(
            session,
            cohort,
            mode,
            candidate,
            rotation=intent.get("successor"),
        )

    def _full_rotation_target(self, candidate: str) -> dict[str, Any]:
        amendment_ids, amendment_digest = self._amendment_snapshot()
        return {
            "mode": "FULL",
            "cohort_id": f"cohort-{len(self.state['cohorts']) + 1}",
            "review_session_id": f"review-{len(self.state['review_sessions']) + 1}",
            "base_commit": self.state["base_commit"],
            "previous_checkpoint": None,
            "candidate_head": candidate,
            "gate_policy_digest": self.state["config"]["gate_policy_digest"],
            "policy_digest": _policy_digest(
                "FULL",
                candidate,
                amendment_digest,
                self.state["config"]["gate_policy_digest"],
            ),
            "amendment_ids": amendment_ids,
            "amendment_digest": amendment_digest,
        }

    def _invoke_worker(
        self,
        prompt: str,
        *,
        resume_thread: str | None = None,
        amendment_ids: list[str],
        amendment_digest: str,
    ) -> tuple[dict[str, Any], AgentResult]:
        assert self.codex is not None
        round_number = self.state["worker_rounds_used"] + (0 if resume_thread else 1)
        record = self._attempt_record(
            role="worker",
            prompt=prompt,
            worker_round=round_number,
            amendment_ids=amendment_ids,
            amendment_digest=amendment_digest,
        )
        attempt = self._start_agent_attempt(
            record,
            prompt,
            resume_thread=resume_thread,
            role="worker",
            approve=bool(self.state["config"]["worker_approve_for_me"]),
        )
        if attempt.receipt is None:
            assert attempt.result is not None
            return record, attempt.result
        if resume_thread is None:
            self.state["worker_rounds_used"] += 1
        self._detach_agent_attempt(attempt)
        result = self._follow_agent_attempt(attempt)
        self._finalize_agent_attempt(attempt, result)
        return record, result

    def _run_worker(self, *, context: str) -> dict[str, Any]:
        self._require_checkpoint()
        input_head = self.state["candidate_head"]
        repair = input_head != self.state["base_commit"] or bool(
            self.state["review_sessions"] or self.state["gate_results"]
        )
        amendment_ids, amendment_digest = self._amendment_snapshot()
        prompt = self._compose_worker(
            context, repair=repair, amendment_ids=amendment_ids
        )
        self.state["phase"] = "WORKER"
        self.state["pending_worker"] = {"input_head": input_head}
        record, result = self._invoke_worker(
            prompt,
            amendment_ids=amendment_ids,
            amendment_digest=amendment_digest,
        )
        return self._handle_worker_result(record, result, input_head)

    def _continue_worker(self) -> dict[str, Any]:
        pending = self.state.get("pending_worker") or {}
        attempt_id = pending.get("attempt_id")
        original = next(
            (item for item in self.state["attempts"] if item["id"] == attempt_id), None
        )
        if not original or not original.get("thread_id"):
            raise UsageError("no interrupted Worker thread can be continued")
        input_head = str(pending["input_head"])
        amendment_ids, amendment_digest = self._amendment_snapshot()
        prompt = self._compose_worker(
            "Continue the interrupted Worker attempt. Inspect its existing clean descendant commits and complete the requested work.",
            repair=True,
            amendment_ids=amendment_ids,
        )
        record, result = self._invoke_worker(
            prompt,
            resume_thread=str(original["thread_id"]),
            amendment_ids=amendment_ids,
            amendment_digest=amendment_digest,
        )
        return self._handle_worker_result(record, result, input_head)

    def _handle_worker_result(
        self,
        record: dict[str, Any],
        result: AgentResult,
        input_head: str,
        *,
        expected_checkout: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        intent = self.state.get("operation_intent") or {}
        start_failure = (
            record
            if intent.get("kind") == "agent_start"
            and intent.get("attempt_id") == record["id"]
            else None
        )

        def wait(reason: str, actions: list[str], continuation: str) -> dict[str, Any]:
            return self._wait(
                reason,
                actions,
                continuation,
                completed_attempt=start_failure,
            )

        cwd = Path(self.state["cwd"])
        actual = self.git.snapshot(cwd)
        if expected_checkout is not None and (
            actual.branch != expected_checkout.get("branch")
            or actual.head != expected_checkout.get("head")
            or list(actual.status) != expected_checkout.get("status")
        ):
            self.state["operation_intent"] = None
            return wait("CHECKOUT_DRIFT", [], "WORKER")
        if actual.branch != self.state["branch"]:
            self.state["operation_intent"] = None
            return wait("CHECKOUT_DRIFT", [], "WORKER")
        if not actual.clean:
            self.state["operation_intent"] = None
            return wait("WORKER_CONTRACT_VIOLATION", [], "WORKER")
        descendant = (
            actual.head != input_head
            and self.git.is_ancestor(cwd, input_head, actual.head)
            and self.git.is_linear(cwd, input_head, actual.head)
        )
        if actual.head != input_head and not descendant:
            self.state["operation_intent"] = None
            return wait("CHECKOUT_DRIFT", [], "WORKER")
        self.state["operation_intent"] = None
        if result.status == "unexpected_continuation":
            self.state["pending_worker"] = {
                "input_head": input_head,
                "attempt_id": record["id"],
                "descendant_head": actual.head if descendant else None,
            }
            return wait("UNEXPECTED_CONTINUATION", [], "WORKER")
        if result.status != "completed":
            actions = _worker_recovery_action_policy(
                result.status,
                record.get("thread_id"),
                has_descendant=descendant,
            )
            self.state["pending_worker"] = {
                "input_head": input_head,
                "attempt_id": record["id"],
                "descendant_head": actual.head if descendant else None,
            }
            return wait(
                "WORKER_INTERRUPTED"
                if result.status in {"failed", "interrupted"}
                else "AGENT_OUTCOME_UNKNOWN",
                actions,
                "WORKER",
            )
        if not descendant:
            self.state["pending_worker"] = None
            return wait(
                "WORKER_NO_CHANGE",
                ["START_NEXT_ROUND", "REQUIRE_FRESH_AUDIT"],
                "WORKER",
            )
        self._freeze_checkpoint(actual.head)
        self.state["pending_worker"] = None
        return self._ensure_gates(advanced_checkpoint=actual.head)

    def _gate_summary(self) -> str:
        attestation = self.state.get("gate_attestation")
        if not self.state["config"]["gates"]:
            return f"Gate summary for {self.state['candidate_head']}: no dynamic gates were configured."
        if not attestation:
            return f"Gate summary for {self.state['candidate_head']}: no valid attestation."
        commands = "\n".join(
            f"- {command}" for command in self.state["config"]["gates"]
        )
        return (
            f"Gate summary for {attestation['candidate_head']}: "
            f"all {len(attestation['results'])} configured gates passed.\n"
            f"Configured gate commands:\n{commands}\n"
            "Passing gates establish command exit only, not test sufficiency or "
            "spec compliance."
        )

    def _valid_attestation(self) -> bool:
        value = self.state.get("gate_attestation")
        return bool(
            value
            and value["candidate_head"] == self.state["candidate_head"]
            and value["policy_digest"] == self.state["config"]["gate_policy_digest"]
        )

    def _ensure_gates(
        self,
        *,
        force_full: bool = False,
        advanced_checkpoint: str | None = None,
    ) -> dict[str, Any]:
        self._require_checkpoint()
        self.state["phase"] = "VERIFY_CHECKPOINT"
        self.state["force_full"] = force_full or bool(self.state.get("force_full"))
        self._save()
        if advanced_checkpoint is not None:
            if advanced_checkpoint != self.state["candidate_head"]:
                raise AssertionError("checkpoint announcement does not match candidate")
            self._say(f"Checkpoint advanced: commit={advanced_checkpoint}")
        if self._valid_attestation():
            return self._review(
                mode="FULL"
                if self.state["force_full"] or not self.state.get("development_cohort")
                else "DELTA"
            )
        commands = self.state["config"]["gates"]
        candidate = self.state["candidate_head"]
        policy = self.state["config"]["gate_policy_digest"]
        results = [
            item
            for item in self.state["gate_results"]
            if item["candidate_head"] == candidate and item["policy_digest"] == policy
        ]
        for index, command in enumerate(commands):
            existing = next(
                (
                    item
                    for item in results
                    if item["index"] == index and item["status"] == "passed"
                ),
                None,
            )
            if existing:
                continue
            before = self.git.snapshot(Path(self.state["cwd"]))
            intent = {
                "kind": "gate",
                "candidate_head": candidate,
                "index": index,
                "command_digest": _digest(command.encode()),
            }
            self.state["operation_intent"] = intent
            self._save()
            self._say(
                f"Gate starting: gate={index + 1}/{len(commands)} "
                f"checkpoint={candidate}"
            )
            execution = self.gates.run(
                command,
                Path(self.state["cwd"]),
                int(self.state["config"]["gate_timeout_seconds"]),
            )
            after = self.git.snapshot(Path(self.state["cwd"]))
            stdout_ref = self._artifact(
                f"gates/{candidate}/gate-{index + 1}.stdout", execution.stdout
            )
            stderr_ref = self._artifact(
                f"gates/{candidate}/gate-{index + 1}.stderr", execution.stderr
            )
            record = {
                "candidate_head": candidate,
                "policy_digest": policy,
                "index": index,
                "command": command,
                "status": execution.status,
                "returncode": execution.returncode,
                "stdout_artifact": stdout_ref,
                "stderr_artifact": stderr_ref,
                "error": execution.error,
                "completed_at": _now(),
            }
            self.state["gate_results"] = [
                item
                for item in self.state["gate_results"]
                if not (
                    item["candidate_head"] == candidate
                    and item["policy_digest"] == policy
                    and item["index"] == index
                )
            ]
            self.state["gate_results"].append(record)
            self.state["operation_intent"] = None
            self._save()
            self._say(
                f"Gate completed: gate={index + 1}/{len(commands)} "
                f"status={execution.status} stdoutArtifact={stdout_ref} "
                f"stderrArtifact={stderr_ref}"
            )
            if before != after:
                return self._wait("GATE_MUTATED_CHECKOUT", [], "VERIFY_CHECKPOINT")
            if execution.status == "execution_error":
                return self._wait(
                    "GATE_EXECUTION_ERROR",
                    ["RETRY_GATES", "REQUIRE_FRESH_AUDIT"],
                    "VERIFY_CHECKPOINT",
                )
            if execution.status == "failed":
                if (
                    self.state["worker_rounds_used"]
                    < self.state["config"]["max_auto_worker_rounds"]
                ):
                    return self._run_worker(context=self._blocking_context())
                return self._wait(
                    "GATE_FAILED",
                    ["START_NEXT_ROUND", "REQUIRE_FRESH_AUDIT"],
                    "VERIFY_CHECKPOINT",
                )
        self.state["gate_attestation"] = {
            "candidate_head": candidate,
            "policy_digest": policy,
            "results": [
                item["index"]
                for item in self.state["gate_results"]
                if item["candidate_head"] == candidate
                and item["policy_digest"] == policy
                and item["status"] == "passed"
            ],
            "created_at": _now(),
        }
        self._save()
        self._say(f"Gates completed: checkpoint={candidate} count={len(commands)}")
        return self._review(
            mode="FULL"
            if self.state["force_full"] or not self.state.get("development_cohort")
            else "DELTA"
        )

    def _store_reviewer_result(
        self,
        session: dict[str, Any],
        role: str,
        record: dict[str, Any],
        receipt: DetachReceipt | None,
        result: AgentResult,
        *,
        agent_completion_pending: bool = False,
    ) -> None:
        existing = session["results"].get(role)
        if existing is not None and existing.get("attempt_id") == record["id"]:
            if existing.get("thread_id") != record.get("thread_id") or existing.get(
                "turn_id"
            ) != record.get("turn_id"):
                raise UsageError("invalid persisted Reviewer result evidence")
            return
        item = {
            "status": result.status,
            "attempt_id": record["id"],
            "thread_id": record.get("thread_id"),
            "turn_id": record.get("turn_id"),
            "observed_turn_ids": list(record.get("observed_turn_ids", [])),
            "message_artifact": None,
            "message": result.final_message or "",
            "verdict": None,
            "error": result.error,
        }
        if result.final_message:
            message_data = result.final_message.encode()
            review_prefix = f"reviews/{session['id']}/"
            message_artifact = next(
                (
                    artifact["path"]
                    for artifact in self.state.get("artifacts", [])
                    if artifact.get("attempt_id") == record["id"]
                    and artifact.get("review_session_id") == session["id"]
                    and artifact.get("path", "").startswith(review_prefix)
                    and artifact.get("sha256") == _digest(message_data)
                    and artifact.get("size") == len(message_data)
                ),
                None,
            )
            if message_artifact is None:
                message_artifact = self._artifact_once(
                    f"reviews/{session['id']}/{_slug(role)}.md",
                    message_data,
                    owner=self._attempt_owner(record),
                )
            item["message_artifact"] = message_artifact
            if message_artifact not in record["output_artifacts"]:
                record["output_artifacts"].append(message_artifact)
        if result.status == "completed" and result.final_message:
            try:
                item["verdict"] = _verdict(result.final_message)
            except UsageError as exc:
                item["status"] = "ambiguous"
                item["error"] = str(exc)
        elif result.status == "completed":
            item["status"] = "protocol_error"
            item["error"] = "completed turn had no agentMessage"
        session["results"][role] = item
        self._save()
        if agent_completion_pending:
            self._say_agent_completed(record)
        verdict = item["verdict"] or "unavailable"
        self._say(
            f"Reviewer completed: reviewSessionId={session['id']} role={role} "
            f"attemptId={record['id']} status={item['status']} verdict={verdict} "
            f"messageArtifact={item['message_artifact'] or '(none)'}"
        )

    def _mark_pending_reviewer_starts_unknown(self, session: dict[str, Any]) -> None:
        for pending in self.state["attempts"]:
            if (
                pending["role"] == "reviewer"
                and pending.get("review_session") == session["id"]
                and pending["status"] == "START_INTENT"
            ):
                pending["status"] = "unknown"
                pending["completed_at"] = _now()
                pending["error"] = "crash occurred before reviewer start was recorded"
                self._store_reviewer_result(
                    session,
                    str(pending["reviewer_role"]),
                    pending,
                    None,
                    AgentResult("unknown", error=pending["error"]),
                    agent_completion_pending=True,
                )

    def _finalize_review(
        self,
        session: dict[str, Any],
        cohort: dict[str, Any],
        mode: str,
        candidate: str,
        *,
        rotation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            self._require_checkpoint()
        except UsageError:
            self.state["operation_intent"] = None
            return self._wait("CHECKOUT_DRIFT", [], "REVIEW")
        failures = [
            item
            for item in session["results"].values()
            if item.get("status") != "completed"
        ]
        if failures or len(session["results"]) != len(self.state["reviewer_roles"]):
            session["status"] = "INCOMPLETE"
            self._save()
            reason = (
                "UNEXPECTED_CONTINUATION"
                if any(
                    item.get("status") == "unexpected_continuation"
                    for item in session["results"].values()
                )
                else "REVIEWER_FAILURE"
            )
            self.state["operation_intent"] = None
            return self._wait(
                reason,
                ["RETRY_REVIEWERS", "REQUIRE_FRESH_AUDIT"],
                "REVIEW",
            )
        session["status"] = (
            "PASSED"
            if all(item["verdict"] == "PASS" for item in session["results"].values())
            else "FAILED"
        )
        session["completed_at"] = _now()
        cohort["last_checkpoint"] = candidate
        self.state["force_full"] = False
        self._save()
        self._say(
            f"Review completed: reviewSessionId={session['id']} "
            f"status={session['status']}"
        )
        if session["status"] == "FAILED":
            if mode == "FULL":
                self.state["development_cohort"] = cohort["id"]
            if (
                self.state["worker_rounds_used"]
                < self.state["config"]["max_auto_worker_rounds"]
            ):
                return self._run_worker(context=self._blocking_context())
            self.state["operation_intent"] = None
            return self._wait(
                "REVIEW_FAILED",
                ["START_NEXT_ROUND", "ACCEPT_FINDINGS", "REQUIRE_FRESH_AUDIT"],
                "REVIEW",
            )
        if mode == "DELTA":
            rotation = rotation or self._full_rotation_target(candidate)
            intent = self.state.get("operation_intent")
            if (
                not isinstance(intent, dict)
                or intent.get("kind") != "review_finalize"
                or intent.get("review_session_id") != session["id"]
            ):
                self.state["operation_intent"] = {
                    "kind": "review_finalize",
                    "review_session_id": session["id"],
                    "cohort_id": cohort["id"],
                    "mode": mode,
                    "candidate_head": candidate,
                    "successor": rotation,
                }
                self._save()
            elif "successor" not in intent:
                intent["successor"] = rotation
                self._save()
            self.state["development_cohort"] = None
            self._save()
            return self._review(mode="FULL", rotation=rotation)
        for older in self.state["review_sessions"][:-1]:
            if older["status"] in BLOCKING_REVIEW_STATUSES:
                older["status"] = "SUPERSEDED_BY_FULL_AUDIT"
        self.state["certificate"] = {
            "candidate_head": candidate,
            "cohort_id": cohort["id"],
            "review_session_id": session["id"],
            "policy_digest": session["policy_digest"],
            "created_at": _now(),
        }
        self.state["status"] = "READY_CERTIFIED"
        self.state["phase"] = "READY_CERTIFIED"
        self.state["waiting_reason"] = None
        self.state["allowed_actions"] = []
        self.state["operation_intent"] = None
        self._save()
        self._say("Terminal result: status=READY_CERTIFIED")
        return self._report()

    def _review(
        self,
        mode: str | None = None,
        *,
        retry: bool = False,
        rotation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_checkpoint()
        if not self._valid_attestation():
            raise UsageError("review requires a current gate attestation")
        self.state["phase"] = "REVIEW"
        candidate = self.state["candidate_head"]
        session_started = False
        if rotation is not None:
            if retry or mode != "FULL":
                raise UsageError("invalid FULL audit rotation")
            self._validate_full_rotation_target(rotation, candidate)
            mode = "FULL"
            amendment_ids = list(rotation["amendment_ids"])
            amendment_digest = str(rotation["amendment_digest"])
            cohort = next(
                (
                    item
                    for item in self.state["cohorts"]
                    if item["id"] == rotation["cohort_id"]
                ),
                None,
            )
            if cohort is None:
                cohort = {
                    "id": rotation["cohort_id"],
                    "fresh": True,
                    "threads": {},
                    "created_at": _now(),
                    "last_checkpoint": None,
                }
                self.state["cohorts"].append(cohort)
            elif (
                not cohort.get("fresh")
                or not isinstance(cohort.get("threads"), dict)
                or cohort.get("last_checkpoint") not in {None, candidate}
            ):
                raise UsageError("invalid persisted FULL audit rotation cohort")
            session = next(
                (
                    item
                    for item in self.state["review_sessions"]
                    if item["id"] == rotation["review_session_id"]
                ),
                None,
            )
            if session is None:
                session = {
                    "id": rotation["review_session_id"],
                    "mode": "FULL",
                    "cohort_id": cohort["id"],
                    "base_commit": rotation["base_commit"],
                    "previous_checkpoint": rotation["previous_checkpoint"],
                    "candidate_head": rotation["candidate_head"],
                    "policy_digest": rotation["policy_digest"],
                    "amendment_ids": amendment_ids,
                    "amendment_digest": amendment_digest,
                    "results": {},
                    "status": "RUNNING",
                    "created_at": _now(),
                }
                self.state["review_sessions"].append(session)
                session_started = True
            else:
                self._validate_full_rotation_session(session, rotation, cohort)
            self._mark_pending_reviewer_starts_unknown(session)
            roles = [
                name
                for name in self.state["reviewer_roles"]
                if session["results"].get(name, {}).get("status") != "completed"
            ]
        elif retry:
            session = self.state["review_sessions"][-1]
            mode = session["mode"]
            amendment_ids = list(session["amendment_ids"])
            amendment_digest = str(session["amendment_digest"])
            cohort = next(
                item
                for item in self.state["cohorts"]
                if item["id"] == session["cohort_id"]
            )
            roles = [
                name
                for name in self.state["reviewer_roles"]
                if session["results"].get(name, {}).get("status") != "completed"
            ]
        else:
            mode = mode or ("DELTA" if self.state.get("development_cohort") else "FULL")
            amendment_ids, amendment_digest = self._amendment_snapshot()
            if mode == "FULL":
                cohort = {
                    "id": f"cohort-{len(self.state['cohorts']) + 1}",
                    "fresh": True,
                    "threads": {},
                    "created_at": _now(),
                    "last_checkpoint": None,
                }
                self.state["cohorts"].append(cohort)
            else:
                cohort = next(
                    item
                    for item in self.state["cohorts"]
                    if item["id"] == self.state["development_cohort"]
                )
            session = {
                "id": f"review-{len(self.state['review_sessions']) + 1}",
                "mode": mode,
                "cohort_id": cohort["id"],
                "base_commit": self.state["base_commit"],
                "previous_checkpoint": cohort.get("last_checkpoint"),
                "candidate_head": candidate,
                "policy_digest": _policy_digest(
                    mode,
                    candidate,
                    amendment_digest,
                    self.state["config"]["gate_policy_digest"],
                ),
                "amendment_ids": amendment_ids,
                "amendment_digest": amendment_digest,
                "results": {},
                "status": "RUNNING",
                "created_at": _now(),
            }
            self.state["review_sessions"].append(session)
            session_started = True
            roles = list(self.state["reviewer_roles"])
        self._save()
        self._say(
            f"Review session {'started' if session_started else 'resumed'}: "
            f"reviewSessionId={session['id']} "
            f"mode={mode} checkpoint={candidate}"
        )

        if rotation is not None and session["status"] != "RUNNING":
            return self._prepare_review_finalization(session, cohort, "FULL", candidate)

        tasks: list[tuple[str, dict[str, Any], str, bool]] = []
        for role in roles:
            previous = session.get("previous_checkpoint")
            prior = ""
            if mode == "DELTA":
                prior_sessions = [
                    item
                    for item in self.state["review_sessions"][:-1]
                    if item["cohort_id"] == cohort["id"]
                ]
                if prior_sessions:
                    prior_result = prior_sessions[-1]["results"].get(role, {})
                    prior = str(prior_result.get("message", ""))
            prompt = self._compose_review(
                role,
                str(mode),
                previous,
                prior,
                amendment_ids=amendment_ids,
            )
            record = self._attempt_record(
                role="reviewer",
                prompt=prompt,
                cohort=cohort["id"],
                session=session["id"],
                reviewer_role=role,
                amendment_ids=amendment_ids,
                amendment_digest=amendment_digest,
            )
            prior_status = session["results"].get(role, {}).get("status")
            resume_existing = mode == "DELTA" or (retry and prior_status == "ambiguous")
            tasks.append((role, record, prompt, resume_existing))

        started: list[tuple[str, _AgentAttempt]] = []
        assert self.codex is not None
        for role, record, prompt, resume_existing in tasks:
            resume_thread = (
                str(cohort["threads"][role])
                if resume_existing and role in cohort["threads"]
                else None
            )
            attempt = self._start_agent_attempt(
                record,
                prompt,
                resume_thread=resume_thread,
                role="reviewer",
                approve=False,
            )
            if attempt.receipt is None:
                assert attempt.result is not None
                self._store_reviewer_result(
                    session,
                    role,
                    record,
                    None,
                    attempt.result,
                    agent_completion_pending=True,
                )
                self._save()
                continue
            cohort["threads"][role] = attempt.receipt.thread_id
            self._detach_agent_attempt(attempt)
            started.append((role, attempt))

        def follow(
            task: tuple[str, _AgentAttempt],
        ) -> tuple[str, _AgentAttempt, AgentResult]:
            role, attempt = task
            return role, attempt, self._follow_agent_attempt(attempt)

        # Agent execution is concurrent; Git and state mutations remain orchestrator-owned.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(roles) or 1
        ) as executor:
            futures = [executor.submit(follow, task) for task in started]
            for future in concurrent.futures.as_completed(futures):
                role, attempt, result = future.result()
                self._finalize_agent_attempt(attempt, result)
                assert attempt.receipt is not None
                self._store_reviewer_result(
                    session, role, attempt.record, attempt.receipt, result
                )

        return self._prepare_review_finalization(session, cohort, str(mode), candidate)

    def _validate_full_rotation_target(
        self, target: dict[str, Any], candidate: str
    ) -> None:
        required = {
            "mode",
            "cohort_id",
            "review_session_id",
            "base_commit",
            "previous_checkpoint",
            "candidate_head",
            "gate_policy_digest",
            "policy_digest",
            "amendment_ids",
            "amendment_digest",
        }
        if not required.issubset(target):
            raise UsageError("invalid persisted FULL audit rotation target")
        amendment_ids = target["amendment_ids"]
        amendment_digest = target["amendment_digest"]
        amendment_digests: list[str] = []
        by_id = {item["id"]: item["sha256"] for item in self.state["amendments"]}
        if isinstance(amendment_ids, list):
            try:
                amendment_digests = [
                    by_id[amendment_id] for amendment_id in amendment_ids
                ]
            except KeyError, TypeError:
                pass
        if (
            target["mode"] != "FULL"
            or target["candidate_head"] != candidate
            or target["base_commit"] != self.state["base_commit"]
            or target["previous_checkpoint"] is not None
            or target["gate_policy_digest"]
            != self.state["config"]["gate_policy_digest"]
            or not isinstance(amendment_ids, list)
            or not isinstance(amendment_digest, str)
            or amendment_digest != _policy_digest(*amendment_digests)
            or target["policy_digest"]
            != _policy_digest(
                "FULL",
                candidate,
                amendment_digest,
                self.state["config"]["gate_policy_digest"],
            )
        ):
            raise UsageError("invalid persisted FULL audit rotation target")

    def _validate_full_rotation_session(
        self,
        session: dict[str, Any],
        target: dict[str, Any],
        cohort: dict[str, Any],
    ) -> None:
        if (
            session.get("mode") != "FULL"
            or session.get("cohort_id") != cohort["id"]
            or session.get("base_commit") != target["base_commit"]
            or session.get("previous_checkpoint") != target["previous_checkpoint"]
            or session.get("candidate_head") != target["candidate_head"]
            or session.get("policy_digest") != target["policy_digest"]
            or session.get("amendment_ids") != target["amendment_ids"]
            or session.get("amendment_digest") != target["amendment_digest"]
            or not isinstance(session.get("results"), dict)
        ):
            raise UsageError("invalid persisted FULL audit rotation session")

    def _blocking_context(self) -> str:
        assert self.store is not None
        blocks: list[str] = []
        candidate = self.state["candidate_head"]
        failed_gates = [
            item
            for item in self.state["gate_results"]
            if item["candidate_head"] == candidate and item["status"] == "failed"
        ]
        for gate in failed_gates:
            blocks.append(
                f"Gate failed: {gate['command']}\nstdout: {self.store.run_dir / gate['stdout_artifact']}\nstderr: {self.store.run_dir / gate['stderr_artifact']}"
            )
        sessions = [
            item
            for item in self.state["review_sessions"]
            if item["candidate_head"] == candidate and item["status"] == "FAILED"
        ]
        if sessions:
            blocks.append("Current blocking reviewer results:")
            for role, result in sessions[-1]["results"].items():
                blocks.append(f"[{role}] {result['verdict']}\n{result['message']}")
        return (
            "\n\n".join(blocks)
            or "Start the next authorized implementation round from the current checkpoint."
        )

    def _waive(self) -> dict[str, Any]:
        self._require_checkpoint()
        if not self._valid_attestation():
            raise UsageError(
                "findings cannot be accepted without current passing gates"
            )
        session = (
            self.state["review_sessions"][-1] if self.state["review_sessions"] else None
        )
        if (
            not session
            or session["candidate_head"] != self.state["candidate_head"]
            or session["status"] != "FAILED"
        ):
            raise UsageError("only an explicit complete review failure can be waived")
        self.state["waiver"] = {
            "candidate_head": self.state["candidate_head"],
            "review_session_id": session["id"],
            "created_at": _now(),
        }
        session["status"] = "WAIVED"
        self.state["certificate"] = None
        self.state["status"] = "READY_WITH_WAIVER"
        self.state["phase"] = "READY_WITH_WAIVER"
        self.state["allowed_actions"] = []
        self.state["waiting_reason"] = None
        self._save()
        self._say("Terminal result: status=READY_WITH_WAIVER")
        return self._report()


def _reviewer_argument(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("reviewer must use NAME=PATH")
    name, path = value.split("=", 1)
    if not name or not path:
        raise argparse.ArgumentTypeError("reviewer must use NAME=PATH")
    return name, Path(path).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="impl-review-orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--state-dir", type=Path)
    start.add_argument("--output", choices=("text", "json"), default="text")
    start.add_argument("--cwd", type=Path, required=True)
    start.add_argument("--spec", type=Path, required=True)
    start.add_argument("--worker-prompt", type=Path, required=True)
    start.add_argument("--repair-prompt", type=Path, required=True)
    start.add_argument(
        "--reviewer", action="append", type=_reviewer_argument, required=True
    )
    start.add_argument("--gate", action="append", default=[])
    start.add_argument("--branch")
    start.add_argument("--worker-approve-for-me", action="store_true")
    start.add_argument("--max-auto-worker-rounds", type=int, default=2)
    start.add_argument("--gate-timeout-seconds", type=int, default=1800)
    start.add_argument("--model")
    start.add_argument("--effort")
    start.add_argument("--codexctl", default="codexctl")
    start.add_argument("--run-id")
    resume = sub.add_parser("resume")
    resume.add_argument("--state-dir", type=Path)
    resume.add_argument("--output", choices=("text", "json"), default="text")
    resume.add_argument("run_id")
    resume.add_argument("--action", choices=sorted(ACTIONS), required=True)
    amendment = resume.add_mutually_exclusive_group()
    amendment.add_argument("--additional-prompt")
    amendment.add_argument("--additional-prompt-file", type=Path)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--state-dir", type=Path)
    inspect.add_argument("--output", choices=("text", "json"), default="text")
    inspect.add_argument("run_id")
    return parser


def _render(report: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(f"Run: {report['runId']}")
    print(f"Status: {report['status']}")
    print(f"Phase: {report['phase']}")
    print(f"Branch: {report['branch']}")
    print(f"Checkpoint: {report['candidateHead']}")
    if report["waitingReason"]:
        print(f"Waiting: {report['waitingReason']}")
        print(f"Allowed actions: {', '.join(report['allowedActions']) or '(none)'}")


def _render_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        try:
            args = parser.parse_args(argv)
        except SystemExit as exc:
            return 0 if exc.code == 0 else 1
        workflow = Workflow(state_dir=args.state_dir, progress=_render_progress)
        if args.command == "start":
            config = RunConfig(
                cwd=args.cwd.resolve(),
                spec_path=args.spec.resolve(),
                worker_prompt_path=args.worker_prompt.resolve(),
                repair_prompt_path=args.repair_prompt.resolve(),
                reviewers=tuple(args.reviewer),
                gates=tuple(args.gate),
                branch=args.branch,
                worker_approve_for_me=args.worker_approve_for_me,
                max_auto_worker_rounds=args.max_auto_worker_rounds,
                gate_timeout_seconds=args.gate_timeout_seconds,
                model=args.model,
                effort=args.effort,
                codexctl=args.codexctl,
                run_id=args.run_id or uuid.uuid4().hex,
                state_dir=args.state_dir,
            )
            report = workflow.start(config)
        elif args.command == "resume":
            prompt = args.additional_prompt
            if args.additional_prompt_file:
                prompt = (
                    sys.stdin.read()
                    if str(args.additional_prompt_file) == "-"
                    else _read_text(args.additional_prompt_file, "additional prompt")
                )
            report = workflow.resume(args.run_id, args.action, additional_prompt=prompt)
        else:
            report = workflow.inspect(args.run_id)
    except (OrchestratorError, OSError) as exc:
        print(f"impl-review-orchestrator: {exc}", file=sys.stderr)
        return 1
    _render(report, args.output == "json")
    return int(report["exitCode"])


if __name__ == "__main__":
    raise SystemExit(main())

"""Application model: commands, outcomes, projected events, selectors, error codes.

This module defines the closed vocabulary exchanged across the external
``CodexCtl.run(Command)`` seam. Nothing in here knows about JSON-RPC,
sockets, or the Codex protocol wire format.
"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, AsyncIterator, Literal

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorCode(str, Enum):
    """Stable codexctl-owned error codes (public contract)."""

    THREAD_NOT_FOUND = "THREAD_NOT_FOUND"
    THREAD_BUSY = "THREAD_BUSY"
    NO_ACTIVE_TURN = "NO_ACTIVE_TURN"
    TURN_NOT_STEERABLE = "TURN_NOT_STEERABLE"
    TURN_FAILED = "TURN_FAILED"
    TURN_INTERRUPTED = "TURN_INTERRUPTED"
    APP_SERVER_UNAVAILABLE = "APP_SERVER_UNAVAILABLE"
    APP_SERVER_PROTOCOL_ERROR = "APP_SERVER_PROTOCOL_ERROR"
    THREAD_RECOVERY_FAILED = "THREAD_RECOVERY_FAILED"
    UNSUPPORTED_INTERACTION = "UNSUPPORTED_INTERACTION"
    OUTPUT_MODE_NOT_SUPPORTED = "OUTPUT_MODE_NOT_SUPPORTED"
    USAGE_ERROR = "USAGE_ERROR"
    INCOMPATIBLE_CODEX = "INCOMPATIBLE_CODEX"


class CodexCtlError(Exception):
    """Application error carrying a stable public code."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.cause = cause

    def to_document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.thread_id is not None:
            doc["threadId"] = self.thread_id
        if self.turn_id is not None:
            doc["turnId"] = self.turn_id
        return doc


class UsageError(CodexCtlError):
    """Command-line usage error (always maps to exit code 2).

    Usage errors carry their own stable ``USAGE_ERROR`` code;
    ``OUTPUT_MODE_NOT_SUPPORTED`` stays reserved for output-mode matrix
    rejections.
    """

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.USAGE_ERROR, message)


# ---------------------------------------------------------------------------
# Start configuration
# ---------------------------------------------------------------------------


class SandboxPolicy(Enum):
    """Domain sandbox presets accepted by ``start --sandbox``.

    These values are deliberately independent of the app-server wire enum;
    serialization belongs to the app-server adapter.
    """

    readOnly = auto()
    workspaceWrite = auto()
    dangerFullAccess = auto()


DEFAULT_SANDBOX_POLICY = SandboxPolicy.workspaceWrite


class ApprovalPolicy(Enum):
    """Domain approval policy accepted by ``start``.

    These values are deliberately independent of the app-server wire enum;
    serialization belongs to the app-server adapter.
    """

    untrusted = auto()
    onRequest = auto()
    never = auto()


class ApprovalsReviewer(Enum):
    """Domain reviewer that resolves escalated approval requests.

    Independent of the app-server wire enum; serialization belongs to the
    app-server adapter.
    """

    user = auto()
    autoReview = auto()


DEFAULT_APPROVAL_POLICY = ApprovalPolicy.never


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StartConfig:
    """Deliberately limited start-time configuration.

    ``sandbox`` uses the public Codex sandbox presets. ``approval_policy``
    and ``approvals_reviewer`` select the approval behavior; the defaults
    keep unattended execution (``never``, no reviewer).
    """

    cwd: str | None = None
    model: str | None = None
    effort: str | None = None
    sandbox: SandboxPolicy | None = None
    approval_policy: ApprovalPolicy = DEFAULT_APPROVAL_POLICY
    approvals_reviewer: ApprovalsReviewer | None = None


@dataclass(frozen=True)
class Start:
    prompt: str
    config: StartConfig = field(default_factory=StartConfig)
    detach: bool = False


@dataclass(frozen=True)
class Resume:
    thread_id: str
    prompt: str
    detach: bool = False


@dataclass(frozen=True)
class Status:
    thread_id: str


@dataclass(frozen=True)
class History:
    thread_id: str
    selector: TurnSelector | None = None


@dataclass(frozen=True)
class Follow:
    thread_id: str
    replay: ReplaySelector = field(default_factory=lambda: ReplayActiveTurn())
    persist: bool = False


@dataclass(frozen=True)
class Steer:
    thread_id: str
    input: str


@dataclass(frozen=True)
class Interrupt:
    thread_id: str


@dataclass(frozen=True)
class ListThreads:
    all_threads: bool = False


@dataclass(frozen=True)
class Doctor:
    pass


type Command = (
    Start
    | Resume
    | Status
    | History
    | Follow
    | Steer
    | Interrupt
    | ListThreads
    | Doctor
)


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SingleIndex:
    index: int


@dataclass(frozen=True)
class SliceSelector:
    start: int | None
    stop: int | None
    step: int | None


type TurnSelector = SingleIndex | SliceSelector


@dataclass(frozen=True)
class ReplayActiveTurn:
    """``-1``: replay only the anchor turn's known history.

    The anchor is the active turn when one exists, otherwise the last
    turn in history.
    """


@dataclass(frozen=True)
class ReplayTail:
    """``-N:``: replay the latest N turns including the anchor turn.

    The anchor is the active turn when one exists, otherwise the end of
    history.
    """

    count: int


@dataclass(frozen=True)
class ReplayAll:
    """``:``: replay the entire available history."""


type ReplaySelector = ReplayActiveTurn | ReplayTail | ReplayAll


def _parse_bound(text: str) -> int | None:
    text = text.strip()
    if text == "":
        return None
    return int(text)


def parse_turn_selector(text: str) -> TurnSelector:
    """Parse ``--turns`` following Python indexing/slicing semantics exactly."""
    text = text.strip()
    if text == "":
        raise ValueError("empty turn selector")
    if ":" not in text:
        return SingleIndex(index=int(text))
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"invalid turn selector: {text!r}")
    while len(parts) < 3:
        parts.append("")
    start, stop, step = (_parse_bound(p) for p in parts)
    if step == 0:
        raise ValueError("slice step cannot be zero")
    return SliceSelector(start=start, stop=stop, step=step)


def parse_replay_selector(text: str) -> ReplaySelector:
    """Parse ``--replay-turns``; only ``-1``, ``-N:``, and ``:`` are accepted."""
    text = text.strip()
    if text == "-1":
        return ReplayActiveTurn()
    if text == ":":
        return ReplayAll()
    if text.endswith(":") and ":" not in text[:-1]:
        head = text[:-1].strip()
        if head.startswith("-") and head[1:].isdigit() and int(head[1:]) >= 1:
            return ReplayTail(count=int(head[1:]))
    raise ValueError(f"invalid replay selector: {text!r} (accepted forms: -1, -N:, :)")


def apply_turn_selector(
    turns: list[Any], selector: TurnSelector | None
) -> list[tuple[int, Any]]:
    """Apply a selector to the chronological turn list.

    Returns ``(original_index, turn)`` pairs. A single index is normalized
    into a one-element collection, matching Python semantics (including
    negative indexes). Out-of-range single indexes raise ``IndexError``.
    """
    if selector is None:
        return list(enumerate(turns))
    if isinstance(selector, SingleIndex):
        item = turns[selector.index]  # may raise IndexError
        index = selector.index
        if index < 0:
            index += len(turns)
        return [(index, item)]
    s = slice(selector.start, selector.stop, selector.step)
    return [(i, t) for i, t in list(enumerate(turns))[s]]


def select_replay_turns(
    turns: list[Any], selector: ReplaySelector
) -> list[tuple[int, Any]]:
    """Select the continuous chronological suffix used by ``follow`` replay."""
    if isinstance(selector, ReplayAll):
        return list(enumerate(turns))
    count = 1 if isinstance(selector, ReplayActiveTurn) else selector.count
    if count >= len(turns):
        return list(enumerate(turns))
    offset = len(turns) - count
    return [(i, t) for i, t in enumerate(turns[offset:], start=offset)]


# ---------------------------------------------------------------------------
# Projected events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectedEvent:
    """Stable projected event emitted by projection and consumed by renderers.

    ``source`` marks delivery provenance (``replay`` vs ``live``), not event
    kind. ``extra`` carries type-specific stable fields such as ``status``,
    ``error``, or ``usage``.
    """

    type: str
    thread_id: str | None = None
    turn_id: str | None = None
    item: dict[str, Any] | None = None
    source: Literal["live", "replay"] | None = None
    turn_index: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def dedup_key(self) -> tuple[str, str | None, str | None]:
        item_id = self.item.get("id") if self.item else None
        return (self.type, self.turn_id, item_id)

    def to_document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"type": self.type}
        if self.thread_id is not None:
            doc["threadId"] = self.thread_id
        if self.turn_id is not None:
            doc["turnId"] = self.turn_id
        if self.source is not None:
            doc["source"] = self.source
        if self.turn_index is not None:
            doc["turnIndex"] = self.turn_index
        if self.item is not None:
            doc["item"] = self.item
        doc.update(self.extra)
        return doc


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


# Matches the baseline reserved by the upstream TUI for system prompts,
# fixed tool instructions, and compaction space.
CONTEXT_BASELINE_TOKENS = 12_000


def context_usage_ratio(used_tokens: int, window_tokens: int) -> float:
    """Return the upstream-compatible effective context usage ratio.

    The upstream TUI rounds the remaining percentage before deriving the used
    percentage.  Keep that order here so text output and the JSON ratio agree
    at half-percent boundaries as well.
    """
    if window_tokens <= CONTEXT_BASELINE_TOKENS:
        return 1.0

    effective_window = window_tokens - CONTEXT_BASELINE_TOKENS
    used = max(used_tokens - CONTEXT_BASELINE_TOKENS, 0)
    remaining = max(effective_window - used, 0)
    remaining_percent = int(
        min(max(remaining / effective_window * 100.0, 0.0), 100.0) + 0.5
    )
    return (100 - remaining_percent) / 100


@dataclass(frozen=True)
class ContextUsage:
    used_tokens: int
    window_tokens: int
    ratio: float
    source: str


def usage_to_context(usage: dict[str, Any] | None) -> ContextUsage | None:
    """Project a streamed ``thread/tokenUsage/updated`` usage into context.

    ``usage`` is the stable projected usage shape carried by token-usage
    events (``usedTokens`` / ``windowTokens`` / ``ratio``); it is ``None``
    or empty when the runtime provided no usable usage.
    """
    if not usage:
        return None
    return ContextUsage(
        used_tokens=usage.get("usedTokens", 0),
        window_tokens=usage.get("windowTokens", 0),
        ratio=usage.get("ratio", 0.0),
        source="live",
    )


@dataclass(frozen=True)
class TurnTerminal:
    """Terminal observation of the followed turn."""

    thread_id: str
    turn_id: str
    status: str  # completed | interrupted | failed
    error: str | None = None
    context: ContextUsage | None = None


@dataclass
class EventStreamOutcome:
    """Streaming outcome: projected events plus the terminal turn state.

    ``result`` resolves once the followed turn reaches a terminal state.
    With ``Follow(persist=True)`` the session spans turns, so ``turn_id``
    is ``None`` when no turn is active at attach time and ``result``
    resolves only at session end, to the last observed terminal turn or
    ``None`` when no turn completed during the session.
    """

    thread_id: str
    turn_id: str | None
    events: AsyncIterator[ProjectedEvent]
    result: asyncio.Future[TurnTerminal | None]


@dataclass(frozen=True)
class DetachedTurnStarted:
    thread_id: str
    turn_id: str


@dataclass(frozen=True)
class SteerAcknowledged:
    thread_id: str
    turn_id: str


@dataclass(frozen=True)
class InterruptResult:
    thread_id: str
    turn_id: str
    status: str


@dataclass(frozen=True)
class StatusSnapshot:
    thread_id: str
    status: str  # notLoaded | idle | active | systemError
    active_flags: list[str] = field(default_factory=list)
    active_turn_id: str | None = None
    context: ContextUsage | None = None


@dataclass(frozen=True)
class HistoryTurn:
    id: str
    index: int
    status: str
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class HistorySnapshot:
    thread_id: str
    turns: list[HistoryTurn] = field(default_factory=list)


@dataclass(frozen=True)
class ThreadRecord:
    thread_id: str
    status: str
    preview: str | None = None
    updated_at: int | None = None


@dataclass(frozen=True)
class ThreadListSnapshot:
    threads: list[ThreadRecord] = field(default_factory=list)


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str | None = None


@dataclass(frozen=True)
class DoctorSnapshot:
    codexctl_version: str
    endpoint_mode: str  # managed | external | stdio
    checks: list[DoctorCheck] = field(default_factory=list)
    codex_cli_version: str | None = None
    app_server_version: str | None = None
    compatible: bool = False

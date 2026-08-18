#!/usr/bin/env python3
"""A deliberately boring implementation-review workflow demo.

The example is intentionally a single file.  It has a deep ``Workflow``
interface and four small adapters so tests can exercise the observable
workflow without starting Codex or GitHub.  The production adapters use only
the Python standard library and the documented codexctl JSONL interface.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import urlsplit

STATE_VERSION = 1
INLINE_LIMIT = 32 * 1024
COMMENT_LIMIT = 60_000
LABEL_NAME = "impl-review-orchestrator"
PHASES = (
    "PREPARE",
    "WORKER",
    "REVIEWERS_RUNNING",
    "REVIEW_DECISION",
    "GATES",
    "READY_FOR_HANDOFF",
    "WAITING_FOR_USER",
)
PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
REVIEW_VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(PASS|FAIL)\s*$", re.IGNORECASE)
REVIEW_INSTRUCTION = """
Review the checkout without modifying it. Do not commit, merge, create a
worktree, or clean up files.

End your response with exactly one terminal line, choosing one of these:
VERDICT: PASS
VERDICT: FAIL
""".strip()
REVIEW_VERDICT_RETRY_PROMPT = "output VERDICT: PASS|FAIL"


class OrchestratorError(Exception):
    """A user-facing, non-interactive workflow error."""


class UsageError(OrchestratorError):
    pass


class ArtifactError(OrchestratorError):
    pass


class JsonlError(OrchestratorError):
    pass


class PublicationError(OrchestratorError):
    pass


class DriftError(OrchestratorError):
    pass


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _slug(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return result or "unnamed"


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(f"prompt is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise UsageError(f"cannot read prompt {path}: {exc}") from exc


def _read_opaque(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise UsageError(f"cannot read spec {path}: {exc}") from exc


def parse_issue_uri(value: str) -> tuple[str, str]:
    """Return ``OWNER/REPO`` and issue number from a GitHub issue URI."""

    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise UsageError(
            "--issue must be a GitHub issue URI such as "
            "https://github.com/OWNER/REPO/issues/123"
        ) from exc
    if parsed.scheme.lower() != "https" or parsed.hostname != "github.com":
        raise UsageError(
            "--issue must be a GitHub issue URI such as "
            "https://github.com/OWNER/REPO/issues/123"
        )
    match = re.fullmatch(
        r"/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
        r"([A-Za-z0-9][A-Za-z0-9._-]*)/issues/([1-9][0-9]*)/?",
        parsed.path,
    )
    if match is None:
        raise UsageError(
            "--issue must be a GitHub issue URI such as "
            "https://github.com/OWNER/REPO/issues/123"
        )
    owner, repository, number = match.groups()
    return f"{owner}/{repository}", number


def validate_template(template: str, *, allowed: Iterable[str]) -> tuple[str, ...]:
    """Reject unknown and malformed ``{{name}}`` placeholders."""

    allowed_set = set(allowed)
    found: list[str] = []
    covered: list[tuple[int, int]] = []
    for match in PLACEHOLDER_RE.finditer(template):
        name = match.group(1)
        if name not in allowed_set:
            raise UsageError(f"unknown prompt placeholder: {{{{{name}}}}}")
        found.append(name)
        covered.append(match.span())

    for token in ("{{", "}}"):
        cursor = 0
        while True:
            index = template.find(token, cursor)
            if index < 0:
                break
            if not any(start <= index < end for start, end in covered):
                raise UsageError(f"malformed prompt placeholder near {token}")
            cursor = index + len(token)
    return tuple(found)


def _inline_or_reference(
    content: str | bytes,
    reference: str,
    *,
    limit: int = INLINE_LIMIT,
) -> str:
    """Inline bounded UTF-8 content, otherwise point to its artifact."""

    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return f"[See durable artifact: {reference}]"
    else:
        text = content
    if len(text.encode("utf-8")) <= limit:
        return text
    return f"[See durable artifact: {reference}]"


def _render_unstaged_diff(
    diff: str,
    files: Iterable[str],
    command: str,
    *,
    limit: int = INLINE_LIMIT,
) -> str:
    if len(diff.encode("utf-8")) <= limit:
        return diff
    file_list = "\n".join(f"- {path}" for path in files) or "- (none reported)"
    return (
        "[The unstaged diff is too large to inline.]\n"
        "Files with unstaged changes:\n"
        f"{file_list}\n"
        "Run this command to retrieve the full unstaged diff:\n"
        f"{command or 'git diff'}"
    )


def render_prompt(
    template: str,
    *,
    spec: str | bytes,
    spec_reference: str,
    cwd: str,
    round: int,
    issue: str = "",
    gates: Iterable[str] = (),
    review_findings: str | bytes = "",
    review_findings_reference: str = "",
    unstaged_diff: str = "",
    unstaged_files: Iterable[str] = (),
    unstaged_diff_command: str = "",
    is_repair: bool = False,
    is_reviewer: bool = False,
) -> str:
    """Validate and render one prompt from its immutable source snapshot."""

    allowed = {"spec", "cwd", "round", "issue", "gates"}
    if is_repair:
        allowed.add("review_findings")
    if is_reviewer:
        allowed.add("unstaged_diff")
    placeholders = validate_template(template, allowed=allowed)
    if not is_repair and "review_findings" in placeholders:
        raise UsageError("{{review_findings}} is only valid in the repair prompt")

    values = {
        "spec": _inline_or_reference(spec, spec_reference),
        "cwd": cwd,
        "round": str(round),
        "issue": issue or "none",
        "gates": "\n".join(gates),
        "review_findings": _inline_or_reference(
            review_findings,
            review_findings_reference or "review-findings.txt",
        ),
        "unstaged_diff": _render_unstaged_diff(
            unstaged_diff,
            unstaged_files,
            unstaged_diff_command,
        ),
    }
    rendered = PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], template)
    if is_repair:
        return rendered
    return rendered


def parse_reviewer(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise UsageError("--reviewer must use NAME=PATH")
    name, path = value.split("=", 1)
    if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise UsageError(f"invalid reviewer name: {name!r}")
    if not path:
        raise UsageError(f"reviewer {name!r} has no prompt path")
    return name, Path(path).expanduser().resolve()


@dataclass(frozen=True)
class CheckoutSnapshot:
    path: str
    branch: str
    head: str
    status: tuple[str, ...]
    remote: str | None = None
    index_fingerprint: str | None = None

    @property
    def clean(self) -> bool:
        return not self.status

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "branch": self.branch,
            "head": self.head,
            "status": list(self.status),
            "clean": self.clean,
            "remote": self.remote,
            "index_fingerprint": self.index_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CheckoutSnapshot":
        return cls(
            path=str(value["path"]),
            branch=str(value["branch"]),
            head=str(value["head"]),
            status=tuple(str(item) for item in value.get("status", [])),
            remote=value.get("remote"),
            index_fingerprint=value.get("index_fingerprint"),
        )


@dataclass(frozen=True)
class UnstagedChanges:
    diff: str
    files: tuple[str, ...]
    command: str


class GitPort(Protocol):
    def snapshot(self, cwd: Path) -> CheckoutSnapshot: ...

    def unstaged_changes(self, cwd: Path) -> UnstagedChanges: ...

    def stage_all(self, cwd: Path) -> None: ...


class GitAdapter:
    """Git adapter for checkout inspection and explicit staging."""

    @staticmethod
    def _git(cwd: Path, *args: str, check: bool = True) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(cwd), *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise UsageError(
                f"Git inspection failed in {cwd}: {detail.strip()}"
            ) from exc
        if check and completed.returncode != 0:
            detail = completed.stderr or f"exit {completed.returncode}"
            raise UsageError(f"Git inspection failed in {cwd}: {detail.strip()}")
        return completed.stdout.strip()

    def snapshot(self, cwd: Path) -> CheckoutSnapshot:
        root = Path(self._git(cwd, "rev-parse", "--show-toplevel")).resolve()
        if root != cwd.resolve():
            raise UsageError(f"--cwd must be the Git checkout root: {cwd}")
        branch = self._git(cwd, "symbolic-ref", "--short", "-q", "HEAD", check=False)
        if not branch:
            branch = f"(detached:{self._git(cwd, 'rev-parse', '--short', 'HEAD')})"
        status_text = self._git(
            cwd, "status", "--porcelain=v1", "--untracked-files=all"
        )
        status = tuple(line for line in status_text.splitlines() if line)
        return CheckoutSnapshot(
            path=str(root),
            branch=branch,
            head=self._git(cwd, "rev-parse", "HEAD"),
            status=status,
            remote=None,
            index_fingerprint=hashlib.sha256(
                self._git(cwd, "ls-files", "--stage", "-z").encode("utf-8")
            ).hexdigest(),
        )

    def unstaged_changes(self, cwd: Path) -> UnstagedChanges:
        diff_args = ("diff", "--no-ext-diff", "--no-color")
        diff = self._git(cwd, *diff_args)
        files = self._git(cwd, "diff", "--name-only", "--no-ext-diff", "--no-color")
        command = shlex.join(["git", "-C", str(cwd), *diff_args])
        return UnstagedChanges(
            diff=diff,
            files=tuple(line for line in files.splitlines() if line),
            command=command,
        )

    def stage_all(self, cwd: Path) -> None:
        self._git(cwd, "add", "--all")


class ArtifactStore:
    """Immutable artifacts plus atomic replacement of the mutable state file."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def create(self, relative: str, content: bytes) -> str:
        relative_path = Path(relative)
        candidate = relative_path
        attempt = 2
        while (self.run_dir / candidate).exists():
            candidate = relative_path.with_name(
                f"{relative_path.stem}.attempt-{attempt}{relative_path.suffix}"
            )
            attempt += 1
        destination = self.run_dir / candidate
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("xb") as handle:
                handle.write(content)
        except FileExistsError as exc:
            raise ArtifactError(f"artifact already exists: {destination}") from exc
        return candidate.as_posix()

    def reference(self, relative: str) -> str:
        """Return an absolute reference usable by a child in another cwd."""

        return str((self.run_dir / relative).resolve())

    def write_state(self, state: dict[str, Any]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        state_path = self.run_dir / "state.json"
        fd, temporary_name = tempfile.mkstemp(
            prefix="state.json.", suffix=".tmp", dir=self.run_dir
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(_json_bytes(state))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, state_path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def read_state(self) -> dict[str, Any]:
        try:
            return json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(
                f"cannot read orchestrator state in {self.run_dir}: {exc}"
            ) from exc


def _state_root(value: Path | None) -> Path:
    if value is not None:
        return value.expanduser().resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    return (
        (Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state")
        / "codexctl"
        / "impl-review-orchestrator"
    )


def _repo_id(cwd: Path, git: GitPort) -> str:
    del git
    digest = hashlib.sha256(str(cwd.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"{_slug(cwd.name)}-{digest}"


def _same_checkout(expected: CheckoutSnapshot, actual: CheckoutSnapshot) -> bool:
    if (
        expected.path != actual.path
        or expected.branch != actual.branch
        or expected.head != actual.head
        or expected.status != actual.status
        or expected.remote != actual.remote
    ):
        return False
    return (
        expected.index_fingerprint is None
        or actual.index_fingerprint is None
        or expected.index_fingerprint == actual.index_fingerprint
    )


def _same_worker_baseline(expected: CheckoutSnapshot, actual: CheckoutSnapshot) -> bool:
    """Check the Git state a worker must leave unchanged."""

    if (
        expected.path != actual.path
        or expected.branch != actual.branch
        or expected.head != actual.head
    ):
        return False
    return (
        expected.index_fingerprint is None
        or actual.index_fingerprint is None
        or expected.index_fingerprint == actual.index_fingerprint
    )


def _event_thread_id(event: dict[str, Any]) -> str | None:
    value = event.get("threadId")
    if value:
        return str(value)
    thread = event.get("thread")
    if isinstance(thread, dict) and thread.get("id"):
        return str(thread["id"])
    params = event.get("params")
    if isinstance(params, dict):
        if params.get("threadId"):
            return str(params["threadId"])
        nested = params.get("thread")
        if isinstance(nested, dict) and nested.get("id"):
            return str(nested["id"])
    return None


@dataclass
class AgentRun:
    returncode: int
    stdout: bytes
    stderr: bytes
    thread_id: str | None = None
    final_text: str | None = None
    parse_error: str | None = None


def parse_codex_jsonl(raw: str | bytes) -> AgentRun:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    events: list[dict[str, Any]] = []
    thread_id: str | None = None
    parse_error: str | None = None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return AgentRun(0, data, b"", parse_error=f"JSONL is not UTF-8: {exc}")

    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_error = f"invalid JSONL at line {line_number}: {exc.msg}"
            continue
        if not isinstance(value, dict):
            parse_error = f"JSONL line {line_number} is not an object"
            continue
        events.append(value)
        thread_id = thread_id or _event_thread_id(value)

    final_text: str | None = None
    for event in events:
        if event.get("type") != "item/completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            text_value = item.get("text")
            if isinstance(text_value, str):
                final_text = text_value
    if final_text is None and parse_error is None:
        parse_error = "JSONL did not contain a completed agentMessage item"
    return AgentRun(
        returncode=0,
        stdout=data,
        stderr=b"",
        thread_id=thread_id,
        final_text=final_text,
        parse_error=parse_error,
    )


def parse_codex_text(raw: str | bytes) -> AgentRun:
    """Parse the final agent message and thread ID from text renderer output."""

    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return AgentRun(0, data, b"", parse_error=f"text is not UTF-8: {exc}")

    thread_match = re.search(r"^Thread: ([^\r\n]+)$", text, re.MULTILINE)
    agent_markers = list(re.finditer(r"^\[agent\]\r?\n", text, re.MULTILINE))
    if not agent_markers:
        return AgentRun(
            0,
            data,
            b"",
            thread_id=thread_match.group(1) if thread_match else None,
            parse_error="text output did not contain a completed agentMessage",
        )

    final_text = text[agent_markers[-1].end() :]
    terminal_marker = re.search(
        r"\r?\n(?:Turn completed|Turn ended: [^\r\n]*)(?:\r?\n|$)",
        final_text,
    )
    if terminal_marker:
        final_text = final_text[: terminal_marker.start()]
    final_text = final_text.rstrip()
    if not final_text:
        return AgentRun(
            0,
            data,
            b"",
            thread_id=thread_match.group(1) if thread_match else None,
            parse_error="text output contained an empty agentMessage",
        )
    return AgentRun(
        0,
        data,
        b"",
        thread_id=thread_match.group(1) if thread_match else None,
        final_text=final_text,
    )


def extract_final_agent_message(raw: str | bytes) -> str | None:
    """Return only the final completed ``agentMessage`` text, if present."""

    return parse_codex_jsonl(raw).final_text


def extract_verdict(text: str) -> str:
    """Require one terminal PASS/FAIL marker in a reviewer response."""

    nonempty = [line for line in text.splitlines() if line.strip()]
    if not nonempty or not REVIEW_VERDICT_RE.fullmatch(nonempty[-1]):
        raise JsonlError(
            "reviewer response must end with VERDICT: PASS or VERDICT: FAIL"
        )
    matches = [REVIEW_VERDICT_RE.fullmatch(line) for line in nonempty]
    verdicts = [match.group(1).upper() for match in matches if match]
    if len(verdicts) != 1:
        raise JsonlError("reviewer response must contain exactly one terminal verdict")
    return verdicts[0]


class CodexctlPort(Protocol):
    def invoke(
        self,
        *,
        role: str,
        prompt: str,
        cwd: Path,
        thread_id: str | None,
        round: int,
    ) -> AgentRun: ...


class CodexctlAdapter:
    def __init__(
        self,
        executable: str = "codexctl",
        *,
        output: Callable[[str], None] | None = None,
    ) -> None:
        self.executable = executable
        self.output = output

    def _communicate_text(
        self, process: subprocess.Popen[bytes]
    ) -> tuple[bytes, bytes]:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout = process.stdout
        stderr = process.stderr
        if stdout is None or stderr is None:
            raise RuntimeError("codexctl pipes were not configured")

        def drain_stderr() -> None:
            while True:
                chunk = stderr.read(4096)
                if not chunk:
                    return
                stderr_chunks.append(chunk)

        stderr_thread = threading.Thread(target=drain_stderr)
        stderr_thread.start()
        while True:
            chunk = stdout.readline()
            if not chunk:
                break
            stdout_chunks.append(chunk)
            if self.output is not None:
                self.output(chunk.decode("utf-8", "replace"))
        process.wait()
        stderr_thread.join()
        return b"".join(stdout_chunks), b"".join(stderr_chunks)

    def invoke(
        self,
        *,
        role: str,
        prompt: str,
        cwd: Path,
        thread_id: str | None,
        round: int,
    ) -> AgentRun:
        del round
        sandbox = "read-only" if role.startswith("reviewer") else "workspace-write"
        output_mode = "text" if role == "worker" else "jsonl"
        if thread_id:
            argv = [
                self.executable,
                "resume",
                thread_id,
                "--output",
                output_mode,
                "--",
                prompt,
            ]
        else:
            argv = [
                self.executable,
                "start",
                "--output",
                output_mode,
                "--cwd",
                str(cwd),
                "--sandbox",
                sandbox,
                "--",
                prompt,
            ]
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if role == "worker":
                stdout, stderr = self._communicate_text(process)
            else:
                stdout, stderr = process.communicate()
        except (OSError, subprocess.SubprocessError) as exc:
            return AgentRun(
                returncode=127,
                stdout=b"",
                stderr=str(exc).encode("utf-8", "replace"),
                parse_error=f"could not invoke codexctl: {exc}",
            )
        parsed = (
            parse_codex_text(stdout) if role == "worker" else parse_codex_jsonl(stdout)
        )
        parsed.returncode = process.returncode
        parsed.stderr = stderr
        return parsed


@dataclass
class GateResult:
    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "passed": self.passed,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class GatePort(Protocol):
    def run(self, command: str, cwd: Path) -> GateResult: ...


class GateAdapter:
    def run(self, command: str, cwd: Path) -> GateResult:
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except OSError as exc:
            return GateResult(command, 127, "", str(exc))
        return GateResult(
            command, completed.returncode, completed.stdout, completed.stderr
        )


class PublisherPort(Protocol):
    def publish(
        self,
        *,
        cwd: Path,
        issue: str,
        run_id: str,
        round: int,
        reviews: dict[str, Any],
    ) -> str: ...


def _run_gh(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise PublicationError(f"could not invoke gh: {exc}") from exc


class GitHubPublisher:
    def publish(
        self,
        *,
        cwd: Path,
        issue: str,
        run_id: str,
        round: int,
        reviews: dict[str, Any],
    ) -> str:
        target_repo, issue_number = parse_issue_uri(issue)
        marker = f"<!-- {LABEL_NAME}:run={run_id}:round={round} -->"
        attribution = "<sub><em>Generated by impl-review-orchestrator and AI</em></sub>"
        lines = [marker, attribution, f"## Implementation review round {round}", ""]
        text_limit = max(256, (COMMENT_LIMIT - 512) // max(1, len(reviews)))
        for name, review in reviews.items():
            verdict = review.get("verdict") or "AMBIGUOUS"
            text = str(review.get("final_text") or "").strip()
            if len(text) > text_limit:
                text = text[: text_limit - len("\n[truncated]")] + "\n[truncated]"
            lines.extend([f"### {name}: {verdict}", text, ""])
        body = "\n".join(lines)
        if len(body) > COMMENT_LIMIT:
            body = body[: COMMENT_LIMIT - len("\n\n[truncated]")] + "\n\n[truncated]"

        view = _run_gh(
            [
                "gh",
                "issue",
                "view",
                issue_number,
                "--repo",
                target_repo,
                "--json",
                "comments",
            ],
            cwd,
        )
        if view.returncode != 0:
            raise PublicationError(view.stderr.strip() or "gh issue view failed")
        try:
            comments = json.loads(view.stdout).get("comments", [])
        except (json.JSONDecodeError, AttributeError) as exc:
            raise PublicationError("gh returned invalid comments JSON") from exc
        existing = any(marker in str(comment.get("body", "")) for comment in comments)
        if not existing:
            comment = _run_gh(
                [
                    "gh",
                    "issue",
                    "comment",
                    issue_number,
                    "--repo",
                    target_repo,
                    "--body",
                    body,
                ],
                cwd,
            )
            if comment.returncode != 0:
                raise PublicationError(
                    comment.stderr.strip() or "gh issue comment failed"
                )

        return f"published round {round} to {target_repo}#{issue_number}"


@dataclass
class RunConfig:
    cwd: Path
    spec_path: Path
    worker_prompt_path: Path
    repair_prompt_path: Path
    reviewers: tuple[tuple[str, Path], ...]
    gates: tuple[str, ...] = ()
    issue: str = ""
    codexctl: str = "codexctl"
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state_dir: Path | None = None
    publish_review_findings: bool = False


def _snapshot_name(path: Path, fallback: str) -> str:
    suffix = path.suffix or ".txt"
    return fallback + suffix


class Workflow:
    """The deep workflow seam: stateful decisions in, one report out."""

    def __init__(
        self,
        *,
        config: RunConfig,
        store: ArtifactStore,
        state: dict[str, Any],
        git: GitPort | None = None,
        codexctl: CodexctlPort | None = None,
        gates: GatePort | None = None,
        publisher: PublisherPort | None = None,
        progress: Callable[[str], None] | None = None,
        agent_output: Callable[[str], None] | None = None,
        cwd_override: Path | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.state = state
        self.git = git or GitAdapter()
        self.codexctl = codexctl or CodexctlAdapter(
            config.codexctl, output=agent_output
        )
        self.gates = gates or GateAdapter()
        self.publisher = publisher or GitHubPublisher()
        self.progress = progress or (lambda _message: None)
        self.cwd_override = cwd_override.resolve() if cwd_override is not None else None
        self._state_lock = threading.Lock()

    @classmethod
    def new(
        cls,
        config: RunConfig,
        *,
        git: GitPort | None = None,
        codexctl: CodexctlPort | None = None,
        gates: GatePort | None = None,
        publisher: PublisherPort | None = None,
        progress: Callable[[str], None] | None = None,
        agent_output: Callable[[str], None] | None = None,
    ) -> "Workflow":
        git_adapter = git or GitAdapter()
        cwd = config.cwd.resolve()
        if config.publish_review_findings and not config.issue:
            raise UsageError("--publish-review-findings requires --issue")
        if config.issue:
            parse_issue_uri(config.issue)
        if config.worker_prompt_path is None:
            raise UsageError("--worker-prompt is required")
        names = [name for name, _path in config.reviewers]
        if len(names) != len(set(names)):
            raise UsageError("reviewer names must be unique")
        snapshot = git_adapter.snapshot(cwd)
        if not snapshot.clean:
            raise UsageError("--cwd must be a clean Git checkout at startup")
        repo_id = _repo_id(cwd, git_adapter)
        root = _state_root(config.state_dir)
        run_dir = root / repo_id / _slug(config.run_id)
        if run_dir.exists():
            raise UsageError(f"run already exists: {config.run_id}")
        store = ArtifactStore(run_dir)
        spec_bytes = _read_opaque(config.spec_path)
        worker_template = _read_utf8(config.worker_prompt_path)
        repair_template = _read_utf8(config.repair_prompt_path)
        validate_template(
            worker_template,
            allowed={"spec", "cwd", "round", "issue", "gates"},
        )
        validate_template(
            repair_template,
            allowed={
                "spec",
                "cwd",
                "round",
                "issue",
                "gates",
                "review_findings",
            },
        )
        reviewer_templates: dict[str, str] = {}
        for name, path in config.reviewers:
            template = _read_utf8(path)
            validate_template(
                template,
                allowed={
                    "spec",
                    "cwd",
                    "round",
                    "issue",
                    "gates",
                    "unstaged_diff",
                },
            )
            reviewer_templates[name] = template
        spec_artifact = store.create(
            _snapshot_name(config.spec_path, "spec"), spec_bytes
        )
        worker_artifact = store.create(
            "prompts/worker.txt", worker_template.encode("utf-8")
        )
        repair_artifact = store.create(
            "prompts/repair.txt", repair_template.encode("utf-8")
        )
        reviewer_artifacts = []
        for name, path in config.reviewers:
            reviewer_artifacts.append(
                {
                    "name": name,
                    "source": store.create(
                        f"prompts/{_slug(name)}.txt",
                        reviewer_templates[name].encode("utf-8"),
                    ),
                }
            )
        state = {
            "version": STATE_VERSION,
            "run_id": config.run_id,
            "repo_id": repo_id,
            "state": "PREPARE",
            "phase": "PREPARE",
            "round": 1,
            "cwd": str(cwd),
            "initial_checkout": snapshot.to_dict(),
            "checkout": snapshot.to_dict(),
            "config": {
                "cwd": str(cwd),
                "issue": config.issue,
                "codexctl": config.codexctl,
                "publish_review_findings": config.publish_review_findings,
                "spec": spec_artifact,
                "worker_prompt": worker_artifact,
                "repair_prompt": repair_artifact,
                "reviewers": reviewer_artifacts,
                "gates": list(config.gates),
            },
            "artifacts": [
                spec_artifact,
                worker_artifact,
                repair_artifact,
                *(item["source"] for item in reviewer_artifacts),
            ],
            "rounds": {},
            "review_verdicts": {},
            "gate_results": [],
            "publications": {},
            "vcs_mutation_by_orchestrator": False,
            "handoff": None,
            "pending": None,
        }
        workflow = cls(
            config=config,
            store=store,
            state=state,
            git=git_adapter,
            codexctl=codexctl or CodexctlAdapter(config.codexctl, output=agent_output),
            gates=gates,
            publisher=publisher,
            progress=progress,
        )
        workflow._save()
        return workflow

    @classmethod
    def from_store(
        cls,
        store: ArtifactStore,
        *,
        codexctl: str | None = None,
        cwd_override: Path | None = None,
        publisher: PublisherPort | None = None,
        progress: Callable[[str], None] | None = None,
        git: GitPort | None = None,
        codex_runner: CodexctlPort | None = None,
        gate_runner: GatePort | None = None,
        agent_output: Callable[[str], None] | None = None,
    ) -> "Workflow":
        state = store.read_state()
        config_data = state.get("config", {})
        cwd = Path(config_data["cwd"]).resolve()
        if cwd_override is not None and cwd_override.resolve() != cwd:
            # Keep the saved path as authoritative; drift handling will report
            # the mismatch rather than silently adopting a new checkout.
            pass
        config = RunConfig(
            cwd=cwd,
            spec_path=store.run_dir / config_data["spec"],
            worker_prompt_path=store.run_dir / config_data["worker_prompt"],
            repair_prompt_path=store.run_dir / config_data["repair_prompt"],
            reviewers=tuple(
                (item["name"], store.run_dir / item["source"])
                for item in config_data.get("reviewers", [])
            ),
            gates=tuple(config_data.get("gates", [])),
            issue=str(config_data.get("issue", "")),
            codexctl=codexctl or str(config_data.get("codexctl", "codexctl")),
            run_id=str(state["run_id"]),
            state_dir=store.run_dir.parent.parent,
            publish_review_findings=bool(config_data.get("publish_review_findings")),
        )
        if config.issue:
            parse_issue_uri(config.issue)
        return cls(
            config=config,
            store=store,
            state=state,
            git=git or GitAdapter(),
            codexctl=codex_runner
            or CodexctlAdapter(config.codexctl, output=agent_output),
            gates=gate_runner or GateAdapter(),
            publisher=publisher,
            progress=progress,
            cwd_override=cwd_override,
        )

    def _save(self) -> None:
        with self._state_lock:
            self.store.write_state(self.state)

    def _artifact(self, relative: str, content: bytes) -> str:
        artifact = self.store.create(relative, content)
        self.state.setdefault("artifacts", []).append(artifact)
        return artifact

    def _round(self, round_number: int) -> dict[str, Any]:
        return self.state.setdefault("rounds", {}).setdefault(str(round_number), {})

    def _set_phase(self, phase: str, *, round_number: int | None = None) -> None:
        if phase not in PHASES:
            raise AssertionError(phase)
        self.state["state"] = phase
        self.state["phase"] = phase
        if round_number is not None:
            self.state["round"] = round_number
        self._save()

    def _report(self, message: str) -> None:
        self.progress(message)

    def _result(self) -> dict[str, Any]:
        reviews = self.state.get("review_verdicts", {})
        return {
            "runId": self.state["run_id"],
            "statePath": str(self.store.run_dir / "state.json"),
            "state": self.state["state"],
            "artifacts": list(self.state.get("artifacts", [])),
            "reviewVerdicts": reviews,
            "gateResults": list(self.state.get("gate_results", [])),
            "handoffStatus": (
                self.state.get("handoff", {}).get("status")
                if self.state.get("handoff")
                else self.state["state"]
            ),
            "handoff": self.state.get("handoff"),
            "pending": self.state.get("pending"),
        }

    def _waiting(
        self,
        *,
        reason: str,
        pending: dict[str, Any],
        message: str,
    ) -> dict[str, Any]:
        self.state["pending"] = {"reason": reason, **pending}
        self.state["message"] = message
        self._set_phase("WAITING_FOR_USER")
        return self._result()

    def _record_agent_artifacts(
        self,
        *,
        round_number: int,
        stem: str,
        result: AgentRun,
        raw_suffix: str = "jsonl",
    ) -> dict[str, str]:
        artifacts = {
            "raw": self._artifact(
                f"rounds/{round_number}/{stem}.{raw_suffix}", result.stdout
            ),
            "stderr": self._artifact(
                f"rounds/{round_number}/{stem}.stderr", result.stderr
            ),
        }
        if result.final_text is not None:
            artifacts["final"] = self._artifact(
                f"rounds/{round_number}/{stem}.final.txt",
                result.final_text.encode("utf-8"),
            )
        return artifacts

    def _render_source(self, relative: str) -> str:
        try:
            return (self.store.run_dir / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ArtifactError(
                f"cannot read prompt snapshot {relative}: {exc}"
            ) from exc

    def _spec_bytes(self) -> bytes:
        try:
            return (self.store.run_dir / self.state["config"]["spec"]).read_bytes()
        except OSError as exc:
            raise ArtifactError(f"cannot read spec snapshot: {exc}") from exc

    def _review_findings(self, round_number: int) -> tuple[str, str]:
        round_state = self._round(round_number)
        existing = round_state.get("review_findings")
        if existing:
            return (
                (self.store.run_dir / existing).read_text(encoding="utf-8"),
                self.store.reference(existing),
            )
        blocks = []
        for name, review in round_state.get("reviewers", {}).items():
            blocks.append(
                f"Reviewer: {name}\nVerdict: {review.get('verdict', 'AMBIGUOUS')}\n"
                f"{review.get('final_text', '')}\n"
            )
        content = "\n".join(blocks).encode("utf-8")
        artifact = self._artifact(f"rounds/{round_number}/review-findings.txt", content)
        round_state["review_findings"] = artifact
        self._save()
        return content.decode("utf-8"), self.store.reference(artifact)

    def _run_worker(self, round_number: int) -> dict[str, Any] | None:
        self._set_phase("WORKER", round_number=round_number)
        self.state["pending"] = {
            "kind": "agent",
            "role": "worker",
            "round": round_number,
            "decision": "retry",
        }
        self._save()
        self._report(f"round {round_number}: worker")
        prompt_source = self.state["config"][
            "repair_prompt" if round_number > 1 else "worker_prompt"
        ]
        template = self._render_source(prompt_source)
        findings, findings_reference = ("", "")
        if round_number > 1:
            findings, findings_reference = self._review_findings(round_number - 1)
        prompt = render_prompt(
            template,
            spec=self._spec_bytes(),
            spec_reference=self.store.reference(self.state["config"]["spec"]),
            cwd=str(self.config.cwd),
            round=round_number,
            issue=self.config.issue,
            gates=self.config.gates,
            review_findings=findings,
            review_findings_reference=findings_reference,
            is_repair=round_number > 1,
        )
        round_state = self._round(round_number)
        prompt_artifact = self._artifact(
            f"rounds/{round_number}/worker.prompt.txt", prompt.encode("utf-8")
        )
        round_state["worker_prompt"] = prompt_artifact
        self._save()
        worker_before = self.git.snapshot(self.config.cwd)
        round_state["worker_checkout_before"] = worker_before.to_dict()
        self._save()
        interrupted = False
        try:
            result = self.codexctl.invoke(
                role="worker",
                prompt=prompt,
                cwd=self.config.cwd,
                thread_id=None,
                round=round_number,
            )
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                interrupted = True
                result = AgentRun(
                    130,
                    b"",
                    b"worker interrupted",
                    parse_error="worker interrupted",
                )
            else:
                result = AgentRun(
                    127, b"", str(exc).encode("utf-8"), parse_error=str(exc)
                )
        worker_after = self.git.snapshot(self.config.cwd)
        round_state["worker_checkout_after"] = worker_after.to_dict()
        self._save()
        artifacts = self._record_agent_artifacts(
            round_number=round_number,
            stem="worker",
            result=result,
            raw_suffix="txt",
        )
        worker_record = {
            "thread_id": result.thread_id,
            "returncode": result.returncode,
            "final_text": result.final_text,
            "artifacts": artifacts,
            "error": result.parse_error,
        }
        round_state["worker"] = worker_record
        self._save()
        if not _same_worker_baseline(worker_before, worker_after):
            return self._waiting(
                reason="worker-vcs-mutation",
                pending={
                    "kind": "drift",
                    "resume_phase": "WORKER",
                    "decision": "acknowledge-drift",
                },
                message=(
                    "The worker changed branch, HEAD, or staged Git state; "
                    "inspect the checkout before retrying."
                ),
            )
        if interrupted:
            return self._waiting(
                reason="agent-interrupted",
                pending={
                    "kind": "agent",
                    "role": "worker",
                    "round": round_number,
                    "decision": "retry",
                },
                message="The worker was interrupted; resume with --decision retry.",
            )
        if result.returncode != 0 or result.parse_error or result.final_text is None:
            return self._waiting(
                reason="agent-failure",
                pending={
                    "kind": "agent",
                    "role": "worker",
                    "round": round_number,
                    "decision": "retry",
                },
                message="The worker result is ambiguous; resume with --decision retry.",
            )
        self.state["checkout"] = worker_after.to_dict()
        self.state["pending"] = None
        self._save()
        return None

    def _review_prompt(
        self,
        name: str,
        round_number: int,
        unstaged_changes: UnstagedChanges,
    ) -> str:
        source = next(
            item["source"]
            for item in self.state["config"]["reviewers"]
            if item["name"] == name
        )
        template = self._render_source(source)
        prompt = render_prompt(
            template,
            spec=self._spec_bytes(),
            spec_reference=self.store.reference(self.state["config"]["spec"]),
            cwd=str(self.config.cwd),
            round=round_number,
            issue=self.config.issue,
            gates=self.config.gates,
            unstaged_diff=unstaged_changes.diff,
            unstaged_files=unstaged_changes.files,
            unstaged_diff_command=unstaged_changes.command,
            is_reviewer=True,
        )
        return f"{prompt.rstrip()}\n\n{REVIEW_INSTRUCTION}\n"

    def _review_thread(self, name: str, round_number: int) -> str | None:
        current = (
            self._round(round_number)
            .get("reviewers", {})
            .get(name, {})
            .get("thread_id")
        )
        if current:
            return current
        if round_number == 1:
            return None
        return (
            self._round(round_number - 1)
            .get("reviewers", {})
            .get(name, {})
            .get("thread_id")
        )

    def _run_reviewers(
        self, round_number: int, *, retry_verdicts: bool = False
    ) -> dict[str, Any] | None:
        self._set_phase("REVIEWERS_RUNNING", round_number=round_number)
        self.state["pending"] = {
            "kind": "agent",
            "role": "reviewers",
            "round": round_number,
            "decision": "retry",
        }
        round_state = self._round(round_number)
        reviewers = round_state.setdefault("reviewers", {})
        expected = CheckoutSnapshot.from_dict(self.state["checkout"])
        reviewer_before = self.git.snapshot(self.config.cwd)
        if not _same_checkout(expected, reviewer_before):
            return self._waiting(
                reason="checkout-drift",
                pending={
                    "kind": "drift",
                    "resume_phase": "REVIEWERS_RUNNING",
                    "decision": "acknowledge-drift",
                },
                message=(
                    "The checkout changed before reviewers ran; inspect it and "
                    "resume with --decision acknowledge-drift."
                ),
            )
        unstaged_changes = self.git.unstaged_changes(self.config.cwd)
        round_state["reviewer_checkout_before"] = reviewer_before.to_dict()
        self._save()
        self._report(f"round {round_number}: reviewers (concurrent)")
        names = [item["name"] for item in self.state["config"]["reviewers"]]
        pending_names = [
            name
            for name in names
            if reviewers.get(name, {}).get("verdict") not in {"PASS", "FAIL"}
        ]
        prompts: dict[str, str] = {}
        for name in pending_names:
            prompt = (
                REVIEW_VERDICT_RETRY_PROMPT
                if retry_verdicts
                else self._review_prompt(name, round_number, unstaged_changes)
            )
            prompts[name] = prompt
            reviewers.setdefault(name, {})["prompt"] = self._artifact(
                f"rounds/{round_number}/{_slug(name)}.prompt.txt",
                prompt.encode("utf-8"),
            )
        self._save()

        def invoke(name: str) -> AgentRun:
            thread_id = self._review_thread(name, round_number)
            if round_number > 1 and not thread_id:
                return AgentRun(
                    127,
                    b"",
                    b"",
                    parse_error=f"reviewer thread missing for {name}; cannot resume",
                )
            return self.codexctl.invoke(
                role=f"reviewer:{name}",
                prompt=prompts[name],
                cwd=self.config.cwd,
                thread_id=thread_id,
                round=round_number,
            )

        results: dict[str, AgentRun] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(pending_names))
        ) as executor:
            futures = {executor.submit(invoke, name): name for name in pending_names}
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                except BaseException as exc:
                    result = AgentRun(
                        127, b"", str(exc).encode("utf-8"), parse_error=str(exc)
                    )
                results[name] = result
                if result.final_text is not None:
                    self._report(f"reviewer {name} completed:\n{result.final_text}")

        reviewer_after = self.git.snapshot(self.config.cwd)
        round_state["reviewer_checkout_after"] = reviewer_after.to_dict()
        for name in pending_names:
            result = results[name]
            artifacts = self._record_agent_artifacts(
                round_number=round_number,
                stem=_slug(name),
                result=result,
            )
            verdict: str | None = None
            error = result.parse_error
            if result.returncode == 0 and not error and result.final_text is not None:
                try:
                    verdict = extract_verdict(result.final_text)
                except JsonlError as exc:
                    error = str(exc)
            elif result.returncode != 0:
                error = error or f"codexctl exited with {result.returncode}"
            reviewers[name] = {
                "thread_id": result.thread_id,
                "returncode": result.returncode,
                "verdict": verdict,
                "final_text": result.final_text,
                "artifacts": artifacts,
                "error": error,
            }
        self.state["review_verdicts"][str(round_number)] = {
            name: reviewers[name].get("verdict") for name in names
        }
        self._save()
        if not _same_checkout(reviewer_before, reviewer_after):
            return self._waiting(
                reason="reviewer-vcs-mutation",
                pending={
                    "kind": "drift",
                    "resume_phase": "REVIEW_DECISION",
                    "decision": "acknowledge-drift",
                },
                message=(
                    "Git state changed while reviewers were running; the change "
                    "may be external. Inspect the checkout before repair, gates, "
                    "or handoff, then resume with "
                    "--decision acknowledge-drift."
                ),
            )
        failed = [
            name
            for name in names
            if reviewers[name].get("verdict") not in {"PASS", "FAIL"}
        ]
        if failed:
            return self._waiting(
                reason="agent-failure",
                pending={
                    "kind": "agent",
                    "role": "reviewers",
                    "round": round_number,
                    "reviewers": failed,
                    "decision": "retry",
                },
                message=(
                    "One or more reviewer results are ambiguous ("
                    + ", ".join(failed)
                    + "); resume with --decision retry."
                ),
            )
        return None

    def _publish(self, round_number: int, *, next_phase: str) -> dict[str, Any] | None:
        if not self.config.publish_review_findings:
            return None
        publications = self.state.setdefault("publications", {})
        if publications.get(str(round_number), {}).get("published"):
            return None
        self._report(f"round {round_number}: publish review findings")
        try:
            message = self.publisher.publish(
                cwd=self.config.cwd,
                issue=self.config.issue,
                run_id=self.state["run_id"],
                round=round_number,
                reviews=self._round(round_number).get("reviewers", {}),
            )
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                detail = "publication interrupted"
            else:
                detail = str(exc)
            return self._waiting(
                reason="publication-failed",
                pending={
                    "kind": "publication",
                    "round": round_number,
                    "next_phase": next_phase,
                    "decision": "retry-publication",
                },
                message=f"Review findings were retained locally; {detail}. Resume with --decision retry-publication.",
            )
        publications[str(round_number)] = {"published": True, "message": message}
        self._save()
        return None

    def _stage_failed_review(self, failed_round: int) -> None:
        round_state = self._round(failed_round)
        expected = CheckoutSnapshot.from_dict(self.state["checkout"])
        self.git.stage_all(self.config.cwd)
        checkout = self.git.snapshot(self.config.cwd)
        if round_state.get("staged_for_repair") and _same_checkout(expected, checkout):
            return
        fixed_point = self._artifact(
            f"rounds/{failed_round}/fixed-point.json",
            _json_bytes(checkout.to_dict()),
        )
        round_state["fixed_point"] = fixed_point
        round_state["staged_for_repair"] = True
        self.state["vcs_mutation_by_orchestrator"] = True
        self.state["checkout"] = checkout.to_dict()
        self._save()

    def _start_repair_round(self, failed_round: int) -> dict[str, Any]:
        self._stage_failed_review(failed_round)
        next_round = failed_round + 1
        self._report(
            f"round {failed_round} failed review; starting fresh worker repair "
            f"round {next_round}"
        )
        worker_wait = self._run_worker(next_round)
        if worker_wait:
            return worker_wait
        reviewer_wait = self._run_reviewers(next_round)
        if reviewer_wait:
            return reviewer_wait
        return self._after_review(next_round)

    def _after_review(self, round_number: int) -> dict[str, Any]:
        self._set_phase("REVIEW_DECISION", round_number=round_number)
        publication_wait = self._publish(round_number, next_phase="REVIEW_DECISION")
        if publication_wait:
            return publication_wait
        reviews = self._round(round_number).get("reviewers", {})
        passed = all(review.get("verdict") == "PASS" for review in reviews.values())
        if passed:
            return self._run_gates(round_number)
        if round_number == 1:
            return self._start_repair_round(round_number)
        return self._waiting(
            reason="review-failed",
            pending={
                "kind": "review",
                "round": round_number,
                "decision": "start-next-round",
                "available_decisions": ["start-next-round", "accept"],
            },
            message=(
                "Review findings remain; resume with --decision "
                "start-next-round to create another repair round, or "
                "--decision accept to continue to gates."
            ),
        )

    def _run_gates(self, round_number: int) -> dict[str, Any]:
        self._set_phase("GATES", round_number=round_number)
        self.state["pending"] = {
            "kind": "gates",
            "round": round_number,
            "decision": "retry",
        }
        self._save()
        self._report("final review passed; running gates sequentially")
        results = self.state.setdefault("gate_results", [])
        for index, command in enumerate(self.config.gates):
            existing = next(
                (item for item in results if item.get("index") == index), None
            )
            if existing and existing.get("passed"):
                continue
            result = self.gates.run(command, self.config.cwd)
            record = {"index": index, **result.to_dict()}
            if existing:
                results[results.index(existing)] = record
            else:
                results.append(record)
            artifact = self._artifact(
                f"rounds/{round_number}/gate-{index + 1}.json", _json_bytes(record)
            )
            record["artifact"] = artifact
            self._save()
            if not result.passed:
                return self._waiting(
                    reason="gate-failed",
                    pending={
                        "kind": "gates",
                        "round": round_number,
                        "failed_index": index,
                        "decision": "retry",
                        "available_decisions": ["retry"],
                    },
                    message=f"Gate {index + 1} failed; resume with --decision retry.",
                )

        final_checkout = self.git.snapshot(self.config.cwd)
        initial = CheckoutSnapshot.from_dict(self.state["initial_checkout"])
        if (
            final_checkout.branch != initial.branch
            or final_checkout.head != initial.head
        ):
            return self._waiting(
                reason="vcs-mutated",
                pending={
                    "kind": "drift",
                    "resume_phase": "GATES",
                    "decision": "acknowledge-drift",
                },
                message=(
                    "The branch or HEAD changed; inspect the checkout and resume "
                    "with --decision acknowledge-drift."
                ),
            )
        self.state["checkout"] = final_checkout.to_dict()
        self.state["pending"] = None
        self.state["handoff"] = {
            "status": "READY_FOR_HANDOFF",
            "verified": True,
            "branch": final_checkout.branch,
            "head": final_checkout.head,
            "clean": final_checkout.clean,
            "vcs_mutation_by_orchestrator": bool(
                self.state.get("vcs_mutation_by_orchestrator", False)
            ),
        }
        self._set_phase("READY_FOR_HANDOFF", round_number=round_number)
        self._report(
            "READY_FOR_HANDOFF: verified; caller retains commit/merge/worktree ownership"
        )
        return self._result()

    def _check_drift(self) -> tuple[bool, CheckoutSnapshot]:
        if (
            self.cwd_override is not None
            and self.cwd_override != self.config.cwd.resolve()
        ):
            expected = CheckoutSnapshot.from_dict(self.state["checkout"])
            return (
                False,
                CheckoutSnapshot(
                    str(self.cwd_override),
                    expected.branch,
                    expected.head,
                    expected.status,
                ),
            )
        actual = self.git.snapshot(self.config.cwd)
        expected = CheckoutSnapshot.from_dict(self.state["checkout"])
        return _same_checkout(expected, actual), actual

    def _acknowledge_drift(self, actual: CheckoutSnapshot) -> None:
        """Adopt an inspected checkout and discard results from the old one."""

        self.state["checkout"] = actual.to_dict()
        self.state["gate_results"] = []
        self.state["handoff"] = None
        self._save()

    def execute(self, *, decision: str | None = None) -> dict[str, Any]:
        if self.state["state"] == "READY_FOR_HANDOFF":
            same, actual = self._check_drift()
            if not same:
                return self._waiting(
                    reason="checkout-drift",
                    pending={"kind": "drift", "decision": "acknowledge-drift"},
                    message="The saved checkout changed after handoff; inspect it before resuming.",
                )
            return self._result()

        same, actual = self._check_drift()
        if not same:
            if (
                self.cwd_override is not None
                and self.cwd_override != self.config.cwd.resolve()
            ):
                return self._waiting(
                    reason="checkout-path-mismatch",
                    pending={"kind": "drift", "decision": "retry"},
                    message=(
                        f"--cwd does not match the saved checkout {self.config.cwd}; "
                        "resume again without a different --cwd."
                    ),
                )
            pending = self.state.get("pending") or {}
            if decision != "acknowledge-drift":
                resume_phase = pending.get("resume_phase")
                if resume_phase not in {
                    "WORKER",
                    "REVIEWERS_RUNNING",
                    "REVIEW_DECISION",
                    "GATES",
                }:
                    pending_kind = pending.get("kind")
                    resume_phase = {
                        "agent": (
                            "WORKER"
                            if pending.get("role") == "worker"
                            else "REVIEWERS_RUNNING"
                        ),
                        "gates": "GATES",
                        "review": "REVIEW_DECISION",
                        "publication": "REVIEW_DECISION",
                    }.get(
                        str(pending_kind) if pending_kind is not None else "",
                        "WORKER",
                    )
                return self._waiting(
                    reason="checkout-drift",
                    pending={
                        "kind": "drift",
                        "resume_phase": resume_phase,
                        "decision": "acknowledge-drift",
                    },
                    message=(
                        "The saved checkout path, branch, HEAD, or Git state differs. "
                        "Inspect it and resume with --decision acknowledge-drift."
                    ),
                )
            self._acknowledge_drift(actual)
            self.state["pending"] = pending
            self._save()
            if (
                decision == "acknowledge-drift"
                and self.state["state"] == "WAITING_FOR_USER"
                and pending.get("kind") != "drift"
            ):
                decision = str(pending.get("decision", "retry"))
            elif decision == "acknowledge-drift" and self.state.get("phase") in {
                "WORKER",
                "REVIEWERS_RUNNING",
                "REVIEW_DECISION",
                "GATES",
            }:
                decision = "retry"

        if self.state["state"] == "PREPARE":
            worker_wait = self._run_worker(1)
            if worker_wait:
                return worker_wait
            reviewer_wait = self._run_reviewers(1)
            if reviewer_wait:
                return reviewer_wait
            return self._after_review(1)

        if decision is None:
            return self._waiting(
                reason="decision-required",
                pending={"kind": "resume", "decision": "retry"},
                message="This run needs an explicit resume decision; stdin is never read.",
            )

        phase = self.state.get("phase")
        pending = self.state.get("pending") or {}
        kind = pending.get("kind")
        if self.state["state"] == "WAITING_FOR_USER":
            if kind == "publication" and decision == "retry-publication":
                wait = self._publish(
                    int(pending["round"]), next_phase="REVIEW_DECISION"
                )
                if wait:
                    return wait
                return self._after_review(int(pending["round"]))
            if kind in {"agent", "gates"} and decision == "retry":
                round_number = int(pending.get("round", self.state.get("round", 1)))
                if kind == "agent" and pending.get("role") == "worker":
                    worker_wait = self._run_worker(round_number)
                    if worker_wait:
                        return worker_wait
                    reviewer_wait = self._run_reviewers(round_number)
                    if reviewer_wait:
                        return reviewer_wait
                    return self._after_review(round_number)
                if kind == "agent":
                    reviewer_wait = self._run_reviewers(
                        round_number, retry_verdicts=True
                    )
                    if reviewer_wait:
                        return reviewer_wait
                    return self._after_review(round_number)
                return self._run_gates(round_number)
            if kind == "review" and decision == "accept":
                return self._run_gates(
                    int(pending.get("round", self.state.get("round", 1)))
                )
            if kind == "review" and decision == "start-next-round":
                return self._start_repair_round(
                    int(pending.get("round", self.state.get("round", 1)))
                )
            if kind == "drift" and decision == "acknowledge-drift":
                self._acknowledge_drift(actual)
                resume_phase = pending.get("resume_phase")
                if resume_phase in {
                    "WORKER",
                    "REVIEWERS_RUNNING",
                    "REVIEW_DECISION",
                    "GATES",
                }:
                    self.state["state"] = resume_phase
                    self.state["phase"] = resume_phase
                    self.state["pending"] = {"kind": "resume", "decision": "retry"}
                    self._save()
                    return self.execute(decision="retry")
                self.state["pending"] = None
                self._save()
                return self._waiting(
                    reason="decision-required",
                    pending={"kind": "resume", "decision": "retry"},
                    message="Drift acknowledged; choose an explicit resume decision.",
                )
            return self._waiting(
                reason="invalid-decision",
                pending=pending,
                message=f"Decision {decision!r} is not valid for this state; inspect state.json.",
            )

        if phase == "WORKER" and decision == "retry":
            worker_wait = self._run_worker(int(self.state["round"]))
            if worker_wait:
                return worker_wait
            reviewer_wait = self._run_reviewers(int(self.state["round"]))
            if reviewer_wait:
                return reviewer_wait
            return self._after_review(int(self.state["round"]))
        if phase == "REVIEWERS_RUNNING" and decision == "retry":
            reviewer_wait = self._run_reviewers(
                int(self.state["round"]), retry_verdicts=True
            )
            if reviewer_wait:
                return reviewer_wait
            return self._after_review(int(self.state["round"]))
        if phase == "GATES" and decision == "retry":
            return self._run_gates(int(self.state["round"]))
        if phase == "REVIEW_DECISION" and decision in {"retry", "continue", "accept"}:
            return self._after_review(int(self.state["round"]))
        return self._waiting(
            reason="invalid-decision",
            pending={"kind": "resume", "decision": "retry"},
            message=f"Decision {decision!r} cannot resume phase {phase}; inspect state.json.",
        )


def _find_run_dir(state_dir: Path, run_id: str, cwd: Path | None, git: GitPort) -> Path:
    root = _state_root(state_dir)
    if cwd is not None:
        candidate = root / _repo_id(cwd.resolve(), git) / _slug(run_id)
        if (candidate / "state.json").exists():
            return candidate
    matches = []
    if root.exists():
        for state_path in root.glob("*/*/state.json"):
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
            except OSError, json.JSONDecodeError:
                continue
            if str(data.get("run_id")) == run_id:
                matches.append(state_path.parent)
    if len(matches) != 1:
        raise UsageError(f"could not uniquely locate run {run_id!r}")
    return matches[0]


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--issue", default="")
    parser.add_argument("--worker-prompt", type=Path, required=True)
    parser.add_argument("--repair-prompt", type=Path, required=True)
    parser.add_argument(
        "--reviewer", action="append", required=True, metavar="NAME=PATH"
    )
    parser.add_argument("--gate", action="append", default=[])
    parser.add_argument("--codexctl", default="codexctl")
    parser.add_argument("--run-id")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--publish-review-findings", action="store_true")
    _add_output_arguments(parser)


def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", choices=("text", "json"), default="text")
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="impl-review-orchestrator")
    _add_run_arguments(parser)
    return parser


def build_resume_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="impl-review-orchestrator")
    parser.add_argument("command", choices=("resume",), help="resume a workflow")
    parser.add_argument("resume_run_id", nargs="?")
    parser.add_argument("--run-id")
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--codexctl")
    parser.add_argument("--decision", required=True)
    parser.add_argument("--output", choices=("text", "json"), default="text")
    parser.add_argument("--json", action="store_true")
    return parser


def _config_from_args(args: argparse.Namespace) -> RunConfig:
    reviewers = tuple(parse_reviewer(value) for value in args.reviewer)
    cwd = args.cwd.expanduser().resolve()
    spec = args.spec.expanduser().resolve()
    worker = args.worker_prompt.expanduser().resolve()
    repair = args.repair_prompt.expanduser().resolve()
    if args.publish_review_findings and not args.issue:
        raise UsageError("--publish-review-findings requires --issue")
    if args.issue:
        parse_issue_uri(args.issue)
    return RunConfig(
        cwd=cwd,
        spec_path=spec,
        worker_prompt_path=worker,
        repair_prompt_path=repair,
        reviewers=reviewers,
        gates=tuple(args.gate),
        issue=str(args.issue),
        codexctl=args.codexctl,
        run_id=args.run_id or uuid.uuid4().hex,
        state_dir=args.state_dir,
        publish_review_findings=bool(args.publish_review_findings),
    )


def _render_text(result: dict[str, Any], *, out: Any = sys.stdout) -> None:
    out.write(
        f"Run: {result['runId']}\n"
        f"State: {result['state']}\n"
        f"State file: {result['statePath']}\n"
    )
    verdicts = result.get("reviewVerdicts", {})
    if verdicts:
        out.write(f"Reviews: {json.dumps(verdicts, sort_keys=True)}\n")
    gates = result.get("gateResults", [])
    if gates:
        out.write("Gates:\n")
        for gate in gates:
            out.write(f"  {'PASS' if gate['passed'] else 'FAIL'} {gate['command']}\n")
    if result.get("handoffStatus"):
        out.write(f"Handoff: {result['handoffStatus']}\n")
    if result.get("pending"):
        out.write(
            "Resume: impl-review-orchestrator resume "
            f"--run-id {result['runId']} --decision "
            f"{result['pending'].get('decision', 'retry')}\n"
        )
    out.flush()


def _stream_text(value: str) -> None:
    sys.stdout.write(value)
    sys.stdout.flush()


def _render_json(result: dict[str, Any], *, out: Any = sys.stdout) -> None:
    json.dump(result, out, ensure_ascii=False, indent=2, sort_keys=True)
    out.write("\n")
    out.flush()


def _emit_error(error: Exception, *, json_mode: bool) -> None:
    if json_mode:
        _render_json({"error": str(error)})
    else:
        print(f"impl-review-orchestrator: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = (
        build_resume_parser()
        if raw_argv and raw_argv[0] == "resume"
        else build_parser()
    )
    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2
    try:
        if getattr(args, "command", None) != "resume":
            config = _config_from_args(args)
            json_mode = args.output == "json" or args.json
            workflow = Workflow.new(
                config,
                progress=(lambda message: print(message, flush=True))
                if not json_mode
                else None,
                agent_output=_stream_text if not json_mode else None,
            )
            result = workflow.execute()
        elif args.command == "resume":
            run_id = args.run_id or args.resume_run_id
            if not run_id:
                raise UsageError("resume requires --run-id")
            json_mode = args.output == "json" or args.json
            git = GitAdapter()
            run_dir = _find_run_dir(args.state_dir, run_id, args.cwd, git)
            workflow = Workflow.from_store(
                ArtifactStore(run_dir),
                codexctl=args.codexctl,
                cwd_override=args.cwd,
                progress=(lambda message: print(message, flush=True))
                if not json_mode
                else None,
                agent_output=_stream_text if not json_mode else None,
            )
            result = workflow.execute(decision=args.decision)
        else:
            raise UsageError("use the direct workflow form or resume")
    except (OrchestratorError, OSError) as exc:
        json_mode = bool(
            "--json" in raw_argv or "--output" in raw_argv and "json" in raw_argv
        )
        _emit_error(exc, json_mode=json_mode)
        return 2

    if json_mode:
        _render_json(result)
    else:
        _render_text(result)
    return 0 if result["state"] == "READY_FOR_HANDOFF" else 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

MODULE_PATH = Path(__file__).parents[1] / "impl_review.py"
SPEC = importlib.util.spec_from_file_location("impl_review", MODULE_PATH)
assert SPEC and SPEC.loader
impl_review = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = impl_review
SPEC.loader.exec_module(impl_review)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test Worker")
    git(path, "config", "user.email", "worker@example.test")
    git(path, "config", "commit.gpgsign", "false")
    (path / "base.txt").write_text("base\n")
    git(path, "add", "base.txt")
    git(path, "commit", "-qm", "chore: base")
    return path


def inputs(tmp_path: Path) -> tuple[Path, Path, Path, tuple[tuple[str, Path], ...]]:
    spec = tmp_path / "spec.md"
    worker = tmp_path / "worker.md"
    repair = tmp_path / "repair.md"
    reviewer = tmp_path / "reviewer.md"
    spec.write_text("Implement the ticket.")
    worker.write_text("Implement the specification.")
    repair.write_text("Repair the supplied failures.")
    reviewer.write_text("Audit correctness and the specification.")
    return spec, worker, repair, (("spec", reviewer),)


class ScriptedCodex:
    def __init__(
        self,
        repo: Path,
        worker_actions: list[Callable[[], None]],
        verdicts: list[str | impl_review.AgentResult],
        worker_results: list[str | impl_review.AgentResult] | None = None,
    ):
        self.repo = repo
        self.worker_actions = worker_actions
        self.verdicts = verdicts
        self.worker_results = worker_results or []
        self.prompts: list[tuple[str, str, str | None]] = []
        self._serial = 0
        self._results: dict[str, impl_review.AgentResult] = {}
        self._lock = threading.Lock()
        self.history_calls: list[tuple[str, str]] = []
        self.follow_calls: list[tuple[str, str]] = []

    def _receipt(self, role: str, prompt: str, thread: str | None = None):
        with self._lock:
            self._serial += 1
            thread_id = thread or f"thread-{self._serial}"
            turn_id = f"turn-{self._serial}"
            self.prompts.append((role, prompt, thread))
            if role == "worker":
                action = self.worker_actions.pop(0)
                action()
                scripted = (
                    self.worker_results.pop(0)
                    if self.worker_results
                    else "Worker finished."
                )
            else:
                scripted = self.verdicts.pop(0)
            if isinstance(scripted, impl_review.AgentResult):
                result = scripted
                if not result.observed_turn_ids:
                    result.observed_turn_ids = [turn_id]
            else:
                raw = (
                    json.dumps(
                        {
                            "type": "item/completed",
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "item": {"type": "agentMessage", "text": scripted},
                        }
                    )
                    + "\n"
                ).encode()
                result = impl_review.AgentResult(
                    "completed", raw, [scripted], [turn_id]
                )
            self._results[turn_id] = result
            return impl_review.DetachReceipt(thread_id, turn_id)

    def start(self, *, prompt, cwd, role, approve, model, effort):
        assert cwd == self.repo
        assert role != "reviewer" or not approve
        return self._receipt(role, prompt)

    def resume(self, *, thread_id, prompt):
        role = "reviewer" if "VERDICT: PASS" in prompt else "worker"
        return self._receipt(role, prompt, thread_id)

    def follow(self, *, thread_id, turn_id):
        self.follow_calls.append((thread_id, turn_id))
        return self._results[turn_id]

    def history(self, *, thread_id, turn_id):
        self.history_calls.append((thread_id, turn_id))
        return self._results[turn_id]


class SimulatedCrash(BaseException):
    pass


class CrashAfterTerminalStateWriteStore(impl_review.ArtifactStore):
    marker: Path
    role_to_crash: str

    def write_state(self, state):
        super().write_state(state)
        intent = state.get("operation_intent") or {}
        if intent.get("kind") != "agent_terminal":
            return
        attempt = next(
            item for item in state["attempts"] if item["id"] == intent["attempt_id"]
        )
        if (
            attempt["role"] == self.role_to_crash
            and attempt["status"] == "completed"
            and not self.marker.exists()
        ):
            self.marker.touch()
            raise SimulatedCrash


class CrashAtRotationBoundaryStore(impl_review.ArtifactStore):
    marker: Path
    boundary: str

    def write_state(self, state):
        super().write_state(state)
        if self.marker.exists():
            return
        intent = state.get("operation_intent") or {}
        sessions = state.get("review_sessions", [])
        successor = intent.get("successor")
        target_sessions = [
            session
            for session in sessions
            if successor and session["id"] == successor.get("review_session_id")
        ]
        if (
            not target_sessions
            and len(sessions) >= 3
            and sessions[-1]["mode"] == "FULL"
        ):
            target_sessions = [sessions[-1]]
        target_attempts = [
            attempt
            for attempt in state.get("attempts", [])
            if target_sessions
            and attempt.get("review_session") == target_sessions[0]["id"]
        ]
        should_crash = (
            (
                self.boundary == "before_successor"
                and successor is not None
                and state.get("development_cohort") is None
                and not target_sessions
            )
            or (
                self.boundary == "after_session"
                and successor is not None
                and target_sessions
                and not target_attempts
            )
            or (
                self.boundary == "during_starts"
                and len(sessions) >= 3
                and sessions[-1]["mode"] == "FULL"
                and target_attempts
                and sum(attempt["status"] == "DETACHED" for attempt in target_attempts)
                == 1
                and (state.get("operation_intent") or {}).get("kind") == "agent_follow"
            )
            or (
                self.boundary == "after_cleanup"
                and state.get("status") == "READY_CERTIFIED"
                and len(sessions) >= 3
                and sessions[-1]["mode"] == "FULL"
                and state.get("operation_intent") is None
            )
        )
        if should_crash:
            self.marker.touch()
            raise SimulatedCrash


class CrashDuringFollowCodex(ScriptedCodex):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.crashed_turn: str | None = None

    def follow(self, *, thread_id, turn_id):
        if self.crashed_turn is None:
            self.crashed_turn = turn_id
            raise SimulatedCrash
        if turn_id == self.crashed_turn:
            return impl_review.AgentResult("unknown")
        return super().follow(thread_id=thread_id, turn_id=turn_id)


class ReviewerFollowFailureCodex(ScriptedCodex):
    def __init__(self, *args, failing_turns: set[str], **kwargs):
        super().__init__(*args, **kwargs)
        self.failing_turns = set(failing_turns)

    def follow(self, *, thread_id, turn_id):
        if turn_id in self.failing_turns:
            self.follow_calls.append((thread_id, turn_id))
            raise impl_review.OrchestratorError(f"follow failed for {turn_id}")
        return super().follow(thread_id=thread_id, turn_id=turn_id)


class BlockingReviewerFollowCodex(ScriptedCodex):
    def __init__(self, *args, blocked_turn: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.blocked_turn = blocked_turn
        self.blocked = threading.Event()
        self.release = threading.Event()

    def follow(self, *, thread_id, turn_id):
        if turn_id == self.blocked_turn:
            self.blocked.set()
            if not self.release.wait(10):
                raise AssertionError("blocked reviewer was not released")
        return super().follow(thread_id=thread_id, turn_id=turn_id)


class BlockingStartCodex:
    def __init__(self, ready_path: Path, release_path: Path):
        self.ready_path = ready_path
        self.release_path = release_path

    def start(self, *, prompt, cwd, role, approve, model, effort):
        assert role == "worker"
        assert not approve
        self.ready_path.touch()
        while not self.release_path.exists():
            time.sleep(0.01)
        return impl_review.DetachReceipt("worker-thread", "worker-turn")

    def follow(self, *, thread_id, turn_id):
        return impl_review.AgentResult("completed")


class RecordingGit:
    def __init__(self, events_path: Path):
        self.delegate = impl_review.GitAdapter()
        self.events_path = events_path

    def preflight(self, cwd):
        with self.events_path.open("a") as handle:
            handle.write(f"{os.getpid()}:preflight\n")
        return self.delegate.preflight(cwd)

    def __getattr__(self, name):
        return getattr(self.delegate, name)


def run_start_process(
    config: impl_review.RunConfig,
    events_path: Path,
    result_path: Path,
    ready_path: Path | None = None,
    release_path: Path | None = None,
) -> None:
    codex = (
        BlockingStartCodex(ready_path, release_path)
        if ready_path is not None and release_path is not None
        else ScriptedCodex(config.cwd, [lambda: None], [])
    )
    workflow = impl_review.Workflow(
        state_dir=config.state_dir,
        git=RecordingGit(events_path),
        codex=codex,
    )
    try:
        report = workflow.start(config)
    except BaseException as exc:
        result_path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        )
    else:
        result_path.write_text(json.dumps({"ok": True, "report": report}))


def commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-qm", f"feat: add {name}")


def config(repo: Path, tmp_path: Path, **changes):
    spec, worker, repair, reviewers = inputs(tmp_path)
    values = dict(
        cwd=repo,
        spec_path=spec,
        worker_prompt_path=worker,
        repair_prompt_path=repair,
        reviewers=reviewers,
        state_dir=tmp_path / "state",
        run_id="test-run",
        gates=("git diff --quiet",),
    )
    values.update(changes)
    return impl_review.RunConfig(**values)


def test_multiple_commits_are_certified_as_one_cumulative_subject(
    repo: Path, tmp_path: Path
):
    def worker() -> None:
        commit(repo, "one.txt", "one\n")
        commit(repo, "two.txt", "two\n")

    codex = ScriptedCodex(repo, [worker], ["Looks good.\nVERDICT: PASS"])
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    report = workflow.start(config(repo, tmp_path))

    assert report["status"] == "READY_CERTIFIED"
    assert report["exitCode"] == 0
    assert report["certificate"]["candidate_head"] == git(repo, "rev-parse", "HEAD")
    reviewer_prompt = next(
        prompt for role, prompt, _ in codex.prompts if role == "reviewer"
    )
    assert f"{report['baseCommit']}..{report['candidateHead']}" in reviewer_prompt
    assert "Do not run gates" in reviewer_prompt
    assert workflow.inspect("test-run")["status"] == "READY_CERTIFIED"
    assert (
        impl_review.main(
            [
                "inspect",
                "--state-dir",
                str(tmp_path / "state"),
                "--output",
                "json",
                "test-run",
            ]
        )
        == 0
    )


def test_failed_full_then_delta_pass_rotates_to_fresh_full(repo: Path, tmp_path: Path):
    codex = ScriptedCodex(
        repo,
        [
            lambda: commit(repo, "first.txt", "first\n"),
            lambda: commit(repo, "repair.txt", "repair\n"),
        ],
        [
            "Fix the implementation.\nVERDICT: FAIL",
            "Repair is correct.\nVERDICT: PASS",
            "Cumulative audit passes.\nVERDICT: PASS",
        ],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    report = workflow.start(config(repo, tmp_path, max_auto_worker_rounds=2))

    assert report["status"] == "READY_CERTIFIED"
    reviewer_calls = [
        (prompt, resumed)
        for role, prompt, resumed in codex.prompts
        if role == "reviewer"
    ]
    assert "FULL audit" in reviewer_calls[0][0]
    assert "DELTA review" in reviewer_calls[1][0]
    assert reviewer_calls[1][1] is not None
    assert "FULL audit" in reviewer_calls[2][0]
    assert reviewer_calls[2][1] is None
    state = json.loads(Path(report["statePath"]).read_text())
    assert state["review_sessions"][0]["status"] == "SUPERSEDED_BY_FULL_AUDIT"


@pytest.mark.parametrize(
    "boundary", ["before_successor", "after_session", "during_starts", "after_cleanup"]
)
def test_delta_to_full_rotation_reconciles_each_crash_boundary(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
):
    _, _, _, default_reviewers = inputs(tmp_path)
    rubric = default_reviewers[0][1]
    marker = tmp_path / f"{boundary}.crash"
    CrashAtRotationBoundaryStore.marker = marker
    CrashAtRotationBoundaryStore.boundary = boundary
    monkeypatch.setattr(impl_review, "ArtifactStore", CrashAtRotationBoundaryStore)
    codex = ScriptedCodex(
        repo,
        [
            lambda: commit(repo, "first.txt", "first\n"),
            lambda: commit(repo, "repair.txt", "repair\n"),
        ],
        [
            "OLD FINDING one.\nVERDICT: FAIL",
            "OLD FINDING two.\nVERDICT: FAIL",
            "Delta recheck passes.\nVERDICT: PASS",
            "Delta recheck also passes.\nVERDICT: PASS",
            "Fresh cumulative audit passes.\nVERDICT: PASS",
            "Fresh cumulative audit also passes.\nVERDICT: PASS",
        ],
    )
    config_value = config(
        repo,
        tmp_path,
        reviewers=(("one", rubric), ("two", rubric)),
        max_auto_worker_rounds=2,
    )

    with pytest.raises(SimulatedCrash):
        impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
            config_value
        )

    state_path = next((tmp_path / "state").glob("*/test-run/state.json"))
    crashed = json.loads(state_path.read_text())
    if boundary in {"before_successor", "after_session"}:
        intent = crashed["operation_intent"]
        assert intent["kind"] == "review_finalize"
        assert intent["successor"]["mode"] == "FULL"
        assert intent["successor"]["candidate_head"] == crashed["candidate_head"]
        assert (
            intent["successor"]["gate_policy_digest"]
            == crashed["config"]["gate_policy_digest"]
        )
    if boundary == "after_session":
        assert crashed["review_sessions"][-1]["mode"] == "FULL"
        assert not any(
            attempt["review_session"] == crashed["review_sessions"][-1]["id"]
            for attempt in crashed["attempts"]
        )
    if boundary == "during_starts":
        assert crashed["review_sessions"][-1]["mode"] == "FULL"
        assert crashed["operation_intent"]["kind"] == "agent_follow"
        assert (
            sum(
                attempt["review_session"] == crashed["review_sessions"][-1]["id"]
                for attempt in crashed["attempts"]
            )
            == 2
        )
    if boundary == "after_cleanup":
        assert crashed["status"] == "READY_CERTIFIED"
        assert crashed["operation_intent"] is None

    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    recovered = workflow.resume("test-run")
    if boundary == "during_starts":
        assert recovered["status"] == "WAITING"
        assert recovered["waitingReason"] == "REVIEWER_FAILURE"
        recovered = workflow.resume("test-run", "RETRY_REVIEWERS")
    assert recovered["status"] == "READY_CERTIFIED"

    repeated = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )
    assert repeated == recovered

    state = json.loads(state_path.read_text())
    assert len(state["review_sessions"]) == 3
    assert len(state["cohorts"]) == 2
    assert not any(attempt["status"] == "START_INTENT" for attempt in state["attempts"])
    assert not any(
        session["status"] == "RUNNING" for session in state["review_sessions"]
    )
    successor = state["review_sessions"][-1]
    successor_cohort = state["cohorts"][-1]
    assert successor["mode"] == "FULL"
    assert successor["status"] == "PASSED"
    assert successor["cohort_id"] == successor_cohort["id"]
    assert successor["candidate_head"] == state["candidate_head"]
    assert successor["policy_digest"] == state["certificate"]["policy_digest"]
    assert state["gate_attestation"]["candidate_head"] == successor["candidate_head"]
    assert (
        state["gate_attestation"]["policy_digest"]
        == state["config"]["gate_policy_digest"]
    )
    assert successor["previous_checkpoint"] is None
    assert successor_cohort["fresh"] is True
    assert set(successor_cohort["threads"].values()).isdisjoint(
        state["cohorts"][0]["threads"].values()
    )
    old_finding = "OLD FINDING"
    target_prompts = [
        prompt
        for role, prompt, resumed in codex.prompts
        if role == "reviewer" and resumed is None
    ][-2:]
    assert target_prompts
    assert all(old_finding not in prompt for prompt in target_prompts)


def test_prompt_amendment_is_persistent_and_waiver_is_distinct(
    repo: Path, tmp_path: Path
):
    codex = ScriptedCodex(
        repo,
        [
            lambda: commit(repo, "first.txt", "first\n"),
            lambda: commit(repo, "second.txt", "second\n"),
        ],
        ["Still wrong.\nVERDICT: FAIL", "Still wrong.\nVERDICT: FAIL"],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    waiting = workflow.start(config(repo, tmp_path, max_auto_worker_rounds=1))
    assert waiting["allowedActions"] == [
        "START_NEXT_ROUND",
        "ACCEPT_FINDINGS",
        "REQUIRE_FRESH_AUDIT",
    ]

    waiting = workflow.resume(
        "test-run",
        "START_NEXT_ROUND",
        additional_prompt="Also preserve the public API.",
    )
    assert waiting["status"] == "WAITING"
    assert "Also preserve the public API." in codex.prompts[-2][1]
    waived = workflow.resume("test-run", "ACCEPT_FINDINGS")
    assert waived["status"] == "READY_WITH_WAIVER"
    assert waived["exitCode"] == 3
    assert waived["certificate"] is None
    assert (
        impl_review.main(
            [
                "inspect",
                "--state-dir",
                str(tmp_path / "state"),
                "test-run",
            ]
        )
        == 3
    )


def test_amendment_snapshots_bind_each_prompt_to_its_policy(repo: Path, tmp_path: Path):
    codex = ScriptedCodex(
        repo,
        [
            lambda: commit(repo, "first.txt", "first\n"),
            lambda: commit(repo, "second.txt", "second\n"),
            lambda: commit(repo, "third.txt", "third\n"),
        ],
        [
            "Needs repair.\nVERDICT: FAIL",
            "Still needs repair.\nVERDICT: FAIL",
            "Delta passes.\nVERDICT: PASS",
            "Fresh full audit passes.\nVERDICT: PASS",
        ],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)

    waiting = workflow.start(config(repo, tmp_path, max_auto_worker_rounds=1))
    waiting = workflow.resume(
        "test-run",
        "START_NEXT_ROUND",
        additional_prompt="Preserve the public API.",
    )
    assert waiting["status"] == "WAITING"
    report = workflow.resume(
        "test-run",
        "START_NEXT_ROUND",
        additional_prompt="Keep the audit trace complete.",
    )

    assert report["status"] == "READY_CERTIFIED"
    state = json.loads(Path(report["statePath"]).read_text())
    expected_ids = [
        [],
        ["amendment-1"],
        ["amendment-1", "amendment-2"],
        ["amendment-1", "amendment-2"],
    ]
    assert [
        session["amendment_ids"] for session in state["review_sessions"]
    ] == expected_ids

    amendment_digests = {
        amendment["id"]: amendment["sha256"] for amendment in state["amendments"]
    }
    expected_digests = [
        hashlib.sha256(b"").hexdigest(),
        hashlib.sha256(amendment_digests["amendment-1"].encode()).hexdigest(),
        hashlib.sha256(
            "\0".join(
                [
                    amendment_digests["amendment-1"],
                    amendment_digests["amendment-2"],
                ]
            ).encode()
        ).hexdigest(),
        hashlib.sha256(
            "\0".join(
                [
                    amendment_digests["amendment-1"],
                    amendment_digests["amendment-2"],
                ]
            ).encode()
        ).hexdigest(),
    ]
    assert [
        session["amendment_digest"] for session in state["review_sessions"]
    ] == expected_digests

    worker_attempts = [
        attempt for attempt in state["attempts"] if attempt["role"] == "worker"
    ]
    assert [attempt["amendment_ids"] for attempt in worker_attempts] == expected_ids[:3]
    for attempt in state["attempts"]:
        prompt = (
            Path(report["statePath"]).parent / attempt["prompt_artifact"]
        ).read_text()
        assert (
            attempt["prompt_policy_digest"]
            == hashlib.sha256(
                f"{prompt}\0{attempt['amendment_digest']}".encode()
            ).hexdigest()
        )

    reviewer_attempts = [
        attempt for attempt in state["attempts"] if attempt["role"] == "reviewer"
    ]
    for session in state["review_sessions"]:
        matching = [
            attempt
            for attempt in reviewer_attempts
            if attempt["review_session"] == session["id"]
        ]
        assert matching
        assert all(
            attempt["amendment_ids"] == session["amendment_ids"]
            and attempt["amendment_digest"] == session["amendment_digest"]
            for attempt in matching
        )


def test_reports_and_manifest_preserve_all_agent_identity_associations(
    repo: Path, tmp_path: Path
):
    _, _, _, default_reviewers = inputs(tmp_path)
    rubric = default_reviewers[0][1]
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [
            "One passes.\nVERDICT: PASS",
            impl_review.AgentResult("interrupted", error="reviewer interrupted"),
            "Replacement passes.\nVERDICT: PASS",
        ],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    report = workflow.start(
        config(
            repo,
            tmp_path,
            reviewers=(("one", rubric), ("two", rubric)),
            max_auto_worker_rounds=1,
        )
    )
    report = workflow.resume("test-run", "RETRY_REVIEWERS")

    state = json.loads(Path(report["statePath"]).read_text())
    attempts = {attempt["id"]: attempt for attempt in state["attempts"]}
    assert all(
        attempt["thread_id"] and attempt["turn_id"] for attempt in attempts.values()
    )
    report_attempts = {attempt["attemptId"]: attempt for attempt in report["attempts"]}
    assert set(report_attempts) == set(attempts)
    for attempt_id, attempt in attempts.items():
        projected = report_attempts[attempt_id]
        assert projected["threadId"] == attempt["thread_id"]
        assert projected["turnId"] == attempt["turn_id"]
        assert projected["observedTurnIds"] == attempt["observed_turn_ids"]

    for session in state["review_sessions"]:
        for role, result in session["results"].items():
            attempt = attempts[result["attempt_id"]]
            assert result["thread_id"] == attempt["thread_id"]
            assert result["turn_id"] == attempt["turn_id"]
            assert result["observed_turn_ids"] == attempt["observed_turn_ids"]
            projected = next(
                item
                for item in report["reviewSessions"]
                if item["reviewSessionId"] == session["id"]
            )["results"][role]
            assert projected["threadId"] == result["thread_id"]
            assert projected["turnId"] == result["turn_id"]

    manifest = [
        artifact
        for artifact in state["artifacts"]
        if artifact["attempt_id"] is not None
    ]
    assert manifest
    for artifact in manifest:
        attempt = attempts[artifact["attempt_id"]]
        assert artifact["review_session_id"] == attempt["review_session"]
        assert artifact["role"] == attempt["role"]
        assert artifact["thread_id"] == attempt["thread_id"]
        assert artifact["turn_id"] == attempt["turn_id"]
        assert artifact["observed_turn_ids"] == attempt["observed_turn_ids"]


def test_no_change_waits_without_inferring_the_spec_is_satisfied(
    repo: Path, tmp_path: Path
):
    codex = ScriptedCodex(repo, [lambda: None], [])
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)

    report = workflow.start(config(repo, tmp_path))

    assert report["waitingReason"] == "WORKER_NO_CHANGE"
    assert report["allowedActions"] == ["START_NEXT_ROUND", "REQUIRE_FRESH_AUDIT"]
    assert git(repo, "status", "--porcelain") == ""
    assert (
        impl_review.main(
            [
                "inspect",
                "--state-dir",
                str(tmp_path / "state"),
                "test-run",
            ]
        )
        == 2
    )


def test_gate_timeout_is_execution_error_and_does_not_start_repair(
    repo: Path, tmp_path: Path
):
    codex = ScriptedCodex(repo, [lambda: commit(repo, "work.txt", "work\n")], [])
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)

    report = workflow.start(
        config(repo, tmp_path, gates=("sleep 2",), gate_timeout_seconds=1)
    )

    assert report["waitingReason"] == "GATE_EXECUTION_ERROR"
    assert report["allowedActions"] == ["RETRY_GATES", "REQUIRE_FRESH_AUDIT"]
    assert len([role for role, _, _ in codex.prompts if role == "worker"]) == 1


def test_gate_checkout_mutation_is_never_cleaned_or_adopted(repo: Path, tmp_path: Path):
    codex = ScriptedCodex(repo, [lambda: commit(repo, "work.txt", "work\n")], [])
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)

    report = workflow.start(config(repo, tmp_path, gates=("touch gate-output",)))

    assert report["waitingReason"] == "GATE_MUTATED_CHECKOUT"
    assert report["allowedActions"] == []
    assert (repo / "gate-output").exists()


def test_verify_checkpoint_crash_before_gate_intent_reconciles_each_gate(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class RecordingGates:
        def __init__(self):
            self.calls: list[str] = []

        def run(self, command, cwd, timeout):
            self.calls.append(command)
            return impl_review.GateExecution("passed", 0, b"", b"")

    class CrashAfterVerifyCheckpointStore(impl_review.ArtifactStore):
        marker = tmp_path / "verify-checkpoint-crash"

        def write_state(self, state):
            super().write_state(state)
            if self.marker.exists():
                return
            if (
                state.get("status") == "RUNNING"
                and state.get("phase") == "VERIFY_CHECKPOINT"
                and state.get("operation_intent") is None
                and not state.get("gate_results")
                and state.get("gate_attestation") is None
            ):
                self.marker.touch()
                raise SimulatedCrash

    monkeypatch.setattr(impl_review, "ArtifactStore", CrashAfterVerifyCheckpointStore)
    gates = RecordingGates()
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        ["Cumulative review passes.\nVERDICT: PASS"],
    )
    workflow = impl_review.Workflow(
        state_dir=tmp_path / "state", codex=codex, gates=gates
    )

    with pytest.raises(SimulatedCrash):
        workflow.start(config(repo, tmp_path, gates=("gate one", "gate two")))

    crashed = workflow.inspect("test-run")
    assert crashed["status"] == "RUNNING"
    assert crashed["phase"] == "VERIFY_CHECKPOINT"
    assert gates.calls == []

    recovered = impl_review.Workflow(
        state_dir=tmp_path / "state", codex=codex, gates=gates
    ).resume("test-run")

    assert recovered["status"] == "READY_CERTIFIED"
    assert gates.calls == ["gate one", "gate two"]
    reviewer_prompts = [
        prompt for role, prompt, _ in codex.prompts if role == "reviewer"
    ]
    assert len(reviewer_prompts) == 1

    repeated = impl_review.Workflow(
        state_dir=tmp_path / "state", codex=codex, gates=gates
    ).resume("test-run")
    assert repeated == recovered
    assert gates.calls == ["gate one", "gate two"]


def test_gate_recovery_drift_waiting_state_is_loadable(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class CrashAfterGateIntentStore(impl_review.ArtifactStore):
        marker = tmp_path / "gate-intent-crash"

        def write_state(self, state):
            super().write_state(state)
            if self.marker.exists():
                return
            if (
                state.get("status") == "RUNNING"
                and state.get("phase") == "VERIFY_CHECKPOINT"
                and (state.get("operation_intent") or {}).get("kind") == "gate"
            ):
                self.marker.touch()
                raise SimulatedCrash

    class RecordingGates:
        def __init__(self):
            self.calls: list[str] = []

        def run(self, command, cwd, timeout):
            self.calls.append(command)
            return impl_review.GateExecution("passed", 0, b"", b"")

    monkeypatch.setattr(impl_review, "ArtifactStore", CrashAfterGateIntentStore)
    gates = RecordingGates()
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        ["Review passes.\nVERDICT: PASS"],
    )
    with pytest.raises(SimulatedCrash):
        impl_review.Workflow(
            state_dir=tmp_path / "state", codex=codex, gates=gates
        ).start(config(repo, tmp_path, gates=("gate one",)))

    state_path = next((tmp_path / "state").glob("*/test-run/state.json"))
    assert json.loads(state_path.read_text())["operation_intent"]["kind"] == "gate"
    commit(repo, "external.txt", "external\n")

    waiting = impl_review.Workflow(
        state_dir=tmp_path / "state", codex=codex, gates=gates
    ).resume("test-run")

    assert waiting["status"] == "WAITING"
    assert waiting["waitingReason"] == "GATE_MUTATED_CHECKOUT"
    persisted = json.loads(state_path.read_text())
    assert persisted["operation_intent"] is None
    assert persisted["continuation_phase"] == "VERIFY_CHECKPOINT"
    assert persisted["waiting_reason"] == "GATE_MUTATED_CHECKOUT"
    assert persisted["allowed_actions"] == []
    inspected = impl_review.Workflow(
        state_dir=tmp_path / "state", codex=codex, gates=gates
    ).inspect("test-run")
    assert inspected["status"] == "WAITING"
    resumed = impl_review.Workflow(
        state_dir=tmp_path / "state", codex=codex, gates=gates
    ).resume("test-run")
    assert resumed == waiting
    assert gates.calls == []


def test_gate_attestation_crash_reconciles_delta_without_rerunning_gates(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class RecordingGates:
        def __init__(self):
            self.calls: list[str] = []

        def run(self, command, cwd, timeout):
            self.calls.append(command)
            return impl_review.GateExecution("passed", 0, b"", b"")

    class CrashAfterAttestationStore(impl_review.ArtifactStore):
        marker = tmp_path / "gate-attestation-crash"

        def write_state(self, state):
            super().write_state(state)
            if self.marker.exists():
                return
            if (
                state.get("status") == "RUNNING"
                and state.get("phase") == "VERIFY_CHECKPOINT"
                and state.get("operation_intent") is None
                and state.get("development_cohort")
                and state.get("gate_attestation") is not None
            ):
                self.marker.touch()
                raise SimulatedCrash

    monkeypatch.setattr(impl_review, "ArtifactStore", CrashAfterAttestationStore)
    gates = RecordingGates()
    codex = ScriptedCodex(
        repo,
        [
            lambda: commit(repo, "first.txt", "first\n"),
            lambda: commit(repo, "repair.txt", "repair\n"),
        ],
        [
            "Needs repair.\nVERDICT: FAIL",
            "Delta review passes.\nVERDICT: PASS",
            "Fresh full audit passes.\nVERDICT: PASS",
        ],
    )
    workflow = impl_review.Workflow(
        state_dir=tmp_path / "state", codex=codex, gates=gates
    )

    with pytest.raises(SimulatedCrash):
        workflow.start(
            config(
                repo,
                tmp_path,
                gates=("gate one", "gate two"),
                max_auto_worker_rounds=2,
            )
        )

    crashed = workflow.inspect("test-run")
    assert crashed["status"] == "RUNNING"
    assert crashed["phase"] == "VERIFY_CHECKPOINT"
    assert gates.calls == ["gate one", "gate two", "gate one", "gate two"]

    recovered = impl_review.Workflow(
        state_dir=tmp_path / "state", codex=codex, gates=gates
    ).resume("test-run")

    assert recovered["status"] == "READY_CERTIFIED"
    assert gates.calls == ["gate one", "gate two", "gate one", "gate two"]
    reviewer_prompts = [
        prompt for role, prompt, _ in codex.prompts if role == "reviewer"
    ]
    assert "mode=FULL" in reviewer_prompts[0]
    assert "mode=DELTA" in reviewer_prompts[1]
    assert "mode=FULL" in reviewer_prompts[2]

    repeated = impl_review.Workflow(
        state_dir=tmp_path / "state", codex=codex, gates=gates
    ).resume("test-run")
    assert repeated == recovered
    assert gates.calls == ["gate one", "gate two", "gate one", "gate two"]


def test_detached_base_is_valid_and_branch_collision_is_rejected(
    repo: Path, tmp_path: Path
):
    git(repo, "checkout", "--detach", "-q")
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        ["Pass.\nVERDICT: PASS"],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    report = workflow.start(config(repo, tmp_path, branch="review/work"))
    assert report["status"] == "READY_CERTIFIED"
    assert git(repo, "branch", "--show-current") == "review/work"

    other_state = tmp_path / "other-state"
    other = impl_review.Workflow(state_dir=other_state, codex=codex)
    with pytest.raises(impl_review.UsageError, match="branch already exists"):
        other.start(
            config(
                repo,
                tmp_path,
                state_dir=other_state,
                run_id="other",
                branch="review/work",
            )
        )


@pytest.mark.parametrize("drift", ["commit", "branch", "tracked", "untracked"])
def test_start_rejects_checkout_drift_before_branch_creation(
    repo: Path, tmp_path: Path, drift: str
):
    target = "review/drift-window"

    def mutate() -> None:
        if drift == "commit":
            commit(repo, "external.txt", "external\n")
        elif drift == "branch":
            git(repo, "switch", "-c", "external-drift", "-q")
        elif drift == "tracked":
            (repo / "base.txt").write_text("changed outside the workflow\n")
        else:
            (repo / "untracked.txt").write_text("created outside the workflow\n")

    class DriftAfterPreflightGit:
        def __init__(self):
            self.delegate = impl_review.GitAdapter()
            self.mutated = False

        def preflight(self, cwd):
            return self.delegate.preflight(cwd)

        def snapshot(self, cwd):
            if not self.mutated:
                self.mutated = True
                mutate()
            return self.delegate.snapshot(cwd)

        def __getattr__(self, name):
            return getattr(self.delegate, name)

    initial_branch = git(repo, "branch", "--show-current")
    initial_head = git(repo, "rev-parse", "HEAD")
    codex = ScriptedCodex(repo, [lambda: None], [])

    with pytest.raises(impl_review.UsageError, match="CHECKOUT_DRIFT"):
        impl_review.Workflow(
            state_dir=tmp_path / "state",
            git=DriftAfterPreflightGit(),
            codex=codex,
        ).start(config(repo, tmp_path, branch=target))

    assert git(repo, "branch", "--show-current") == (
        "external-drift" if drift == "branch" else initial_branch
    )
    assert git(repo, "branch", "--list", target) == ""
    assert not codex.prompts
    if drift == "commit":
        assert git(repo, "rev-parse", "HEAD") != initial_head
    else:
        assert git(repo, "rev-parse", "HEAD") == initial_head
    if drift in {"tracked", "untracked"}:
        assert git(repo, "status", "--porcelain")


def test_review_prompts_use_snapshotted_full_and_delta_wrappers(
    repo: Path, tmp_path: Path
):
    codex = ScriptedCodex(
        repo,
        [
            lambda: commit(repo, "work.txt", "work\n"),
            lambda: commit(repo, "repair.txt", "repair\n"),
        ],
        [
            "Needs repair.\nVERDICT: FAIL",
            "Delta passes.\nVERDICT: PASS",
            "Full passes.\nVERDICT: PASS",
        ],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    waiting = workflow.start(config(repo, tmp_path, max_auto_worker_rounds=1))
    assert waiting["waitingReason"] == "REVIEW_FAILED"

    original_full = impl_review.FULL_WRAPPER
    original_delta = impl_review.DELTA_WRAPPER
    try:
        impl_review.FULL_WRAPPER = "MUTATED FULL {candidate_head}"
        impl_review.DELTA_WRAPPER = "MUTATED DELTA {candidate_head}"
        report = workflow.resume("test-run", "START_NEXT_ROUND")
    finally:
        impl_review.FULL_WRAPPER = original_full
        impl_review.DELTA_WRAPPER = original_delta

    assert report["status"] == "READY_CERTIFIED"
    review_prompts = [prompt for role, prompt, _ in codex.prompts if role == "reviewer"]
    assert "DELTA review" in review_prompts[1]
    assert "FULL audit" in review_prompts[2]
    assert all("MUTATED" not in prompt for prompt in review_prompts)


def test_dirty_worker_completion_is_a_contract_violation(repo: Path, tmp_path: Path):
    def leave_dirty() -> None:
        (repo / "untracked.txt").write_text("dirty\n")

    codex = ScriptedCodex(repo, [leave_dirty], [])
    report = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
        config(repo, tmp_path)
    )

    assert report["status"] == "WAITING"
    assert report["waitingReason"] == "WORKER_CONTRACT_VIOLATION"
    assert report["allowedActions"] == []
    assert report["exitCode"] == 2


@pytest.mark.parametrize(
    ("status", "expected_actions"),
    [
        (
            "failed",
            ["CONTINUE_WORKER", "ACCEPT_WORKER_RESULT"],
        ),
        (
            "interrupted",
            ["CONTINUE_WORKER", "ACCEPT_WORKER_RESULT"],
        ),
        ("unknown", ["CONTINUE_WORKER"]),
        ("protocol_error", ["CONTINUE_WORKER"]),
    ],
)
def test_worker_recovery_actions_match_after_reload(
    repo: Path,
    tmp_path: Path,
    status: str,
    expected_actions: list[str],
):
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [],
        worker_results=[impl_review.AgentResult(status)],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)

    immediate = workflow.start(config(repo, tmp_path, max_auto_worker_rounds=1))
    reloaded = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )

    assert immediate["allowedActions"] == expected_actions
    assert reloaded["allowedActions"] == expected_actions


@pytest.mark.parametrize(
    ("field", "value"),
    [("phase", "NOT_A_PHASE"), ("status", "NOT_A_STATUS")],
)
def test_invalid_persisted_phase_and_status_are_usage_errors(
    repo: Path, tmp_path: Path, field: str, value: str
):
    codex = ScriptedCodex(repo, [lambda: None], [])
    report = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
        config(repo, tmp_path, max_auto_worker_rounds=1)
    )
    state_path = Path(report["statePath"])
    state = json.loads(state_path.read_text())
    state[field] = value
    state_path.write_text(json.dumps(state))

    with pytest.raises(
        impl_review.UsageError, match="invalid persisted workflow state"
    ):
        impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).inspect(
            "test-run"
        )


def test_invalid_persisted_intent_is_a_usage_error(repo: Path, tmp_path: Path):
    codex = ScriptedCodex(repo, [lambda: None], [])
    report = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
        config(repo, tmp_path, max_auto_worker_rounds=1)
    )
    state_path = Path(report["statePath"])
    state = json.loads(state_path.read_text())
    state["operation_intent"] = {"kind": "agent_follow"}
    state_path.write_text(json.dumps(state))

    with pytest.raises(
        impl_review.UsageError, match="invalid persisted workflow state"
    ):
        impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).inspect(
            "test-run"
        )


def test_invalid_persisted_attempt_status_is_a_usage_error(repo: Path, tmp_path: Path):
    codex = ScriptedCodex(repo, [lambda: None], [])
    report = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
        config(repo, tmp_path, max_auto_worker_rounds=1)
    )
    state_path = Path(report["statePath"])
    state = json.loads(state_path.read_text())
    state["attempts"][-1]["status"] = "NOT_AN_ATTEMPT_STATUS"
    state_path.write_text(json.dumps(state))

    with pytest.raises(
        impl_review.UsageError, match="invalid persisted workflow state"
    ):
        impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).inspect(
            "test-run"
        )


def test_invalid_persisted_reviewer_record_status_is_a_usage_error(
    repo: Path, tmp_path: Path
):
    rubric = inputs(tmp_path)[3][0][1]
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        ["Needs repair.\nVERDICT: FAIL"],
    )
    report = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
        config(
            repo,
            tmp_path,
            reviewers=(("spec", rubric),),
            max_auto_worker_rounds=1,
        )
    )
    state_path = Path(report["statePath"])
    state = json.loads(state_path.read_text())
    state["review_sessions"][-1]["results"]["spec"]["status"] = "NOT_A_RECORD_STATUS"
    state_path.write_text(json.dumps(state))

    with pytest.raises(
        impl_review.UsageError, match="invalid persisted workflow state"
    ):
        impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).inspect(
            "test-run"
        )


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_accept_worker_result_uses_the_recorded_descendant(
    repo: Path, tmp_path: Path, status: str
):
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        ["Accepted Worker result passes.\nVERDICT: PASS"],
        worker_results=[impl_review.AgentResult(status)],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)

    waiting = workflow.start(config(repo, tmp_path, max_auto_worker_rounds=1))
    state = json.loads(Path(waiting["statePath"]).read_text())
    recorded = state["pending_worker"]["descendant_head"]

    assert waiting["waitingReason"] == "WORKER_INTERRUPTED"
    assert waiting["allowedActions"] == ["CONTINUE_WORKER", "ACCEPT_WORKER_RESULT"]
    assert recorded == git(repo, "rev-parse", "HEAD")

    report = workflow.resume("test-run", "ACCEPT_WORKER_RESULT")

    assert report["status"] == "READY_CERTIFIED"
    assert report["candidateHead"] == recorded


def test_unknown_worker_result_does_not_offer_descendant_adoption(
    repo: Path, tmp_path: Path
):
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [],
        worker_results=[impl_review.AgentResult("unknown", error="ambiguous result")],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)

    waiting = workflow.start(config(repo, tmp_path, max_auto_worker_rounds=1))

    assert waiting["waitingReason"] == "AGENT_OUTCOME_UNKNOWN"
    assert waiting["allowedActions"] == ["CONTINUE_WORKER"]
    assert waiting["candidateHead"] == waiting["baseCommit"]
    with pytest.raises(impl_review.UsageError, match="not allowed"):
        workflow.resume("test-run", "ACCEPT_WORKER_RESULT")
    assert workflow.inspect("test-run")["allowedActions"] == ["CONTINUE_WORKER"]


def test_unknown_worker_without_detach_receipt_cannot_adopt_descendant(
    repo: Path, tmp_path: Path
):
    class UnknownBeforeDetachCodex(ScriptedCodex):
        def start(self, **kwargs):
            assert kwargs["role"] == "worker"
            self.worker_actions.pop(0)()
            raise impl_review.OrchestratorError("detach receipt unavailable")

    codex = UnknownBeforeDetachCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)

    waiting = workflow.start(config(repo, tmp_path, max_auto_worker_rounds=1))

    assert waiting["waitingReason"] == "AGENT_OUTCOME_UNKNOWN"
    assert waiting["allowedActions"] == []
    assert waiting["candidateHead"] == waiting["baseCommit"]
    reloaded = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )
    assert reloaded["allowedActions"] == waiting["allowedActions"]
    with pytest.raises(impl_review.UsageError, match="not allowed"):
        workflow.resume("test-run", "ACCEPT_WORKER_RESULT")
    assert workflow.inspect("test-run")["candidateHead"] == waiting["baseCommit"]


def test_stale_accept_worker_result_is_rejected_for_unknown_attempt(
    repo: Path, tmp_path: Path
):
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [],
        worker_results=[impl_review.AgentResult("unknown", error="ambiguous result")],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    waiting = workflow.start(config(repo, tmp_path, max_auto_worker_rounds=1))
    state_path = Path(waiting["statePath"])
    state = json.loads(state_path.read_text())
    state["allowed_actions"] = ["ACCEPT_WORKER_RESULT"]
    state_path.write_text(json.dumps(state))

    recovered = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    with pytest.raises(impl_review.UsageError, match="not allowed"):
        recovered.resume("test-run", "ACCEPT_WORKER_RESULT")
    assert recovered.inspect("test-run")["allowedActions"] == waiting["allowedActions"]


def test_accept_worker_result_rejects_a_later_external_commit(
    repo: Path, tmp_path: Path
):
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [],
        worker_results=[impl_review.AgentResult("interrupted")],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)

    waiting = workflow.start(config(repo, tmp_path, max_auto_worker_rounds=1))
    recorded = json.loads(Path(waiting["statePath"]).read_text())["pending_worker"][
        "descendant_head"
    ]
    commit(repo, "external.txt", "external\n")

    with pytest.raises(impl_review.UsageError, match="recorded Worker descendant"):
        workflow.resume("test-run", "ACCEPT_WORKER_RESULT")

    assert workflow.inspect("test-run")["waitingReason"] == "CHECKOUT_DRIFT"
    assert git(repo, "rev-parse", "HEAD") != recorded


def test_worker_start_persists_recovery_evidence_at_agent_start_boundary(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original_store = impl_review.ArtifactStore

    class BoundaryCheckingStore(original_store):
        def write_state(self, state):
            pending = state.get("pending_worker")
            if (
                state.get("status") == "RUNNING"
                and state.get("phase") == "WORKER"
                and pending is not None
            ):
                intent = state.get("operation_intent") or {}
                assert intent.get("kind") in {"agent_start", "agent_follow"}
                assert pending.get("attempt_id") == intent.get("attempt_id")
                assert any(
                    item["id"] == intent.get("attempt_id") for item in state["attempts"]
                )
            super().write_state(state)

    monkeypatch.setattr(impl_review, "ArtifactStore", BoundaryCheckingStore)

    class CrashBeforeAgentStartCodex(ScriptedCodex):
        def start(self, **kwargs):
            raise SimulatedCrash

    codex = CrashBeforeAgentStartCodex(repo, [lambda: None], [])
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    with pytest.raises(SimulatedCrash):
        workflow.start(config(repo, tmp_path))

    state_path = next((tmp_path / "state").glob("*/test-run/state.json"))
    state = json.loads(state_path.read_text())
    pending = state["pending_worker"]
    intent = state["operation_intent"]

    assert state["status"] == "RUNNING"
    assert state["phase"] == "WORKER"
    assert intent == {"kind": "agent_start", "attempt_id": pending["attempt_id"]}
    assert state["attempts"][-1]["status"] == "START_INTENT"

    recovered = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )

    assert recovered["waitingReason"] == "AGENT_OUTCOME_UNKNOWN"
    assert recovered["allowedActions"] == []


def test_rewritten_worker_history_is_checkout_drift(repo: Path, tmp_path: Path):
    def rewrite_history() -> None:
        commit(repo, "work.txt", "work\n")
        tree = git(repo, "rev-parse", "HEAD^{tree}")
        unrelated = git(repo, "commit-tree", tree, "-m", "feat: unrelated root")
        git(repo, "update-ref", "HEAD", unrelated)

    codex = ScriptedCodex(repo, [rewrite_history], [])
    report = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
        config(repo, tmp_path)
    )

    assert git(repo, "status", "--porcelain") == ""
    assert report["waitingReason"] == "CHECKOUT_DRIFT"
    assert report["allowedActions"] == []


def test_partial_reviewer_completion_retries_only_the_affected_role(
    repo: Path, tmp_path: Path
):
    _, _, _, default_reviewers = inputs(tmp_path)
    rubric = default_reviewers[0][1]
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [
            "First passes.\nVERDICT: PASS",
            impl_review.AgentResult("interrupted", error="reviewer interrupted"),
            "Replacement passes.\nVERDICT: PASS",
        ],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    waiting = workflow.start(
        config(
            repo,
            tmp_path,
            reviewers=(("one", rubric), ("two", rubric)),
            max_auto_worker_rounds=1,
        )
    )

    assert waiting["waitingReason"] == "REVIEWER_FAILURE"
    assert waiting["allowedActions"] == ["RETRY_REVIEWERS", "REQUIRE_FRESH_AUDIT"]
    report = workflow.resume("test-run", "RETRY_REVIEWERS")
    assert report["status"] == "READY_CERTIFIED"
    reviewer_attempts = [
        attempt for attempt in report["attempts"] if attempt["role"] == "reviewer"
    ]
    assert len(reviewer_attempts) == 3


def test_reviewer_follow_exception_waits_and_retries_only_the_failed_role(
    repo: Path, tmp_path: Path
):
    _, _, _, default_reviewers = inputs(tmp_path)
    rubric = default_reviewers[0][1]
    codex = ReviewerFollowFailureCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [
            "First reviewer passes.\nVERDICT: PASS",
            "Failed reviewer placeholder",
            "Replacement passes.\nVERDICT: PASS",
        ],
        failing_turns={"turn-3"},
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)

    waiting = workflow.start(
        config(
            repo,
            tmp_path,
            reviewers=(("one", rubric), ("two", rubric)),
            max_auto_worker_rounds=1,
        )
    )

    assert waiting["status"] == "WAITING"
    assert waiting["waitingReason"] == "REVIEWER_FAILURE"
    assert waiting["allowedActions"] == ["RETRY_REVIEWERS", "REQUIRE_FRESH_AUDIT"]
    results = waiting["reviewSessions"][-1]["results"]
    assert results["one"]["status"] == "completed"
    assert results["two"]["status"] == "unknown"
    failed_receipt = next(
        (item["threadId"], item["turnId"])
        for item in waiting["attempts"]
        if item["reviewerRole"] == "two"
    )

    report = workflow.resume("test-run", "RETRY_REVIEWERS")

    assert report["status"] == "READY_CERTIFIED"
    assert report["reviewSessions"][-1]["results"]["one"]["status"] == "completed"
    assert [item["role"] for item in report["attempts"]].count("reviewer") == 3
    assert codex.follow_calls.count(failed_receipt) == 1


def test_completed_reviewer_is_durable_while_another_follow_is_blocked(
    repo: Path, tmp_path: Path
):
    _, _, _, default_reviewers = inputs(tmp_path)
    rubric = default_reviewers[0][1]
    codex = BlockingReviewerFollowCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [
            "First reviewer passes.\nVERDICT: PASS",
            "Second reviewer passes.\nVERDICT: PASS",
        ],
        blocked_turn="turn-3",
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    result: list[dict[str, object] | BaseException] = []

    def run() -> None:
        try:
            result.append(
                workflow.start(
                    config(
                        repo,
                        tmp_path,
                        reviewers=(("one", rubric), ("two", rubric)),
                    )
                )
            )
        except BaseException as exc:
            result.append(exc)

    runner = threading.Thread(target=run)
    runner.start()
    try:
        assert codex.blocked.wait(5)
        deadline = time.monotonic() + 5
        state_path: Path | None = None
        state: dict[str, object] | None = None
        while time.monotonic() < deadline:
            paths = list((tmp_path / "state").glob("*/*/state.json"))
            if paths:
                candidate = json.loads(paths[0].read_text())
                results = candidate["review_sessions"][-1]["results"]
                if results.get("one", {}).get("status") == "completed":
                    state_path = paths[0]
                    state = candidate
                    break
            time.sleep(0.01)
        assert state_path is not None
        assert state is not None

        observed = impl_review.Workflow(
            state_dir=tmp_path / "state", codex=codex
        ).inspect("test-run")
        completed_result = observed["reviewSessions"][-1]["results"]["one"]
        completed_attempt = next(
            item
            for item in observed["attempts"]
            if item["attemptId"] == completed_result["attemptId"]
        )
        assert completed_attempt["status"] == "completed"
        assert completed_result["status"] == "completed"
        assert completed_result["turnId"] == completed_attempt["turnId"]
        assert completed_result["observedTurnIds"] == [completed_attempt["turnId"]]
        assert completed_result["messageArtifact"]
        assert completed_attempt["outputArtifacts"]
        assert state["cohorts"][0]["threads"]["one"] == completed_attempt["threadId"]
        assert all(
            (state_path.parent / artifact["path"]).is_file()
            for artifact in state["artifacts"]
            if artifact["attempt_id"] == completed_attempt["attemptId"]
        )
        assert observed["reviewSessions"][-1]["results"].get("two") is None
    finally:
        codex.release.set()
        runner.join(10)

    assert not runner.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], dict)
    assert result[0]["status"] == "READY_CERTIFIED"


def test_reviewer_completion_reconciles_after_per_result_write(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, _, _, default_reviewers = inputs(tmp_path)
    rubric = default_reviewers[0][1]

    CrashAfterTerminalStateWriteStore.marker = tmp_path / "reviewer-terminal-crash"
    CrashAfterTerminalStateWriteStore.role_to_crash = "reviewer"
    monkeypatch.setattr(impl_review, "ArtifactStore", CrashAfterTerminalStateWriteStore)
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [
            "First reviewer passes.\nVERDICT: PASS",
            "Second reviewer passes.\nVERDICT: PASS",
        ],
    )

    with pytest.raises(SimulatedCrash):
        impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
            config(
                repo,
                tmp_path,
                reviewers=(("one", rubric), ("two", rubric)),
                max_auto_worker_rounds=1,
            )
        )

    state_path = next((tmp_path / "state").glob("*/test-run/state.json"))
    crashed = json.loads(state_path.read_text())
    intent = crashed["operation_intent"]
    assert intent["kind"] == "agent_terminal"
    completed = [
        item
        for item in crashed["attempts"]
        if item["role"] == "reviewer" and item["status"] == "completed"
    ]
    detached = [
        item
        for item in crashed["attempts"]
        if item["role"] == "reviewer" and item["status"] == "DETACHED"
    ]
    assert len(completed) == 1
    assert len(detached) == 1
    assert intent["attempt_id"] == completed[0]["id"]
    assert intent["turn_id"] == completed[0]["turn_id"]
    assert crashed["review_sessions"][-1]["results"] == {}

    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    report = workflow.resume("test-run")

    assert report["status"] == "READY_CERTIFIED"
    assert report["reviewSessions"][-1]["results"]["one"]["status"] == "completed"
    assert report["reviewSessions"][-1]["results"]["two"]["status"] == "completed"
    completed_receipt = (completed[0]["thread_id"], completed[0]["turn_id"])
    assert codex.follow_calls.count(completed_receipt) == 1

    artifact_paths = [item["path"] for item in report["artifactManifest"]]
    repeated = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )
    assert repeated == report
    assert [
        item["path"] for item in json.loads(state_path.read_text())["artifacts"]
    ] == artifact_paths
    assert codex.follow_calls.count(completed_receipt) == 1


def test_all_unknown_reviewer_results_wait_without_auto_retry(
    repo: Path, tmp_path: Path
):
    _, _, _, default_reviewers = inputs(tmp_path)
    rubric = default_reviewers[0][1]
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [
            impl_review.AgentResult("unknown", error="first runtime failure"),
            impl_review.AgentResult("failed", error="second runtime failure"),
            "Replacement one passes.\nVERDICT: PASS",
            "Replacement two passes.\nVERDICT: PASS",
        ],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)

    waiting = workflow.start(
        config(
            repo,
            tmp_path,
            reviewers=(("one", rubric), ("two", rubric)),
            max_auto_worker_rounds=1,
        )
    )

    assert waiting["waitingReason"] == "REVIEWER_FAILURE"
    assert [
        waiting["reviewSessions"][-1]["results"][role]["status"]
        for role in ("one", "two")
    ] == ["unknown", "failed"]
    assert (
        len([item for item in waiting["attempts"] if item["role"] == "reviewer"]) == 2
    )

    report = workflow.resume("test-run", "RETRY_REVIEWERS")

    assert report["status"] == "READY_CERTIFIED"
    assert len([item for item in report["attempts"] if item["role"] == "reviewer"]) == 4


def test_stale_follow_intent_with_persisted_reviewer_result_reconciles(
    repo: Path, tmp_path: Path
):
    _, _, _, default_reviewers = inputs(tmp_path)
    rubric = default_reviewers[0][1]
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [
            impl_review.AgentResult("unknown", error="runtime failure"),
            impl_review.AgentResult("failed", error="runtime failure"),
        ],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    waiting = workflow.start(
        config(
            repo,
            tmp_path,
            reviewers=(("one", rubric), ("two", rubric)),
            max_auto_worker_rounds=1,
        )
    )
    state_path = Path(waiting["statePath"])
    state = json.loads(state_path.read_text())
    attempt = next(item for item in state["attempts"] if item["reviewer_role"] == "one")
    attempt["status"] = "DETACHED"
    attempt["completed_at"] = None
    state["status"] = "RUNNING"
    state["waiting_reason"] = None
    state["allowed_actions"] = []
    state["operation_intent"] = {
        "kind": "agent_follow",
        "attempt_id": attempt["id"],
        "thread_id": attempt["thread_id"],
        "turn_id": attempt["turn_id"],
    }
    state_path.write_text(json.dumps(state))
    follow_calls = list(codex.follow_calls)

    recovered = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )

    assert recovered["status"] == "WAITING"
    assert recovered["waitingReason"] == "REVIEWER_FAILURE"
    assert recovered["allowedActions"] == ["RETRY_REVIEWERS", "REQUIRE_FRESH_AUDIT"]
    assert codex.follow_calls == follow_calls


def test_reviewer_finalization_intent_reconciles_after_crash(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, _, _, default_reviewers = inputs(tmp_path)
    rubric = default_reviewers[0][1]

    class CrashAfterFinalizationIntentStore(impl_review.ArtifactStore):
        marker: Path

        def write_state(self, state):
            super().write_state(state)
            if (state.get("operation_intent") or {}).get(
                "kind"
            ) == "review_finalize" and not self.marker.exists():
                self.marker.touch()
                raise SimulatedCrash

    CrashAfterFinalizationIntentStore.marker = tmp_path / "review-finalize-crash"
    monkeypatch.setattr(impl_review, "ArtifactStore", CrashAfterFinalizationIntentStore)
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [
            impl_review.AgentResult("unknown", error="one runtime failure"),
            impl_review.AgentResult("failed", error="two runtime failure"),
        ],
    )

    with pytest.raises(SimulatedCrash):
        impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
            config(
                repo,
                tmp_path,
                reviewers=(("one", rubric), ("two", rubric)),
                max_auto_worker_rounds=1,
            )
        )

    state_path = next((tmp_path / "state").glob("*/test-run/state.json"))
    assert json.loads(state_path.read_text())["operation_intent"]["kind"] == (
        "review_finalize"
    )
    follow_calls = list(codex.follow_calls)
    recovered = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )

    assert recovered["status"] == "WAITING"
    assert recovered["waitingReason"] == "REVIEWER_FAILURE"
    assert recovered["allowedActions"] == ["RETRY_REVIEWERS", "REQUIRE_FRESH_AUDIT"]
    assert codex.follow_calls == follow_calls
    assert (
        impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
            "test-run"
        )
        == recovered
    )


def test_reviewer_finalization_recovery_drift_waiting_state_is_loadable(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, _, _, default_reviewers = inputs(tmp_path)
    rubric = default_reviewers[0][1]

    class CrashAfterFinalizationIntentStore(impl_review.ArtifactStore):
        marker = tmp_path / "review-finalize-drift-crash"

        def write_state(self, state):
            super().write_state(state)
            if self.marker.exists():
                return
            if (state.get("operation_intent") or {}).get("kind") == "review_finalize":
                self.marker.touch()
                raise SimulatedCrash

    monkeypatch.setattr(impl_review, "ArtifactStore", CrashAfterFinalizationIntentStore)
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [
            impl_review.AgentResult("unknown", error="one runtime failure"),
            impl_review.AgentResult("failed", error="two runtime failure"),
        ],
    )
    with pytest.raises(SimulatedCrash):
        impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
            config(
                repo,
                tmp_path,
                reviewers=(("one", rubric), ("two", rubric)),
                max_auto_worker_rounds=1,
            )
        )

    state_path = next((tmp_path / "state").glob("*/test-run/state.json"))
    assert json.loads(state_path.read_text())["operation_intent"]["kind"] == (
        "review_finalize"
    )
    commit(repo, "external.txt", "external\n")

    waiting = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )

    assert waiting["status"] == "WAITING"
    assert waiting["waitingReason"] == "CHECKOUT_DRIFT"
    persisted = json.loads(state_path.read_text())
    assert persisted["operation_intent"] is None
    assert persisted["continuation_phase"] == "REVIEW"
    assert persisted["waiting_reason"] == "CHECKOUT_DRIFT"
    assert persisted["allowed_actions"] == []
    inspected = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).inspect(
        "test-run"
    )
    assert inspected == waiting
    resumed = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )
    assert resumed == waiting


def test_run_lock_blocks_resume_but_not_inspect(repo: Path, tmp_path: Path):
    codex = ScriptedCodex(repo, [lambda: None], [])
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    report = workflow.start(config(repo, tmp_path))
    lock_path = Path(report["statePath"]).with_name("run.lock")

    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert workflow.inspect("test-run")["waitingReason"] == "WORKER_NO_CHANGE"
        with pytest.raises(impl_review.UsageError, match="RUN_BUSY"):
            workflow.resume("test-run", "START_NEXT_ROUND")
        fcntl.flock(lock, fcntl.LOCK_UN)


def test_start_initialization_and_advancement_hold_run_lock(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_dir = tmp_path / "state"

    def assert_run_lock_held() -> None:
        locks = list(state_dir.glob("*/*/run.lock"))
        assert len(locks) == 1
        with locks[0].open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            fcntl.flock(lock, fcntl.LOCK_UN)
        raise AssertionError("run lock was not held")

    original_store = impl_review.ArtifactStore

    class LockCheckingStore(original_store):
        def write_state(self, state):
            assert_run_lock_held()
            super().write_state(state)

    class LockCheckingGit:
        def __init__(self):
            self.delegate = impl_review.GitAdapter()

        def preflight(self, cwd):
            assert_run_lock_held()
            return self.delegate.preflight(cwd)

        def branch_exists(self, cwd, branch):
            assert_run_lock_held()
            return self.delegate.branch_exists(cwd, branch)

        def create_branch(self, cwd, branch, base):
            assert_run_lock_held()
            return self.delegate.create_branch(cwd, branch, base)

        def snapshot(self, cwd):
            assert_run_lock_held()
            return self.delegate.snapshot(cwd)

        def __getattr__(self, name):
            return getattr(self.delegate, name)

    class LockCheckingCodex(ScriptedCodex):
        def start(self, **kwargs):
            assert_run_lock_held()
            return super().start(**kwargs)

        def follow(self, *, thread_id, turn_id):
            assert_run_lock_held()
            return super().follow(thread_id=thread_id, turn_id=turn_id)

    monkeypatch.setattr(impl_review, "ArtifactStore", LockCheckingStore)
    codex = LockCheckingCodex(repo, [lambda: None], [])
    report = impl_review.Workflow(
        state_dir=state_dir, git=LockCheckingGit(), codex=codex
    ).start(config(repo, tmp_path, state_dir=state_dir))

    assert report["waitingReason"] == "WORKER_NO_CHANGE"


def test_concurrent_starts_for_one_run_id_cannot_both_advance(
    repo: Path, tmp_path: Path
):
    state_dir = tmp_path / "state"
    events_path = tmp_path / "git-events.log"
    ready_path = tmp_path / "worker-ready"
    release_path = tmp_path / "worker-release"
    first_result_path = tmp_path / "first-result.json"
    second_result_path = tmp_path / "second-result.json"
    first_config = config(repo, tmp_path, state_dir=state_dir)
    second_config = config(repo, tmp_path, state_dir=state_dir)
    context = multiprocessing.get_context("fork")
    first = context.Process(
        target=run_start_process,
        args=(first_config, events_path, first_result_path, ready_path, release_path),
    )
    second: multiprocessing.Process | None = None

    first.start()
    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists() and first.is_alive():
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        assert ready_path.exists(), (
            f"first start did not reach Worker: {first.exitcode}"
        )

        second = context.Process(
            target=run_start_process,
            args=(second_config, events_path, second_result_path),
        )
        second.start()
        second.join(5)
        assert not second.is_alive()
        second_result = json.loads(second_result_path.read_text())
        assert second_result == {
            "ok": False,
            "error_type": "UsageError",
            "error": "RUN_BUSY",
        }
    finally:
        release_path.touch()
        if second is not None:
            second.join(5)
            if second.is_alive():
                second.terminate()
                second.join()
        first.join(5)
        if first.is_alive():
            first.terminate()
            first.join()

    assert first.exitcode == 0
    first_result = json.loads(first_result_path.read_text())
    assert first_result["ok"] is True
    assert first_result["report"]["waitingReason"] == "WORKER_NO_CHANGE"
    assert events_path.read_text().splitlines() == [f"{first.pid}:preflight"]


def test_crash_after_detach_recovers_exact_worker_turn_from_history(
    repo: Path, tmp_path: Path
):
    codex = CrashDuringFollowCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        ["Recovered run passes.\nVERDICT: PASS"],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    with pytest.raises(SimulatedCrash):
        workflow.start(config(repo, tmp_path))

    report = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )

    assert report["status"] == "READY_CERTIFIED"
    assert codex.history_calls == [("thread-1", "turn-1")]
    assert report["attempts"][0]["threadId"] == "thread-1"
    assert report["attempts"][0]["turnId"] == "turn-1"


def test_completed_worker_transition_reconciles_without_following_again(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    CrashAfterTerminalStateWriteStore.marker = tmp_path / "worker-crash-replayed"
    CrashAfterTerminalStateWriteStore.role_to_crash = "worker"
    monkeypatch.setattr(impl_review, "ArtifactStore", CrashAfterTerminalStateWriteStore)
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        ["Recovered worker result passes.\nVERDICT: PASS"],
    )

    with pytest.raises(SimulatedCrash):
        impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
            config(repo, tmp_path)
        )

    state_path = next((tmp_path / "state").glob("*/test-run/state.json"))
    crashed = json.loads(state_path.read_text())
    assert crashed["operation_intent"]["kind"] == "agent_terminal"
    assert crashed["operation_intent"]["role"] == "worker"
    assert crashed["attempts"][-1]["status"] == "completed"
    assert crashed["candidate_head"] == crashed["base_commit"]
    assert crashed["operation_intent"]["checkout"]["head"] == git(
        repo, "rev-parse", "HEAD"
    )
    worker_receipt = (
        crashed["attempts"][-1]["thread_id"],
        crashed["attempts"][-1]["turn_id"],
    )
    assert crashed["operation_intent"]["thread_id"] == worker_receipt[0]
    assert crashed["operation_intent"]["turn_id"] == worker_receipt[1]

    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    report = workflow.resume("test-run")
    assert report["status"] == "READY_CERTIFIED"
    assert codex.follow_calls.count(worker_receipt) == 1
    assert json.loads(state_path.read_text())["operation_intent"] is None

    artifact_paths = [
        item["path"] for item in json.loads(state_path.read_text())["artifacts"]
    ]
    repeated = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )
    assert repeated == report
    assert [
        item["path"] for item in json.loads(state_path.read_text())["artifacts"]
    ] == artifact_paths
    assert codex.follow_calls.count(worker_receipt) == 1


def test_completed_reviewer_transition_reconciles_idempotently(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    CrashAfterTerminalStateWriteStore.marker = tmp_path / "reviewer-crash-replayed"
    CrashAfterTerminalStateWriteStore.role_to_crash = "reviewer"
    monkeypatch.setattr(impl_review, "ArtifactStore", CrashAfterTerminalStateWriteStore)
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        ["Recovered reviewer result passes.\nVERDICT: PASS"],
    )

    with pytest.raises(SimulatedCrash):
        impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
            config(repo, tmp_path)
        )

    state_path = next((tmp_path / "state").glob("*/test-run/state.json"))
    crashed = json.loads(state_path.read_text())
    intent = crashed["operation_intent"]
    assert intent["kind"] == "agent_terminal"
    assert intent["role"] == "reviewer"
    assert crashed["attempts"][-1]["status"] == "completed"
    assert crashed["review_sessions"][-1]["results"] == {}
    reviewer_receipt = (intent["thread_id"], intent["turn_id"])
    assert reviewer_receipt == (
        crashed["attempts"][-1]["thread_id"],
        crashed["attempts"][-1]["turn_id"],
    )

    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    report = workflow.resume("test-run")
    assert report["status"] == "READY_CERTIFIED"
    assert report["reviewSessions"][-1]["results"]["spec"]["status"] == "completed"
    assert codex.follow_calls.count(reviewer_receipt) == 1
    assert json.loads(state_path.read_text())["operation_intent"] is None

    artifact_paths = [
        item["path"] for item in json.loads(state_path.read_text())["artifacts"]
    ]
    repeated = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )
    assert repeated == report
    assert [
        item["path"] for item in json.loads(state_path.read_text())["artifacts"]
    ] == artifact_paths
    assert codex.follow_calls.count(reviewer_receipt) == 1


def test_branch_creation_crash_window_reconciles_persisted_intent(
    repo: Path, tmp_path: Path
):
    class CrashBeforeBranchMutationGit:
        def __init__(self):
            self.delegate = impl_review.GitAdapter()
            self.crashed = False

        def preflight(self, cwd):
            return self.delegate.preflight(cwd)

        def branch_exists(self, cwd, branch):
            return self.delegate.branch_exists(cwd, branch)

        def snapshot(self, cwd):
            if not self.crashed:
                self.crashed = True
                raise SimulatedCrash
            return self.delegate.snapshot(cwd)

        def __getattr__(self, name):
            return getattr(self.delegate, name)

    initial_branch = git(repo, "branch", "--show-current")
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        ["Recovered branch run passes.\nVERDICT: PASS"],
    )
    crashing = impl_review.Workflow(
        state_dir=tmp_path / "state",
        git=CrashBeforeBranchMutationGit(),
        codex=codex,
    )
    with pytest.raises(SimulatedCrash):
        crashing.start(config(repo, tmp_path, branch="review/crash-window"))

    assert git(repo, "branch", "--show-current") == initial_branch
    report = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )
    assert report["status"] == "READY_CERTIFIED"
    assert git(repo, "branch", "--show-current") == "review/crash-window"


def test_branch_creation_recovery_drift_waiting_state_is_loadable(
    repo: Path, tmp_path: Path
):
    class CrashBeforeBranchSnapshotGit:
        def __init__(self):
            self.delegate = impl_review.GitAdapter()
            self.crashed = False

        def preflight(self, cwd):
            return self.delegate.preflight(cwd)

        def snapshot(self, cwd):
            if not self.crashed:
                self.crashed = True
                raise SimulatedCrash
            return self.delegate.snapshot(cwd)

        def __getattr__(self, name):
            return getattr(self.delegate, name)

    codex = ScriptedCodex(repo, [lambda: None], [])
    with pytest.raises(SimulatedCrash):
        impl_review.Workflow(
            state_dir=tmp_path / "state",
            git=CrashBeforeBranchSnapshotGit(),
            codex=codex,
        ).start(config(repo, tmp_path, branch="review/drift-recovery"))

    git(repo, "switch", "-c", "external-drift", "-q")
    waiting = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )

    assert waiting["status"] == "WAITING"
    assert waiting["waitingReason"] == "CHECKOUT_DRIFT"
    state_path = Path(waiting["statePath"])
    persisted = json.loads(state_path.read_text())
    assert persisted["operation_intent"] is None
    assert persisted["continuation_phase"] == "PREPARE_BRANCH"
    assert persisted["waiting_reason"] == "CHECKOUT_DRIFT"
    assert persisted["allowed_actions"] == []

    inspected = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).inspect(
        "test-run"
    )
    assert inspected == waiting
    resumed = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )
    assert resumed == waiting


def test_completed_branch_creation_is_reconciled_at_expected_base(
    repo: Path, tmp_path: Path
):
    class CrashAfterBranchMutationGit:
        def __init__(self):
            self.delegate = impl_review.GitAdapter()
            self.crashed = False

        def preflight(self, cwd):
            return self.delegate.preflight(cwd)

        def create_branch(self, cwd, branch, base):
            self.delegate.create_branch(cwd, branch, base)
            if not self.crashed:
                self.crashed = True
                raise SimulatedCrash

        def __getattr__(self, name):
            return getattr(self.delegate, name)

    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        ["Recovered branch run passes.\nVERDICT: PASS"],
    )
    initial_head = git(repo, "rev-parse", "HEAD")
    crashing = impl_review.Workflow(
        state_dir=tmp_path / "state",
        git=CrashAfterBranchMutationGit(),
        codex=codex,
    )
    with pytest.raises(SimulatedCrash):
        crashing.start(config(repo, tmp_path, branch="review/completed-create"))

    assert git(repo, "branch", "--show-current") == "review/completed-create"
    assert git(repo, "rev-parse", "HEAD") == initial_head

    report = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).resume(
        "test-run"
    )

    assert report["status"] == "READY_CERTIFIED"
    assert git(repo, "branch", "--show-current") == "review/completed-create"


def test_codexctl_adapter_never_follows_a_different_active_turn(tmp_path: Path):
    calls: list[list[str]] = []
    target_history = (
        json.dumps(
            {
                "type": "turn/completed",
                "threadId": "thread-1",
                "turnId": "older-turn",
                "status": "completed",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "item/completed",
                "threadId": "thread-1",
                "turnId": "target-turn",
                "item": {"type": "agentMessage", "text": "Target finished."},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "turn/completed",
                "threadId": "thread-1",
                "turnId": "target-turn",
                "status": "completed",
            }
        )
        + "\n"
    ).encode()

    def run(
        argv: list[str], *, cwd: Path, **_: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        if argv[1] == "status":
            return subprocess.CompletedProcess(
                argv, 0, b'{"activeTurnId":"extra-turn"}', b""
            )
        if argv[1] == "history":
            return subprocess.CompletedProcess(argv, 0, target_history, b"")
        raise AssertionError(f"unexpected command: {argv}")

    adapter = impl_review.CodexctlAdapter("codexctl", tmp_path, subprocess_runner=run)
    result = adapter.follow(thread_id="thread-1", turn_id="target-turn")

    assert result.status == "unexpected_continuation"
    assert result.final_message == "Target finished."
    assert result.observed_turn_ids == ["target-turn", "extra-turn"]
    assert [argv[1] for argv in calls] == ["status", "history"]


def test_codexctl_adapter_follow_does_not_pass_isolation_flags(tmp_path: Path):
    calls: list[list[str]] = []
    completed = (
        b'{"type":"turn/completed","threadId":"thread-1",'
        b'"turnId":"target-turn","status":"completed"}\n'
    )

    def run(
        argv: list[str], *, cwd: Path, **_: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        if argv[1] == "status":
            return subprocess.CompletedProcess(
                argv, 0, b'{"activeTurnId":"target-turn"}', b""
            )
        if argv[1] == "follow":
            return subprocess.CompletedProcess(argv, 0, completed, b"")
        raise AssertionError(f"unexpected command: {argv}")

    adapter = impl_review.CodexctlAdapter("codexctl", tmp_path, subprocess_runner=run)
    result = adapter.follow(thread_id="thread-1", turn_id="target-turn")

    assert result.status == "completed"
    assert calls[-1] == ["codexctl", "follow", "thread-1", "-o", "jsonl"]


def test_codexctl_adapter_nonzero_follow_does_not_certify_stdout(
    tmp_path: Path,
):
    calls: list[list[str]] = []
    completed = (
        b'{"type":"item/completed","threadId":"thread-1",'
        b'"turnId":"target-turn","item":{"type":"agentMessage",'
        b'"text":"Looks completed."}}\n'
        b'{"type":"turn/completed","threadId":"thread-1",'
        b'"turnId":"target-turn","status":"completed"}\n'
    )

    def run(
        argv: list[str], *, cwd: Path, **_: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        if argv[1] == "status":
            return subprocess.CompletedProcess(
                argv, 0, b'{"activeTurnId":"target-turn"}', b""
            )
        if argv[1] == "follow":
            return subprocess.CompletedProcess(argv, 17, completed, b"follow crashed")
        if argv[1] == "history":
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        raise AssertionError(f"unexpected command: {argv}")

    adapter = impl_review.CodexctlAdapter("codexctl", tmp_path, subprocess_runner=run)
    result = adapter.follow(thread_id="thread-1", turn_id="target-turn")

    assert result.status == "unknown"
    assert result.final_message is None
    assert result.raw_jsonl == completed
    assert result.observed_turn_ids == []
    assert result.error == ("follow crashed; target turn history unavailable")
    assert [argv[1] for argv in calls] == ["status", "follow", "history"]


def test_codexctl_adapter_nonzero_follow_recovers_exact_target_from_history(
    tmp_path: Path,
):
    calls: list[list[str]] = []
    follow_stdout = (
        b'{"type":"turn/completed","threadId":"thread-1",'
        b'"turnId":"target-turn","status":"completed"}\n'
    )
    history = (
        b'{"type":"item/completed","threadId":"thread-1",'
        b'"turnId":"other-turn","item":{"type":"agentMessage",'
        b'"text":"Other turn."}}\n'
        b'{"type":"turn/completed","threadId":"thread-1",'
        b'"turnId":"other-turn","status":"completed"}\n'
        b'{"type":"item/completed","threadId":"thread-1",'
        b'"turnId":"target-turn","item":{"type":"agentMessage",'
        b'"text":"Recovered target."}}\n'
        b'{"type":"turn/completed","threadId":"thread-1",'
        b'"turnId":"target-turn","status":"completed"}\n'
    )

    def run(
        argv: list[str], *, cwd: Path, **_: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        if argv[1] == "status":
            return subprocess.CompletedProcess(
                argv, 0, b'{"activeTurnId":"target-turn"}', b""
            )
        if argv[1] == "follow":
            return subprocess.CompletedProcess(argv, 23, follow_stdout, b"late failure")
        if argv[1] == "history":
            return subprocess.CompletedProcess(argv, 0, history, b"")
        raise AssertionError(f"unexpected command: {argv}")

    adapter = impl_review.CodexctlAdapter("codexctl", tmp_path, subprocess_runner=run)
    result = adapter.follow(thread_id="thread-1", turn_id="target-turn")

    assert result.status == "completed"
    assert result.final_message == "Recovered target."
    assert result.raw_jsonl == history
    assert result.observed_turn_ids == ["target-turn"]
    assert result.error is None
    assert [argv[1] for argv in calls] == ["status", "follow", "history"]


def test_unexpected_reviewer_continuation_is_typed_and_full_audit_supersedes_it(
    repo: Path, tmp_path: Path
):
    codex = ScriptedCodex(
        repo,
        [lambda: commit(repo, "work.txt", "work\n")],
        [
            impl_review.AgentResult(
                "unexpected_continuation",
                messages=["Target result."],
                observed_turn_ids=["target-turn", "extra-turn"],
                error="observed another turn",
            ),
            "Fresh audit passes.\nVERDICT: PASS",
        ],
    )
    workflow = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex)
    waiting = workflow.start(config(repo, tmp_path, max_auto_worker_rounds=1))

    assert waiting["waitingReason"] == "UNEXPECTED_CONTINUATION"
    assert waiting["attempts"][-1]["status"] == "unexpected_continuation"
    report = workflow.resume("test-run", "REQUIRE_FRESH_AUDIT")
    assert report["status"] == "READY_CERTIFIED"
    state = json.loads(Path(report["statePath"]).read_text())
    assert [session["status"] for session in state["review_sessions"]] == [
        "SUPERSEDED_BY_FULL_AUDIT",
        "PASSED",
    ]


def test_unexpected_worker_continuation_is_not_a_generic_agent_failure(
    repo: Path, tmp_path: Path
):
    class UnexpectedWorkerCodex(ScriptedCodex):
        def start(self, **kwargs):
            receipt = super().start(**kwargs)
            self._results[receipt.turn_id] = impl_review.AgentResult(
                "unexpected_continuation",
                observed_turn_ids=[receipt.turn_id, "extra-turn"],
                error="observed another turn",
            )
            return receipt

    codex = UnexpectedWorkerCodex(repo, [lambda: None], [])
    report = impl_review.Workflow(state_dir=tmp_path / "state", codex=codex).start(
        config(repo, tmp_path)
    )

    assert report["waitingReason"] == "UNEXPECTED_CONTINUATION"
    assert report["allowedActions"] == []
    assert report["attempts"][-1]["status"] == "unexpected_continuation"


def test_cli_usage_failure_exits_one(tmp_path: Path):
    assert (
        impl_review.main(
            ["inspect", "--state-dir", str(tmp_path / "missing"), "missing-run"]
        )
        == 1
    )

import io
import json
import sys
import threading
import time
from pathlib import Path
from typing import TypeVar

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import impl_review as orchestrator  # noqa: E402
from impl_review import (  # noqa: E402
    AgentRun,
    CheckoutSnapshot,
    CodexctlAdapter,
    GateResult,
    RunConfig,
    UsageError,
    Workflow,
    build_parser,
    extract_final_agent_message,
    extract_verdict,
    parse_codex_jsonl,
    parse_issue_uri,
    render_prompt,
    validate_template,
)

GitT = TypeVar("GitT", bound=orchestrator.GitPort)
GateT = TypeVar("GateT", bound=orchestrator.GatePort)


def _agent_result(thread_id: str, text: str, *, returncode: int = 0) -> AgentRun:
    raw = "\n".join(
        [
            json.dumps({"type": "thread/started", "threadId": thread_id}),
            json.dumps(
                {
                    "type": "item/completed",
                    "threadId": thread_id,
                    "item": {"type": "agentMessage", "text": text},
                }
            ),
        ]
    ).encode()
    result = parse_codex_jsonl(raw)
    result.returncode = returncode
    result.stderr = b"stderr"
    return result


class FakeGit:
    def __init__(self, *, drift: bool = False) -> None:
        self.drift = drift
        self.calls: list[str] = []
        self._calls = 0
        self.clean = CheckoutSnapshot("/checkout", "main", "head", ())
        self.changed = CheckoutSnapshot(
            "/checkout", "main", "head", (" M implementation.py",)
        )

    def snapshot(self, cwd: Path) -> CheckoutSnapshot:
        self.calls.append("snapshot")
        self._calls += 1
        if self.drift:
            return CheckoutSnapshot(str(cwd), "main", "different-head", ())
        if self._calls >= 3:
            return CheckoutSnapshot(str(cwd), "main", "head", self.changed.status)
        return CheckoutSnapshot(str(cwd), "main", "head", ())

    def unstaged_changes(self, cwd: Path) -> orchestrator.UnstagedChanges:
        return orchestrator.UnstagedChanges(
            "diff", ("implementation.py",), f"git -C {cwd} diff"
        )

    def stage_all(self, cwd: Path) -> None:
        del cwd
        self.calls.append("stage_all")


class WorkerMutationGit:
    def __init__(self, mutation: str) -> None:
        self.mutation = mutation
        self.calls: list[str] = []
        self._snapshots = 0

    def snapshot(self, cwd: Path) -> CheckoutSnapshot:
        self._snapshots += 1
        self.calls.append("snapshot")
        if self._snapshots < 4:
            return CheckoutSnapshot(
                str(cwd), "main", "head", (), index_fingerprint="index-before"
            )
        if self.mutation == "commit":
            return CheckoutSnapshot(
                str(cwd),
                "main",
                "committed-head",
                (),
                index_fingerprint="index-after",
            )
        return CheckoutSnapshot(
            str(cwd),
            "main",
            "head",
            ("M  implementation.py",),
            index_fingerprint="index-after",
        )

    def unstaged_changes(self, cwd: Path) -> orchestrator.UnstagedChanges:
        return orchestrator.UnstagedChanges(
            "diff", ("implementation.py",), f"git -C {cwd} diff"
        )

    def stage_all(self, cwd: Path) -> None:
        del cwd
        self.calls.append("stage_all")


class FakeCodex:
    def __init__(
        self,
        *,
        fail_worker_once: bool = False,
        fail_spec_rounds: set[int] | None = None,
        ambiguous_spec_once: bool = False,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_worker_once = fail_worker_once
        self.fail_spec_rounds = fail_spec_rounds or {1}
        self.ambiguous_spec_once = ambiguous_spec_once
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def invoke(
        self,
        *,
        role: str,
        prompt: str,
        cwd: Path,
        thread_id: str | None,
        round: int,
    ) -> AgentRun:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(
                {
                    "role": role,
                    "prompt": prompt,
                    "thread_id": thread_id,
                    "round": round,
                }
            )
        try:
            time.sleep(0.02 if role.startswith("reviewer:") else 0)
            if role == "worker" and self.fail_worker_once:
                self.fail_worker_once = False
                return _agent_result("worker", "", returncode=1)
            if role.startswith("reviewer:"):
                name = role.split(":", 1)[1]
                if name == "spec" and self.ambiguous_spec_once:
                    self.ambiguous_spec_once = False
                    return _agent_result("spec-thread", "finding without verdict")
                verdict = (
                    "FAIL"
                    if round in self.fail_spec_rounds and name == "spec"
                    else "PASS"
                )
                return _agent_result(f"{name}-thread", f"finding\nVERDICT: {verdict}")
            return _agent_result(f"worker-{round}", "implemented")
        finally:
            with self.lock:
                self.active -= 1


class FakeGates:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, command: str, cwd: Path) -> GateResult:
        self.calls.append(command)
        return GateResult(command, 0, "ok", "")


class FlakyGates:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, command: str, cwd: Path) -> GateResult:
        del cwd
        self.calls.append(command)
        return GateResult(command, 1 if len(self.calls) == 1 else 0, "", "failed")


class StatusDriftGit:
    def __init__(self) -> None:
        self.drift = False

    def snapshot(self, cwd: Path) -> CheckoutSnapshot:
        status = (" M externally-changed.py",) if self.drift else ()
        return CheckoutSnapshot(str(cwd), "main", "head", status)

    def unstaged_changes(self, cwd: Path) -> orchestrator.UnstagedChanges:
        return orchestrator.UnstagedChanges(
            "diff", ("externally-changed.py",), f"git -C {cwd} diff"
        )

    def stage_all(self, cwd: Path) -> None:
        del cwd


class ReviewerMutationGit:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self, cwd: Path) -> CheckoutSnapshot:
        self.calls += 1
        status = (" M reviewer-mutation.py",) if self.calls >= 6 else ()
        return CheckoutSnapshot(str(cwd), "main", "head", status)

    def unstaged_changes(self, cwd: Path) -> orchestrator.UnstagedChanges:
        return orchestrator.UnstagedChanges(
            "diff", ("reviewer-mutation.py",), f"git -C {cwd} diff"
        )

    def stage_all(self, cwd: Path) -> None:
        del cwd


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def publish(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return "published"


def _workflow(
    tmp_path: Path,
    *,
    codex: FakeCodex | None = None,
    git: GitT | None = None,
    publisher: FakePublisher | None = None,
    gate_runner: GateT | None = None,
    progress: list[str] | None = None,
) -> tuple[Workflow, FakeCodex, GitT | FakeGit, GateT | FakeGates]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("opaque specification", encoding="utf-8")
    worker = tmp_path / "worker.md"
    worker.write_text(
        "Work on {{spec}} in {{cwd}} for issue {{issue}}.\n{{gates}}",
        encoding="utf-8",
    )
    repair_path = tmp_path / "repair.md"
    repair_path.write_text(
        "Repair round {{round}} using {{review_findings}}.\n{{gates}}",
        encoding="utf-8",
    )
    standards = tmp_path / "standards.md"
    standards.write_text(
        "Review {{spec}} {{unstaged_diff}}\n{{gates}}", encoding="utf-8"
    )
    spec_review = tmp_path / "spec-review.md"
    spec_review.write_text(
        "Review {{spec}} {{unstaged_diff}}\n{{gates}}", encoding="utf-8"
    )
    config = RunConfig(
        cwd=checkout,
        spec_path=spec,
        worker_prompt_path=worker,
        repair_prompt_path=repair_path,
        reviewers=(("standards", standards), ("spec", spec_review)),
        gates=("gate-one", "gate-two"),
        issue="https://github.com/owner/repo/issues/19",
        state_dir=tmp_path / "state",
        run_id="demo",
        publish_review_findings=publisher is not None,
    )
    fake_codex = codex or FakeCodex()
    fake_git = git or FakeGit()
    fake_gates = gate_runner or FakeGates()
    workflow = Workflow.new(
        config,
        git=fake_git,
        codexctl=fake_codex,
        gates=fake_gates,
        publisher=publisher,
        progress=progress.append if progress is not None else None,
    )
    return workflow, fake_codex, fake_git, fake_gates


def test_rendering_rejects_unknowns_and_references_large_content(
    tmp_path: Path,
) -> None:
    with pytest.raises(UsageError):
        render_prompt(
            "{{run_id}}",
            spec="spec",
            spec_reference="spec.md",
            cwd="/checkout",
            round=1,
        )
    spec_reference = tmp_path / "state" / "spec.json"
    findings_reference = tmp_path / "state" / "rounds" / "1" / "findings.txt"
    issue_uri = "https://github.com/owner/repo/issues/19"
    rendered = render_prompt(
        "{{spec}} {{cwd}} {{round}} {{issue}}\n{{gates}}\n{{review_findings}}",
        spec=b"\xff",
        spec_reference=str(spec_reference),
        cwd="/checkout",
        round=2,
        issue=issue_uri,
        gates=("uv run pytest", "ruff check ."),
        review_findings="x" * (32 * 1024 + 1),
        review_findings_reference=str(findings_reference),
        is_repair=True,
    )
    assert str(spec_reference) in rendered
    assert f"/checkout 2 {issue_uri}" in rendered
    assert f"[See durable artifact: {findings_reference}]" in rendered
    assert "uv run pytest\nruff check ." in rendered
    assert (
        render_prompt(
            "issue={{issue}}",
            spec="spec",
            spec_reference="spec.md",
            cwd="/checkout",
            round=1,
        )
        == "issue=none"
    )
    assert (
        render_prompt(
            "{{gates}}",
            spec="spec",
            spec_reference="spec.md",
            cwd="/checkout",
            round=1,
            gates=("gate-one", "gate-two"),
        )
        == "gate-one\ngate-two"
    )
    assert (
        render_prompt(
            "{{unstaged_diff}}",
            spec="spec",
            spec_reference="spec.md",
            cwd="/checkout",
            round=1,
            unstaged_diff="diff --git a/file.py b/file.py",
            is_reviewer=True,
        )
        == "diff --git a/file.py b/file.py"
    )
    large_diff = render_prompt(
        "{{unstaged_diff}}",
        spec="spec",
        spec_reference="spec.md",
        cwd="/checkout",
        round=1,
        unstaged_diff="x" * (32 * 1024 + 1),
        unstaged_files=("file.py", "tests/test_file.py"),
        unstaged_diff_command="git -C /checkout diff --no-ext-diff --no-color",
        is_reviewer=True,
    )
    assert "Files with unstaged changes:" in large_diff
    assert "- file.py" in large_diff
    assert "- tests/test_file.py" in large_diff
    assert "git -C /checkout diff --no-ext-diff --no-color" in large_diff
    with pytest.raises(UsageError):
        render_prompt(
            "{{unstaged_diff}}",
            spec="spec",
            spec_reference="spec.md",
            cwd="/checkout",
            round=1,
        )


def test_renderers_include_state_file_path(tmp_path: Path) -> None:
    workflow, _, _, _ = _workflow(tmp_path)
    result = workflow.execute()
    state_path = str(workflow.store.run_dir / "state.json")

    assert result["statePath"] == state_path

    text_output = io.StringIO()
    orchestrator._render_text(result, out=text_output)
    assert f"State file: {state_path}" in text_output.getvalue()

    json_output = io.StringIO()
    orchestrator._render_json(result, out=json_output)
    assert json.loads(json_output.getvalue())["statePath"] == state_path


def test_issue_uri_parsing_rejects_non_github_issue_values() -> None:
    assert parse_issue_uri("https://github.com/owner/repo/issues/19") == (
        "owner/repo",
        "19",
    )
    for value in (
        "19",
        "https://github.com/owner/repo/pull/19",
        "https://gitlab.com/owner/repo/issues/19",
    ):
        with pytest.raises(UsageError, match="--issue must be a GitHub issue URI"):
            parse_issue_uri(value)


def test_worker_prompt_is_required_and_only_direct_cli_form_is_supported() -> None:
    parser = build_parser()
    common = [
        "--cwd",
        "/checkout",
        "--spec",
        "/spec.json",
        "--repair-prompt",
        "/repair.md",
        "--reviewer",
        "spec=/review.md",
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(common)
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--cwd",
                "/checkout",
                "--spec",
                "/spec.json",
                "--worker-prompt",
                "/worker.md",
                "--reviewer",
                "spec=/review.md",
            ]
        )
    parsed = parser.parse_args([*common, "--worker-prompt", "/worker.md"])
    assert parsed.worker_prompt == Path("/worker.md")
    with pytest.raises(SystemExit):
        parser.parse_args(
            [*common, "--worker-prompt", "/worker.md", "--repo", "owner/repo"]
        )
    for alias in ("run", "start"):
        with pytest.raises(SystemExit):
            parser.parse_args([alias, *common, "--worker-prompt", "/worker.md"])


def test_example_prompt_templates_use_supported_placeholders() -> None:
    prompt_dir = Path(__file__).parent / "prompts"
    base = {"spec", "cwd", "round", "issue", "gates"}
    reviewer_base = base | {"unstaged_diff"}
    worker = (prompt_dir / "worker.md").read_text(encoding="utf-8")
    repair = (prompt_dir / "repair.md").read_text(encoding="utf-8")
    standards = (prompt_dir / "reviewers" / "standards.md").read_text(encoding="utf-8")
    spec = (prompt_dir / "reviewers" / "spec.md").read_text(encoding="utf-8")
    validate_template(worker, allowed=base)
    validate_template(repair, allowed=base | {"review_findings"})
    validate_template(standards, allowed=reviewer_base)
    validate_template(spec, allowed=reviewer_base)
    assert "{{review_findings}}" in repair
    assert "{{gates}}" in worker
    assert "{{gates}}" in repair
    assert "{{unstaged_diff}}" in standards
    assert "{{unstaged_diff}}" in spec


def test_jsonl_extracts_only_final_completed_agent_message() -> None:
    raw = "\n".join(
        [
            '{"type":"item/completed","item":{"type":"agentMessage","text":"old"}}',
            '{"type":"item/completed","item":{"type":"plan","text":"ignore"}}',
            '{"type":"item/completed","item":{"type":"agentMessage","text":"final"}}',
        ]
    )
    assert extract_final_agent_message(raw) == "final"
    assert parse_codex_jsonl(raw).parse_error is None
    assert extract_verdict("notes\nVERDICT: PASS") == "PASS"
    with pytest.raises(Exception, match="VERDICT"):
        extract_verdict("VERDICT: PASS\nmore")


def test_failed_first_review_repairs_with_fresh_worker_and_resumed_reviewers(
    tmp_path: Path,
) -> None:
    workflow, codex, git, gates = _workflow(tmp_path)
    result = workflow.execute()

    assert result["state"] == "READY_FOR_HANDOFF"
    assert result["reviewVerdicts"] == {
        "1": {"standards": "PASS", "spec": "FAIL"},
        "2": {"standards": "PASS", "spec": "PASS"},
    }
    workers = [call for call in codex.calls if call["role"] == "worker"]
    assert [call["round"] for call in workers] == [1, 2]
    assert all("gate-one\ngate-two" in str(call["prompt"]) for call in codex.calls)
    round_two_reviewers = [
        call
        for call in codex.calls
        if call["round"] == 2 and str(call["role"]).startswith("reviewer:")
    ]
    assert {call["thread_id"] for call in round_two_reviewers} == {
        "standards-thread",
        "spec-thread",
    }
    assert codex.max_active >= 2
    assert gates.calls == ["gate-one", "gate-two"]
    state = json.loads((workflow.store.run_dir / "state.json").read_text())
    assert state["state"] == "READY_FOR_HANDOFF"
    assert state["vcs_mutation_by_orchestrator"] is True
    assert state["config"]["issue"] == "https://github.com/owner/repo/issues/19"
    assert "repo" not in state["config"]
    assert (workflow.store.run_dir / "spec.md").exists()
    assert (workflow.store.run_dir / "prompts" / "repair.txt").exists()


def test_publication_is_injected_after_each_completed_review_round(
    tmp_path: Path,
) -> None:
    publisher = FakePublisher()
    workflow, _, _, _ = _workflow(tmp_path, publisher=publisher)
    assert workflow.execute()["state"] == "READY_FOR_HANDOFF"
    assert [call["round"] for call in publisher.calls] == [1, 2]
    assert all(
        call["issue"] == "https://github.com/owner/repo/issues/19"
        for call in publisher.calls
    )


def test_second_failed_review_can_manually_start_another_round(
    tmp_path: Path,
) -> None:
    codex = FakeCodex(fail_spec_rounds={1, 2, 3})
    workflow, codex, git, gates = _workflow(tmp_path, codex=codex)

    waiting = workflow.execute()

    assert waiting["state"] == "WAITING_FOR_USER"
    assert waiting["pending"] == {
        "reason": "review-failed",
        "kind": "review",
        "round": 2,
        "decision": "start-next-round",
        "available_decisions": ["start-next-round", "accept"],
    }
    assert git.calls.count("stage_all") == 1

    third_round = workflow.execute(decision="start-next-round")

    assert third_round["state"] == "WAITING_FOR_USER"
    assert third_round["pending"]["round"] == 3
    assert third_round["pending"]["decision"] == "start-next-round"
    assert git.calls.count("stage_all") == 2

    result = workflow.execute(decision="start-next-round")

    assert result["state"] == "READY_FOR_HANDOFF"
    assert result["reviewVerdicts"] == {
        "1": {"standards": "PASS", "spec": "FAIL"},
        "2": {"standards": "PASS", "spec": "FAIL"},
        "3": {"standards": "PASS", "spec": "FAIL"},
        "4": {"standards": "PASS", "spec": "PASS"},
    }
    assert [call["round"] for call in codex.calls if call["role"] == "worker"] == [
        1,
        2,
        3,
        4,
    ]
    assert gates.calls == ["gate-one", "gate-two"]


def test_reviewer_adapter_uses_read_only_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.dumps(
        {
            "type": "item/completed",
            "item": {"type": "agentMessage", "text": "review\nVERDICT: PASS"},
        }
    ).encode()
    captured: dict[str, object] = {}

    class Process:
        returncode = 0

        def communicate(self) -> tuple[bytes, bytes]:
            return raw, b""

    def fake_popen(argv: list[str], **kwargs: object) -> Process:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_popen)
    result = CodexctlAdapter("codexctl").invoke(
        role="reviewer:spec",
        prompt="review",
        cwd=tmp_path,
        thread_id=None,
        round=1,
    )
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert "--sandbox" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert result.final_text == "review\nVERDICT: PASS"


def test_worker_adapter_streams_text_renderer_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text_chunks = [
        b"Thread: worker-thread\n\n",
        b"Turn: turn-1\n",
        b"\n[agent]\nworker final\n",
        b"\nTurn completed\n",
    ]
    rendered: list[str] = []

    class Pipe:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = iter(chunks)

        def readline(self) -> bytes:
            return next(self.chunks, b"")

        def read(self, size: int = -1) -> bytes:
            del size
            return b"".join(self.chunks)

    class Process:
        returncode = 0

        def __init__(self) -> None:
            self.stdout = Pipe(text_chunks)
            self.stderr = Pipe([b"worker warning\n"])

        def wait(self) -> int:
            return self.returncode

    captured: dict[str, object] = {}

    def fake_popen(argv: list[str], **kwargs: object) -> Process:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_popen)
    result = CodexctlAdapter("codexctl", output=rendered.append).invoke(
        role="worker",
        prompt="implement",
        cwd=tmp_path,
        thread_id=None,
        round=1,
    )

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--output") + 1] == "text"
    assert "".join(rendered) == b"".join(text_chunks).decode()
    assert result.thread_id == "worker-thread"
    assert result.final_text == "worker final"
    assert result.stderr == b"worker warning\n"


def test_reviewer_completion_reports_last_agent_message(tmp_path: Path) -> None:
    progress: list[str] = []
    workflow, _, _, _ = _workflow(tmp_path, progress=progress)

    result = workflow.execute()

    assert result["state"] == "READY_FOR_HANDOFF"
    assert "reviewer standards completed:\nfinding\nVERDICT: PASS" in progress
    assert "reviewer spec completed:\nfinding\nVERDICT: PASS" in progress


def test_reviewer_git_mutation_pauses_before_repair_or_gates(
    tmp_path: Path,
) -> None:
    git = ReviewerMutationGit()
    workflow, codex, _, gates = _workflow(tmp_path, git=git)

    result = workflow.execute()

    assert result["state"] == "WAITING_FOR_USER"
    assert result["pending"]["kind"] == "drift"
    assert result["pending"]["resume_phase"] == "REVIEW_DECISION"
    assert workflow.state["message"].startswith(
        "Git state changed while reviewers were running"
    )
    assert [call["round"] for call in codex.calls if call["role"] == "worker"] == [1]
    assert gates.calls == []


def test_acknowledged_drift_discards_stale_gates_and_reruns_all_gates(
    tmp_path: Path,
) -> None:
    codex = FakeCodex(fail_spec_rounds={1, 2})
    git = StatusDriftGit()
    gates = FlakyGates()
    workflow, _, _, _ = _workflow(
        tmp_path,
        codex=codex,
        git=git,
        gate_runner=gates,
    )

    assert workflow.execute()["state"] == "WAITING_FOR_USER"
    assert workflow.execute(decision="accept")["state"] == "WAITING_FOR_USER"
    assert gates.calls == ["gate-one"]

    git.drift = True
    waiting = workflow.execute(decision="retry")
    assert waiting["pending"]["kind"] == "drift"
    assert workflow.state["gate_results"]

    result = workflow.execute(decision="acknowledge-drift")
    assert result["state"] == "READY_FOR_HANDOFF"
    assert gates.calls == ["gate-one", "gate-one", "gate-two"]
    assert all(record["passed"] for record in result["gateResults"])


def test_ambiguous_worker_result_waits_and_resume_retries_without_stdin(
    tmp_path: Path,
) -> None:
    codex = FakeCodex(fail_worker_once=True)
    workflow, codex, _, _ = _workflow(tmp_path, codex=codex)
    waiting = workflow.execute()
    assert waiting["state"] == "WAITING_FOR_USER"
    assert waiting["pending"]["decision"] == "retry"
    state = json.loads((workflow.store.run_dir / "state.json").read_text())
    resumed = Workflow.from_store(
        workflow.store,
        git=StatusDriftGit(),
        codex_runner=codex,
        gate_runner=FakeGates(),
    ).execute(decision="retry")
    assert resumed["state"] == "READY_FOR_HANDOFF"
    assert state["state"] == "WAITING_FOR_USER"


@pytest.mark.parametrize("mutation", ["commit", "stage"])
def test_worker_git_mutation_pauses_before_review(
    tmp_path: Path, mutation: str
) -> None:
    git = WorkerMutationGit(mutation)
    workflow, codex, _, _ = _workflow(tmp_path, git=git)

    result = workflow.execute()

    assert result["state"] == "WAITING_FOR_USER"
    assert result["pending"] == {
        "reason": "worker-vcs-mutation",
        "kind": "drift",
        "resume_phase": "WORKER",
        "decision": "acknowledge-drift",
    }
    assert [call["role"] for call in codex.calls] == ["worker"]


def test_ambiguous_reviewer_retry_resumes_same_thread_with_verdict_prompt(
    tmp_path: Path,
) -> None:
    codex = FakeCodex(fail_spec_rounds={2}, ambiguous_spec_once=True)
    workflow, codex, _, _ = _workflow(tmp_path, codex=codex)

    waiting = workflow.execute()
    assert waiting["state"] == "WAITING_FOR_USER"
    assert waiting["pending"] == {
        "reason": "agent-failure",
        "kind": "agent",
        "role": "reviewers",
        "round": 1,
        "reviewers": ["spec"],
        "decision": "retry",
    }

    result = workflow.execute(decision="retry")

    assert result["state"] == "READY_FOR_HANDOFF"
    spec_calls = [call for call in codex.calls if call["role"] == "reviewer:spec"]
    assert [call["thread_id"] for call in spec_calls] == [None, "spec-thread"]
    assert spec_calls[1]["prompt"] == "output VERDICT: PASS|FAIL"


def test_resume_refuses_checkout_drift_until_explicit_acknowledgement(
    tmp_path: Path,
) -> None:
    codex = FakeCodex(fail_worker_once=True)
    workflow, codex, _, _ = _workflow(tmp_path, codex=codex)
    assert workflow.execute()["state"] == "WAITING_FOR_USER"
    drifted = FakeGit(drift=True)
    resumed = Workflow.from_store(
        workflow.store,
        git=drifted,
        codex_runner=codex,
        gate_runner=FakeGates(),
    )
    waiting = resumed.execute(decision="retry")
    assert waiting["state"] == "WAITING_FOR_USER"
    assert waiting["pending"]["kind"] == "drift"
    assert resumed.execute(decision="acknowledge-drift")["state"] == "WAITING_FOR_USER"


def test_gate_execution_order_is_authoritative_after_failed_review_is_staged(
    tmp_path: Path,
) -> None:
    codex = FakeCodex(fail_spec_rounds={1, 2})
    workflow, _, git, gates = _workflow(tmp_path, codex=codex)
    # After both repair rounds fail, accept is still available to allow gates.
    waiting = workflow.execute()
    assert waiting["state"] == "WAITING_FOR_USER"
    assert gates.calls == []
    assert "stage_all" in git.calls
    result = workflow.execute(decision="accept")
    assert result["state"] == "READY_FOR_HANDOFF"
    assert gates.calls == ["gate-one", "gate-two"]


def test_production_git_adapter_uses_explicit_git_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> object:
        del kwargs
        commands.append(argv)
        git_command = argv[3:]
        stdout = {
            ("rev-parse", "--show-toplevel"): str(tmp_path),
            ("symbolic-ref", "--short", "-q", "HEAD"): "main",
            ("status", "--porcelain=v1", "--untracked-files=all"): "",
            ("rev-parse", "HEAD"): "head",
            ("diff", "--no-ext-diff", "--no-color"): "diff text",
            ("ls-files", "--stage", "-z"): "100644 blob 0\tfile.py\0",
            (
                "diff",
                "--name-only",
                "--no-ext-diff",
                "--no-color",
            ): "file.py\n",
            ("add", "--all"): "",
        }[tuple(git_command)]
        return orchestrator.subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    adapter = orchestrator.GitAdapter()
    snapshot = adapter.snapshot(tmp_path)
    assert snapshot.clean
    changes = adapter.unstaged_changes(tmp_path)
    assert changes.diff == "diff text"
    assert changes.files == ("file.py",)
    assert changes.command == (f"git -C {tmp_path} diff --no-ext-diff --no-color")
    adapter.stage_all(tmp_path)
    assert all(
        command[3] in {"rev-parse", "symbolic-ref", "status", "diff", "ls-files", "add"}
        for command in commands
    )

"""CodexCtl core behaviors, driven by the scripted fake app-server.

Covers the behaviors the design document pins down: start/resume contracts,
busy detection, follow replay/live frontier and dedup, steer with
expectedTurnId, interrupt waiting, read-only status, history selection,
stable error mapping, and the unattended interaction policy.
"""

import asyncio
from pathlib import Path

import pytest
from conftest import FakeAppServer, FakeRuntimeProvider, collect, make_ctl

import codexctl.core as core
from codexctl.appserver import UNSUPPORTED_INTERACTION_METHOD
from codexctl.endpoint import (
    AppServerEndpoint,
    LifecycleOwnership,
    RuntimePolicy,
    UnixSocketTarget,
)
from codexctl.model import (
    ApprovalPolicy,
    ApprovalsReviewer,
    CodexCtlError,
    DetachedTurnStarted,
    Doctor,
    ErrorCode,
    EventStreamOutcome,
    Follow,
    History,
    HistorySnapshot,
    HistoryTurn,
    Interrupt,
    IsolationOptions,
    ListThreads,
    ReplayActiveTurn,
    ReplayAll,
    ReplayTail,
    Resume,
    SandboxPolicy,
    Start,
    StartConfig,
    Status,
    Steer,
    parse_turn_selector,
)
from codexctl.render import snapshot_document

# ---------------------------------------------------------------------------
# Wire-shape builders
# ---------------------------------------------------------------------------


def thread_doc(status="idle", turns=None, flags=None):
    status_value = {"type": status}
    if flags:
        status_value["activeFlags"] = flags
    return {"id": "t1", "status": status_value, "turns": turns or []}


def turn_doc(turn_id, status="completed", items=None):
    return {"id": turn_id, "status": status, "items": items or []}


def agent_message(item_id, text="hello"):
    return {"type": "agentMessage", "id": item_id, "text": text}


def command_item(item_id, command="ls", exit_code=0):
    return {
        "type": "commandExecution",
        "id": item_id,
        "command": command,
        "exitCode": exit_code,
    }


def emit_completed(server, thread_id, turn_id, status="completed", error=None):
    turn = {"id": turn_id, "status": status}
    if error:
        turn["error"] = {"message": error}
    server.emit("turn/completed", {"threadId": thread_id, "turn": turn})


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


class TestStart:
    def _script(self, server: FakeAppServer):
        server.result("thread/start", {"thread": {"id": "t1"}})
        server.result("turn/start", {"turn": {"id": "u1", "status": "inProgress"}})

    async def test_streams_until_turn_terminal(self):
        server = FakeAppServer()
        self._script(server)
        server.emit(
            "item/completed",
            {"threadId": "t1", "turnId": "u1", "item": agent_message("i1")},
        )
        server.emit(
            "thread/tokenUsage/updated",
            {
                "threadId": "t1",
                "turnId": "u1",
                "tokenUsage": {
                    "total": {
                        "inputTokens": 1000000,
                        "cachedInputTokens": 500000,
                    },
                    "last": {"totalTokens": 83000},
                    "modelContextWindow": 200000,
                },
            },
        )
        server.emit(
            "thread/tokenUsage/updated",
            {
                "threadId": "t1",
                "turnId": "u1",
                "tokenUsage": {
                    "total": {
                        "inputTokens": 1100000,
                        "cachedInputTokens": 600000,
                    },
                    "last": {"totalTokens": 94000},
                    "modelContextWindow": 200000,
                },
            },
        )
        emit_completed(server, "t1", "u1")

        outcome = await make_ctl(server).run(Start(prompt="hello"))
        assert isinstance(outcome, EventStreamOutcome)
        assert outcome.thread_id == "t1" and outcome.turn_id == "u1"
        events, terminal = await collect(outcome)

        assert [e.type for e in events] == [
            "turn/started",  # synthesized start marker
            "item/completed",
            "thread/tokenUsage/updated",
            "thread/tokenUsage/updated",
            "turn/completed",
        ]
        usage_events = [
            event for event in events if event.type == "thread/tokenUsage/updated"
        ]
        assert [event.source for event in usage_events] == ["live", "live"]
        assert [event.extra["usage"] for event in usage_events] == [
            {"usedTokens": 83000, "windowTokens": 200000, "ratio": 0.38},
            {"usedTokens": 94000, "windowTokens": 200000, "ratio": 0.44},
        ]
        assert terminal.status == "completed"
        assert terminal.context is not None
        assert terminal.context.used_tokens == 94000
        assert terminal.context.source == "live"
        assert "t1" in server.unsubscribed
        assert server.closed

    async def test_unavailable_usage_is_omitted_without_failing(self):
        server = FakeAppServer()
        self._script(server)
        server.emit(
            "thread/tokenUsage/updated",
            {
                "threadId": "t1",
                "turnId": "u1",
                "tokenUsage": {
                    "total": {"inputTokens": 80000},
                    "last": {"totalTokens": 83000},
                },
            },
        )
        emit_completed(server, "t1", "u1")

        outcome = await make_ctl(server).run(Start(prompt="hello"))
        events, terminal = await collect(outcome)

        assert [event.type for event in events] == [
            "turn/started",
            "turn/completed",
        ]
        assert terminal.context is None

    async def test_unattended_defaults_are_forwarded(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        server = FakeAppServer()
        self._script(server)
        emit_completed(server, "t1", "u1")
        outcome = await make_ctl(server).run(Start(prompt="hello"))
        await collect(outcome)

        assert server.thread_starts == [StartConfig(cwd=str(Path.cwd()))]
        assert server.turn_starts == [("t1", "hello", None)]

    async def test_config_forwarding(self):
        server = FakeAppServer()
        self._script(server)
        emit_completed(server, "t1", "u1")
        outcome = await make_ctl(server).run(
            Start(
                prompt="hi",
                config=StartConfig(
                    cwd="/work",
                    model="gpt-5",
                    effort="low",
                    sandbox=SandboxPolicy.readOnly,
                ),
            )
        )
        await collect(outcome)
        assert server.thread_starts == [
            StartConfig(
                cwd="/work",
                model="gpt-5",
                effort="low",
                sandbox=SandboxPolicy.readOnly,
            )
        ]
        assert server.turn_starts == [("t1", "hi", "low")]

    async def test_isolation_options_reach_thread_start(self):
        server = FakeAppServer()
        self._script(server)
        emit_completed(server, "t1", "u1")
        outcome = await make_ctl(server).run(
            Start(
                prompt="hi",
                config=StartConfig(
                    isolation=IsolationOptions(no_goals=True, no_agents=True)
                ),
            )
        )
        await collect(outcome)

        assert [config.isolation for config in server.thread_starts] == [
            IsolationOptions(no_goals=True, no_agents=True)
        ]

    async def test_default_cwd_comes_from_runtime_policy(self):
        server = FakeAppServer()
        self._script(server)
        emit_completed(server, "t1", "u1")
        policy = RuntimePolicy(
            default_cwd="/remote/workspace",
            lifecycle=LifecycleOwnership.EXTERNAL,
            supports_rollout_enrichment=False,
        )

        outcome = await make_ctl(
            server, FakeRuntimeProvider(mode="ssh", policy=policy)
        ).run(Start(prompt="hello"))
        await collect(outcome)

        assert server.thread_starts == [StartConfig(cwd="/remote/workspace")]

    async def test_approval_configuration_is_forwarded(self):
        server = FakeAppServer()
        self._script(server)
        emit_completed(server, "t1", "u1")
        outcome = await make_ctl(server).run(
            Start(
                prompt="hi",
                config=StartConfig(
                    cwd="/work",
                    approval_policy=ApprovalPolicy.onRequest,
                    approvals_reviewer=ApprovalsReviewer.autoReview,
                ),
            )
        )
        await collect(outcome)
        assert server.thread_starts == [
            StartConfig(
                cwd="/work",
                approval_policy=ApprovalPolicy.onRequest,
                approvals_reviewer=ApprovalsReviewer.autoReview,
            )
        ]

    async def test_detach_returns_ids_and_disconnects_without_interrupting(self):
        server = FakeAppServer()
        self._script(server)
        outcome = await make_ctl(server).run(Start(prompt="hello", detach=True))
        assert outcome == DetachedTurnStarted(thread_id="t1", turn_id="u1")
        assert server.closed
        assert "turn/interrupt" not in server.methods_requested

    async def test_turn_start_race_maps_to_busy(self):
        server = FakeAppServer()
        server.result("thread/start", {"thread": {"id": "t1"}})
        server.fail("turn/start", -32000, "thread already has an active turn")
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Start(prompt="hello"))
        assert excinfo.value.code == ErrorCode.THREAD_BUSY

    async def test_method_not_found_maps_to_incompatible(self):
        server = FakeAppServer()
        server.result("thread/start", {"thread": {"id": "t1"}})
        server.fail("turn/start", -32601, "method not found")
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Start(prompt="hello"))
        assert excinfo.value.code == ErrorCode.INCOMPATIBLE_CODEX

    async def test_missing_method_precedes_not_steerable_mapping(self):
        server = FakeAppServer()
        server.result("thread/start", {"thread": {"id": "t1"}})
        server.fail(
            "turn/start",
            -32601,
            "active turn cannot be steered",
            {"codexErrorInfo": "ActiveTurnNotSteerable"},
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Start(prompt="hello"))
        assert excinfo.value.code == ErrorCode.INCOMPATIBLE_CODEX

    async def test_notifications_from_other_threads_are_ignored(self):
        server = FakeAppServer()
        self._script(server)
        server.emit(
            "item/completed",
            {"threadId": "other", "turnId": "x", "item": agent_message("i9")},
        )
        emit_completed(server, "t1", "u1")
        outcome = await make_ctl(server).run(Start(prompt="hello"))
        events, _ = await collect(outcome)
        assert [e.type for e in events] == ["turn/started", "turn/completed"]

    async def test_failed_turn_terminal_carries_error(self):
        server = FakeAppServer()
        self._script(server)
        emit_completed(server, "t1", "u1", status="failed", error="model exploded")
        outcome = await make_ctl(server).run(Start(prompt="hello"))
        events, terminal = await collect(outcome)
        assert terminal.status == "failed"
        assert terminal.error == "model exploded"

    async def test_connection_loss_is_protocol_error_without_interrupt(self):
        server = FakeAppServer()
        self._script(server)
        server.end_stream()
        outcome = await make_ctl(server).run(Start(prompt="hello"))
        events = [e async for e in outcome.events]
        assert events[-1].type == "error"
        assert (
            events[-1].extra["error"]["code"]
            == ErrorCode.APP_SERVER_PROTOCOL_ERROR.value
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await outcome.result
        assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        # Losing the stream never sends a turn interrupt.
        assert "turn/interrupt" not in server.methods_requested

    async def test_unsupported_interaction_surfaces_as_error_event(self):
        server = FakeAppServer()
        self._script(server)
        server.emit(
            UNSUPPORTED_INTERACTION_METHOD,
            {
                "method": "item/commandExecution/requestApproval",
                "threadId": "t1",
                "turnId": "u1",
            },
        )
        emit_completed(server, "t1", "u1")
        outcome = await make_ctl(server).run(Start(prompt="hello"))
        events, _ = await collect(outcome)
        errors = [e for e in events if e.type == "error"]
        assert len(errors) == 1
        assert (
            errors[0].extra["error"]["code"] == ErrorCode.UNSUPPORTED_INTERACTION.value
        )


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


class TestResume:
    async def test_busy_thread_status(self):
        server = FakeAppServer()
        server.result(
            "thread/resume",
            {"thread": thread_doc(status="active", flags=["waitingOnUserInput"])},
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Resume(thread_id="t1", prompt="more"))
        assert excinfo.value.code == ErrorCode.THREAD_BUSY
        assert "turn/start" not in server.methods_requested

    async def test_busy_via_in_progress_turn(self):
        server = FakeAppServer()
        server.result(
            "thread/resume",
            {"thread": thread_doc(turns=[turn_doc("u1", status="inProgress")])},
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Resume(thread_id="t1", prompt="more"))
        assert excinfo.value.code == ErrorCode.THREAD_BUSY

    async def test_idle_thread_starts_new_turn(self):
        server = FakeAppServer()
        server.result(
            "thread/resume",
            {"thread": thread_doc(turns=[turn_doc("u0", status="completed")])},
        )
        server.result("turn/start", {"turn": {"id": "u2"}})
        emit_completed(server, "t1", "u2")
        outcome = await make_ctl(server).run(Resume(thread_id="t1", prompt="more"))
        events, terminal = await collect(outcome)
        assert outcome.turn_id == "u2"
        assert terminal.status == "completed"

    async def test_resume_reports_ignored_effective_overrides(self):
        server = FakeAppServer()
        server.result(
            "thread/resume",
            {
                "thread": thread_doc(turns=[turn_doc("u0", status="completed")]),
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "sandbox": {"type": "workspaceWrite"},
            },
        )
        server.result("turn/start", {"turn": {"id": "u2"}})
        emit_completed(server, "t1", "u2")

        outcome = await make_ctl(server).run(
            Resume(
                thread_id="t1",
                prompt="more",
                approval_policy=ApprovalPolicy.onRequest,
                approvals_reviewer=ApprovalsReviewer.autoReview,
                sandbox=SandboxPolicy.readOnly,
            )
        )
        events, terminal = await collect(outcome)

        assert events[0].type == "warning"
        assert events[0].extra["warning"] == {
            "code": "RESUME_OVERRIDE_IGNORED",
            "message": (
                "app-server ignored resume override(s) for the loaded thread: "
                "approvalPolicy, approvalsReviewer, sandbox"
            ),
            "overrides": ["approvalPolicy", "approvalsReviewer", "sandbox"],
        }
        assert events[1].type == "turn/started"
        assert terminal.status == "completed"

    async def test_busy_resume_reports_ignored_config_override(self):
        server = FakeAppServer()
        server.result(
            "thread/resume",
            {"thread": thread_doc(status="active")},
        )

        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(
                Resume(
                    thread_id="t1",
                    prompt="more",
                    isolation=IsolationOptions(no_goals=True),
                )
            )

        assert excinfo.value.code == ErrorCode.THREAD_BUSY
        assert [warning.to_document() for warning in excinfo.value.warnings] == [
            {
                "code": "RESUME_OVERRIDE_IGNORED",
                "message": (
                    "app-server ignored resume override(s) for the loaded thread: "
                    "config"
                ),
                "overrides": ["config"],
            }
        ]

    async def test_isolation_options_reach_thread_resume(self):
        server = FakeAppServer()
        server.result(
            "thread/resume",
            {"thread": thread_doc(turns=[turn_doc("u0", status="completed")])},
        )
        server.result("turn/start", {"turn": {"id": "u2"}})
        emit_completed(server, "t1", "u2")

        outcome = await make_ctl(server).run(
            Resume(
                thread_id="t1",
                prompt="more",
                isolation=IsolationOptions(no_goals=True, no_agents=True),
                approval_policy=ApprovalPolicy.onRequest,
                approvals_reviewer=ApprovalsReviewer.autoReview,
                sandbox=SandboxPolicy.readOnly,
            )
        )
        await collect(outcome)

        assert server.thread_resumes == [
            ("t1", IsolationOptions(no_goals=True, no_agents=True))
        ]
        assert server.thread_resume_configs == [
            (
                "t1",
                ApprovalPolicy.onRequest,
                ApprovalsReviewer.autoReview,
                SandboxPolicy.readOnly,
            )
        ]

    async def test_thread_not_found(self):
        server = FakeAppServer()
        server.fail("thread/resume", -32000, "thread not found: t1")
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Resume(thread_id="t1", prompt="more"))
        assert excinfo.value.code == ErrorCode.THREAD_NOT_FOUND

    async def test_missing_resume_method_maps_to_incompatible(self):
        server = FakeAppServer()
        server.fail("thread/resume", -32601, "method not found: thread/resume")
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Resume(thread_id="t1", prompt="more"))
        assert excinfo.value.code == ErrorCode.INCOMPATIBLE_CODEX

    async def test_recovery_failure_never_creates_replacement_thread(self):
        server = FakeAppServer()
        server.fail("thread/resume", -32000, "rollout file corrupt")
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Resume(thread_id="t1", prompt="more"))
        assert excinfo.value.code == ErrorCode.THREAD_RECOVERY_FAILED
        assert "thread/start" not in server.methods_requested

    async def test_detach(self):
        server = FakeAppServer()
        server.result("thread/resume", {"thread": thread_doc()})
        server.result("turn/start", {"turn": {"id": "u2"}})
        outcome = await make_ctl(server).run(
            Resume(thread_id="t1", prompt="more", detach=True)
        )
        assert outcome == DetachedTurnStarted(thread_id="t1", turn_id="u2")
        assert server.closed


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    async def test_strictly_read_only(self):
        server = FakeAppServer()
        server.result(
            "thread/read",
            {
                "thread": thread_doc(
                    status="active",
                    flags=["waitingOnApproval"],
                    turns=[turn_doc("u1", status="inProgress")],
                )
            },
        )
        snapshot = await make_ctl(server).run(Status(thread_id="t1"))
        assert "thread/resume" not in server.methods_requested
        assert snapshot.status == "active"
        assert snapshot.active_flags == ["waitingOnApproval"]
        assert snapshot.active_turn_id == "u1"

    async def test_context_enrichment_from_rollout(self, isolated_codex_home):
        import json as _json

        directory = isolated_codex_home / "sessions" / "2026" / "08" / "15"
        directory.mkdir(parents=True)
        record = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 500,
                        "cached_input_tokens": 100,
                    },
                    "last_token_usage": {"total_tokens": 600},
                    "model_context_window": 200000,
                },
            },
        }
        (directory / "rollout-2026-08-15T00-00-00-t1.jsonl").write_text(
            _json.dumps(record) + "\n"
        )
        server = FakeAppServer()
        server.result("thread/read", {"thread": thread_doc()})
        snapshot = await make_ctl(server).run(Status(thread_id="t1"))
        assert snapshot.context is not None
        assert snapshot.context.used_tokens == 600
        assert snapshot.context.source == "rollout"

    @pytest.mark.parametrize("mode", ["external", "stdio"])
    async def test_non_managed_runtime_does_not_read_local_rollout(
        self, isolated_codex_home, mode
    ):
        import json as _json

        directory = isolated_codex_home / "sessions" / "2026" / "08" / "15"
        directory.mkdir(parents=True)
        record = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 600},
                    "model_context_window": 200000,
                },
            },
        }
        (directory / "rollout-2026-08-15T00-00-00-t1.jsonl").write_text(
            _json.dumps(record) + "\n"
        )
        server = FakeAppServer()
        server.result("thread/read", {"thread": thread_doc()})

        snapshot = await make_ctl(server, FakeRuntimeProvider(mode=mode)).run(
            Status(thread_id="t1")
        )

        assert snapshot.context is None

    async def test_unknown_thread(self):
        server = FakeAppServer()
        server.fail("thread/read", -32000, "thread does not exist")
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Status(thread_id="t1"))
        assert excinfo.value.code == ErrorCode.THREAD_NOT_FOUND


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def _history_server() -> FakeAppServer:
    server = FakeAppServer()
    turns = [
        turn_doc(
            f"u{n}", status="completed", items=[agent_message(f"i{n}", f"msg {n}")]
        )
        for n in range(4)
    ]
    server.result("thread/read", {"thread": thread_doc(turns=turns)})
    return server


class TestHistory:
    async def test_all_turns_by_default(self):
        snapshot = await make_ctl(_history_server()).run(History(thread_id="t1"))
        assert [t.index for t in snapshot.turns] == [0, 1, 2, 3]
        assert snapshot.turns[0].items == [
            {"id": "i0", "type": "agentMessage", "text": "msg 0"}
        ]

    async def test_single_negative_index(self):
        server = _history_server()
        snapshot = await make_ctl(server).run(
            History(thread_id="t1", selector=parse_turn_selector("-1"))
        )
        assert [(t.index, t.id) for t in snapshot.turns] == [(3, "u3")]

    async def test_slice(self):
        server = _history_server()
        snapshot = await make_ctl(server).run(
            History(thread_id="t1", selector=parse_turn_selector("1:3"))
        )
        assert [(t.index, t.id) for t in snapshot.turns] == [(1, "u1"), (2, "u2")]

    async def test_out_of_range_index_is_usage_error(self):
        server = _history_server()
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(
                History(thread_id="t1", selector=parse_turn_selector("9"))
            )
        assert excinfo.value.code == ErrorCode.USAGE_ERROR

    def test_jsonl_records_no_turn_completed_for_in_progress_turn(self):
        # history -o jsonl emits canonical projected records; a turn still
        # in progress is not completed, so its sequence carries no
        # turn/completed record — matching the follow replay prelude.
        snapshot = HistorySnapshot(
            thread_id="t1",
            turns=[
                HistoryTurn(
                    id="u0",
                    index=0,
                    status="completed",
                    items=[{"id": "i0", "type": "agentMessage", "text": "done"}],
                ),
                HistoryTurn(
                    id="u1",
                    index=1,
                    status="inProgress",
                    items=[{"id": "i1", "type": "agentMessage", "text": "wip"}],
                ),
            ],
        )
        events = core.history_to_events(snapshot)
        assert [(e.type, e.turn_id) for e in events] == [
            ("turn/started", "u0"),
            ("item/completed", "u0"),
            ("turn/completed", "u0"),
            ("turn/started", "u1"),
            ("item/completed", "u1"),
        ]
        assert all(e.turn_id != "u1" for e in events if e.type == "turn/completed")


# ---------------------------------------------------------------------------
# follow
# ---------------------------------------------------------------------------


def _follow_server(turns) -> FakeAppServer:
    server = FakeAppServer()
    server.result("thread/resume", {"thread": thread_doc(turns=turns)})
    return server


class TestFollow:
    async def test_no_active_turn(self):
        server = _follow_server([turn_doc("u0", status="completed")])
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Follow(thread_id="t1"))
        assert excinfo.value.code == ErrorCode.NO_ACTIVE_TURN
        assert server.thread_resumes == [("t1", IsolationOptions())]
        assert "t1" in server.unsubscribed
        assert server.closed

    async def test_replay_active_turn_then_live_with_dedup(self):
        active_items = [command_item("i1")]
        server = _follow_server(
            [
                turn_doc("u0", status="completed", items=[agent_message("i0")]),
                turn_doc("u1", status="inProgress", items=active_items),
            ]
        )
        # Live: the same item completes again (must be deduplicated), then a
        # new message, then the terminal notification.
        server.emit(
            "item/completed",
            {"threadId": "t1", "turnId": "u1", "item": command_item("i1")},
        )
        server.emit(
            "item/completed",
            {"threadId": "t1", "turnId": "u1", "item": agent_message("i2")},
        )
        server.emit(
            "thread/tokenUsage/updated",
            {
                "threadId": "t1",
                "turnId": "u1",
                "tokenUsage": {
                    "total": {"inputTokens": 1000000, "cachedInputTokens": 500000},
                    "last": {"totalTokens": 83000},
                    "modelContextWindow": 200000,
                },
            },
        )
        emit_completed(server, "t1", "u1")

        outcome = await make_ctl(server).run(Follow(thread_id="t1"))
        assert outcome.turn_id == "u1"
        events, terminal = await collect(outcome)

        assert [(e.type, e.source, (e.item or {}).get("id")) for e in events] == [
            ("item/completed", "replay", "i1"),  # replayed active-turn history
            ("turn/started", "live", None),  # synthesized replay/live boundary
            ("item/completed", "live", "i2"),  # i1 live duplicate dropped
            ("thread/tokenUsage/updated", "live", None),
            ("turn/completed", "live", None),
        ]
        assert terminal.status == "completed"
        assert server.thread_resumes == [("t1", IsolationOptions())]

    async def test_in_progress_item_started_is_delivered_by_live_not_lost(self):
        # Replay suppresses item/started of the in-progress turn; that
        # suppression must not occupy `seen`, or the live delivery of the
        # same started event would be silently dropped.
        server = _follow_server(
            [turn_doc("u1", status="inProgress", items=[command_item("i1")])]
        )
        server.emit(
            "item/started",
            {"threadId": "t1", "turnId": "u1", "item": command_item("i1")},
        )
        emit_completed(server, "t1", "u1")

        outcome = await make_ctl(server).run(Follow(thread_id="t1"))
        events, terminal = await collect(outcome)

        assert [(e.type, e.source, (e.item or {}).get("id")) for e in events] == [
            ("item/completed", "replay", "i1"),  # snapshot fact, replayed
            ("turn/started", "live", None),  # synthesized replay/live boundary
            ("item/started", "live", "i1"),  # not lost to replay suppression
            ("turn/completed", "live", None),
        ]
        assert terminal.status == "completed"

    async def test_busy_follow_turn_marker_lands_between_replay_and_live(self):
        # The attached turn started before subscription, so its turn/started
        # is synthesized at the replay/live boundary: after the replay block,
        # before any live event.
        server = _follow_server(
            [turn_doc("u1", status="inProgress", items=[agent_message("i1")])]
        )
        server.emit(
            "item/completed",
            {"threadId": "t1", "turnId": "u1", "item": agent_message("i2")},
        )
        emit_completed(server, "t1", "u1")

        outcome = await make_ctl(server).run(Follow(thread_id="t1"))
        events, terminal = await collect(outcome)

        assert [(e.type, e.source, e.turn_id) for e in events] == [
            ("item/completed", "replay", "u1"),
            ("turn/started", "live", "u1"),  # boundary marker, emitted once
            ("item/completed", "live", "u1"),
            ("turn/completed", "live", "u1"),
        ]
        assert terminal.status == "completed"

    async def test_busy_follow_marker_is_not_duplicated_by_live_redelivery(self):
        # Even if the live stream redelivers the attached turn's
        # turn/started, the synthesized boundary marker occupies its dedup
        # key: the marker is emitted exactly once.
        server = _follow_server([turn_doc("u1", status="inProgress", items=[])])
        server.emit("turn/started", {"threadId": "t1", "turn": {"id": "u1"}})
        emit_completed(server, "t1", "u1")

        outcome = await make_ctl(server).run(Follow(thread_id="t1"))
        events, _ = await collect(outcome)

        markers = [e for e in events if e.type == "turn/started"]
        assert [(e.turn_id, e.source) for e in markers] == [("u1", "live")]

    async def test_replay_all_includes_finished_turns_with_turn_completed(self):
        server = _follow_server(
            [
                turn_doc("u0", status="completed", items=[agent_message("i0")]),
                turn_doc("u1", status="inProgress", items=[]),
            ]
        )
        emit_completed(server, "t1", "u1")
        outcome = await make_ctl(server).run(Follow(thread_id="t1", replay=ReplayAll()))
        events, _ = await collect(outcome)
        assert [(e.type, e.turn_id, e.source, e.turn_index) for e in events] == [
            ("item/started", "u0", "replay", 0),
            ("item/completed", "u0", "replay", 0),
            ("turn/completed", "u0", "replay", 0),
            ("turn/started", "u1", "live", None),
            ("turn/completed", "u1", "live", None),
        ]

    async def test_replay_tail(self):
        server = _follow_server(
            [
                turn_doc("u0", status="completed", items=[]),
                turn_doc("u1", status="completed", items=[]),
                turn_doc("u2", status="inProgress", items=[]),
            ]
        )
        emit_completed(server, "t1", "u2")
        outcome = await make_ctl(server).run(
            Follow(thread_id="t1", replay=ReplayTail(2))
        )
        events, _ = await collect(outcome)
        replayed_turns = {e.turn_id for e in events if e.source == "replay"}
        assert replayed_turns == {"u1"}

    async def test_exits_only_when_active_turn_terminates(self):
        server = _follow_server(
            [
                turn_doc("u0", status="completed", items=[]),
                turn_doc("u1", status="inProgress", items=[]),
            ]
        )
        # A completed notification for an older turn must not end the stream.
        emit_completed(server, "t1", "u0")
        emit_completed(server, "t1", "u1", status="interrupted")
        outcome = await make_ctl(server).run(Follow(thread_id="t1", replay=ReplayAll()))
        events, terminal = await collect(outcome)
        assert terminal.status == "interrupted"
        live_completed = [
            e for e in events if e.type == "turn/completed" and e.source == "live"
        ]
        assert len(live_completed) == 1 and live_completed[0].turn_id == "u1"

    async def test_connection_loss_never_interrupts(self):
        server = _follow_server([turn_doc("u1", status="inProgress", items=[])])
        server.end_stream()
        outcome = await make_ctl(server).run(Follow(thread_id="t1"))
        events = [e async for e in outcome.events]
        assert events[-1].type == "error"
        with pytest.raises(CodexCtlError):
            await outcome.result
        assert "turn/interrupt" not in server.methods_requested


class TestFollowPersist:
    async def test_idle_attach_waits_silently_then_streams_next_turn(self):
        server = _follow_server(
            [turn_doc("u0", status="completed", items=[agent_message("i0")])]
        )
        server.emit("turn/started", {"threadId": "t1", "turn": {"id": "u1"}})
        server.emit(
            "item/completed",
            {"threadId": "t1", "turnId": "u1", "item": agent_message("i1")},
        )
        emit_completed(server, "t1", "u1")
        server.end_stream()

        outcome = await make_ctl(server).run(Follow(thread_id="t1", persist=True))
        assert outcome.turn_id is None
        events = [e async for e in outcome.events]

        # No NO_ACTIVE_TURN, no synthetic idle events: the default replay
        # anchors on the last completed turn, then live events follow.
        assert [(e.type, e.source, e.turn_id) for e in events] == [
            ("item/started", "replay", "u0"),
            ("item/completed", "replay", "u0"),
            ("turn/completed", "replay", "u0"),
            ("turn/started", "live", "u1"),
            ("item/completed", "live", "u1"),
            ("turn/completed", "live", "u1"),
            ("error", "live", None),
        ]
        assert "t1" in server.unsubscribed
        assert server.closed

    async def test_busy_attach_streams_active_turn_then_keeps_streaming(self):
        server = _follow_server([turn_doc("u1", status="inProgress", items=[])])
        emit_completed(server, "t1", "u1")
        server.emit("turn/started", {"threadId": "t1", "turn": {"id": "u2"}})
        server.emit(
            "item/completed",
            {"threadId": "t1", "turnId": "u2", "item": agent_message("i2")},
        )
        emit_completed(server, "t1", "u2")
        server.end_stream()

        outcome = await make_ctl(server).run(Follow(thread_id="t1", persist=True))
        assert outcome.turn_id == "u1"
        events = [e async for e in outcome.events]

        turn_events = [
            (e.type, e.turn_id)
            for e in events
            if e.type in ("turn/started", "turn/completed")
        ]
        assert turn_events == [
            ("turn/started", "u1"),  # synthesized replay/live boundary
            ("turn/completed", "u1"),
            ("turn/started", "u2"),
            ("turn/completed", "u2"),
        ]

    async def test_busy_attach_turn_marker_lands_between_replay_and_live(self):
        # Persist busy attach: the synthesized boundary marker lands after
        # the replay block and before any live event, and the session keeps
        # streaming past the attached turn's end until connection loss.
        server = _follow_server(
            [turn_doc("u1", status="inProgress", items=[agent_message("i1")])]
        )
        server.emit(
            "item/completed",
            {"threadId": "t1", "turnId": "u1", "item": agent_message("i2")},
        )
        emit_completed(server, "t1", "u1")
        server.end_stream()

        outcome = await make_ctl(server).run(Follow(thread_id="t1", persist=True))
        events = [e async for e in outcome.events]

        assert [(e.type, e.source, e.turn_id) for e in events] == [
            ("item/completed", "replay", "u1"),
            ("turn/started", "live", "u1"),  # boundary marker, emitted once
            ("item/completed", "live", "u1"),
            ("turn/completed", "live", "u1"),
            ("error", "live", "u1"),  # connection loss ends the session
        ]

    async def test_failed_turn_does_not_end_the_session(self):
        server = _follow_server([turn_doc("u1", status="inProgress", items=[])])
        emit_completed(server, "t1", "u1", status="failed", error="model exploded")
        server.emit("turn/started", {"threadId": "t1", "turn": {"id": "u2"}})
        emit_completed(server, "t1", "u2", status="interrupted")
        server.end_stream()

        outcome = await make_ctl(server).run(Follow(thread_id="t1", persist=True))
        events = [e async for e in outcome.events]

        completed = [e for e in events if e.type == "turn/completed"]
        assert [(e.turn_id, e.extra.get("status")) for e in completed] == [
            ("u1", "failed"),
            ("u2", "interrupted"),
        ]
        # Only connection loss ends the session: the stream's final event
        # is the deterministic protocol error, not a turn terminal.
        assert events[-1].type == "error"
        assert (
            events[-1].extra["error"]["code"]
            == ErrorCode.APP_SERVER_PROTOCOL_ERROR.value
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await outcome.result
        assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR

    async def test_idle_connection_loss_never_interrupts(self):
        server = _follow_server([])
        server.end_stream()

        outcome = await make_ctl(server).run(Follow(thread_id="t1", persist=True))
        events = [e async for e in outcome.events]

        assert events[-1].type == "error"
        assert (
            events[-1].extra["error"]["code"]
            == ErrorCode.APP_SERVER_PROTOCOL_ERROR.value
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await outcome.result
        assert excinfo.value.code == ErrorCode.APP_SERVER_PROTOCOL_ERROR
        assert "turn/interrupt" not in server.methods_requested
        assert "t1" in server.unsubscribed
        assert server.closed

    async def test_cancellation_resolves_result_to_last_terminal(self):
        server = _follow_server([])
        server.emit("turn/started", {"threadId": "t1", "turn": {"id": "u1"}})
        emit_completed(server, "t1", "u1", status="interrupted")

        outcome = await make_ctl(server).run(Follow(thread_id="t1", persist=True))
        collected: list = []
        saw_completed = asyncio.Event()

        async def drain() -> None:
            async for event in outcome.events:
                collected.append(event)
                if event.type == "turn/completed":
                    saw_completed.set()

        task = asyncio.create_task(drain())
        await saw_completed.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        terminal = await outcome.result
        assert terminal is not None
        assert terminal.turn_id == "u1"
        assert terminal.status == "interrupted"
        # Cancellation ends the session cleanly: no connection-loss error
        # event, and never a turn interrupt.
        assert all(e.type != "error" for e in collected)
        assert "turn/interrupt" not in server.methods_requested

    async def test_cancellation_without_any_completed_turn_resolves_none(self):
        server = _follow_server([])
        server.emit("turn/started", {"threadId": "t1", "turn": {"id": "u1"}})

        outcome = await make_ctl(server).run(Follow(thread_id="t1", persist=True))
        saw_started = asyncio.Event()

        async def drain() -> None:
            async for event in outcome.events:
                if event.type == "turn/started":
                    saw_started.set()

        task = asyncio.create_task(drain())
        await saw_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await outcome.result is None

    @pytest.mark.parametrize(
        ("selector", "expected_replayed"),
        [
            (ReplayActiveTurn(), {"u2"}),
            (ReplayTail(2), {"u1", "u2"}),
            (ReplayAll(), {"u0", "u1", "u2"}),
        ],
    )
    async def test_replay_anchors_on_end_of_history_when_idle(
        self, selector, expected_replayed
    ):
        server = _follow_server(
            [
                turn_doc("u0", status="completed", items=[]),
                turn_doc("u1", status="failed", items=[]),
                turn_doc("u2", status="completed", items=[]),
            ]
        )
        server.end_stream()

        outcome = await make_ctl(server).run(
            Follow(thread_id="t1", replay=selector, persist=True)
        )
        events = [e async for e in outcome.events]

        replayed = {e.turn_id for e in events if e.source == "replay"}
        assert replayed == expected_replayed


# ---------------------------------------------------------------------------
# steer
# ---------------------------------------------------------------------------


class TestSteer:
    async def test_sends_expected_turn_id(self):
        server = FakeAppServer()
        server.result(
            "thread/read",
            {"thread": thread_doc(turns=[turn_doc("u1", status="inProgress")])},
        )
        server.result("turn/steer", {"turnId": "u1"})
        result = await make_ctl(server).run(Steer(thread_id="t1", input="also do X"))
        assert result.turn_id == "u1"
        assert server.params_of("turn/steer") == {
            "threadId": "t1",
            "input": [{"type": "text", "text": "also do X"}],
            "expectedTurnId": "u1",
        }

    async def test_no_active_turn(self):
        server = FakeAppServer()
        server.result(
            "thread/read",
            {"thread": thread_doc(turns=[turn_doc("u0", status="completed")])},
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Steer(thread_id="t1", input="x"))
        assert excinfo.value.code == ErrorCode.NO_ACTIVE_TURN
        assert "turn/steer" not in server.methods_requested

    async def test_not_steerable(self):
        server = FakeAppServer()
        server.result(
            "thread/read",
            {"thread": thread_doc(turns=[turn_doc("u1", status="inProgress")])},
        )
        server.fail(
            "turn/steer",
            -32000,
            "active turn cannot be steered",
            {"codexErrorInfo": "ActiveTurnNotSteerable"},
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Steer(thread_id="t1", input="x"))
        assert excinfo.value.code == ErrorCode.TURN_NOT_STEERABLE

    async def test_expected_turn_mismatch_maps_to_no_active_turn(self):
        server = FakeAppServer()
        server.result(
            "thread/read",
            {"thread": thread_doc(turns=[turn_doc("u1", status="inProgress")])},
        )
        server.fail("turn/steer", -32000, "expectedTurnId does not match")
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Steer(thread_id="t1", input="x"))
        assert excinfo.value.code == ErrorCode.NO_ACTIVE_TURN

    async def test_missing_steer_method_maps_to_incompatible(self):
        server = FakeAppServer()
        server.result(
            "thread/read",
            {"thread": thread_doc(turns=[turn_doc("u1", status="inProgress")])},
        )
        server.fail("turn/steer", -32601, "method not found: turn/steer")
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Steer(thread_id="t1", input="x"))
        assert excinfo.value.code == ErrorCode.INCOMPATIBLE_CODEX

    async def test_missing_steer_method_precedes_not_steerable_mapping(self):
        server = FakeAppServer()
        server.result(
            "thread/read",
            {"thread": thread_doc(turns=[turn_doc("u1", status="inProgress")])},
        )
        server.fail(
            "turn/steer",
            -32601,
            "active turn cannot be steered",
            {"codexErrorInfo": "ActiveTurnNotSteerable"},
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Steer(thread_id="t1", input="x"))
        assert excinfo.value.code == ErrorCode.INCOMPATIBLE_CODEX


# ---------------------------------------------------------------------------
# interrupt
# ---------------------------------------------------------------------------


class TestInterrupt:
    async def test_waits_until_terminal(self, monkeypatch):
        monkeypatch.setattr(core, "INTERRUPT_POLL_INTERVAL", 0)
        server = FakeAppServer()
        server.sequence(
            "thread/read",
            [
                {"thread": thread_doc(turns=[turn_doc("u1", status="inProgress")])},
                {"thread": thread_doc(turns=[turn_doc("u1", status="inProgress")])},
                {"thread": thread_doc(turns=[turn_doc("u1", status="interrupted")])},
            ],
        )
        server.result("turn/interrupt", {})
        result = await make_ctl(server).run(Interrupt(thread_id="t1"))
        assert result.turn_id == "u1" and result.status == "interrupted"
        assert server.params_of("turn/interrupt") == {
            "threadId": "t1",
            "turnId": "u1",
        }

    async def test_no_active_turn(self):
        server = FakeAppServer()
        server.result(
            "thread/read",
            {"thread": thread_doc(turns=[turn_doc("u0", status="completed")])},
        )
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Interrupt(thread_id="t1"))
        assert excinfo.value.code == ErrorCode.NO_ACTIVE_TURN
        assert "turn/interrupt" not in server.methods_requested

    async def test_rejected_interrupt_never_retries_other_turn(self):
        server = FakeAppServer()
        server.result(
            "thread/read",
            {"thread": thread_doc(turns=[turn_doc("u1", status="inProgress")])},
        )
        server.fail("turn/interrupt", -32000, "no active turn")
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Interrupt(thread_id="t1"))
        assert excinfo.value.code == ErrorCode.NO_ACTIVE_TURN
        assert server.methods_requested.count("turn/interrupt") == 1

    async def test_missing_interrupt_method_is_no_active_turn(self):
        server = FakeAppServer()
        server.result(
            "thread/read",
            {"thread": thread_doc(turns=[turn_doc("u1", status="inProgress")])},
        )
        server.fail("turn/interrupt", -32601, "method not found: turn/interrupt")
        with pytest.raises(CodexCtlError) as excinfo:
            await make_ctl(server).run(Interrupt(thread_id="t1"))
        assert excinfo.value.code == ErrorCode.NO_ACTIVE_TURN
        assert server.methods_requested.count("turn/interrupt") == 1


# ---------------------------------------------------------------------------
# list / doctor
# ---------------------------------------------------------------------------


class TestList:
    async def test_pagination(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        server = FakeAppServer()
        pages = iter(
            [
                {
                    "data": [
                        {"id": "t1", "status": {"type": "idle"}, "preview": "first"},
                    ],
                    "nextCursor": "c1",
                },
                {
                    "data": [
                        {"id": "t2", "status": {"type": "active", "activeFlags": []}},
                    ],
                },
            ]
        )
        server.on("thread/list", lambda params: next(pages))
        snapshot = await make_ctl(server).run(ListThreads())
        assert [t.thread_id for t in snapshot.threads] == ["t1", "t2"]
        assert snapshot.threads[0].preview == "first"
        assert snapshot.threads[1].status == "active"
        assert server.requests[0][1] == {"limit": 100, "cwd": str(tmp_path)}
        # second request carried the cursor
        assert server.requests[1][1] == {
            "limit": 100,
            "cursor": "c1",
            "cwd": str(tmp_path),
        }

    async def test_all_lists_across_workspaces(self):
        server = FakeAppServer()
        server.result("thread/list", {"data": []})

        await make_ctl(server).run(ListThreads(all_threads=True))

        assert server.params_of("thread/list") == {"limit": 100}

    async def test_uses_runtime_policy_cwd_and_explicit_command_cwd(self):
        policy = RuntimePolicy(
            default_cwd="/remote/workspace",
            lifecycle=LifecycleOwnership.EXTERNAL,
            supports_rollout_enrichment=False,
        )
        server = FakeAppServer()
        server.result("thread/list", {"data": []})

        await make_ctl(server, FakeRuntimeProvider(mode="ssh", policy=policy)).run(
            ListThreads()
        )
        assert server.params_of("thread/list") == {
            "limit": 100,
            "cwd": "/remote/workspace",
        }

        server = FakeAppServer()
        server.result("thread/list", {"data": []})
        await make_ctl(server, FakeRuntimeProvider(mode="ssh", policy=policy)).run(
            ListThreads(cwd="/explicit/workspace")
        )
        assert server.params_of("thread/list") == {
            "limit": 100,
            "cwd": "/explicit/workspace",
        }


class TestDoctor:
    async def test_compatible_runtime_reports_lifecycle_operations(self):
        server = FakeAppServer()
        snapshot = await make_ctl(server).run(Doctor())

        assert snapshot.compatible is True
        lifecycle_check = next(
            c for c in snapshot.checks if c.name == "required lifecycle operations"
        )
        assert lifecycle_check.ok is True
        assert lifecycle_check.detail == "all available"
        context_check = next(
            c for c in snapshot.checks if c.name == "context usage enrichment"
        )
        assert context_check.ok is False

    async def test_missing_lifecycle_operation_reports_incompatible(self):
        server = FakeAppServer()
        server.missing_lifecycle_operations.add("steer turn")
        snapshot = await make_ctl(server).run(Doctor())

        assert snapshot.compatible is False
        lifecycle_check = next(
            c for c in snapshot.checks if c.name == "required lifecycle operations"
        )
        assert lifecycle_check.ok is False
        assert lifecycle_check.detail == "unavailable: steer turn"

    async def test_unreachable_endpoint_reports_incompatible_false(self):
        endpoint = FakeRuntimeProvider(
            resolve_error=CodexCtlError(ErrorCode.APP_SERVER_UNAVAILABLE, "no daemon")
        )
        server = FakeAppServer()
        snapshot = await make_ctl(server, endpoint).run(Doctor())
        assert snapshot.compatible is False
        assert snapshot.checks[0].ok is False

    async def test_managed_mode_reports_cli_version_from_endpoint_port(self):
        endpoint = FakeRuntimeProvider(cli_version="codex-cli 0.101.0", mode="managed")
        server = FakeAppServer()
        snapshot = await make_ctl(server, endpoint).run(Doctor())
        assert snapshot.codex_cli_version == "codex-cli 0.101.0"
        cli_check = next(c for c in snapshot.checks if c.name == "codex cli version")
        assert cli_check.ok is True
        assert cli_check.detail == "codex-cli 0.101.0"

    async def test_external_mode_skips_cli_version_check(self):
        endpoint = FakeRuntimeProvider(mode="external")
        server = FakeAppServer()
        snapshot = await make_ctl(server, endpoint).run(Doctor())
        assert snapshot.endpoint_mode == "external"
        assert snapshot.lifecycle_ownership == "external"
        assert snapshot.codex_cli_version is None
        assert all(c.name != "codex cli version" for c in snapshot.checks)
        assert all(c.name != "context usage enrichment" for c in snapshot.checks)

    async def test_stdio_mode_exposes_only_mode_in_doctor_document(self):
        endpoint = FakeRuntimeProvider(mode="stdio")
        snapshot = await make_ctl(FakeAppServer(), endpoint).run(Doctor())

        document = snapshot_document(snapshot)
        assert document["endpointMode"] == "stdio"
        assert "executable" not in document
        assert "arguments" not in document
        assert all(c.name != "context usage enrichment" for c in snapshot.checks)

    async def test_lifecycle_policy_is_independent_of_endpoint_mode(self):
        policy = RuntimePolicy(
            default_cwd=None,
            lifecycle=LifecycleOwnership.MANAGED,
            supports_rollout_enrichment=False,
        )
        endpoint = FakeRuntimeProvider(
            mode="ssh", cli_version="codex-cli 0.101.0", policy=policy
        )

        snapshot = await make_ctl(FakeAppServer(), endpoint).run(Doctor())

        assert snapshot.endpoint_mode == "ssh"
        assert snapshot.codex_cli_version == "codex-cli 0.101.0"
        assert any(c.name == "codex cli version" for c in snapshot.checks)
        assert all(c.name != "context usage enrichment" for c in snapshot.checks)

    @pytest.mark.parametrize(
        ("mode", "supports_remote_socket_metadata", "expected_socket"),
        [
            ("not-ssh", True, "/remote/codex.sock"),
            ("ssh", False, None),
        ],
    )
    async def test_remote_socket_metadata_uses_runtime_policy(
        self, mode, supports_remote_socket_metadata, expected_socket
    ):
        policy = RuntimePolicy(
            default_cwd=None,
            lifecycle=LifecycleOwnership.EXTERNAL,
            supports_rollout_enrichment=False,
            supports_remote_socket_metadata=supports_remote_socket_metadata,
        )
        endpoint = FakeRuntimeProvider(
            mode=mode,
            policy=policy,
            endpoint=AppServerEndpoint(
                display="runtime",
                target=UnixSocketTarget(Path("/remote/codex.sock")),
                socket_path=Path("/remote/codex.sock"),
            ),
        )

        snapshot = await make_ctl(FakeAppServer(), endpoint).run(Doctor())

        assert snapshot.remote_socket == expected_socket

"""CodexCtl core behaviors, driven by the scripted fake app-server.

Covers the behaviors the design document pins down: start/resume contracts,
busy detection, follow replay/live frontier and dedup, steer with
expectedTurnId, interrupt waiting, read-only status, history selection,
stable error mapping, and the unattended interaction policy.
"""

from __future__ import annotations

import pytest

import codexctl.core as core
from codexctl.appserver import UNSUPPORTED_INTERACTION_METHOD
from codexctl.model import (
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
    ListThreads,
    ReplayAll,
    ReplayTail,
    Resume,
    Start,
    StartConfig,
    Status,
    Steer,
    parse_turn_selector,
)

from conftest import FakeAppServer, FakeEndpoint, collect, make_ctl


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
                    "total": {"inputTokens": 80000, "cachedInputTokens": 3000},
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
            "turn/completed",
        ]
        # tokenUsage is captured for the footer, never emitted as an event
        assert terminal.status == "completed"
        assert terminal.context is not None
        assert terminal.context.used_tokens == 83000
        assert terminal.context.source == "live"
        assert "t1" in server.unsubscribed
        assert server.closed

    async def test_unattended_defaults_on_the_wire(self):
        server = FakeAppServer()
        self._script(server)
        emit_completed(server, "t1", "u1")
        outcome = await make_ctl(server).run(Start(prompt="hello"))
        await collect(outcome)

        assert server.params_of("thread/start") == {
            "approvalPolicy": "never",
            "sandbox": "workspaceWrite",
        }
        assert server.params_of("turn/start") == {
            "threadId": "t1",
            "input": [{"type": "text", "text": "hello"}],
        }

    async def test_config_forwarding(self):
        server = FakeAppServer()
        self._script(server)
        emit_completed(server, "t1", "u1")
        outcome = await make_ctl(server).run(
            Start(
                prompt="hi",
                config=StartConfig(
                    cwd="/work", model="gpt-5", effort="low", sandbox="readOnly"
                ),
            )
        )
        await collect(outcome)
        assert server.params_of("thread/start") == {
            "approvalPolicy": "never",
            "sandbox": "readOnly",
            "cwd": "/work",
            "model": "gpt-5",
        }
        assert server.params_of("turn/start")["effort"] == "low"

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
            errors[0].extra["error"]["code"]
            == ErrorCode.UNSUPPORTED_INTERACTION.value
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
                    "total_token_usage": {"input_tokens": 500, "cached_input_tokens": 100},
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
        turn_doc(f"u{n}", status="completed", items=[agent_message(f"i{n}", f"msg {n}")])
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
            "item/completed", {"threadId": "t1", "turnId": "u1", "item": command_item("i1")}
        )
        server.emit(
            "item/completed",
            {"threadId": "t1", "turnId": "u1", "item": agent_message("i2")},
        )
        emit_completed(server, "t1", "u1")

        outcome = await make_ctl(server).run(Follow(thread_id="t1"))
        assert outcome.turn_id == "u1"
        events, terminal = await collect(outcome)

        assert [
            (e.type, e.source, (e.item or {}).get("id")) for e in events
        ] == [
            ("item/completed", "replay", "i1"),  # replayed active-turn history
            ("item/completed", "live", "i2"),  # i1 live duplicate dropped
            ("turn/completed", "live", None),
        ]
        assert terminal.status == "completed"

    async def test_in_progress_item_started_is_delivered_by_live_not_lost(self):
        # Replay suppresses item/started of the in-progress turn; that
        # suppression must not occupy `seen`, or the live delivery of the
        # same started event would be silently dropped.
        server = _follow_server(
            [turn_doc("u1", status="inProgress", items=[command_item("i1")])]
        )
        server.emit(
            "item/started", {"threadId": "t1", "turnId": "u1", "item": command_item("i1")}
        )
        emit_completed(server, "t1", "u1")

        outcome = await make_ctl(server).run(Follow(thread_id="t1"))
        events, terminal = await collect(outcome)

        assert [
            (e.type, e.source, (e.item or {}).get("id")) for e in events
        ] == [
            ("item/completed", "replay", "i1"),  # snapshot fact, replayed
            ("item/started", "live", "i1"),  # not lost to replay suppression
            ("turn/completed", "live", None),
        ]
        assert terminal.status == "completed"

    async def test_replay_all_includes_finished_turns_with_turn_completed(self):
        server = _follow_server(
            [
                turn_doc("u0", status="completed", items=[agent_message("i0")]),
                turn_doc("u1", status="inProgress", items=[]),
            ]
        )
        emit_completed(server, "t1", "u1")
        outcome = await make_ctl(server).run(
            Follow(thread_id="t1", replay=ReplayAll())
        )
        events, _ = await collect(outcome)
        assert [
            (e.type, e.turn_id, e.source, e.turn_index) for e in events
        ] == [
            ("item/started", "u0", "replay", 0),
            ("item/completed", "u0", "replay", 0),
            ("turn/completed", "u0", "replay", 0),
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
        outcome = await make_ctl(server).run(
            Follow(thread_id="t1", replay=ReplayAll())
        )
        events, terminal = await collect(outcome)
        assert terminal.status == "interrupted"
        live_completed = [
            e for e in events if e.type == "turn/completed" and e.source == "live"
        ]
        assert len(live_completed) == 1 and live_completed[0].turn_id == "u1"

    async def test_connection_loss_never_interrupts(self):
        server = _follow_server(
            [turn_doc("u1", status="inProgress", items=[])]
        )
        server.end_stream()
        outcome = await make_ctl(server).run(Follow(thread_id="t1"))
        events = [e async for e in outcome.events]
        assert events[-1].type == "error"
        with pytest.raises(CodexCtlError):
            await outcome.result
        assert "turn/interrupt" not in server.methods_requested


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
    async def test_pagination(self):
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
        # second request carried the cursor
        assert server.requests[1][1] == {"limit": 100, "cursor": "c1"}


class TestDoctor:
    async def test_unreachable_endpoint_reports_incompatible_false(self):
        endpoint = FakeEndpoint(
            resolve_error=CodexCtlError(
                ErrorCode.APP_SERVER_UNAVAILABLE, "no daemon"
            )
        )
        server = FakeAppServer()
        snapshot = await make_ctl(server, endpoint).run(Doctor())
        assert snapshot.compatible is False
        assert snapshot.checks[0].ok is False

    async def test_managed_mode_reports_cli_version_from_endpoint_port(self):
        endpoint = FakeEndpoint(cli_version="codex-cli 0.101.0", mode="managed")
        server = FakeAppServer()
        snapshot = await make_ctl(server, endpoint).run(Doctor())
        assert snapshot.codex_cli_version == "codex-cli 0.101.0"
        cli_check = next(c for c in snapshot.checks if c.name == "codex cli version")
        assert cli_check.ok is True
        assert cli_check.detail == "codex-cli 0.101.0"

    async def test_external_mode_skips_cli_version_check(self):
        endpoint = FakeEndpoint(mode="external")
        server = FakeAppServer()
        snapshot = await make_ctl(server, endpoint).run(Doctor())
        assert snapshot.endpoint_mode == "external"
        assert snapshot.codex_cli_version is None
        assert all(c.name != "codex cli version" for c in snapshot.checks)

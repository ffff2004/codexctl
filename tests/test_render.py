"""Text rendering of projected items."""

from io import StringIO

from codexctl.model import (
    ContextUsage,
    HistorySnapshot,
    HistoryTurn,
    ProjectedEvent,
    TurnTerminal,
)
from codexctl.render import TextRenderer, format_context_line

ITEMS = [
    {"type": "agentMessage", "text": "  hello  "},
    {"type": "userMessage", "text": "  prompt  "},
    {
        "type": "commandExecution",
        "id": "command-1",
        "command": "echo hi",
        "exitCode": 0,
    },
    {
        "type": "fileChange",
        "changes": [{"kind": "add", "path": "new.py"}],
    },
    {"type": "contextCompaction"},
]


def test_stream_and_history_render_all_supported_item_kinds():
    stream_out = StringIO()
    stream_renderer = TextRenderer(out=stream_out)
    for item in ITEMS:
        stream_renderer.event(ProjectedEvent(type="item/completed", item=item))

    assert stream_out.getvalue() == (
        "\n[agent]\n  hello  \n"
        "\n[user]\n  prompt  \n"
        "$ echo hi\n"
        "exit 0\n"
        "A new.py\n"
        "[context compacted]\n"
    )

    history_out = StringIO()
    history_renderer = TextRenderer(out=history_out)
    history_renderer.snapshot(
        HistorySnapshot(
            thread_id="thread-1",
            turns=[HistoryTurn(id="turn-1", index=0, status="completed", items=ITEMS)],
        )
    )

    assert history_out.getvalue() == (
        "Turn 0 turn-1 [completed]\n"
        "  [agent] hello\n"
        "  [user] prompt\n"
        "  $ echo hi (exit 0)\n"
        "  ~ new.py\n"
        "  [context compacted]\n"
    )


def test_stream_renders_command_lifecycle_with_status_and_exit_result():
    out = StringIO()
    renderer = TextRenderer(out=out)
    item = {
        "type": "commandExecution",
        "id": "command-1",
        "command": "echo hi",
        "exitCode": 0,
    }

    renderer.event(ProjectedEvent(type="item/started", item=item))
    renderer.event(ProjectedEvent(type="item/completed", item=item))

    assert out.getvalue() == "$ echo hi\nstarted\n$ echo hi\nexit 0\n"


def test_stream_renders_no_exit_code_without_repeating_command():
    out = StringIO()
    renderer = TextRenderer(out=out)
    item = {
        "type": "commandExecution",
        "id": "command-1",
        "command": "echo hi",
    }

    renderer.event(ProjectedEvent(type="item/started", item=item))
    renderer.event(ProjectedEvent(type="item/completed", item=item))

    assert out.getvalue() == "$ echo hi\nstarted\nno exit code\n"


def test_unknown_item_kind_is_ignored_by_both_text_paths():
    item = {"type": "futureItem", "value": "ignored"}

    stream_out = StringIO()
    TextRenderer(out=stream_out).event(ProjectedEvent(type="item/completed", item=item))
    assert stream_out.getvalue() == ""

    history_out = StringIO()
    TextRenderer(out=history_out).snapshot(
        HistorySnapshot(
            thread_id="thread-1",
            turns=[HistoryTurn(id="turn-1", index=0, status="completed", items=[item])],
        )
    )
    assert history_out.getvalue() == "Turn 0 turn-1 [completed]\n"


def test_context_line_uses_effective_context_usage_ratio():
    assert (
        format_context_line(
            ContextUsage(
                used_tokens=83000,
                window_tokens=200000,
                ratio=0.38,
                source="rollout",
            )
        )
        == "Context: 83k / 200k (38%)"
    )


USAGE = {"usedTokens": 83000, "windowTokens": 200000, "ratio": 0.38}


def test_stream_header_prints_only_thread_line_and_marker_moves_to_turn_started():
    out = StringIO()
    renderer = TextRenderer(out=out)

    renderer.stream_header("t1", "u1")
    assert out.getvalue() == "Thread: t1\n\n"

    # The turn marker is emitted from every turn/started event, in all
    # streaming commands and both follow modes.
    renderer.event(
        ProjectedEvent("turn/started", thread_id="t1", turn_id="u1", source="live")
    )
    assert out.getvalue() == "Thread: t1\n\nTurn: u1\n"


def test_context_usage_line_is_event_driven_after_each_turn_completed():
    out = StringIO()
    renderer = TextRenderer(out=out)

    renderer.event(
        ProjectedEvent(
            "thread/tokenUsage/updated",
            thread_id="t1",
            turn_id="u1",
            source="live",
            extra={"usage": USAGE},
        )
    )
    renderer.event(
        ProjectedEvent(
            "turn/completed",
            thread_id="t1",
            turn_id="u1",
            source="live",
            extra={"status": "completed"},
        )
    )

    assert out.getvalue() == "\nTurn completed\nContext: 83k / 200k (38%)\n"


def test_no_context_usage_line_when_no_usage_event_was_seen():
    out = StringIO()
    renderer = TextRenderer(out=out)

    renderer.event(
        ProjectedEvent(
            "turn/completed",
            thread_id="t1",
            turn_id="u1",
            source="live",
            extra={"status": "completed"},
        )
    )

    assert out.getvalue() == "\nTurn completed\n"


def test_latest_usage_is_reprinted_for_later_turns():
    out = StringIO()
    renderer = TextRenderer(out=out)

    renderer.event(
        ProjectedEvent(
            "thread/tokenUsage/updated",
            thread_id="t1",
            turn_id="u1",
            source="live",
            extra={"usage": USAGE},
        )
    )
    renderer.event(
        ProjectedEvent(
            "turn/completed",
            thread_id="t1",
            turn_id="u1",
            source="live",
            extra={"status": "completed"},
        )
    )
    # A second turn closes without a new usage event: the latest usage seen
    # in the stream is printed again.
    renderer.event(
        ProjectedEvent(
            "turn/completed",
            thread_id="t1",
            turn_id="u2",
            source="live",
            extra={"status": "failed"},
        )
    )

    assert out.getvalue() == (
        "\nTurn completed\nContext: 83k / 200k (38%)\n"
        "\nTurn ended: failed\nContext: 83k / 200k (38%)\n"
    )


def test_stream_footer_prints_nothing_even_with_terminal_context():
    # The usage line is event-stream-driven; the footer never reprints it
    # from TurnTerminal.context (no double printing in either mode).
    out = StringIO()
    renderer = TextRenderer(out=out)

    renderer.stream_footer(
        TurnTerminal(
            thread_id="t1",
            turn_id="u1",
            status="completed",
            context=ContextUsage(
                used_tokens=83000, window_tokens=200000, ratio=0.38, source="live"
            ),
        )
    )
    renderer.stream_footer(None)

    assert out.getvalue() == ""

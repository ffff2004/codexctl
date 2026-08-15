"""Text rendering of projected items."""

from __future__ import annotations

from io import StringIO

from codexctl.model import HistorySnapshot, HistoryTurn, ProjectedEvent
from codexctl.render import TextRenderer


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


def test_unknown_item_kind_is_ignored_by_both_text_paths():
    item = {"type": "futureItem", "value": "ignored"}

    stream_out = StringIO()
    TextRenderer(out=stream_out).event(
        ProjectedEvent(type="item/completed", item=item)
    )
    assert stream_out.getvalue() == ""

    history_out = StringIO()
    TextRenderer(out=history_out).snapshot(
        HistorySnapshot(
            thread_id="thread-1",
            turns=[HistoryTurn(id="turn-1", index=0, status="completed", items=[item])],
        )
    )
    assert history_out.getvalue() == "Turn 0 turn-1 [completed]\n"

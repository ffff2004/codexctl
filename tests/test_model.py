"""Selector parsing and application: exact Python indexing semantics."""

from __future__ import annotations

import pytest

from codexctl.model import (
    ReplayActiveTurn,
    ReplayAll,
    ReplayTail,
    SingleIndex,
    SliceSelector,
    apply_turn_selector,
    parse_replay_selector,
    parse_turn_selector,
    select_replay_turns,
)

TURNS = ["t0", "t1", "t2", "t3", "t4"]


class TestParseTurnSelector:
    def test_single_index(self):
        assert parse_turn_selector("0") == SingleIndex(0)
        assert parse_turn_selector("-1") == SingleIndex(-1)
        assert parse_turn_selector(" 3 ") == SingleIndex(3)

    def test_slices(self):
        assert parse_turn_selector("1:3") == SliceSelector(1, 3, None)
        assert parse_turn_selector(":") == SliceSelector(None, None, None)
        assert parse_turn_selector("::-1") == SliceSelector(None, None, -1)
        assert parse_turn_selector("-2:") == SliceSelector(-2, None, None)
        assert parse_turn_selector("1:4:2") == SliceSelector(1, 4, 2)

    def test_invalid(self):
        for bad in ("", "abc", "1:2:3:4", "::0"):
            with pytest.raises(ValueError):
                parse_turn_selector(bad)

    def test_zero_step_rejected(self):
        with pytest.raises(ValueError):
            parse_turn_selector("1:2:0")


class TestApplyTurnSelector:
    def test_none_selects_all_with_original_indexes(self):
        assert apply_turn_selector(TURNS, None) == list(enumerate(TURNS))

    @pytest.mark.parametrize("index", [0, 2, 4, -1, -5])
    def test_single_index_matches_python(self, index):
        pairs = apply_turn_selector(TURNS, SingleIndex(index))
        assert pairs == [(index % len(TURNS), TURNS[index])]

    def test_single_index_out_of_range_raises(self):
        with pytest.raises(IndexError):
            apply_turn_selector(TURNS, SingleIndex(5))
        with pytest.raises(IndexError):
            apply_turn_selector(TURNS, SingleIndex(-6))

    @pytest.mark.parametrize(
        "selector",
        [
            SliceSelector(1, 3, None),
            SliceSelector(None, None, None),
            SliceSelector(None, None, -1),
            SliceSelector(-3, None, None),
            SliceSelector(None, None, 2),
            SliceSelector(9, 20, None),  # clamping like Python
        ],
    )
    def test_slice_matches_python(self, selector):
        expected = [
            (i, t)
            for i, t in list(enumerate(TURNS))[
                slice(selector.start, selector.stop, selector.step)
            ]
        ]
        assert apply_turn_selector(TURNS, selector) == expected


class TestParseReplaySelector:
    def test_accepted_forms(self):
        assert isinstance(parse_replay_selector("-1"), ReplayActiveTurn)
        assert isinstance(parse_replay_selector(":"), ReplayAll)
        assert parse_replay_selector("-1:") == ReplayTail(1)
        assert parse_replay_selector("-3:") == ReplayTail(3)
        assert parse_replay_selector("-100:") == ReplayTail(100)

    @pytest.mark.parametrize(
        "bad", ["0", "1", "-0:", "5:", ":-1", "1:3", "abc", ""]
    )
    def test_rejected_forms(self, bad):
        with pytest.raises(ValueError):
            parse_replay_selector(bad)


class TestSelectReplayTurns:
    def test_all(self):
        assert select_replay_turns(TURNS, ReplayAll()) == list(enumerate(TURNS))

    def test_active_turn_only(self):
        assert select_replay_turns(TURNS, ReplayActiveTurn()) == [(4, "t4")]

    def test_tail_is_continuous_suffix(self):
        assert select_replay_turns(TURNS, ReplayTail(2)) == [(3, "t3"), (4, "t4")]

    def test_tail_larger_than_history_clamps(self):
        assert select_replay_turns(TURNS, ReplayTail(50)) == list(enumerate(TURNS))

    def test_empty_history(self):
        assert select_replay_turns([], ReplayAll()) == []
        assert select_replay_turns([], ReplayActiveTurn()) == []

"""Tests for the shared card write path."""
from custom_components.trello_todo.api import parse_board
from custom_components.trello_todo.writes import apply_card_fields

from .common import board, card


def test_mirror_start_and_due_complete():
    b = parse_board(board(cards=[card("c1", "A", "l_alex", 1, due="2026-10-15T22:30:00.000Z")]))
    apply_card_fields(b, b.card("c1"), {"due": "2026-10-20T14:30:00+00:00", "start": "2026-10-20T13:00:00+00:00",
                                        "dueComplete": True})
    c = b.card("c1")
    assert (c.due, c.start, c.due_complete) == ("2026-10-20T14:30:00+00:00", "2026-10-20T13:00:00+00:00", True)


def test_mirror_clears_start():
    b = parse_board(board(cards=[card("c1", "A", "l_alex", 1, start="2026-10-15T20:00:00.000Z")]))
    apply_card_fields(b, b.card("c1"), {"due": None, "start": None})
    assert (b.card("c1").due, b.card("c1").start) == (None, None)

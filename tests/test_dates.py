"""Tests for the card model additions and the shared date helpers."""
from datetime import UTC, datetime

from custom_components.trello_todo.api import BOARD_PARAMS, parse_board
from custom_components.trello_todo.dates import card_is_complete, parse_trello_time

from .common import board, card


def _board(*cards):
    return parse_board(board(cards=list(cards)))


def test_start_requested():
    assert "start" in BOARD_PARAMS["card_fields"].split(",")


def test_card_fields_parsed():
    b = _board(card("c1", "A", "l_alex", 1, due="2026-10-15T22:30:00.000Z", start="2026-10-15T20:00:00.000Z",
                    due_complete=True, members=["m2", "gone", "m1"]))
    c = b.card("c1")
    assert c.due_complete is True
    assert c.start == "2026-10-15T20:00:00.000Z"
    assert c.member_ids == ("m2", "m1")  # ids not on the board are dropped, order kept
    assert c.members == ("A", "S")


def test_card_defaults():
    c = _board(card("c1", "A", "l_alex", 1)).card("c1")
    assert (c.due_complete, c.start, c.member_ids) == (False, None, ())


def test_parse_trello_time():
    assert parse_trello_time("2026-10-15T22:30:00.000Z") == datetime(2026, 10, 15, 22, 30, tzinfo=UTC)
    assert parse_trello_time(None) is None
    assert parse_trello_time("") is None
    assert parse_trello_time("not a date") is None


def test_card_is_complete():
    b = _board(
        card("t", "Ticked", "l_alex", 1, due_complete=True),
        card("d", "In done", "l_done", 1),
        card("o", "Open", "l_alex", 2),
    )
    assert card_is_complete(b.card("t"), "l_done")
    assert card_is_complete(b.card("d"), "l_done")
    assert not card_is_complete(b.card("o"), "l_done")
    assert not card_is_complete(b.card("d"), None)

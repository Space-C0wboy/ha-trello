"""Tests for name → id resolution."""
import pytest

from custom_components.trello_todo.api import parse_board
from custom_components.trello_todo.resolve import (
    ResolveError,
    find_card_in_list,
    parse_card_ref,
    resolve_board,
    resolve_label,
    resolve_list,
    resolve_member,
)

from .common import LISTS, board, card

BOARD = parse_board(board())


def _err(fn, *args):
    with pytest.raises(ResolveError) as info:
        fn(*args)
    return info.value.key, info.value.placeholders


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://trello.com/c/abc12345/7-buy-stamps", "abc12345"),
        ("trello.com/c/abc12345", "abc12345"),
        ("  5f0c2b3a4d5e6f7a8b9c0d1e ", "5f0c2b3a4d5e6f7a8b9c0d1e"),
        ("abc12345", "abc12345"),
    ],
)
def test_parse_card_ref(value, expected):
    assert parse_card_ref(value) == expected


def test_parse_card_ref_empty():
    assert _err(parse_card_ref, "  ")[0] == "card_not_found"


def test_resolve_list_by_name_case_insensitive_and_id():
    assert resolve_list(BOARD, "done & archive") == "l_done"
    assert resolve_list(BOARD, "l_alex") == "l_alex"


def test_list_not_found():
    assert _err(resolve_list, BOARD, "Dones") == ("list_not_found", {"value": "Dones", "board": "Household"})


def test_list_ambiguous():
    dup = parse_board(board(lists=[*LISTS, {"id": "l_alex2", "name": "alex", "pos": 9}]))
    assert _err(resolve_list, dup, "Alex")[0] == "list_ambiguous"


def test_resolve_label_by_name_id_and_colour():
    assert resolve_label(BOARD, "urgent") == "lb1"
    assert resolve_label(BOARD, "lb2") == "lb2"
    assert resolve_label(BOARD, "green") == "lb2"  # the only unnamed green label


def test_label_colour_ambiguous():
    raw = board()
    raw["labels"].append({"id": "lb3", "name": "", "color": "green"})
    assert _err(resolve_label, parse_board(raw), "green")[0] == "label_ambiguous"


def test_label_not_found():
    assert _err(resolve_label, BOARD, "purple")[0] == "label_not_found"


def test_resolve_member_by_full_name_username_and_id():
    assert resolve_member(BOARD, "alex") == "m2"  # username (also full name, same member)
    assert resolve_member(BOARD, "SAM") == "m1"
    assert resolve_member(BOARD, "m2") == "m2"


def test_member_ambiguous():
    raw = board()
    raw["members"].append({"id": "m3", "fullName": "Alex", "initials": "A2", "username": "alex2"})
    assert _err(resolve_member, parse_board(raw), "Alex")[0] == "member_ambiguous"


def test_member_not_found():
    assert _err(resolve_member, BOARD, "Pat")[0] == "member_not_found"


def test_resolve_board():
    boards = {"b1": "Household", "b2": "Projects"}
    assert resolve_board(boards, "projects") == "b2"
    assert resolve_board(boards, "b1") == "b1"
    assert _err(resolve_board, boards, "Nope")[0] == "board_not_found"


def test_find_card_in_list():
    cards = BOARD.cards["l_alex"]
    assert find_card_in_list(cards, "buy stamps").id == "c1"
    assert find_card_in_list(cards, "c3").id == "c3"


def test_find_card_ambiguous_and_missing():
    cards = [card("d1", "Dup", "l_alex", 1), card("d2", "dup", "l_alex", 2)]
    parsed = parse_board(board(cards=cards)).cards["l_alex"]
    key, placeholders = _err(find_card_in_list, parsed, "DUP")
    assert key == "card_ambiguous"
    assert "https://trello.com/c/d1" in placeholders["matches"]
    assert _err(find_card_in_list, parsed, "Nope")[0] == "card_not_found"

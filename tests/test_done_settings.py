"""Tests for the per-board done-settings helpers."""
import pytest

from custom_components.trello_todo.subentry_flow import default_done_settings, done_settings_from_input

from .common import LISTS, LISTS_B2


def test_default_picks_done_list():
    assert default_done_settings(LISTS) == {"done_behavior": "move", "done_list": "l_done"}


def test_default_without_done_list_archives():
    assert default_done_settings(LISTS_B2) == {"done_behavior": "archive", "done_list": None}


def test_default_takes_first_done_list_by_position():
    lists = [{"id": "x", "name": "Done later", "pos": 5}, {"id": "y", "name": "DONE", "pos": 1}]
    assert default_done_settings(lists)["done_list"] == "y"


@pytest.mark.parametrize(
    ("user_input", "expected"),
    [
        ({"done_behavior": "move"}, ({}, {"done_list": "done_list_required"})),
        ({"done_behavior": "move_and_archive", "done_list": ""}, ({}, {"done_list": "done_list_required"})),
        ({"done_behavior": "archive", "done_list": "l_done"}, ({"done_behavior": "archive", "done_list": None}, {})),
        ({"done_behavior": "move", "done_list": "l_done"}, ({"done_behavior": "move", "done_list": "l_done"}, {})),
    ],
)
def test_done_settings_from_input(user_input, expected):
    assert done_settings_from_input(user_input) == expected

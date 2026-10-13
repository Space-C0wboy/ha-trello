"""Tests for diagnostics."""
from custom_components.trello_todo.diagnostics import async_get_config_entry_diagnostics

from .common import ACCESS_TOKEN, CLIENT_SECRET, REFRESH_TOKEN, subentry_id


async def test_diagnostics_redacts_tokens(hass, loaded):
    diag = await async_get_config_entry_diagnostics(hass, loaded)
    assert diag["entry"]["data"] == {"auth_implementation": "trello_todo", "token": "**REDACTED**"}
    assert diag["entry"]["options"] == {"scan_interval": 120}
    (b1,) = diag["boards"]
    assert b1 == {
        "subentry_id": subentry_id(loaded, "b1"),
        "board_id": "b1",
        "title": "Household",
        "settings": {"board_id": "b1", "done_behavior": "move", "done_list": "l_done"},
        "last_update_success": True,
        "lists": {"l_alex": "Alex", "l_sam": "Sam", "l_done": "Done & Archive"},
        "open_cards": {"l_alex": 3, "l_sam": 1, "l_done": 1},
    }
    text = str(diag)
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET):
        assert secret not in text


async def test_diagnostics_when_not_loaded(hass, entry):
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"]["token"] == "**REDACTED**"
    (b1,) = diag["boards"]
    assert b1["board_id"] == "b1"
    assert b1["settings"] == {"board_id": "b1", "done_behavior": "move", "done_list": "l_done"}
    assert b1["lists"] is None

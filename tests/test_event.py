"""Tests for the per-board activity event entity."""
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from custom_components.trello_todo.const import DOMAIN

from .common import (
    BOARD_URL,
    action,
    actions_url,
    board,
    board_subentry,
    board_url,
    make_entry,
    second_board,
    subentry_id,
)

EVENT = "event.household_activity"
CARD = {"id": "c1", "name": "Buy stamps", "shortLink": "abc"}
ALEX_LIST = {"id": "l_alex", "name": "Alex"}


def _coordinator(entry, board_id="b1"):
    return entry.runtime_data.boards[subentry_id(entry, board_id)]


def _capture(hass):
    seen = []

    @callback
    def _changed(event):
        if event.data["entity_id"] == EVENT and event.data["new_state"] is not None:
            seen.append(event.data["new_state"].attributes)

    hass.bus.async_listen("state_changed", _changed)
    return seen


async def test_entity_created(hass, loaded):
    state = hass.states.get(EVENT)
    assert state is not None
    assert state.attributes["source"] == "feed"
    assert "comment_added" in state.attributes["event_types"]
    reg = er.async_get(hass).async_get(EVENT)
    assert reg.unique_id == "b1_activity"
    assert reg.config_subentry_id == subentry_id(loaded, "b1")


async def test_event_fired_with_attributes(hass, loaded, mock_trello):
    seen = _capture(hass)
    await _coordinator(loaded).processor.async_process(
        [action("a1", "commentCard", card=CARD, list=ALEX_LIST, text="On it")]
    )
    await hass.async_block_till_done()
    attrs = [a for a in seen if a.get("event_type") == "comment_added"][-1]
    assert attrs["comment"] == "On it"
    assert attrs["card_name"] == "Buy stamps"
    assert attrs["by"] == {"id": "m2", "name": "Alex", "initials": "A"}
    assert attrs["by_me"] is False


async def test_batch_fires_every_event_in_order(hass, loaded):
    """Review Focus 4: each event in a burst reaches automations."""
    seen = _capture(hass)
    await _coordinator(loaded).processor.async_process([
        action("a2", "commentCard", date="2026-10-13T12:00:02Z", card=CARD, list=ALEX_LIST, text="second"),
        action("a1", "commentCard", date="2026-10-13T12:00:01Z", card=CARD, list=ALEX_LIST, text="first"),
    ])
    await hass.async_block_till_done()
    assert [a["comment"] for a in seen if a.get("event_type") == "comment_added"] == ["first", "second"]


async def test_source_follows_mode(hass, loaded):
    _coordinator(loaded).set_mode("webhook")
    await hass.async_block_till_done()
    assert hass.states.get(EVENT).attributes["source"] == "webhook"


async def test_unavailable_with_board(hass, loaded, mock_trello):
    mock_trello.clear()
    mock_trello.get(BOARD_URL, status=500)
    await _coordinator(loaded).async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(EVENT).state == "unavailable"


async def test_board_down_at_startup_gets_event_entity_later(hass, setup_credentials, mock_trello):
    entry = make_entry(hass, board_subentry(), board_subentry("b2", "Projects", "archive", None))
    mock_trello.get(BOARD_URL, payload=board(), repeat=True)
    mock_trello.get(actions_url("b1"), payload=[], repeat=True)
    mock_trello.get(actions_url("b2"), payload=[], repeat=True)
    mock_trello.get(board_url("b2"), status=500)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get_entity_id("event", DOMAIN, "b2_activity") is None
    mock_trello.get(board_url("b2"), payload=second_board())
    await _coordinator(entry, "b2").async_refresh()
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get_entity_id("event", DOMAIN, "b2_activity") == "event.projects_activity"

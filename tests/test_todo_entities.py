"""Tests for to-do entities: one per open list, kept in step with the board."""
from datetime import datetime
from zoneinfo import ZoneInfo

from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.trello_todo.const import DOMAIN

from .common import (
    ACCESS_TOKEN,
    BOARD_URL,
    LISTS,
    REFRESH_TOKEN,
    board,
    board_subentry,
    board_url,
    card,
    make_entry,
    subentry_id,
    second_board,
)

ALEX = "todo.alex"
DONE = "todo.done_archive"
CHI = ZoneInfo("America/Chicago")
SECOND = board_subentry("b2", "Projects", "archive", None)


def _coordinator(entry, board_id="b1"):
    return entry.runtime_data.boards[subentry_id(entry, board_id)]


async def _items(hass, entity_id=ALEX):
    resp = await hass.services.async_call(
        "todo", "get_items", {"entity_id": entity_id}, blocking=True, return_response=True
    )
    return resp[entity_id]["items"]


async def _refresh_with(hass, entry, mock_trello, payload, board_id="b1"):
    mock_trello.clear()
    mock_trello.get(BOARD_URL, payload=payload, repeat=True)
    await _coordinator(entry, board_id).async_refresh()
    await hass.async_block_till_done()


async def test_entity_per_open_list(hass, loaded):
    assert hass.states.get(ALEX).state == "3"
    assert hass.states.get("todo.sam").state == "1"
    assert hass.states.get(DONE).state == "0"
    entity = er.async_get(hass).async_get(ALEX)
    assert entity.unique_id == "l_alex"
    assert entity.has_entity_name
    assert entity.config_subentry_id == subentry_id(loaded, "b1")
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device.name == "Household"
    assert (DOMAIN, "b1") in device.identifiers
    assert hass.states.get(ALEX).attributes["friendly_name"] == "Household Alex"


async def test_item_mapping_in_pos_order(hass, loaded):
    items = await _items(hass)
    assert [i["uid"] for i in items] == ["c1", "c2", "c3"]
    assert items[0] == {
        "uid": "c1",
        "summary": "Buy stamps",
        "description": "Forever stamps",
        "status": "needs_action",
        "due": "2026-10-15",  # 05:00Z is local midnight -> date
    }
    assert items[1]["due"] == datetime(2026, 10, 15, 17, 30, tzinfo=CHI).isoformat()
    assert "due" not in items[2]
    assert "description" not in items[2]


async def test_done_list_items_are_completed(hass, loaded):
    assert await _items(hass, DONE) == [{"uid": "c9", "summary": "Old thing", "status": "completed"}]
    attrs = hass.states.get(DONE).attributes
    assert attrs["items_total"] == 1
    assert attrs["items"][0]["uid"] == "c9"


async def test_items_attribute(hass, loaded):
    attrs = hass.states.get(ALEX).attributes
    assert attrs["items_total"] == 3
    assert attrs["items"][0] == {
        "uid": "c1",
        "summary": "Buy stamps",
        "due": "2026-10-15T05:00:00+00:00",
        "labels": [{"name": "Urgent", "color": "red"}],
        "members": ["S", "A"],
        "url": "https://trello.com/c/c1",
    }
    assert attrs["items"][2]["due"] is None
    assert [i["uid"] for i in attrs["items"]] == ["c1", "c2", "c3"]


async def test_items_attribute_capped_and_unrecorded(hass, load_entry):
    cards = [card(f"x{n}", f"Task {n}", "l_alex", n) for n in range(30)]
    await load_entry(b1=board(cards))
    state = hass.states.get(ALEX)
    assert state.state == "30"
    assert len(state.attributes["items"]) == 25
    assert state.attributes["items_total"] == 30
    entity = hass.data["entity_components"]["todo"].get_entity(ALEX)
    assert {"items", "items_total"} <= entity._unrecorded_attributes


async def test_no_secrets_in_attributes(hass, loaded):
    for entity_id in hass.states.async_entity_ids("todo"):
        text = str(hass.states.get(entity_id).as_dict())
        assert ACCESS_TOKEN not in text and REFRESH_TOKEN not in text


async def test_new_list_appears(hass, loaded, mock_trello):
    await _refresh_with(hass, loaded, mock_trello, board(lists=[*LISTS, {"id": "l_groc", "name": "Groceries", "pos": 4}]))
    assert hass.states.get("todo.groceries").state == "0"


async def test_archived_list_removed(hass, loaded, mock_trello):
    await _refresh_with(hass, loaded, mock_trello, board(lists=[LISTS[0], LISTS[2]]))
    assert hass.states.get("todo.sam") is None
    assert er.async_get(hass).async_get_entity_id("todo", DOMAIN, "l_sam") is None
    assert hass.states.get(ALEX).state == "3"


async def test_stale_registry_entries_removed_at_startup(hass, entry, mock_trello):
    """Review Focus 2: a list archived while HA was down leaves no dead entity."""
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "todo", DOMAIN, "l_gone", config_entry=entry, config_subentry_id=subentry_id(entry, "b1"), suggested_object_id="gone"
    )
    mock_trello.get(BOARD_URL, payload=board(), repeat=True)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(stale.entity_id) is None
    assert hass.states.get(ALEX) is not None


async def test_same_list_name_on_two_boards(hass, load_entry):
    await load_entry(
        board_subentry(), SECOND, b2=second_board(lists=[{"id": "l_w_alex", "name": "Alex", "pos": 1}], cards=[])
    )
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("todo", DOMAIN, "l_alex") == "todo.alex"
    assert registry.async_get_entity_id("todo", DOMAIN, "l_w_alex") == "todo.alex_2"


async def test_emoji_only_list_name(hass, load_entry):
    """Review Focus 4: a name that slugifies to nothing still gets a usable entity."""
    await load_entry(b1=board(lists=[*LISTS, {"id": "l_party", "name": "🎉", "pos": 4}]))
    entity_id = er.async_get(hass).async_get_entity_id("todo", DOMAIN, "l_party")
    assert entity_id is not None
    assert entity_id != "todo.unknown"
    assert hass.states.get(entity_id).state == "0"


async def test_5xx_marks_board_unavailable(hass, loaded, mock_trello):
    mock_trello.clear()
    mock_trello.get(BOARD_URL, status=500)
    await _coordinator(loaded).async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ALEX).state == "unavailable"


async def test_board_down_at_startup_gets_entities_later(hass, setup_credentials, mock_trello):
    entry = make_entry(hass, board_subentry(), SECOND)
    mock_trello.get(BOARD_URL, payload=board(), repeat=True)
    mock_trello.get(board_url("b2"), status=500)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("todo.venues") is None

    mock_trello.get(board_url("b2"), payload=second_board())
    await _coordinator(entry, "b2").async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("todo.venues").state == "1"


async def test_list_moved_between_boards(hass, load_entry, mock_trello):
    """Trello "Move list" from board 1 to board 2: the entity follows to board 2, whichever board refreshes first."""
    entry = await load_entry(board_subentry(), SECOND)
    moved = {"id": "l_sam", "name": "Sam", "pos": 3}
    mock_trello.clear()
    mock_trello.get(board_url("b2"), payload=second_board(lists=[*second_board()["lists"], moved]), repeat=True)
    mock_trello.get(BOARD_URL, payload=board(lists=[LISTS[0], LISTS[2]]), repeat=True)
    for board_id in ("b2", "b1", "b2"):
        await _coordinator(entry, board_id).async_refresh()
        await hass.async_block_till_done()
    reg = er.async_get(hass).async_get(er.async_get(hass).async_get_entity_id("todo", DOMAIN, "l_sam"))
    assert reg.config_subentry_id == subentry_id(entry, "b2")
    assert hass.states.get(reg.entity_id) is not None

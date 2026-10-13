"""Writes from HA show up immediately, before any refresh."""
from unittest.mock import AsyncMock

import pytest

from .common import API, card

ALEX = "todo.alex"
DONE = "todo.done_archive"


@pytest.fixture
def no_refresh(loaded):
    """Block refreshes so only the local update can change state."""
    for coordinator in loaded.runtime_data.boards.values():
        coordinator.async_request_refresh = AsyncMock()
    return loaded


async def _service(hass, service, data, entity_id=ALEX):
    await hass.services.async_call("todo", service, {"entity_id": entity_id, **data}, blocking=True)


async def _uids(hass, entity_id=ALEX):
    resp = await hass.services.async_call(
        "todo", "get_items", {"entity_id": entity_id}, blocking=True, return_response=True
    )
    return [i["uid"] for i in resp[entity_id]["items"]]


async def test_create_shows_immediately(hass, no_refresh, mock_trello):
    mock_trello.post(f"{API}/cards", payload=card("new", "Milk", "l_alex", 99999))
    await _service(hass, "add_item", {"item": "Milk"})
    assert hass.states.get(ALEX).state == "4"
    assert (await _uids(hass))[-1] == "new"


async def test_complete_moves_to_done_list_immediately(hass, no_refresh, mock_trello):
    mock_trello.put(f"{API}/cards/c1", payload={})
    await _service(hass, "update_item", {"item": "c1", "status": "completed"})
    assert hass.states.get(ALEX).state == "2"
    assert (await _uids(hass, DONE))[0] == "c1"


async def test_back_to_back_writes_both_show(hass, no_refresh, mock_trello):
    mock_trello.put(f"{API}/cards/c1", payload={})
    mock_trello.put(f"{API}/cards/c2", payload={})
    await _service(hass, "update_item", {"item": "c1", "status": "completed"})
    await _service(hass, "update_item", {"item": "c2", "status": "completed"})
    assert hass.states.get(ALEX).state == "1"


async def test_remove_shows_immediately(hass, no_refresh, mock_trello):
    mock_trello.put(f"{API}/cards/c2", payload={})
    await _service(hass, "remove_item", {"item": ["c2"]})
    assert await _uids(hass) == ["c1", "c3"]


async def test_rename_shows_immediately(hass, no_refresh, mock_trello):
    mock_trello.put(f"{API}/cards/c1", payload={})
    await _service(hass, "update_item", {"item": "c1", "rename": "Stamps"})
    assert hass.states.get(ALEX).attributes["items"][0]["summary"] == "Stamps"


async def test_move_shows_immediately(hass, no_refresh, mock_trello, hass_ws_client):
    mock_trello.put(f"{API}/cards/c3", payload={})
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "todo/item/move", "entity_id": ALEX, "uid": "c3"})
    assert (await client.receive_json())["success"]
    assert await _uids(hass) == ["c3", "c1", "c2"]


async def test_failed_write_changes_nothing(hass, no_refresh, mock_trello):
    from homeassistant.exceptions import HomeAssistantError

    mock_trello.put(f"{API}/cards/c1", status=500)
    with pytest.raises(HomeAssistantError):
        await _service(hass, "update_item", {"item": "c1", "status": "completed"})
    assert hass.states.get(ALEX).state == "3"


async def test_create_mirror_does_not_duplicate(hass, no_refresh, mock_trello):
    """If a refresh already brought the new card in, the mirror must not add it twice."""
    mock_trello.post(f"{API}/cards", payload=card("c1", "Buy stamps", "l_alex", 16384))
    await _service(hass, "add_item", {"item": "Buy stamps"})
    assert (await _uids(hass)).count("c1") == 1

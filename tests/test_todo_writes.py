"""Tests for writes from HA to Trello."""
import time

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .common import (
    API,
    LISTS,
    TOKEN_URL,
    board,
    board_subentry,
    bodies,
    calls,
    card,
    card_actions_url,
)

ALEX = "todo.alex"
DONE = "todo.done_archive"


async def _service(hass, service, data, entity_id=ALEX):
    await hass.services.async_call("todo", service, {"entity_id": entity_id, **data}, blocking=True)


def _board_requests(mock, board_id="b1") -> int:
    return len(calls(mock, "GET", f"{API}/boards/{board_id}"))


async def test_create(hass, loaded, mock_trello):
    mock_trello.post(f"{API}/cards", payload=card("new", "Milk", "l_alex", 99))
    before = _board_requests(mock_trello)
    await _service(hass, "add_item", {"item": "Milk", "description": "2%", "due_date": "2026-10-20"})
    assert bodies(mock_trello, "POST", f"{API}/cards") == [
        {"idList": "l_alex", "name": "Milk", "desc": "2%", "due": "2026-10-20T05:00:00+00:00", "pos": "bottom"}
    ]
    assert _board_requests(mock_trello) == before + 1  # refreshed right after the write


async def test_create_with_datetime(hass, loaded, mock_trello):
    mock_trello.post(f"{API}/cards", payload=card("new", "Call", "l_alex", 99))
    await _service(hass, "add_item", {"item": "Call", "due_datetime": "2026-10-20 09:15:00"})
    body = bodies(mock_trello, "POST", f"{API}/cards")[0]
    assert body["due"] == "2026-10-20T14:15:00+00:00"
    assert body["desc"] == ""


async def test_update_fields(hass, loaded, mock_trello):
    mock_trello.put(f"{API}/cards/c1", payload={})
    await _service(hass, "update_item", {"item": "c1", "rename": "Buy more stamps", "description": "x"})
    assert bodies(mock_trello, "PUT", f"{API}/cards/c1") == [{"name": "Buy more stamps", "desc": "x"}]


async def test_update_clears_due(hass, loaded, mock_trello):
    mock_trello.put(f"{API}/cards/c2", payload={})
    await _service(hass, "update_item", {"item": "c2", "due_datetime": None})
    assert bodies(mock_trello, "PUT", f"{API}/cards/c2") == [{"due": None}]


async def test_update_unchanged_sends_nothing(hass, loaded, mock_trello):
    await _service(hass, "update_item", {"item": "c1", "rename": "Buy stamps"})
    assert calls(mock_trello, "PUT", f"{API}/cards/c1") == []


async def test_update_by_summary(hass, loaded, mock_trello):
    mock_trello.put(f"{API}/cards/c3", payload={})
    await _service(hass, "update_item", {"item": "Pay invoice", "status": "completed"})
    assert len(calls(mock_trello, "PUT", f"{API}/cards/c3")) == 1


@pytest.mark.parametrize(
    ("behavior", "done_list", "expected"),
    [
        ("move", "l_done", {"idList": "l_done", "pos": "top"}),
        ("archive", None, {"closed": True}),
        ("move_and_archive", "l_done", {"idList": "l_done", "pos": "top", "closed": True}),
    ],
)
async def test_complete_done_behaviors(hass, load_entry, mock_trello, behavior, done_list, expected):
    await load_entry(board_subentry(done_behavior=behavior, done_list=done_list))
    mock_trello.put(f"{API}/cards/c1", payload={})
    await _service(hass, "update_item", {"item": "c1", "status": "completed"})
    assert bodies(mock_trello, "PUT", f"{API}/cards/c1") == [expected]


async def test_complete_with_rename_is_one_request(hass, loaded, mock_trello):
    mock_trello.put(f"{API}/cards/c1", payload={})
    await _service(hass, "update_item", {"item": "c1", "rename": "Stamps", "status": "completed"})
    assert bodies(mock_trello, "PUT", f"{API}/cards/c1") == [{"name": "Stamps", "idList": "l_done", "pos": "top"}]


async def test_complete_when_done_list_missing(hass, load_entry, mock_trello):
    await load_entry(board_subentry(done_list="l_gone"))
    with pytest.raises(HomeAssistantError) as info:
        await _service(hass, "update_item", {"item": "c1", "status": "completed"})
    assert info.value.translation_key == "done_list_missing"
    assert info.value.translation_placeholders == {"board": "Household"}
    assert calls(mock_trello, "PUT", f"{API}/cards/c1") == []


async def test_delete_archives(hass, loaded, mock_trello):
    mock_trello.put(f"{API}/cards/c1", payload={})
    mock_trello.put(f"{API}/cards/c2", payload={})
    await _service(hass, "remove_item", {"item": ["c1", "c2"]})
    assert bodies(mock_trello, "PUT", f"{API}/cards/c1") == [{"closed": True}]
    assert bodies(mock_trello, "PUT", f"{API}/cards/c2") == [{"closed": True}]
    assert calls(mock_trello, "DELETE", f"{API}/cards/c1") == []


async def _move(hass, hass_ws_client, uid, previous_uid=None):
    client = await hass_ws_client(hass)
    msg = {"id": 1, "type": "todo/item/move", "entity_id": ALEX, "uid": uid}
    if previous_uid:
        msg["previous_uid"] = previous_uid
    await client.send_json(msg)
    resp = await client.receive_json()
    assert resp["success"], resp


@pytest.mark.parametrize(
    ("uid", "previous_uid", "pos"),
    [
        ("c3", None, 8192),  # to the top: half the first card's pos
        ("c3", "c1", 24576),  # between c1 (16384) and c2 (32768)
        ("c1", "c3", 65536 + 16384),  # to the bottom: after the last card
        ("c1", "c2", 49152),  # c1 leaves its own slot first
    ],
)
async def test_move(hass, loaded, mock_trello, hass_ws_client, uid, previous_uid, pos):
    mock_trello.put(f"{API}/cards/{uid}", payload={})
    await _move(hass, hass_ws_client, uid, previous_uid)
    assert bodies(mock_trello, "PUT", f"{API}/cards/{uid}") == [{"pos": pos}]


async def test_unknown_uid(hass, loaded):
    entity = hass.data["entity_components"]["todo"].get_entity(ALEX)
    with pytest.raises(ServiceValidationError):
        await entity.async_delete_todo_items(["nope"])


async def test_write_error_raises(hass, loaded, mock_trello):
    mock_trello.put(f"{API}/cards/c1", status=500)
    with pytest.raises(HomeAssistantError):
        await _service(hass, "update_item", {"item": "c1", "status": "completed"})


async def test_write_401_starts_reauth(hass, loaded, mock_trello):
    mock_trello.put(f"{API}/cards/c1", status=401, body="invalid token")
    with pytest.raises(HomeAssistantError):
        await _service(hass, "update_item", {"item": "c1", "status": "completed"})
    await hass.async_block_till_done()
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == ["reauth"]


async def test_uncheck_returns_card_to_previous_list(hass, loaded, mock_trello):
    mock_trello.get(card_actions_url("c9"), payload=[{"data": {"listBefore": {"id": "l_sam"}}}])
    mock_trello.put(f"{API}/cards/c9", payload={})
    await _service(hass, "update_item", {"item": "c9", "status": "needs_action"}, entity_id=DONE)
    assert bodies(mock_trello, "PUT", f"{API}/cards/c9") == [{"idList": "l_sam", "pos": "top"}]


async def test_uncheck_without_history_uses_first_list(hass, loaded, mock_trello):
    mock_trello.get(card_actions_url("c9"), payload=[])
    mock_trello.put(f"{API}/cards/c9", payload={})
    await _service(hass, "update_item", {"item": "c9", "status": "needs_action"}, entity_id=DONE)
    assert bodies(mock_trello, "PUT", f"{API}/cards/c9") == [{"idList": "l_alex", "pos": "top"}]


async def test_uncheck_when_previous_list_is_gone(hass, loaded, mock_trello):
    mock_trello.get(card_actions_url("c9"), payload=[{"data": {"listBefore": {"id": "l_old"}}}])
    mock_trello.put(f"{API}/cards/c9", payload={})
    await _service(hass, "update_item", {"item": "c9", "status": "needs_action"}, entity_id=DONE)
    assert bodies(mock_trello, "PUT", f"{API}/cards/c9") == [{"idList": "l_alex", "pos": "top"}]


async def test_uncheck_with_no_other_list(hass, load_entry, mock_trello):
    await load_entry(b1=board(lists=[LISTS[2]], cards=[card("c9", "Old thing", "l_done", 1)]))
    mock_trello.get(card_actions_url("c9"), payload=[])
    with pytest.raises(ServiceValidationError) as info:
        await _service(hass, "update_item", {"item": "c9", "status": "needs_action"}, entity_id=DONE)
    assert info.value.translation_key == "no_return_list"
    assert calls(mock_trello, "PUT", f"{API}/cards/c9") == []


async def test_rename_completed_item_keeps_it_done(hass, loaded, mock_trello):
    mock_trello.put(f"{API}/cards/c9", payload={})
    await _service(hass, "update_item", {"item": "c9", "rename": "Older thing"}, entity_id=DONE)
    assert bodies(mock_trello, "PUT", f"{API}/cards/c9") == [{"name": "Older thing"}]


async def test_clear_completed_archives(hass, loaded, mock_trello):
    mock_trello.put(f"{API}/cards/c9", payload={})
    await hass.services.async_call("todo", "remove_completed_items", {"entity_id": DONE}, blocking=True)
    assert bodies(mock_trello, "PUT", f"{API}/cards/c9") == [{"closed": True}]


async def test_write_refreshes_only_its_board(hass, load_entry, mock_trello):
    await load_entry(board_subentry(), board_subentry("b2", "Projects", "archive", None))
    b1_before, b2_before = _board_requests(mock_trello, "b1"), _board_requests(mock_trello, "b2")
    mock_trello.put(f"{API}/cards/c1", payload={})
    await _service(hass, "update_item", {"item": "c1", "status": "completed"})
    assert _board_requests(mock_trello, "b1") == b1_before + 1
    assert _board_requests(mock_trello, "b2") == b2_before


async def test_write_with_expired_token_refreshes_first(hass, loaded, mock_trello):
    """Review Focus 5."""
    hass.config_entries.async_update_entry(
        loaded, data={**loaded.data, "token": {**loaded.data["token"], "expires_at": time.time() - 10}}
    )
    mock_trello.post(
        TOKEN_URL,
        payload={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600, "token_type": "Bearer"},
    )
    mock_trello.put(f"{API}/cards/c1", payload={})
    await _service(hass, "update_item", {"item": "c1", "status": "completed"})
    (put,) = calls(mock_trello, "PUT", f"{API}/cards/c1")
    assert put.kwargs["headers"]["Authorization"] == "Bearer new-access"


async def test_write_when_token_endpoint_down(hass, loaded, mock_trello):
    hass.config_entries.async_update_entry(
        loaded, data={**loaded.data, "token": {**loaded.data["token"], "expires_at": time.time() - 10}}
    )
    mock_trello.post(TOKEN_URL, status=503)
    with pytest.raises(HomeAssistantError) as info:
        await _service(hass, "update_item", {"item": "c1", "status": "completed"})
    assert info.value.translation_key == "api_error"

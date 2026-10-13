"""Tests for the trello_todo actions."""
import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.trello_todo.const import DOMAIN

from .common import API, BOARD_URL, LISTS_B2, board, board_subentry, bodies, calls, card, card_url, checklists_url

INFO = {
    "card_id": "c1",
    "card_url": "https://trello.com/c/c1",
    "card_name": "Buy stamps",
    "list_id": "l_done",
    "list_name": "Done & Archive",
    "board_id": "b1",
    "board_name": "Household",
}


async def _call(hass, service, data, response=False):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=response)


def _board_requests(mock, board_id="b1"):
    return len(calls(mock, "GET", f"{API}/boards/{board_id}"))


def _card_get(mock_trello, card_id="c1", board_id="b1", list_id="l_alex", name="Buy stamps"):
    mock_trello.get(
        card_url(card_id),
        payload={"id": card_id, "idBoard": board_id, "idList": list_id, "name": name,
                 "shortUrl": f"https://trello.com/c/{card_id}", "closed": False},
    )


async def test_actions_registered(hass, loaded):
    for name in ("create_card", "copy_card", "move_card", "archive_card", "restore_card", "set_due"):
        assert hass.services.has_service(DOMAIN, name)


async def test_create_card_in_todo_entity(hass, loaded, mock_trello):
    mock_trello.post(f"{API}/cards", payload=card("new", "Milk", "l_alex", 99))
    before = _board_requests(mock_trello)
    result = await _call(hass, "create_card", {"entity_id": "todo.alex", "name": "Milk"}, response=True)
    assert bodies(mock_trello, "POST", f"{API}/cards") == [{"idList": "l_alex", "name": "Milk", "pos": "bottom"}]
    assert result == {**INFO, "card_id": "new", "card_url": "https://trello.com/c/new", "card_name": "Milk",
                      "list_id": "l_alex", "list_name": "Alex"}
    assert _board_requests(mock_trello) == before + 1


async def test_create_card_by_list_name_with_everything(hass, loaded, mock_trello):
    mock_trello.post(f"{API}/cards", payload=card("new", "Fix", "l_sam", 99))
    await _call(hass, "create_card", {
        "list": "sam", "name": "Fix", "description": "x", "due": "2026-10-20",
        "labels": ["urgent", "green"], "members": ["alex"], "position": "top",
    })
    assert bodies(mock_trello, "POST", f"{API}/cards") == [{
        "idList": "l_sam", "name": "Fix", "pos": "top", "desc": "x", "due": "2026-10-20T05:00:00+00:00",
        "idLabels": "lb1,lb2", "idMembers": "m2",
    }]


async def test_create_card_needs_a_target(hass, loaded):
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "create_card", {"name": "Milk"})
    assert info.value.translation_key == "list_required"


async def test_create_card_target_conflict(hass, loaded):
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "create_card", {"entity_id": "todo.alex", "list": "Sam", "name": "Milk"})
    assert info.value.translation_key == "create_target_conflict"


async def test_create_card_unknown_list(hass, loaded):
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "create_card", {"list": "Dones", "name": "Milk"})
    assert info.value.translation_key == "list_not_found"
    assert info.value.translation_placeholders == {"value": "Dones", "board": "Household"}


async def test_create_card_board_required_with_two_boards(hass, load_entry):
    await load_entry(board_subentry(), board_subentry("b2", "Projects", "archive", None))
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "create_card", {"list": "Alex", "name": "Milk"})
    assert info.value.translation_key == "board_required"


async def test_move_card_by_card_id(hass, loaded, mock_trello):
    """Review Focus 1: event card_id passes straight in."""
    _card_get(mock_trello)
    mock_trello.put(f"{API}/cards/c1", payload=card("c1", "Buy stamps", "l_done", 1))
    result = await _call(hass, "move_card", {"card": "c1", "list": "Done & Archive"}, response=True)
    assert bodies(mock_trello, "PUT", f"{API}/cards/c1") == [{"idList": "l_done", "pos": "top"}]
    assert result == INFO


async def test_move_card_by_url(hass, loaded, mock_trello):
    _card_get(mock_trello, card_id="abc12345")
    mock_trello.put(f"{API}/cards/abc12345", payload=card("abc12345", "X", "l_done", 1))
    await _call(hass, "move_card", {"card": "https://trello.com/c/abc12345/7-x", "list": "Done & Archive"})
    assert len(calls(mock_trello, "GET", f"{API}/cards/abc12345")) == 1


async def test_move_card_by_entity_and_name(hass, loaded, mock_trello):
    mock_trello.put(f"{API}/cards/c1", payload=card("c1", "Buy stamps", "l_sam", 1))
    await _call(hass, "move_card", {"entity_id": "todo.alex", "card_name": "buy stamps", "list": "Sam", "position": "bottom"})
    assert bodies(mock_trello, "PUT", f"{API}/cards/c1") == [{"idList": "l_sam", "pos": "bottom"}]


async def test_move_card_to_other_board(hass, load_entry, mock_trello):
    """Review Focus 5: both boards refresh."""
    await load_entry(board_subentry(), board_subentry("b2", "Projects", "archive", None))
    _card_get(mock_trello)
    venues = LISTS_B2[0]["id"]
    mock_trello.put(f"{API}/cards/c1", payload=card("c1", "Buy stamps", venues, 1))
    b1, b2 = _board_requests(mock_trello, "b1"), _board_requests(mock_trello, "b2")
    await _call(hass, "move_card", {"card": "c1", "board": "projects", "list": "Venues"})
    assert bodies(mock_trello, "PUT", f"{API}/cards/c1") == [{"idList": venues, "pos": "top", "idBoard": "b2"}]
    assert _board_requests(mock_trello, "b1") == b1 + 1
    assert _board_requests(mock_trello, "b2") == b2 + 1


async def test_card_ref_both_or_neither_invalid(hass, loaded):
    with pytest.raises(vol.Invalid):
        await _call(hass, "archive_card", {"card": "c1", "entity_id": "todo.alex", "card_name": "x"})
    with pytest.raises(vol.Invalid):
        await _call(hass, "archive_card", {})


async def test_card_not_found(hass, loaded, mock_trello):
    mock_trello.get(card_url("zz"), status=404, body="not found")
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "archive_card", {"card": "zz"})
    assert info.value.translation_key == "card_not_found"


async def test_card_on_unconfigured_board(hass, loaded, mock_trello):
    _card_get(mock_trello, board_id="elsewhere")
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "archive_card", {"card": "c1"})
    assert info.value.translation_key == "card_board_not_configured"


async def test_card_name_ambiguous(hass, load_entry):
    await load_entry(b1=board(cards=[card("d1", "Dup", "l_alex", 1), card("d2", "dup", "l_alex", 2)]))
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "archive_card", {"entity_id": "todo.alex", "card_name": "Dup"})
    assert info.value.translation_key == "card_ambiguous"


async def test_not_a_trello_list(hass, loaded):
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "archive_card", {"entity_id": "todo.shopping_list", "card_name": "x"})
    assert info.value.translation_key == "not_a_trello_list"


@pytest.mark.parametrize(("service", "closed"), [("archive_card", True), ("restore_card", False)])
async def test_archive_and_restore(hass, loaded, mock_trello, service, closed):
    _card_get(mock_trello)
    mock_trello.put(f"{API}/cards/c1", payload=card("c1", "Buy stamps", "l_alex", 1))
    await _call(hass, service, {"card": "c1"})
    assert bodies(mock_trello, "PUT", f"{API}/cards/c1") == [{"closed": closed}]


@pytest.mark.parametrize(("keep", "source"), [("all", "all"), ("none", ""), ("checklists", "checklists")])
async def test_copy_card(hass, loaded, mock_trello, keep, source):
    mock_trello.post(f"{API}/cards", payload=card("copy", "Buy stamps", "l_sam", 5))
    result = await _call(hass, "copy_card", {"entity_id": "todo.alex", "card_name": "Buy stamps", "list": "Sam", "keep": keep}, response=True)
    assert bodies(mock_trello, "POST", f"{API}/cards") == [
        {"idList": "l_sam", "idCardSource": "c1", "keepFromSource": source, "pos": "bottom"}
    ]
    assert result["card_id"] == "copy"


async def test_set_due_date_is_local_midnight(hass, loaded, mock_trello):
    """Review Focus 4."""
    mock_trello.put(f"{API}/cards/c3", payload=card("c3", "Pay invoice", "l_alex", 1))
    await _call(hass, "set_due", {"entity_id": "todo.alex", "card_name": "Pay invoice", "due": "2026-10-20"})
    assert bodies(mock_trello, "PUT", f"{API}/cards/c3") == [{"due": "2026-10-20T05:00:00+00:00"}]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"due": "2026-10-20 09:15:00"}, {"due": "2026-10-20T14:15:00+00:00"}),
        ({"due": None}, {"due": None}),
        ({"due": ""}, {"due": None}),
        ({"due_complete": True}, {"dueComplete": True}),
    ],
)
async def test_set_due_variants(hass, loaded, mock_trello, data, expected):
    mock_trello.put(f"{API}/cards/c3", payload=card("c3", "Pay invoice", "l_alex", 1))
    await _call(hass, "set_due", {"entity_id": "todo.alex", "card_name": "Pay invoice", **data})
    assert bodies(mock_trello, "PUT", f"{API}/cards/c3") == [expected]


async def test_set_due_needs_something(hass, loaded):
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "set_due", {"entity_id": "todo.alex", "card_name": "Pay invoice"})
    assert info.value.translation_key == "due_required"


async def test_response_only_when_asked(hass, loaded, mock_trello):
    _card_get(mock_trello)
    mock_trello.put(f"{API}/cards/c1", payload=card("c1", "Buy stamps", "l_alex", 1))
    assert await _call(hass, "archive_card", {"card": "c1"}) is None


async def test_401_starts_reauth(hass, loaded, mock_trello):
    _card_get(mock_trello)
    mock_trello.put(f"{API}/cards/c1", status=401, body="invalid token")
    with pytest.raises(HomeAssistantError) as info:
        await _call(hass, "archive_card", {"card": "c1"})
    assert info.value.translation_key == "auth_failed"
    await hass.async_block_till_done()
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == ["reauth"]


async def test_server_error_reason_code(hass, loaded, mock_trello):
    _card_get(mock_trello)
    mock_trello.put(f"{API}/cards/c1", status=503, body="secret-ish upstream text")
    with pytest.raises(HomeAssistantError) as info:
        await _call(hass, "archive_card", {"card": "c1"})
    assert info.value.translation_key == "api_error"
    assert info.value.translation_placeholders == {"error": "connection"}


async def test_add_comment(hass, loaded, mock_trello):
    mock_trello.post(f"{API}/cards/c1/actions/comments", payload={"id": "act1"})
    result = await _call(hass, "add_comment", {"entity_id": "todo.alex", "card_name": "Buy stamps", "text": "On it 🚀"}, response=True)
    assert bodies(mock_trello, "POST", f"{API}/cards/c1/actions/comments") == [{"text": "On it 🚀"}]
    assert result["comment_id"] == "act1"
    assert result["card_id"] == "c1"
    assert result["list_name"] == "Alex"


@pytest.mark.parametrize("label", ["Urgent", "lb1"])
async def test_add_label(hass, loaded, mock_trello, label):
    mock_trello.post(f"{API}/cards/c1/idLabels", payload=[])
    await _call(hass, "add_label", {"entity_id": "todo.alex", "card_name": "Buy stamps", "label": label})
    assert bodies(mock_trello, "POST", f"{API}/cards/c1/idLabels") == [{"value": "lb1"}]


async def test_add_label_already_present_is_success(hass, loaded, mock_trello):
    """Review Focus 2."""
    mock_trello.post(f"{API}/cards/c1/idLabels", status=400, body="that label is already on the card")
    await _call(hass, "add_label", {"entity_id": "todo.alex", "card_name": "Buy stamps", "label": "Urgent"})


async def test_remove_label(hass, loaded, mock_trello):
    mock_trello.delete(f"{API}/cards/c1/idLabels/lb2", payload=[])
    await _call(hass, "remove_label", {"entity_id": "todo.alex", "card_name": "Buy stamps", "label": "green"})
    assert len(calls(mock_trello, "DELETE", f"{API}/cards/c1/idLabels/lb2")) == 1


async def test_unknown_label(hass, loaded):
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "add_label", {"entity_id": "todo.alex", "card_name": "Buy stamps", "label": "purple"})
    assert info.value.translation_key == "label_not_found"


async def test_assign_member_by_username(hass, loaded, mock_trello):
    mock_trello.post(f"{API}/cards/c1/idMembers", payload=[])
    await _call(hass, "assign_member", {"entity_id": "todo.alex", "card_name": "Buy stamps", "member": "alex"})
    assert bodies(mock_trello, "POST", f"{API}/cards/c1/idMembers") == [{"value": "m2"}]


async def test_remove_member_not_present_is_success(hass, loaded, mock_trello):
    """Review Focus 2."""
    mock_trello.delete(f"{API}/cards/c1/idMembers/m1", status=400, body="member is not on the card")
    await _call(hass, "unassign_member", {"entity_id": "todo.alex", "card_name": "Buy stamps", "member": "Sam"})


async def test_unassign_member_404_is_success(hass, loaded, mock_trello):
    mock_trello.delete(f"{API}/cards/c1/idMembers/m1", status=404, body="not found")
    await _call(hass, "unassign_member", {"entity_id": "todo.alex", "card_name": "Buy stamps", "member": "Sam"})


async def test_add_member_server_error_still_fails(hass, loaded, mock_trello):
    mock_trello.post(f"{API}/cards/c1/idMembers", status=500)
    with pytest.raises(HomeAssistantError):
        await _call(hass, "assign_member", {"entity_id": "todo.alex", "card_name": "Buy stamps", "member": "alex"})


GROCERIES = [{"id": "cl1", "name": "Groceries", "checkItems": [
    {"id": "ci1", "name": "Milk", "state": "incomplete"},
    {"id": "ci2", "name": "Eggs", "state": "complete"},
]}]


async def test_add_checklist_item_creates_checklist(hass, loaded, mock_trello):
    mock_trello.get(checklists_url("c1"), payload=[])
    mock_trello.post(f"{API}/cards/c1/checklists", payload={"id": "clN", "name": "Checklist"})
    mock_trello.post(f"{API}/checklists/clN/checkItems", payload={"id": "ciN"})
    result = await _call(hass, "add_checklist_item", {"entity_id": "todo.alex", "card_name": "Buy stamps", "item": "Milk"}, response=True)
    assert bodies(mock_trello, "POST", f"{API}/cards/c1/checklists") == [{"name": "Checklist"}]
    assert bodies(mock_trello, "POST", f"{API}/checklists/clN/checkItems") == [{"name": "Milk"}]
    assert (result["checklist_id"], result["item_id"]) == ("clN", "ciN")


async def test_add_checklist_item_reuses_checklist(hass, loaded, mock_trello):
    mock_trello.get(checklists_url("c1"), payload=GROCERIES)
    mock_trello.post(f"{API}/checklists/cl1/checkItems", payload={"id": "ci3"})
    await _call(hass, "add_checklist_item", {"entity_id": "todo.alex", "card_name": "Buy stamps", "checklist": "groceries", "item": "Bread"})
    assert calls(mock_trello, "POST", f"{API}/cards/c1/checklists") == []


@pytest.mark.parametrize(("item", "checked", "item_id", "state"), [
    ("milk", True, "ci1", "complete"),
    ("ci2", False, "ci2", "incomplete"),
])
async def test_set_checklist_item(hass, loaded, mock_trello, item, checked, item_id, state):
    mock_trello.get(checklists_url("c1"), payload=GROCERIES)
    mock_trello.put(f"{API}/cards/c1/checkItem/{item_id}", payload={"id": item_id})
    await _call(hass, "set_checklist_item", {"entity_id": "todo.alex", "card_name": "Buy stamps", "item": item, "checked": checked})
    assert bodies(mock_trello, "PUT", f"{API}/cards/c1/checkItem/{item_id}") == [{"state": state}]


async def test_set_checklist_item_ambiguous(hass, loaded, mock_trello):
    """Review Focus 3."""
    two = [*GROCERIES, {"id": "cl2", "name": "Party", "checkItems": [{"id": "ci9", "name": "Milk", "state": "incomplete"}]}]
    mock_trello.get(checklists_url("c1"), payload=two)
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "set_checklist_item", {"entity_id": "todo.alex", "card_name": "Buy stamps", "item": "Milk", "checked": True})
    assert info.value.translation_key == "checklist_item_ambiguous"


async def test_set_checklist_item_scoped_by_checklist(hass, loaded, mock_trello):
    two = [*GROCERIES, {"id": "cl2", "name": "Party", "checkItems": [{"id": "ci9", "name": "Milk", "state": "incomplete"}]}]
    mock_trello.get(checklists_url("c1"), payload=two)
    mock_trello.put(f"{API}/cards/c1/checkItem/ci9", payload={"id": "ci9"})
    await _call(hass, "set_checklist_item", {"entity_id": "todo.alex", "card_name": "Buy stamps", "checklist": "Party", "item": "Milk", "checked": True})
    assert len(calls(mock_trello, "PUT", f"{API}/cards/c1/checkItem/ci9")) == 1


@pytest.mark.parametrize(("data", "key"), [
    ({"item": "Butter"}, "checklist_item_not_found"),
    ({"checklist": "Nope", "item": "Milk"}, "checklist_not_found"),
])
async def test_set_checklist_item_missing(hass, loaded, mock_trello, data, key):
    mock_trello.get(checklists_url("c1"), payload=GROCERIES)
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "set_checklist_item", {"entity_id": "todo.alex", "card_name": "Buy stamps", "checked": True, **data})
    assert info.value.translation_key == key


async def test_malformed_card_reference_is_card_not_found(hass, loaded, mock_trello):
    """Final review I1: Trello answers 400 "invalid id" for a malformed id, which is still "card not found"."""
    mock_trello.get(card_url("buy-stamps"), status=400, body="invalid id")
    with pytest.raises(ServiceValidationError) as info:
        await _call(hass, "archive_card", {"card": "buy-stamps"})
    assert info.value.translation_key == "card_not_found"


async def test_card_name_found_after_fresh_refresh(hass, loaded, mock_trello):
    """Final review I2: a card created moments ago isn't in the cache yet; look again after a forced refresh."""
    mock_trello.clear()
    fresh = board(cards=[*[c for c in board()["cards"]], card("new", "Just created", "l_alex", 99)])
    mock_trello.get(BOARD_URL, payload=fresh, repeat=True)
    mock_trello.post(f"{API}/cards/new/actions/comments", payload={"id": "act9"})
    await _call(hass, "add_comment", {"entity_id": "todo.alex", "card_name": "Just created", "text": "hi"})
    assert bodies(mock_trello, "POST", f"{API}/cards/new/actions/comments") == [{"text": "hi"}]

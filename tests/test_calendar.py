"""Tests for the per-board due-date calendar."""
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.calendar import DATA_COMPONENT, CalendarEntityFeature
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.trello_todo.const import DOMAIN

from .common import API, BOARD_URL, board, bodies, calls, card, subentry_id

CAL = "calendar.household_due_dates"
NOW = "2026-10-14T15:00:00+00:00"  # 10:00 in Chicago

CARDS = [
    card("c1", "Pay bill", "l_alex", 1, desc="Water", due="2026-10-15T22:00:00.000Z"),
    card("c2", "Paint fence", "l_sam", 1, due="2026-10-16T22:00:00.000Z", start="2026-10-16T14:00:00.000Z"),
    card("c3", "Bad start", "l_sam", 2, due="2026-10-17T22:00:00.000Z", start="2026-10-18T14:00:00.000Z"),
    card("c4", "Ticked", "l_alex", 2, due="2026-10-13T15:00:00.000Z", due_complete=True),
    card("c5", "Filed", "l_done", 1, due="2026-10-12T15:00:00.000Z"),
    card("c6", "No due", "l_alex", 3),
    card("c7", "Garbled", "l_alex", 4, due="garbage"),
]


@pytest.fixture(autouse=True)
def _now(freezer):
    freezer.move_to(NOW)


@pytest.fixture
async def cal(load_entry):
    return await load_entry(b1=board(cards=CARDS))


def _entity(hass):
    return hass.data[DATA_COMPONENT].get_entity(CAL)


async def _events(hass, start="2026-10-01T00:00:00+00:00", end="2026-11-01T00:00:00+00:00"):
    resp = await hass.services.async_call(
        "calendar", "get_events", {"entity_id": CAL, "start_date_time": start, "end_date_time": end},
        blocking=True, return_response=True,
    )
    return resp[CAL]["events"]


def _by_summary(events):
    return {e["summary"]: e for e in events}


async def test_entity_on_board_device(hass, cal):
    reg = er.async_get(hass).async_get(CAL)
    assert reg.unique_id == "b1_calendar"
    assert reg.config_subentry_id == subentry_id(cal, "b1")
    state = hass.states.get(CAL)
    assert state.attributes["supported_features"] == (
        CalendarEntityFeature.UPDATE_EVENT | CalendarEntityFeature.DELETE_EVENT
    )


async def test_events_built_from_cards(hass, cal):
    events = _by_summary(await _events(hass))
    assert set(events) == {"Pay bill", "Paint fence", "Bad start", "✓ Ticked", "✓ Filed"}
    pay = events["Pay bill"]
    assert dt_util.parse_datetime(pay["end"]) == dt_util.parse_datetime("2026-10-15T22:00:00+00:00")
    assert dt_util.parse_datetime(pay["start"]) == dt_util.parse_datetime("2026-10-15T21:30:00+00:00")
    assert pay["description"] == "Water\n\nList: Alex\nhttps://trello.com/c/c1"
    fence = events["Paint fence"]
    assert dt_util.parse_datetime(fence["start"]) == dt_util.parse_datetime("2026-10-16T14:00:00+00:00")


async def test_start_after_due_falls_back_to_default(hass, cal):
    bad = _by_summary(await _events(hass))["Bad start"]
    assert dt_util.parse_datetime(bad["start"]) == dt_util.parse_datetime("2026-10-17T21:30:00+00:00")


async def test_range_filter(hass, cal):
    events = await _events(hass, "2026-10-15T00:00:00+00:00", "2026-10-16T00:00:00+00:00")
    assert [e["summary"] for e in events] == ["Pay bill"]


async def test_state_and_next_event(hass, cal, freezer):
    state = hass.states.get(CAL)
    assert state.state == "off"
    assert state.attributes["message"] == "Pay bill"
    freezer.move_to("2026-10-15T21:45:00+00:00")
    cal.runtime_data.boards[subentry_id(cal, "b1")].async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(CAL).state == "on"


async def test_update_via_websocket(hass, cal, mock_trello, hass_ws_client):
    mock_trello.put(f"{API}/cards/c1", payload={})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "calendar/event/update", "entity_id": CAL, "uid": "c1",
        "event": {"summary": "Pay bill", "dtstart": "2026-10-20T09:00:00-05:00", "dtend": "2026-10-20T09:30:00-05:00"},
    })
    assert (await client.receive_json())["success"]
    assert bodies(mock_trello, "PUT", f"{API}/cards/c1") == [{"due": "2026-10-20T14:30:00+00:00", "start": None}]


async def test_update_longer_event_sets_start(hass, cal, mock_trello):
    mock_trello.put(f"{API}/cards/c1", payload={})
    await _entity(hass).async_update_event(
        "c1",
        {"dtstart": dt_util.parse_datetime("2026-10-20T13:00:00+00:00"),
         "dtend": dt_util.parse_datetime("2026-10-20T14:30:00+00:00")},
    )
    assert bodies(mock_trello, "PUT", f"{API}/cards/c1") == [
        {"due": "2026-10-20T14:30:00+00:00", "start": "2026-10-20T13:00:00+00:00"}
    ]


@pytest.mark.parametrize(
    "event",
    [
        {"dtstart": dt_util.parse_datetime("2026-10-20T14:30:00+00:00"),
         "dtend": dt_util.parse_datetime("2026-10-20T13:00:00+00:00")},
        {"dtstart": dt_util.parse_datetime("2026-10-20T14:30:00+00:00"),
         "dtend": dt_util.parse_datetime("2026-10-20T14:30:00+00:00")},
    ],
)
async def test_update_bad_times_rejected(hass, cal, mock_trello, event):
    with pytest.raises(ServiceValidationError) as info:
        await _entity(hass).async_update_event("c1", event)
    assert info.value.translation_key == "invalid_event_times"
    assert calls(mock_trello, "PUT", f"{API}/cards/c1") == []


async def test_update_all_day_rejected(hass, cal, mock_trello):
    with pytest.raises(ServiceValidationError) as info:
        await _entity(hass).async_update_event("c1", {"dtstart": date(2026, 10, 20), "dtend": date(2026, 10, 21)})
    assert info.value.translation_key == "invalid_event_times"
    assert calls(mock_trello, "PUT", f"{API}/cards/c1") == []


async def test_update_unknown_uid(hass, cal):
    with pytest.raises(ServiceValidationError) as info:
        await _entity(hass).async_update_event(
            "nope", {"dtstart": dt_util.utcnow(), "dtend": dt_util.utcnow() + timedelta(hours=1)}
        )
    assert info.value.translation_key == "card_not_found"


async def test_update_mirrors_locally(hass, cal, mock_trello):
    for coordinator in cal.runtime_data.boards.values():
        coordinator.async_request_refresh = AsyncMock()
    mock_trello.put(f"{API}/cards/c1", payload={})
    await _entity(hass).async_update_event(
        "c1",
        {"dtstart": dt_util.parse_datetime("2026-10-20T14:00:00+00:00"),
         "dtend": dt_util.parse_datetime("2026-10-20T14:30:00+00:00")},
    )
    events = await _events(hass, "2026-10-20T00:00:00+00:00", "2026-10-21T00:00:00+00:00")
    assert [e["summary"] for e in events] == ["Pay bill"]


async def test_delete_clears_due_and_start(hass, cal, mock_trello, hass_ws_client):
    mock_trello.put(f"{API}/cards/c2", payload={})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "calendar/event/delete", "entity_id": CAL, "uid": "c2"})
    assert (await client.receive_json())["success"]
    assert bodies(mock_trello, "PUT", f"{API}/cards/c2") == [{"due": None, "start": None}]


@pytest.mark.parametrize(
    ("status", "error", "key"),
    [(404, ServiceValidationError, "card_not_found"), (429, HomeAssistantError, "api_error")],
)
async def test_write_errors(hass, cal, mock_trello, status, error, key):
    mock_trello.put(f"{API}/cards/c1", status=status, body="nope")
    with pytest.raises(error) as info:
        await _entity(hass).async_delete_event("c1")
    assert info.value.translation_key == key


async def test_auth_error_starts_reauth(hass, cal, mock_trello):
    mock_trello.put(f"{API}/cards/c1", status=401, body="invalid token")
    with pytest.raises(HomeAssistantError) as info:
        await _entity(hass).async_delete_event("c1")
    assert info.value.translation_key == "auth_failed"
    await hass.async_block_till_done()
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN))


async def test_unavailable_when_board_inaccessible(hass, cal, mock_trello):
    mock_trello.clear()
    mock_trello.get(BOARD_URL, status=404, body="board not found")
    await cal.runtime_data.boards[subentry_id(cal, "b1")].async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(CAL).state == "unavailable"

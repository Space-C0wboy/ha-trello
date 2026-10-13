"""Tests for the due-date summary sensors."""
import time

import pytest
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .common import BOARD_URL, board, card, subentry_id

NOW = "2026-10-14T15:00:00+00:00"  # Wed 10:00 in Chicago; local midnight tonight = 2026-10-15T05:00Z
OVERDUE, TODAY, SOON, NEXT = (f"sensor.household_{k}" for k in ("overdue", "due_today", "due_soon", "next_due"))

CARDS = [
    card("o1", "Late", "l_alex", 1, due="2026-10-13T15:00:00.000Z", members=["m1"]),
    card("t1", "This afternoon", "l_alex", 2, due="2026-10-14T20:00:00.000Z", members=["m1", "m2"]),
    card("t2", "Before midnight", "l_sam", 1, due="2026-10-15T04:59:00.000Z"),
    card("s1", "At midnight", "l_sam", 2, due="2026-10-15T05:00:00.000Z"),
    card("s3", "Inside the week", "l_sam", 3, due="2026-10-21T14:59:00.000Z"),
    card("s2", "Week edge", "l_sam", 4, due="2026-10-21T15:00:00.000Z"),
    card("x1", "Ticked", "l_alex", 3, due="2026-10-13T15:00:00.000Z", due_complete=True),
    card("x2", "Filed", "l_done", 1, due="2026-10-13T15:00:00.000Z"),
    card("n1", "Whenever", "l_alex", 4),
    card("g1", "Garbled", "l_alex", 5, due="garbage"),
]


@pytest.fixture(autouse=True)
def _now(freezer):
    freezer.move_to(NOW)


# The tests move the clock hours ahead; keep the sign-in valid so refreshes don't fail on token expiry.
LONG_TOKEN = time.time() + 30 * 86400


@pytest.fixture
async def sensors(load_entry):
    return await load_entry(b1=board(cards=CARDS), expires_at=LONG_TOKEN)


async def _tick(hass):
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_entities_on_board_device(hass, sensors):
    registry = er.async_get(hass)
    for entity_id, key in ((OVERDUE, "overdue"), (TODAY, "due_today"), (SOON, "due_soon"), (NEXT, "next_due")):
        reg = registry.async_get(entity_id)
        assert reg.unique_id == f"b1_{key}"
        assert reg.config_subentry_id == subentry_id(sensors, "b1")


async def test_counts_and_windows(hass, sensors):
    assert hass.states.get(OVERDUE).state == "1"
    assert hass.states.get(TODAY).state == "2"  # t1, t2 (23:59 local)
    assert hass.states.get(SOON).state == "2"  # s1 (exactly midnight), s3; s2 (exactly now + 7 days) excluded


async def test_by_member_and_cards(hass, sensors):
    today = hass.states.get(TODAY).attributes
    assert today["by_member"] == {"Alex": 1, "Sam": 1, "Unassigned": 1}
    assert today["cards"] == [
        {"name": "This afternoon", "due": "2026-10-14T20:00:00+00:00", "list": "Alex", "members": ["Sam", "Alex"]},
        {"name": "Before midnight", "due": "2026-10-15T04:59:00+00:00", "list": "Sam", "members": []},
    ]
    assert hass.states.get(OVERDUE).attributes["by_member"] == {"Sam": 1}


async def test_next_due(hass, sensors):
    state = hass.states.get(NEXT)
    assert dt_util.parse_datetime(state.state) == dt_util.parse_datetime("2026-10-14T20:00:00+00:00")
    assert state.attributes["card"] == "This afternoon"
    assert state.attributes["list"] == "Alex"
    assert state.attributes["members"] == ["Sam", "Alex"]
    assert state.attributes["url"] == "https://trello.com/c/t1"


async def test_next_due_unknown_when_nothing_ahead(hass, load_entry):
    await load_entry(b1=board(cards=[card("o1", "Late", "l_alex", 1, due="2026-10-13T15:00:00.000Z")]))
    assert hass.states.get(NEXT).state == "unknown"
    assert hass.states.get(TODAY).attributes["by_member"] == {}


def _no_refresh_schedule(entry):
    """Only the sensors' own one-minute tick may recompute (no board refresh on the clock jump)."""
    for coordinator in entry.runtime_data.boards.values():
        coordinator.update_interval = None
        coordinator._unschedule_refresh()


async def test_tick_flips_card_to_overdue(hass, sensors, freezer):
    _no_refresh_schedule(sensors)
    freezer.move_to("2026-10-14T20:00:00+00:00")  # exactly t1's due time
    await _tick(hass)
    assert hass.states.get(OVERDUE).state == "2"
    assert hass.states.get(TODAY).state == "1"
    assert hass.states.get(NEXT).attributes["card"] == "Before midnight"


async def test_local_day_differs_from_utc_day(hass, load_entry, freezer):
    freezer.move_to("2026-10-15T03:00:00+00:00")  # Wed 22:00 in Chicago, already Thursday in UTC
    await load_entry(b1=board(cards=[
        card("a", "Late tonight", "l_alex", 1, due="2026-10-15T04:30:00.000Z"),  # Wed 23:30 local
        card("b", "Early tomorrow", "l_alex", 2, due="2026-10-15T06:00:00.000Z"),  # Thu 01:00 local
    ]), expires_at=LONG_TOKEN)
    _no_refresh_schedule(hass.config_entries.async_entries("trello_todo")[0])
    assert hass.states.get(TODAY).state == "1"
    assert hass.states.get(SOON).state == "1"
    freezer.move_to("2026-10-15T05:30:00+00:00")  # local midnight passed; "a" is now overdue, "b" due today
    await _tick(hass)
    assert hass.states.get(OVERDUE).state == "1"
    assert hass.states.get(TODAY).state == "1"
    assert hass.states.get(SOON).state == "0"


async def test_cards_capped_at_20(hass, load_entry):
    late = [card(f"o{i:02}", f"Late {i:02}", "l_alex", i, due=f"2026-10-13T{i % 24:02}:00:00.000Z") for i in range(25)]
    await load_entry(b1=board(cards=late))
    attrs = hass.states.get(OVERDUE).attributes
    assert hass.states.get(OVERDUE).state == "25"
    assert len(attrs["cards"]) == 20
    assert [c["due"] for c in attrs["cards"]] == sorted(c["due"] for c in attrs["cards"])


async def test_refresh_updates_counts(hass, sensors, mock_trello):
    mock_trello.clear()
    mock_trello.get(BOARD_URL, payload=board(cards=CARDS[:1]), repeat=True)
    await sensors.runtime_data.boards[subentry_id(sensors, "b1")].async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(TODAY).state == "0"
    assert hass.states.get(OVERDUE).state == "1"


async def test_unavailable_when_board_inaccessible(hass, sensors, mock_trello):
    mock_trello.clear()
    mock_trello.get(BOARD_URL, status=404, body="board not found")
    await sensors.runtime_data.boards[subentry_id(sensors, "b1")].async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(OVERDUE).state == "unavailable"


# --- small-issue fixes ----------------------------------------------------------------------------


async def test_unchanged_tick_writes_nothing(hass, load_entry, freezer):
    # Without "Week edge": it would legitimately enter the 7-day window as the clock moves.
    entry = await load_entry(b1=board(cards=[c for c in CARDS if c["id"] != "s2"]), expires_at=LONG_TOKEN)
    _no_refresh_schedule(entry)
    written = []

    @callback
    def _ours(event_data) -> bool:
        return event_data["entity_id"].startswith("sensor.household_")

    @callback
    def _seen(event) -> None:
        written.append(event.data["entity_id"])

    for event_type in ("state_changed", "state_reported"):  # reported = written again with the same state
        hass.bus.async_listen(event_type, _seen, event_filter=_ours)
    freezer.tick(65)
    await _tick(hass)
    assert written == []


async def test_buckets_computed_once_per_board(hass, sensors, freezer):
    from unittest.mock import patch

    from custom_components.trello_todo import sensor as sensor_module

    _no_refresh_schedule(sensors)
    with patch.object(sensor_module, "due_buckets", wraps=sensor_module.due_buckets) as spy:
        freezer.tick(65)
        await _tick(hass)
        assert spy.call_count == 1  # one tick, four sensors
        spy.reset_mock()
        await sensors.runtime_data.boards[subentry_id(sensors, "b1")].async_refresh()
        await hass.async_block_till_done()
        assert spy.call_count == 1  # one refresh, four sensors

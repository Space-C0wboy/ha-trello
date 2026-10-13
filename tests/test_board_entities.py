"""The calendar and the due-date sensors follow board sub-entries: added, removed, or late at startup."""
from homeassistant.config_entries import SOURCE_USER
from homeassistant.helpers import entity_registry as er

from custom_components.trello_todo.const import DOMAIN

from .common import (
    BOARD_URL,
    BOARDS,
    BOARDS_URL,
    LISTS_B2,
    actions_url,
    board,
    board_subentry,
    board_url,
    lists_url,
    make_entry,
    second_board,
    subentry_id,
)

PER_BOARD = (
    ("calendar", "calendar"),
    ("sensor", "overdue"),
    ("sensor", "due_today"),
    ("sensor", "due_soon"),
    ("sensor", "next_due"),
)


def _entity_ids(hass, board_id: str) -> list[str | None]:
    registry = er.async_get(hass)
    return [registry.async_get_entity_id(domain, DOMAIN, f"{board_id}_{key}") for domain, key in PER_BOARD]


async def test_board_added_gets_calendar_and_sensors(hass, loaded, mock_trello):
    mock_trello.get(BOARDS_URL, payload=BOARDS)
    mock_trello.get(lists_url("b2"), payload=LISTS_B2)
    mock_trello.get(board_url("b2"), payload=second_board(), repeat=True)
    mock_trello.get(actions_url("b2"), payload=[], repeat=True)
    result = await hass.config_entries.subentries.async_init((loaded.entry_id, "board"), context={"source": SOURCE_USER})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"board": "b2"})
    await hass.config_entries.subentries.async_configure(result["flow_id"], {"done_behavior": "archive"})
    await hass.async_block_till_done()
    ids = _entity_ids(hass, "b2")
    assert ids == [
        "calendar.projects_due_dates",
        "sensor.projects_overdue",
        "sensor.projects_due_today",
        "sensor.projects_due_soon",
        "sensor.projects_next_due",
    ]
    assert all(hass.states.get(entity_id) is not None for entity_id in ids)


async def test_board_removed_drops_calendar_and_sensors(hass, load_entry):
    entry = await load_entry(board_subentry(), board_subentry("b2", "Projects", "archive", None))
    assert None not in _entity_ids(hass, "b2")
    hass.config_entries.async_remove_subentry(entry, subentry_id(entry, "b2"))
    await hass.async_block_till_done()
    assert _entity_ids(hass, "b2") == [None] * len(PER_BOARD)
    assert hass.states.get("calendar.projects_due_dates") is None
    assert None not in _entity_ids(hass, "b1")


async def test_board_down_at_startup_gets_them_later(hass, setup_credentials, mock_trello):
    entry = make_entry(hass, board_subentry(), board_subentry("b2", "Projects", "archive", None))
    mock_trello.get(BOARD_URL, payload=board(), repeat=True)
    mock_trello.get(actions_url("b1"), payload=[], repeat=True)
    mock_trello.get(actions_url("b2"), payload=[], repeat=True)
    mock_trello.get(board_url("b2"), status=500)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _entity_ids(hass, "b2") == [None] * len(PER_BOARD)

    mock_trello.get(board_url("b2"), payload=second_board())
    await entry.runtime_data.boards[subentry_id(entry, "b2")].async_refresh()
    await hass.async_block_till_done()
    assert None not in _entity_ids(hass, "b2")
    assert hass.states.get("sensor.projects_overdue").state == "0"

"""Tests for adding, reconfiguring and removing board sub-entries."""
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType

from .common import (
    BOARDS,
    BOARDS_URL,
    LISTS,
    LISTS_B2,
    board_subentry,
    board_url,
    lists_url,
    subentry_id,
    second_board,
)


async def _start(hass, entry):
    return await hass.config_entries.subentries.async_init((entry.entry_id, "board"), context={"source": SOURCE_USER})


def _select_values(result, field: str) -> list[str]:
    schema = result["data_schema"].schema
    key = next(k for k in schema if k == field)
    return [option["value"] for option in schema[key].config["options"]]


async def test_add_board(hass, loaded, mock_trello):
    mock_trello.get(BOARDS_URL, payload=BOARDS)
    mock_trello.get(lists_url("b2"), payload=LISTS_B2)
    mock_trello.get(board_url("b2"), payload=second_board(), repeat=True)
    result = await _start(hass, loaded)
    assert result["step_id"] == "user"
    assert _select_values(result, "board") == ["b2"]  # b1 is already configured

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"board": "b2"})
    assert result["step_id"] == "done"
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"done_behavior": "archive"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    sid = subentry_id(loaded, "b2")
    assert loaded.subentries[sid].title == "Projects"
    assert dict(loaded.subentries[sid].data) == {"board_id": "b2", "done_behavior": "archive", "done_list": None}
    assert loaded.runtime_data.boards[sid].board_id == "b2"


async def test_add_board_requires_done_list_for_move(hass, loaded, mock_trello):
    mock_trello.get(BOARDS_URL, payload=BOARDS)
    mock_trello.get(lists_url("b2"), payload=LISTS_B2)
    result = await _start(hass, loaded)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"board": "b2"})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"done_behavior": "move"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"done_list": "done_list_required"}


async def test_no_more_boards(hass, loaded, mock_trello):
    mock_trello.get(BOARDS_URL, payload=[BOARDS[0]])
    result = await _start(hass, loaded)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_more_boards"


async def test_add_board_when_entry_not_loaded(hass, entry):
    """Review Focus 3: no runtime_data yet, so abort cleanly."""
    assert entry.state is ConfigEntryState.NOT_LOADED
    result = await _start(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"


async def test_reconfigure_done_settings(hass, loaded, mock_trello):
    mock_trello.get(lists_url("b1"), payload=LISTS)
    sid = subentry_id(loaded, "b1")
    runtime = loaded.runtime_data
    result = await loaded.start_subentry_reconfigure_flow(hass, sid)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"done_behavior": "move_and_archive", "done_list": "l_done"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert loaded.subentries[sid].data["done_behavior"] == "move_and_archive"
    assert loaded.runtime_data is not runtime
    assert loaded.runtime_data.boards[sid].done_behavior == "move_and_archive"


async def test_remove_board(hass, load_entry):
    entry = await load_entry(board_subentry(), board_subentry("b2", "Projects", "archive", None))
    hass.config_entries.async_remove_subentry(entry, subentry_id(entry, "b2"))
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert [c.board_id for c in entry.runtime_data.boards.values()] == ["b1"]


async def test_board_on_other_account_not_offered(hass, loaded, mock_trello):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    MockConfigEntry(
        domain="trello_todo", unique_id="m2", title="Alex", data={},
        subentries_data=[board_subentry("b2", "Projects", "archive", None)],
    ).add_to_hass(hass)
    mock_trello.get(BOARDS_URL, payload=BOARDS)
    result = await _start(hass, loaded)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_more_boards"

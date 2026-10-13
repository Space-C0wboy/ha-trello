"""Tests for setup, per-board coordinators, token handling and error mapping."""
import time

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.trello_todo.const import DOMAIN

from .common import (
    ACCESS_TOKEN,
    API,
    BOARD_URL,
    TOKEN_URL,
    board,
    board_subentry,
    board_url,
    calls,
    make_entry,
    subentry_id,
    second_board,
)

B2_URL = board_url("b2")
SECOND = board_subentry("b2", "Projects", "archive", None)


async def _setup(hass, entry) -> bool:
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return result


def _flow_sources(hass) -> list[str]:
    return [f["context"]["source"] for f in hass.config_entries.flow.async_progress()]


async def test_one_bearer_request_per_board(hass, setup_credentials, mock_trello):
    entry = make_entry(hass, board_subentry(), SECOND)
    mock_trello.get(BOARD_URL, payload=board())
    mock_trello.get(B2_URL, payload=second_board())
    assert await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    for board_id in ("b1", "b2"):
        (req,) = calls(mock_trello, "GET", f"{API}/boards/{board_id}")
        assert req.kwargs["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    b1 = entry.runtime_data.boards[subentry_id(entry, "b1")]
    b2 = entry.runtime_data.boards[subentry_id(entry, "b2")]
    assert (b1.board_id, b1.board_title, b1.done_behavior, b1.done_list) == ("b1", "Household", "move", "l_done")
    assert (b2.board_id, b2.done_behavior, b2.done_list) == ("b2", "archive", None)
    assert b1.data.lists["l_alex"] == "Alex"


async def test_update_interval_from_options(hass, loaded):
    for coordinator in loaded.runtime_data.boards.values():
        assert coordinator.update_interval.total_seconds() == 120


async def test_401_starts_reauth(hass, entry, mock_trello, caplog):
    mock_trello.get(BOARD_URL, status=401, body="invalid token")
    assert not await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert _flow_sources(hass) == [SOURCE_REAUTH]
    assert ACCESS_TOKEN not in caplog.text


async def test_one_board_inaccessible_others_load(hass, setup_credentials, mock_trello):
    entry = make_entry(hass, board_subentry(), SECOND)
    mock_trello.get(BOARD_URL, payload=board(), repeat=True)
    mock_trello.get(B2_URL, status=404, body="board not found")
    assert await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    b2 = entry.runtime_data.boards[subentry_id(entry, "b2")]
    assert not b2.last_update_success
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "board_inaccessible_b2")
    assert issue.translation_key == "board_inaccessible"
    assert issue.translation_placeholders == {"board": "Projects"}

    mock_trello.get(B2_URL, payload=second_board())
    await b2.async_refresh()
    assert b2.last_update_success
    assert ir.async_get(hass).async_get_issue(DOMAIN, "board_inaccessible_b2") is None


async def test_all_boards_down_retries_setup(hass, entry, mock_trello):
    mock_trello.get(BOARD_URL, status=502)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_429_backs_off(hass, loaded, mock_trello):
    coordinator = next(iter(loaded.runtime_data.boards.values()))
    mock_trello.clear()
    mock_trello.get(BOARD_URL, status=429, headers={"Retry-After": "30"})
    await coordinator.async_refresh()
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.last_exception.retry_after == 30


async def test_429_without_header_uses_default_backoff(hass, loaded, mock_trello):
    coordinator = next(iter(loaded.runtime_data.boards.values()))
    mock_trello.clear()
    mock_trello.get(BOARD_URL, status=429)
    await coordinator.async_refresh()
    assert coordinator.last_exception.retry_after == 60


async def test_expired_token_refreshed_before_request(hass, setup_credentials, mock_trello):
    entry = make_entry(hass, expires_at=time.time() - 10)
    mock_trello.post(
        TOKEN_URL,
        payload={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600, "token_type": "Bearer"},
    )
    mock_trello.get(BOARD_URL, payload=board())
    assert await _setup(hass, entry)
    assert entry.data["token"]["access_token"] == "new-access"
    assert entry.data["token"]["refresh_token"] == "new-refresh"
    (req,) = calls(mock_trello, "GET", f"{API}/boards/b1")
    assert req.kwargs["headers"]["Authorization"] == "Bearer new-access"


async def test_refresh_token_rejected_starts_reauth(hass, setup_credentials, mock_trello):
    entry = make_entry(hass, expires_at=time.time() - 10)
    mock_trello.post(TOKEN_URL, status=400, payload={"error": "invalid_grant"})
    assert not await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert _flow_sources(hass) == [SOURCE_REAUTH]


async def test_token_refresh_does_not_reload(hass, loaded):
    """OAuth2Session rewrites entry.data hourly; that must not reload the integration (Review Focus 1)."""
    runtime = loaded.runtime_data
    hass.config_entries.async_update_entry(
        loaded, data={**loaded.data, "token": {**loaded.data["token"], "access_token": "rotated"}}
    )
    await hass.async_block_till_done()
    assert loaded.runtime_data is runtime


async def test_board_settings_change_reloads(hass, loaded):
    runtime = loaded.runtime_data
    sub = loaded.subentries[subentry_id(loaded, "b1")]
    hass.config_entries.async_update_subentry(loaded, sub, data={**sub.data, "done_behavior": "archive", "done_list": None})
    await hass.async_block_till_done()
    assert loaded.state is ConfigEntryState.LOADED
    assert loaded.runtime_data is not runtime
    assert next(iter(loaded.runtime_data.boards.values())).done_behavior == "archive"


async def test_unload(hass, loaded):
    assert await hass.config_entries.async_unload(loaded.entry_id)
    assert loaded.state is ConfigEntryState.NOT_LOADED


async def test_token_endpoint_down_retries_setup(hass, setup_credentials, mock_trello):
    entry = make_entry(hass, expires_at=time.time() - 10)
    mock_trello.post(TOKEN_URL, status=503)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.config_entries.flow.async_progress() == []


async def test_only_board_inaccessible_still_loads(hass, entry, mock_trello):
    """A deleted board is not a transient error: load (board unavailable + repair) instead of retrying forever."""
    mock_trello.get(BOARD_URL, status=404, body="board not found", repeat=True)
    assert await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, "board_inaccessible_b1") is not None


async def test_repair_cleared_when_board_removed(hass, setup_credentials, mock_trello):
    entry = make_entry(hass, board_subentry(), SECOND)
    mock_trello.get(BOARD_URL, payload=board(), repeat=True)
    mock_trello.get(B2_URL, status=404, body="board not found", repeat=True)
    assert await _setup(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "board_inaccessible_b2") is not None
    hass.config_entries.async_remove_subentry(entry, subentry_id(entry, "b2"))
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "board_inaccessible_b2") is None


async def test_board_rename_propagates(hass, loaded, mock_trello):
    from homeassistant.helpers import device_registry as dr

    runtime = loaded.runtime_data
    sid = subentry_id(loaded, "b1")
    mock_trello.clear()
    mock_trello.get(BOARD_URL, payload=board(name="Our Home"), repeat=True)
    await runtime.boards[sid].async_refresh()
    await hass.async_block_till_done()
    assert loaded.runtime_data is runtime  # a rename must not reload
    assert runtime.boards[sid].board_title == "Our Home"
    assert loaded.subentries[sid].title == "Our Home"
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), loaded.entry_id)
    assert device.name == "Our Home"

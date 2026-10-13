"""Tests for the OAuth config flow, reauth and account options."""
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from yarl import URL

from custom_components.trello_todo.const import DOMAIN, OAUTH2_AUTHORIZE

from .common import (
    ACCESS_TOKEN,
    API,
    BOARDS,
    BOARDS_URL,
    LISTS,
    LISTS_B2,
    ME,
    ME_URL,
    TOKEN_URL,
    calls,
    lists_url,
    make_entry,
)

pytestmark = pytest.mark.usefixtures("setup_credentials", "current_request_with_host")


@pytest.fixture
def no_setup():
    with patch("custom_components.trello_todo.async_setup_entry", return_value=True):
        yield


async def _start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def _authorize(hass, hass_client_no_auth, mock_trello, result, access_token=ACCESS_TOKEN):
    """Complete the external OAuth step the way the browser would."""
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    url = URL(result["url"])
    assert str(url.with_query(None)) == OAUTH2_AUTHORIZE
    client = await hass_client_no_auth()
    resp = await client.get(f"/auth/external/callback?code=abcd&state={url.query['state']}")
    assert resp.status == 200
    mock_trello.post(
        TOKEN_URL,
        payload={"access_token": access_token, "refresh_token": "refresh", "expires_in": 3600, "token_type": "Bearer"},
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def test_full_flow_two_boards(hass, hass_client_no_auth, mock_trello, no_setup):
    mock_trello.get(ME_URL, payload=ME)
    mock_trello.get(BOARDS_URL, payload=BOARDS)
    mock_trello.get(lists_url("b1"), payload=LISTS)
    mock_trello.get(lists_url("b2"), payload=LISTS_B2)
    result = await _authorize(hass, hass_client_no_auth, mock_trello, await _start(hass))
    assert result["step_id"] == "boards"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"boards": ["b1", "b2"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Sam"
    assert result["data"]["auth_implementation"] == DOMAIN
    assert result["data"]["token"]["access_token"] == ACCESS_TOKEN
    assert result["options"] == {"scan_interval": 120}
    entry = result["result"]
    assert entry.unique_id == "m1"
    subs = {sub.unique_id: sub for sub in entry.subentries.values()}
    assert subs["b1"].title == "Household"
    assert subs["b1"].subentry_type == "board"
    assert dict(subs["b1"].data) == {"board_id": "b1", "done_behavior": "move", "done_list": "l_done"}
    assert dict(subs["b2"].data) == {"board_id": "b2", "done_behavior": "archive", "done_list": None}
    (me_req,) = calls(mock_trello, "GET", f"{API}/members/me")
    assert me_req.kwargs["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_no_board_selected(hass, hass_client_no_auth, mock_trello, no_setup):
    mock_trello.get(ME_URL, payload=ME)
    mock_trello.get(BOARDS_URL, payload=BOARDS)
    result = await _authorize(hass, hass_client_no_auth, mock_trello, await _start(hass))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"boards": []})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"boards": "no_board_selected"}


async def test_no_boards(hass, hass_client_no_auth, mock_trello, no_setup):
    mock_trello.get(ME_URL, payload=ME)
    mock_trello.get(BOARDS_URL, payload=[])
    result = await _authorize(hass, hass_client_no_auth, mock_trello, await _start(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_boards"


async def test_cannot_connect(hass, hass_client_no_auth, mock_trello, no_setup):
    mock_trello.get(ME_URL, status=503)
    result = await _authorize(hass, hass_client_no_auth, mock_trello, await _start(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_account_already_configured(hass, hass_client_no_auth, mock_trello, no_setup):
    make_entry(hass)
    mock_trello.get(ME_URL, payload=ME)
    result = await _authorize(hass, hass_client_no_auth, mock_trello, await _start(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_same_account(hass, hass_client_no_auth, mock_trello, no_setup):
    entry = make_entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    mock_trello.get(ME_URL, payload=ME)
    result = await _authorize(hass, hass_client_no_auth, mock_trello, result, access_token="reauth-access")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["token"]["access_token"] == "reauth-access"
    assert len(entry.subentries) == 1


async def test_reauth_wrong_account(hass, hass_client_no_auth, mock_trello, no_setup):
    entry = make_entry(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    mock_trello.get(ME_URL, payload={**ME, "id": "someone-else"})
    result = await _authorize(hass, hass_client_no_auth, mock_trello, result, access_token="other-access")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["token"]["access_token"] == ACCESS_TOKEN


async def test_options_flow_sets_interval(hass, loaded):
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"scan_interval": 300})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert loaded.options == {"scan_interval": 300}
    assert next(iter(loaded.runtime_data.boards.values())).update_interval.total_seconds() == 300


async def test_board_configured_on_other_account_not_offered(hass, hass_client_no_auth, mock_trello, no_setup):
    make_entry(hass)  # account m1 already has Household (b1)
    mock_trello.get(ME_URL, payload={**ME, "id": "m2", "fullName": "Alex"})
    mock_trello.get(BOARDS_URL, payload=BOARDS)
    result = await _authorize(hass, hass_client_no_auth, mock_trello, await _start(hass), access_token="alex")
    schema = result["data_schema"].schema
    key = next(k for k in schema if k == "boards")
    assert [o["value"] for o in schema[key].config["options"]] == ["b2"]

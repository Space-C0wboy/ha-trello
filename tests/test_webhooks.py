"""Tests for Trello webhooks: URL choice, lifecycle, signed handler."""
import json
import re
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.trello_todo.const import MODE_FEED, MODE_WEBHOOK
from custom_components.trello_todo.webhooks import signature

from .common import (
    BOARD_URL,
    CLIENT_SECRET,
    WEBHOOKS_URL,
    action,
    actions_url,
    board,
    board_subentry,
    board_url,
    calls,
    make_entry,
    second_board,
    subentry_id,
)

EXTERNAL = "https://ha.example.org"
CREATE_URL = re.compile(r"^https://api\.trello\.com/1/webhooks(\?.*)?$")
CARD = {"id": "c1", "name": "Buy stamps", "shortLink": "abc"}
ALEX_LIST = {"id": "l_alex", "name": "Alex"}


@pytest.fixture
async def external_url(hass):
    await hass.config.async_update(external_url=EXTERNAL)


async def _setup(hass, entry, mock_trello, *, boards=("b1",)):
    if "b1" in boards:
        mock_trello.get(BOARD_URL, payload=board(), repeat=True)
        mock_trello.get(actions_url("b1"), payload=[], repeat=True)
    if "b2" in boards:
        mock_trello.get(board_url("b2"), payload=second_board(), repeat=True)
        mock_trello.get(actions_url("b2"), payload=[], repeat=True)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _coordinator(entry, board_id="b1"):
    return entry.runtime_data.boards[subentry_id(entry, board_id)]


async def test_no_public_url_means_feed(hass, setup_credentials, mock_trello):
    entry = make_entry(hass)
    await _setup(hass, entry, mock_trello)
    assert _coordinator(entry).mode == MODE_FEED
    assert calls(mock_trello, "POST", WEBHOOKS_URL) == []
    assert entry.runtime_data.webhooks.failures == {"b1": "no_public_url"}


async def test_external_url_creates_webhook(hass, setup_credentials, mock_trello, external_url):
    entry = make_entry(hass)
    mock_trello.post(CREATE_URL, payload={"id": "w1", "active": True})
    await _setup(hass, entry, mock_trello)
    state = entry.data["webhooks"]["b1"]
    assert state["trello_webhook_id"] == "w1"
    assert state["callback_url"] == f"{EXTERNAL}/api/webhook/{state['webhook_id']}"
    (req,) = calls(mock_trello, "POST", WEBHOOKS_URL)
    assert req.kwargs["params"]["callbackURL"] == state["callback_url"]
    assert req.kwargs["params"]["idModel"] == "b1"
    coordinator = _coordinator(entry)
    assert coordinator.mode == MODE_WEBHOOK
    assert coordinator.update_interval == timedelta(minutes=15)


async def test_webhook_state_save_does_not_reload(hass, setup_credentials, mock_trello, external_url):
    """Review Focus 1."""
    entry = make_entry(hass)
    mock_trello.post(CREATE_URL, payload={"id": "w1", "active": True})
    await _setup(hass, entry, mock_trello)
    runtime = entry.runtime_data
    await hass.async_block_till_done()
    assert entry.runtime_data is runtime


async def test_cloudhook_preferred(hass, setup_credentials, mock_trello, external_url):
    entry = make_entry(hass)
    mock_trello.post(CREATE_URL, payload={"id": "w1", "active": True})
    with (
        patch("custom_components.trello_todo.webhooks._cloud_active", return_value=True),
        patch("custom_components.trello_todo.webhooks._create_cloudhook", AsyncMock(return_value="https://hooks.nabu.casa/x")),
    ):
        await _setup(hass, entry, mock_trello)
    assert entry.data["webhooks"]["b1"]["callback_url"] == "https://hooks.nabu.casa/x"


async def test_reuses_healthy_webhook(hass, setup_credentials, mock_trello, external_url):
    entry = make_entry(hass)
    url = f"{EXTERNAL}/api/webhook/hid"
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "webhooks": {"b1": {"webhook_id": "hid", "trello_webhook_id": "w1", "callback_url": url}}}
    )
    mock_trello.get(f"{WEBHOOKS_URL}/w1", payload={"id": "w1", "active": True, "callbackURL": url}, repeat=True)
    await _setup(hass, entry, mock_trello)
    assert calls(mock_trello, "POST", WEBHOOKS_URL) == []
    assert _coordinator(entry).mode == MODE_WEBHOOK


async def test_recreates_inactive_webhook(hass, setup_credentials, mock_trello, external_url):
    entry = make_entry(hass)
    url = f"{EXTERNAL}/api/webhook/hid"
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "webhooks": {"b1": {"webhook_id": "hid", "trello_webhook_id": "w1", "callback_url": url}}}
    )
    mock_trello.get(f"{WEBHOOKS_URL}/w1", payload={"id": "w1", "active": False, "callbackURL": url})
    mock_trello.delete(f"{WEBHOOKS_URL}/w1", payload={})
    mock_trello.post(CREATE_URL, payload={"id": "w2", "active": True})
    await _setup(hass, entry, mock_trello)
    assert len(calls(mock_trello, "DELETE", f"{WEBHOOKS_URL}/w1")) == 1
    assert entry.data["webhooks"]["b1"]["trello_webhook_id"] == "w2"


async def test_existing_webhook_gets_fresh_callback(hass, setup_credentials, mock_trello, external_url):
    """Review Focus 5."""
    entry = make_entry(hass)
    mock_trello.post(CREATE_URL, status=400, body="A webhook with that callback, model, and token already exists")
    mock_trello.post(CREATE_URL, payload={"id": "w2", "active": True})
    await _setup(hass, entry, mock_trello)
    first, second = calls(mock_trello, "POST", WEBHOOKS_URL)
    assert first.kwargs["params"]["callbackURL"] != second.kwargs["params"]["callbackURL"]
    assert entry.data["webhooks"]["b1"]["callback_url"] == second.kwargs["params"]["callbackURL"]
    assert _coordinator(entry).mode == MODE_WEBHOOK


async def test_creation_failure_means_feed(hass, setup_credentials, mock_trello, external_url, caplog):
    entry = make_entry(hass)
    mock_trello.post(CREATE_URL, status=400, body="URL did not return 200 status code", repeat=True)
    await _setup(hass, entry, mock_trello)
    assert _coordinator(entry).mode == MODE_FEED
    assert "b1" in entry.runtime_data.webhooks.failures
    assert caplog.text.count("polling its activity instead") == 1


async def _post(hass, hass_client_no_auth, entry, body: bytes, sig: str | None):
    state = entry.data["webhooks"]["b1"]
    headers = {} if sig is None else {"X-Trello-Webhook": sig}
    client = await hass_client_no_auth()
    return await client.post(f"/api/webhook/{state['webhook_id']}", data=body, headers=headers)


@pytest.fixture
async def webhook_entry(hass, setup_credentials, mock_trello, external_url):
    entry = make_entry(hass)
    mock_trello.post(CREATE_URL, payload={"id": "w1", "active": True})
    await _setup(hass, entry, mock_trello)
    return entry


async def test_valid_post_fires_event(hass, hass_client_no_auth, webhook_entry):
    body = json.dumps({"action": action("a1", "commentCard", card=CARD, list=ALEX_LIST, text="hi")}).encode()
    sig = signature(CLIENT_SECRET, body, webhook_entry.data["webhooks"]["b1"]["callback_url"])
    resp = await _post(hass, hass_client_no_auth, webhook_entry, body, sig)
    assert resp.status == 200
    await hass.async_block_till_done()
    assert hass.states.get("event.household_activity").attributes["event_type"] == "comment_added"


async def test_bad_signature_rejected(hass, hass_client_no_auth, webhook_entry):
    """Review Focus 3."""
    body = json.dumps({"action": action("a1", "commentCard", card=CARD, list=ALEX_LIST, text="hi")}).encode()
    for sig in ("forged", None):
        resp = await _post(hass, hass_client_no_auth, webhook_entry, body, sig)
        assert resp.status == 401
    await hass.async_block_till_done()
    assert hass.states.get("event.household_activity").attributes.get("event_type") is None
    assert webhook_entry.runtime_data.webhooks.bad_signatures == 2


async def test_bad_body_rejected(hass, hass_client_no_auth, webhook_entry):
    body = b"not json"
    sig = signature(CLIENT_SECRET, body, webhook_entry.data["webhooks"]["b1"]["callback_url"])
    assert (await _post(hass, hass_client_no_auth, webhook_entry, body, sig)).status == 400


async def test_unknown_board_gone(hass, hass_client_no_auth, webhook_entry):
    webhook_entry.runtime_data.webhooks._boards.pop("b1")
    body = b"{}"
    sig = signature(CLIENT_SECRET, body, webhook_entry.data["webhooks"]["b1"]["callback_url"])
    assert (await _post(hass, hass_client_no_auth, webhook_entry, body, sig)).status == 410


async def test_reload_keeps_trello_webhook(hass, webhook_entry, mock_trello):
    url = webhook_entry.data["webhooks"]["b1"]["callback_url"]
    mock_trello.get(f"{WEBHOOKS_URL}/w1", payload={"id": "w1", "active": True, "callbackURL": url}, repeat=True)
    assert await hass.config_entries.async_reload(webhook_entry.entry_id)
    await hass.async_block_till_done()
    assert calls(mock_trello, "DELETE", f"{WEBHOOKS_URL}/w1") == []
    assert webhook_entry.data["webhooks"]["b1"]["trello_webhook_id"] == "w1"


async def test_board_removal_deletes_webhook(hass, setup_credentials, mock_trello, external_url):
    entry = make_entry(hass, board_subentry(), board_subentry("b2", "Projects", "archive", None))
    mock_trello.post(CREATE_URL, payload={"id": "w1", "active": True})
    mock_trello.post(CREATE_URL, payload={"id": "w2", "active": True})
    await _setup(hass, entry, mock_trello, boards=("b1", "b2"))
    b2_hook = entry.data["webhooks"]["b2"]["trello_webhook_id"]
    url_b1 = entry.data["webhooks"]["b1"]["callback_url"]
    b1_hook = entry.data["webhooks"]["b1"]["trello_webhook_id"]
    mock_trello.get(f"{WEBHOOKS_URL}/{b1_hook}", payload={"id": b1_hook, "active": True, "callbackURL": url_b1}, repeat=True)
    mock_trello.delete(f"{WEBHOOKS_URL}/{b2_hook}", payload={})
    hass.config_entries.async_remove_subentry(entry, subentry_id(entry, "b2"))
    await hass.async_block_till_done()
    assert len(calls(mock_trello, "DELETE", f"{WEBHOOKS_URL}/{b2_hook}")) == 1
    assert "b2" not in entry.data["webhooks"]


async def test_entry_removal_deletes_webhooks(hass, webhook_entry, mock_trello):
    mock_trello.delete(f"{WEBHOOKS_URL}/w1", payload={})
    assert await hass.config_entries.async_remove(webhook_entry.entry_id)
    await hass.async_block_till_done()
    assert len(calls(mock_trello, "DELETE", f"{WEBHOOKS_URL}/w1")) == 1


async def test_health_check_recreates(hass, webhook_entry, mock_trello):
    url = webhook_entry.data["webhooks"]["b1"]["callback_url"]
    mock_trello.get(f"{WEBHOOKS_URL}/w1", payload={"id": "w1", "active": False, "callbackURL": url})
    mock_trello.delete(f"{WEBHOOKS_URL}/w1", payload={})
    mock_trello.post(CREATE_URL, payload={"id": "w3", "active": True})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()
    assert webhook_entry.data["webhooks"]["b1"]["trello_webhook_id"] == "w3"


def test_signature_matches_trello_scheme():
    import base64
    import hashlib
    import hmac

    body, url = b'{"a":1}', "https://x/y"
    expected = base64.b64encode(hmac.new(b"secret", body + url.encode(), hashlib.sha1).digest()).decode()
    assert signature("secret", body, url) == expected

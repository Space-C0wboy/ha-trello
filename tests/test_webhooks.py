"""Tests for Trello webhooks: URL choice, lifecycle, signed handler."""
import json
import logging
import re
import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.trello_todo.const import MODE_FEED, MODE_WEBHOOK
from custom_components.trello_todo.webhooks import WebhookManager, signature

from .common import (
    BOARD_URL,
    CLIENT_SECRET,
    TOKEN_URL,
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


# --- final-review fixes -------------------------------------------------------------------------

CLOUD = "custom_components.trello_todo.webhooks"


def _store(hass, entry, url, *, webhook_id="hid", trello_id="w1"):
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "webhooks": {"b1": {"webhook_id": webhook_id, "trello_webhook_id": trello_id, "callback_url": url}}},
    )


async def test_restart_with_cloud_not_yet_connected_reuses_cloudhook(hass, setup_credentials, mock_trello):
    """C1: at boot Nabu Casa isn't connected yet; the stored cloudhook webhook must survive."""
    entry = make_entry(hass)
    url = "https://hooks.nabu.casa/stored"
    _store(hass, entry, url)
    mock_trello.get(f"{WEBHOOKS_URL}/w1", payload={"id": "w1", "active": True, "callbackURL": url}, repeat=True)
    with (
        patch(f"{CLOUD}._cloud_active", return_value=True),
        patch(f"{CLOUD}._create_cloudhook", AsyncMock(side_effect=HomeAssistantError("not connected"))),
    ):
        await _setup(hass, entry, mock_trello)
    assert calls(mock_trello, "POST", WEBHOOKS_URL) == []
    assert calls(mock_trello, "DELETE", f"{WEBHOOKS_URL}/w1") == []
    assert entry.data["webhooks"]["b1"] == {"webhook_id": "hid", "trello_webhook_id": "w1", "callback_url": url}
    assert _coordinator(entry).mode == MODE_WEBHOOK


async def test_cloud_not_connected_on_first_creation_waits(hass, setup_credentials, mock_trello, external_url):
    entry = make_entry(hass)
    with (
        patch(f"{CLOUD}._cloud_active", return_value=True),
        patch(f"{CLOUD}._create_cloudhook", AsyncMock(side_effect=HomeAssistantError("not connected"))),
    ):
        await _setup(hass, entry, mock_trello)
    assert calls(mock_trello, "POST", WEBHOOKS_URL) == []
    assert entry.runtime_data.webhooks.failures == {"b1": "cloud_not_connected"}
    assert _coordinator(entry).mode == MODE_FEED


async def test_existing_cloudhook_without_url_is_recreated(hass, setup_credentials, mock_trello):
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "webhooks": {"b1": {"webhook_id": "hid"}}})
    mock_trello.post(CREATE_URL, payload={"id": "w1", "active": True})
    delete = AsyncMock()
    create = AsyncMock(side_effect=[ValueError("Hook is already enabled"), "https://hooks.nabu.casa/new"])
    with (
        patch(f"{CLOUD}._cloud_active", return_value=True),
        patch(f"{CLOUD}._create_cloudhook", create),
        patch(f"{CLOUD}._delete_cloudhook", delete),
    ):
        await _setup(hass, entry, mock_trello)
    delete.assert_awaited_once()
    assert entry.data["webhooks"]["b1"]["callback_url"] == "https://hooks.nabu.casa/new"
    assert _coordinator(entry).mode == MODE_WEBHOOK


async def test_lost_webhook_is_retired_with_410(hass, hass_client_no_auth, setup_credentials, mock_trello, external_url):
    """I1: HA answers 200 for unknown webhook ids, so the orphan must be answered 410 explicitly."""
    entry = make_entry(hass)
    mock_trello.post(CREATE_URL, status=400, body="A webhook with that callback, model, and token already exists")
    mock_trello.post(CREATE_URL, payload={"id": "w2", "active": True})
    await _setup(hass, entry, mock_trello)
    old_url = calls(mock_trello, "POST", WEBHOOKS_URL)[0].kwargs["params"]["callbackURL"]
    old_id = old_url.rsplit("/", 1)[-1]
    assert old_id in entry.data["retired_webhooks"]
    client = await hass_client_no_auth()
    assert (await client.post(f"/api/webhook/{old_id}", data=b"{}")).status == 410


async def test_failed_delete_on_board_removal_retires(hass, hass_client_no_auth, setup_credentials, mock_trello, external_url):
    entry = make_entry(hass, board_subentry(), board_subentry("b2", "Projects", "archive", None))
    mock_trello.post(CREATE_URL, payload={"id": "w1", "active": True})
    mock_trello.post(CREATE_URL, payload={"id": "w2", "active": True})
    await _setup(hass, entry, mock_trello, boards=("b1", "b2"))
    b1 = entry.data["webhooks"]["b1"]
    b2 = entry.data["webhooks"]["b2"]
    mock_trello.get(
        f"{WEBHOOKS_URL}/{b1['trello_webhook_id']}",
        payload={"id": b1["trello_webhook_id"], "active": True, "callbackURL": b1["callback_url"]},
        repeat=True,
    )
    mock_trello.delete(f"{WEBHOOKS_URL}/{b2['trello_webhook_id']}", status=500)
    hass.config_entries.async_remove_subentry(entry, subentry_id(entry, "b2"))
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    assert (await client.post(f"/api/webhook/{b2['webhook_id']}", data=b"{}")).status == 410


async def test_transient_health_error_keeps_webhook(hass, setup_credentials, mock_trello, external_url):
    """I2: a Trello blip must not tear down a working webhook."""
    entry = make_entry(hass)
    _store(hass, entry, f"{EXTERNAL}/api/webhook/hid")
    mock_trello.get(f"{WEBHOOKS_URL}/w1", status=500, repeat=True)
    await _setup(hass, entry, mock_trello)
    assert calls(mock_trello, "DELETE", f"{WEBHOOKS_URL}/w1") == []
    assert calls(mock_trello, "POST", WEBHOOKS_URL) == []
    assert _coordinator(entry).mode == MODE_WEBHOOK


async def test_unloaded_manager_does_nothing(hass, webhook_entry, mock_trello):
    """I3: a health check still running after unload must not re-register handlers or touch state."""
    manager = webhook_entry.runtime_data.webhooks
    coordinator = _coordinator(webhook_entry)
    state = dict(webhook_entry.data["webhooks"])
    manager.async_unload()
    before = len(calls(mock_trello, "GET", f"{WEBHOOKS_URL}/w1"))
    await manager.async_ensure(coordinator)
    assert manager._by_webhook == {}
    assert len(calls(mock_trello, "GET", f"{WEBHOOKS_URL}/w1")) == before
    assert dict(webhook_entry.data["webhooks"]) == state


async def test_failure_reason_is_a_code(hass, setup_credentials, mock_trello, external_url, caplog):
    """I4: Trello's error text (which can quote the callback URL) never reaches logs or diagnostics."""
    entry = make_entry(hass)
    mock_trello.post(CREATE_URL, status=400, body=f"URL ({EXTERNAL}/api/webhook/x) did not return 200", repeat=True)
    await _setup(hass, entry, mock_trello)
    assert entry.runtime_data.webhooks.failures == {"b1": "rejected"}
    assert not any("ha.example.org" in r.getMessage() for r in caplog.records if r.levelno >= logging.INFO)


async def test_signature_mismatch_falls_back_to_feed(hass, hass_client_no_auth, webhook_entry):
    """I5: if signatures never match, the board must not sit in webhook mode with no events."""
    body = b'{"action": {}}'
    for _ in range(5):
        assert (await _post(hass, hass_client_no_auth, webhook_entry, body, "forged")).status == 401
    coordinator = _coordinator(webhook_entry)
    assert coordinator.mode == MODE_FEED
    assert webhook_entry.runtime_data.webhooks.failures["b1"] == "signature_mismatch"
    await webhook_entry.runtime_data.webhooks.async_ensure(coordinator)  # health check keeps it on the feed
    assert coordinator.mode == MODE_FEED


async def test_empty_client_secret_refuses_webhooks(hass, setup_credentials, mock_trello, external_url):
    entry = make_entry(hass)
    with patch(f"{CLOUD}.get_url", side_effect=NoURLAvailableError):
        await _setup(hass, entry, mock_trello)
    coordinator = _coordinator(entry)
    manager = WebhookManager(hass, make_entry(hass), entry.runtime_data.client, "")
    await manager.async_ensure(coordinator)
    assert manager.failures == {"b1": "no_client_secret"}
    assert coordinator.mode == MODE_FEED
    assert calls(mock_trello, "POST", WEBHOOKS_URL) == []


async def test_entry_removal_refreshes_expired_token(hass, webhook_entry, mock_trello):
    """I7: async_remove_entry runs after the entry is gone, so OAuth2Session can't persist a refresh."""
    hass.config_entries.async_update_entry(
        webhook_entry,
        data={**webhook_entry.data, "token": {**webhook_entry.data["token"], "expires_at": time.time() - 10}},
    )
    mock_trello.post(
        TOKEN_URL,
        payload={"access_token": "fresh", "refresh_token": "r2", "expires_in": 3600, "token_type": "Bearer"},
        repeat=True,
    )
    mock_trello.delete(f"{WEBHOOKS_URL}/w1", payload={})
    assert await hass.config_entries.async_unload(webhook_entry.entry_id)
    assert await hass.config_entries.async_remove(webhook_entry.entry_id)
    await hass.async_block_till_done()
    (req,) = calls(mock_trello, "DELETE", f"{WEBHOOKS_URL}/w1")
    assert req.kwargs["headers"]["Authorization"] == "Bearer fresh"

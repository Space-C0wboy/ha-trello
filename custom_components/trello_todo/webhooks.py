"""Trello webhooks: public URL choice, create/reuse/teardown, health check, and the signed-request handler."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .api import (
    TrelloAccessError,
    TrelloAuthError,
    TrelloClient,
    TrelloError,
    TrelloWebhookExistsError,
    reason_code,
)
from .const import CONF_RETIRED_WEBHOOKS, CONF_WEBHOOKS, DOMAIN, MODE_FEED, MODE_WEBHOOK, WEBHOOK_REFRESH_MINUTES

if TYPE_CHECKING:
    from .coordinator import TrelloBoardCoordinator

_LOGGER = logging.getLogger(__name__)
_API_ERRORS = (TrelloError, ClientError, HomeAssistantError)
# Bad signatures in a row (with none ever valid) before a board stops trusting its webhook.
SIGNATURE_FAILURES_BEFORE_FALLBACK = 5
MAX_RETIRED = 50


def _cloud_active(hass: HomeAssistant) -> bool:
    """Nabu Casa subscription active? `cloud` is imported lazily: it pulls in alexa, camera, ..."""
    if "cloud" not in hass.config.components:
        return False
    from homeassistant.components import cloud  # noqa: PLC0415

    return cloud.async_active_subscription(hass)


async def _create_cloudhook(hass: HomeAssistant, webhook_id: str) -> str:
    from homeassistant.components import cloud  # noqa: PLC0415

    return await cloud.async_create_cloudhook(hass, webhook_id)


async def _delete_cloudhook(hass: HomeAssistant, webhook_id: str) -> None:
    if "cloud" not in hass.config.components:
        return
    from homeassistant.components import cloud  # noqa: PLC0415

    await cloud.async_delete_cloudhook(hass, webhook_id)


def signature(secret: str, body: bytes, callback_url: str) -> str:
    """Trello's X-Trello-Webhook: base64(HMAC-SHA1(client secret, body + callback URL))."""
    digest = hmac.new(secret.encode(), body + callback_url.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class WebhookManager:
    """One per account entry. Board webhook state lives in entry.data["webhooks"], which never triggers a reload."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: TrelloClient, client_secret: str) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self._secret = client_secret
        self._boards: dict[str, TrelloBoardCoordinator] = {}
        self._by_webhook: dict[str, str] = {}  # HA webhook id -> board id
        self._gone: set[str] = set()  # retired HA webhook ids, answered with 410
        self._unsub_health: CALLBACK_TYPE | None = None
        self._closed = False
        self._bad_streak: dict[str, int] = {}
        self._signature_ok: set[str] = set()
        self._signature_broken: set[str] = set()
        self.bad_signatures = 0
        self.failures: dict[str, str] = {}

    # -- state -----------------------------------------------------------------
    def _states(self) -> dict[str, dict[str, Any]]:
        return dict(self.entry.data.get(CONF_WEBHOOKS) or {})

    def _save(self, board_id: str, state: dict[str, Any] | None) -> None:
        states = self._states()
        if state is None:
            states.pop(board_id, None)
        else:
            states[board_id] = state
        self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_WEBHOOKS: states})

    def _retire(self, webhook_id: str, *, persist: bool = True) -> None:
        """A Trello webhook may still deliver to this id: answer 410 so Trello deletes it.

        HA itself answers 200 for unknown webhook ids, which would keep an orphan alive forever. The id's
        cloudhook is kept on purpose so the 410 reaches Trello.
        """
        self._unregister(webhook_id)
        self._register_gone(webhook_id)
        if persist:
            retired = [w for w in self.entry.data.get(CONF_RETIRED_WEBHOOKS) or [] if w != webhook_id]
            retired = [*retired, webhook_id][-MAX_RETIRED:]
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_RETIRED_WEBHOOKS: retired}
            )

    # -- lifecycle -------------------------------------------------------------
    async def async_setup(self, coordinators: Iterable[TrelloBoardCoordinator]) -> None:
        self._boards = {c.board_id: c for c in coordinators}
        for webhook_id in self.entry.data.get(CONF_RETIRED_WEBHOOKS) or []:
            self._register_gone(webhook_id)
        for board_id in [b for b in self._states() if b not in self._boards]:
            await self.async_remove_board(board_id)
        for coordinator in self._boards.values():
            await self.async_ensure(coordinator)
        if self._closed:
            return
        self._unsub_health = async_track_time_interval(
            self.hass,
            self._async_health,
            timedelta(minutes=WEBHOOK_REFRESH_MINUTES),
            name="trello webhook health",
            cancel_on_shutdown=True,
        )

    @callback
    def _async_health(self, _now: datetime | None = None) -> None:
        # An entry background task is cancelled when the entry unloads, so a check can not outlive a reload.
        self.entry.async_create_background_task(self.hass, self._async_check_all(), "trello webhook health")

    async def _async_check_all(self) -> None:
        for coordinator in list(self._boards.values()):
            await self.async_ensure(coordinator)

    @callback
    def async_unload(self) -> None:
        """Reload/restart: keep the Trello webhook, drop only the local handlers."""
        self._closed = True
        if self._unsub_health is not None:
            self._unsub_health()
            self._unsub_health = None
        for webhook_id in list(self._by_webhook):
            self._unregister(webhook_id)
        for webhook_id in list(self._gone):
            webhook.async_unregister(self.hass, webhook_id)
        self._gone.clear()

    async def async_ensure(self, coordinator: TrelloBoardCoordinator) -> None:
        """Make sure the board has a working Trello webhook; otherwise run it on the activity feed."""
        if self._closed:
            return
        board_id = coordinator.board_id
        if not self._secret:
            self._fail(coordinator, "no_client_secret")
            return
        if board_id in self._signature_broken:
            self._fail(coordinator, "signature_mismatch")
            return
        state = self._states().get(board_id, {})
        webhook_id = state.get("webhook_id") or webhook.async_generate_id()
        self._register(webhook_id, board_id)
        trello_id = state.get("trello_webhook_id")
        stored_url = state.get("callback_url")
        if trello_id and stored_url:
            # Reuse the stored URL: Trello only checks it at creation, and at boot Nabu Casa is not connected yet.
            healthy = await self._healthy(trello_id, stored_url)
            if self._closed:
                return
            if healthy is not False:  # healthy, or a Trello/network blip: keep the webhook
                self._ok(coordinator)
                return
            await self._delete_quietly(trello_id)  # if this fails, creating again hits "already exists" below
        url, reason = await self._callback_url(webhook_id)
        if self._closed:
            return
        if url is None:
            if not trello_id:
                self._save(board_id, {"webhook_id": webhook_id})
            self._fail(coordinator, reason)
            return
        try:
            created = await self._create(coordinator, url)
        except TrelloWebhookExistsError:
            # Trello still has a webhook for this URL but we lost its id: retire the URL, move to a fresh one.
            self._retire(webhook_id)
            webhook_id = webhook.async_generate_id()
            self._register(webhook_id, board_id)
            url, reason = await self._callback_url(webhook_id)
            if self._closed:
                return
            if url is None:
                self._save(board_id, {"webhook_id": webhook_id})
                self._fail(coordinator, reason)
                return
            try:
                created = await self._create(coordinator, url)
            except _API_ERRORS as err:
                self._create_failed(coordinator, webhook_id, url, err)
                return
        except _API_ERRORS as err:
            self._create_failed(coordinator, webhook_id, url, err)
            return
        if self._closed:
            return
        self._save(board_id, {"webhook_id": webhook_id, "trello_webhook_id": created["id"], "callback_url": url})
        self._ok(coordinator)

    async def async_remove_board(self, board_id: str, *, save: bool = True) -> None:
        """Board (or account) removed: delete its Trello webhook, cloudhook and handler."""
        state = self._states().get(board_id, {})
        deleted = True
        if trello_id := state.get("trello_webhook_id"):
            deleted = await self._delete_quietly(trello_id)
        if webhook_id := state.get("webhook_id"):
            if deleted or not save:
                self._unregister(webhook_id)
                try:
                    await _delete_cloudhook(self.hass, webhook_id)
                except (HomeAssistantError, ValueError):  # CloudNotAvailable, or no cloudhook for this id
                    pass
            else:
                self._retire(webhook_id)
        self.failures.pop(board_id, None)
        if save:
            self._save(board_id, None)

    # -- helpers ---------------------------------------------------------------
    async def _create(self, coordinator: TrelloBoardCoordinator, url: str) -> dict[str, Any]:
        return await self.client.create_webhook(url, coordinator.board_id, f"Home Assistant: {coordinator.board_title}")

    @callback
    def _create_failed(self, coordinator: TrelloBoardCoordinator, webhook_id: str, url: str, err: Exception) -> None:
        _LOGGER.debug("Creating the Trello webhook for %s failed: %s", coordinator.board_title, err)
        if isinstance(err, TrelloAuthError):
            self.entry.async_start_reauth(self.hass)
        self._save(coordinator.board_id, {"webhook_id": webhook_id, "callback_url": url})
        self._fail(coordinator, reason_code(err))

    async def _callback_url(self, webhook_id: str) -> tuple[str | None, str]:
        """(url, "") or (None, reason). Nabu Casa first, then an HTTPS external URL."""
        if _cloud_active(self.hass):
            try:
                return await _create_cloudhook(self.hass, webhook_id), ""
            except ValueError:
                # A cloudhook already exists for this id but its URL is unknown: replace it.
                try:
                    await _delete_cloudhook(self.hass, webhook_id)
                    return await _create_cloudhook(self.hass, webhook_id), ""
                except (ValueError, HomeAssistantError):
                    return None, "cloud_not_connected"
            except HomeAssistantError:  # CloudNotConnected right after boot: retry at the next health check
                return None, "cloud_not_connected"
        try:
            base = get_url(self.hass, allow_internal=False, allow_ip=False, require_ssl=True, prefer_external=True)
        except NoURLAvailableError:
            return None, "no_public_url"
        return f"{base}{webhook.async_generate_path(webhook_id)}", ""

    async def _healthy(self, trello_id: str, url: str) -> bool | None:
        """True/False when Trello answered; None when it could not be checked (rate limit, 5xx, network)."""
        try:
            info = await self.client.get_webhook(trello_id)
        except TrelloAccessError:
            return False
        except _API_ERRORS as err:
            if isinstance(err, TrelloAuthError):
                self.entry.async_start_reauth(self.hass)
            _LOGGER.debug("Could not check Trello webhook: %s", err)
            return None
        return bool(info.get("active")) and info.get("callbackURL") == url

    async def _delete_quietly(self, trello_id: str) -> bool:
        try:
            await self.client.delete_webhook(trello_id)
        except TrelloAccessError:
            return True  # already gone
        except _API_ERRORS as err:
            _LOGGER.debug("Could not delete Trello webhook: %s", err)
            return False
        return True

    def _register(self, webhook_id: str, board_id: str) -> None:
        if self._closed or webhook_id in self._by_webhook:
            return
        try:
            webhook.async_register(
                self.hass, DOMAIN, f"Trello {board_id}", webhook_id, self._async_handle, allowed_methods={"POST"}
            )
        except ValueError:  # still held by a manager that is unloading
            _LOGGER.debug("Webhook handler for %s is still registered", board_id)
            return
        self._by_webhook[webhook_id] = board_id

    def _register_gone(self, webhook_id: str) -> None:
        if self._closed or webhook_id in self._gone:
            return
        try:
            webhook.async_register(
                self.hass, DOMAIN, "Trello (retired)", webhook_id, self._async_gone, allowed_methods={"POST"}
            )
        except ValueError:
            return
        self._gone.add(webhook_id)

    def _unregister(self, webhook_id: str) -> None:
        if self._by_webhook.pop(webhook_id, None) is not None:
            webhook.async_unregister(self.hass, webhook_id)

    @callback
    def _ok(self, coordinator: TrelloBoardCoordinator) -> None:
        self.failures.pop(coordinator.board_id, None)
        coordinator.set_mode(MODE_WEBHOOK)

    @callback
    def _fail(self, coordinator: TrelloBoardCoordinator, reason: str) -> None:
        if self.failures.get(coordinator.board_id) != reason:
            _LOGGER.warning(
                "Trello webhook unavailable for %s (%s); polling its activity instead", coordinator.board_title, reason
            )
        self.failures[coordinator.board_id] = reason
        coordinator.set_mode(MODE_FEED)

    async def _async_gone(self, hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        return web.Response(status=HTTPStatus.GONE)  # Trello deletes webhooks that answer 410

    async def _async_handle(self, hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        board_id = self._by_webhook.get(webhook_id)
        coordinator = self._boards.get(board_id) if board_id else None
        callback_url = self._states().get(board_id or "", {}).get("callback_url")
        if board_id is None or coordinator is None or not callback_url:
            return web.Response(status=HTTPStatus.GONE)
        # text(), not read(): Nabu Casa delivers cloudhooks as a MockRequest without read(). Trello sends UTF-8,
        # so re-encoding gives back the exact bytes it signed.
        body = (await request.text()).encode("utf-8")
        expected = signature(self._secret, body, callback_url)
        if not self._secret or not hmac.compare_digest(request.headers.get("X-Trello-Webhook", ""), expected):
            self.bad_signatures += 1
            streak = self._bad_streak.get(board_id, 0) + 1
            self._bad_streak[board_id] = streak
            _LOGGER.debug("Rejected a Trello webhook request with a bad signature")
            if streak >= SIGNATURE_FAILURES_BEFORE_FALLBACK and board_id not in self._signature_ok:
                # Never a valid signature: Trello is not signing with our secret. Do not sit in webhook mode silently.
                self._signature_broken.add(board_id)
                self._fail(coordinator, "signature_mismatch")
            return web.Response(status=HTTPStatus.UNAUTHORIZED)
        self._bad_streak.pop(board_id, None)
        self._signature_ok.add(board_id)
        if board_id in self._signature_broken:
            self._signature_broken.discard(board_id)
            self._ok(coordinator)
        try:
            action = json.loads(body)["action"]
        except (ValueError, KeyError, TypeError):
            return web.Response(status=HTTPStatus.BAD_REQUEST)
        if not isinstance(action, dict):
            return web.Response(status=HTTPStatus.BAD_REQUEST)
        self.entry.async_create_background_task(
            hass, coordinator.processor.async_process([action]), "trello webhook activity"
        )
        return web.Response(status=HTTPStatus.OK)

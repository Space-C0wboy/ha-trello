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

from .api import TrelloClient, TrelloError, TrelloWebhookExistsError
from .const import CONF_WEBHOOKS, DOMAIN, MODE_FEED, MODE_WEBHOOK, WEBHOOK_REFRESH_MINUTES

if TYPE_CHECKING:
    from .coordinator import TrelloBoardCoordinator

_LOGGER = logging.getLogger(__name__)
_API_ERRORS = (TrelloError, ClientError, HomeAssistantError)


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
        self._unsub_health: CALLBACK_TYPE | None = None
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

    # -- lifecycle -------------------------------------------------------------
    async def async_setup(self, coordinators: Iterable[TrelloBoardCoordinator]) -> None:
        self._boards = {c.board_id: c for c in coordinators}
        for board_id in [b for b in self._states() if b not in self._boards]:
            await self.async_remove_board(board_id)
        for coordinator in self._boards.values():
            await self.async_ensure(coordinator)
        self._unsub_health = async_track_time_interval(
            self.hass,
            self._async_health,
            timedelta(minutes=WEBHOOK_REFRESH_MINUTES),
            name="trello webhook health",
            cancel_on_shutdown=True,
        )

    async def _async_health(self, _now: datetime | None = None) -> None:
        for coordinator in list(self._boards.values()):
            await self.async_ensure(coordinator)

    @callback
    def async_unload(self) -> None:
        """Reload/restart: keep the Trello webhook, drop only the local handler."""
        if self._unsub_health is not None:
            self._unsub_health()
            self._unsub_health = None
        for webhook_id in list(self._by_webhook):
            self._unregister(webhook_id)

    async def async_ensure(self, coordinator: TrelloBoardCoordinator) -> None:
        """Make sure the board has a working Trello webhook; otherwise run it on the activity feed."""
        board_id = coordinator.board_id
        state = self._states().get(board_id, {})
        webhook_id = state.get("webhook_id") or webhook.async_generate_id()
        self._register(webhook_id, board_id)
        url = await self._callback_url(webhook_id, state)
        if url is None:
            self._save(board_id, {"webhook_id": webhook_id})
            self._fail(coordinator, "no_public_url")
            return
        trello_id = state.get("trello_webhook_id")
        if trello_id and state.get("callback_url") == url and await self._healthy(trello_id, url):
            self._ok(coordinator)
            return
        if trello_id:
            await self._delete_quietly(trello_id)
        try:
            created = await self._create(coordinator, url)
        except TrelloWebhookExistsError:
            # Trello still has a webhook for this URL but we lost its id: move to a fresh callback URL.
            self._unregister(webhook_id)
            webhook_id = webhook.async_generate_id()
            self._register(webhook_id, board_id)
            url = await self._callback_url(webhook_id, {})
            if url is None:
                self._fail(coordinator, "no_public_url")
                return
            try:
                created = await self._create(coordinator, url)
            except _API_ERRORS as err:
                self._save(board_id, {"webhook_id": webhook_id, "callback_url": url})
                self._fail(coordinator, str(err))
                return
        except _API_ERRORS as err:
            self._save(board_id, {"webhook_id": webhook_id, "callback_url": url})
            self._fail(coordinator, str(err))
            return
        self._save(board_id, {"webhook_id": webhook_id, "trello_webhook_id": created["id"], "callback_url": url})
        self._ok(coordinator)

    async def async_remove_board(self, board_id: str, *, save: bool = True) -> None:
        """Board (or account) removed: delete its Trello webhook, cloudhook and handler."""
        state = self._states().get(board_id, {})
        if trello_id := state.get("trello_webhook_id"):
            await self._delete_quietly(trello_id)
        if webhook_id := state.get("webhook_id"):
            self._unregister(webhook_id)
            try:
                await _delete_cloudhook(self.hass, webhook_id)
            except (HomeAssistantError, ValueError):  # CloudNotAvailable, or no cloudhook for this id
                pass
        self.failures.pop(board_id, None)
        if save:
            self._save(board_id, None)

    # -- helpers ---------------------------------------------------------------
    async def _create(self, coordinator: TrelloBoardCoordinator, url: str) -> dict[str, Any]:
        return await self.client.create_webhook(url, coordinator.board_id, f"Home Assistant: {coordinator.board_title}")

    async def _callback_url(self, webhook_id: str, state: dict[str, Any]) -> str | None:
        if _cloud_active(self.hass):
            try:
                return await _create_cloudhook(self.hass, webhook_id)
            except ValueError:  # already created for this webhook id, or cloud not connected right now
                if state.get("callback_url"):
                    return state["callback_url"]
            except HomeAssistantError:  # CloudNotAvailable / CloudNotConnected
                pass
        try:
            base = get_url(self.hass, allow_internal=False, allow_ip=False, require_ssl=True, prefer_external=True)
        except NoURLAvailableError:
            return None
        return f"{base}{webhook.async_generate_path(webhook_id)}"

    async def _healthy(self, trello_id: str, url: str) -> bool:
        try:
            info = await self.client.get_webhook(trello_id)
        except _API_ERRORS:
            return False
        return bool(info.get("active")) and info.get("callbackURL") == url

    async def _delete_quietly(self, trello_id: str) -> None:
        try:
            await self.client.delete_webhook(trello_id)
        except _API_ERRORS as err:
            _LOGGER.debug("Could not delete Trello webhook: %s", err)

    def _register(self, webhook_id: str, board_id: str) -> None:
        if webhook_id in self._by_webhook:
            return
        webhook.async_register(
            self.hass, DOMAIN, f"Trello {board_id}", webhook_id, self._async_handle, allowed_methods={"POST"}
        )
        self._by_webhook[webhook_id] = board_id

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

    async def _async_handle(self, hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        board_id = self._by_webhook.get(webhook_id)
        coordinator = self._boards.get(board_id) if board_id else None
        callback_url = self._states().get(board_id or "", {}).get("callback_url")
        if coordinator is None or not callback_url:
            return web.Response(status=HTTPStatus.GONE)  # Trello deletes webhooks that answer 410
        body = await request.read()
        expected = signature(self._secret, body, callback_url)
        if not hmac.compare_digest(request.headers.get("X-Trello-Webhook", ""), expected):
            self.bad_signatures += 1
            _LOGGER.debug("Rejected a Trello webhook request with a bad signature")
            return web.Response(status=HTTPStatus.UNAUTHORIZED)
        try:
            action = json.loads(body)["action"]
        except (ValueError, KeyError, TypeError):
            return web.Response(status=HTTPStatus.BAD_REQUEST)
        if not isinstance(action, dict):
            return web.Response(status=HTTPStatus.BAD_REQUEST)
        hass.async_create_task(coordinator.processor.async_process([action]), "trello webhook activity")
        return web.Response(status=HTTPStatus.OK)

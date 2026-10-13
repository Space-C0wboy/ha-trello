"""One DataUpdateCoordinator per board: one board request per refresh."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .activity import ACTION_TYPES, ActionContext, ActivityProcessor
from .api import (
    TrelloAccessError,
    TrelloAuthError,
    TrelloBoard,
    TrelloClient,
    TrelloError,
    TrelloRateLimitError,
)
from .const import (
    CONF_BOARD_ID,
    CONF_DONE_BEHAVIOR,
    CONF_DONE_LIST,
    CONF_SCAN_INTERVAL,
    DEFAULT_RATE_LIMIT_BACKOFF,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FEED_INTERVAL_SECONDS,
    MODE_FEED,
    MODE_WEBHOOK,
    WEBHOOK_REFRESH_MINUTES,
)

if TYPE_CHECKING:
    from .webhooks import WebhookManager

_LOGGER = logging.getLogger(__name__)


@dataclass
class TrelloRuntimeData:
    client: TrelloClient
    boards: dict[str, TrelloBoardCoordinator]  # sub-entry id -> coordinator
    board_settings: dict[str, dict[str, Any]]  # sub-entry id -> sub-entry data, as loaded
    options: dict[str, Any]  # account options, as loaded
    webhooks: WebhookManager | None = None


type TrelloConfigEntry = ConfigEntry[TrelloRuntimeData]


class TrelloBoardCoordinator(DataUpdateCoordinator[TrelloBoard]):
    config_entry: TrelloConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: TrelloConfigEntry, client: TrelloClient, subentry: ConfigSubentry
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {subentry.title}",
            update_interval=timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        )
        self.client = client
        self.subentry_id = subentry.subentry_id
        self.board_id: str = subentry.data[CONF_BOARD_ID]
        self.board_title: str = subentry.title
        self.done_behavior: str = subentry.data[CONF_DONE_BEHAVIOR]
        self.done_list: str | None = subentry.data.get(CONF_DONE_LIST)
        self._issue_id = f"board_inaccessible_{self.board_id}"
        self._scan_interval = timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        self.mode = MODE_FEED
        self.processor = ActivityProcessor(self._activity_context, self.async_request_refresh)
        self._feed_unsub: CALLBACK_TYPE | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.board_id)},
            name=self.data.name if self.data else self.board_title,
            manufacturer="Trello",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=f"https://trello.com/b/{self.board_id}",
        )

    def _activity_context(self) -> ActionContext:
        board = self.data
        return ActionContext(
            me=self.config_entry.unique_id,
            done_list=self.done_list,
            members=dict(board.members) if board else {},
        )

    async def async_poll_feed(self, _now: datetime | None = None) -> None:
        """Fetch new board activity: feed mode every minute, and once at startup for the baseline."""
        try:
            actions = await self.client.get_actions(self.board_id, self.processor.newest_id, ACTION_TYPES)
        except (TrelloError, ClientError, HomeAssistantError) as err:
            _LOGGER.debug("Trello activity feed for %s failed: %s", self.board_title, err)
            return
        if not self.processor.baselined:
            self.processor.set_baseline(actions)
        else:
            await self.processor.async_process(actions)

    @callback
    def set_mode(self, mode: str) -> None:
        """webhook: Trello pushes activity, refresh is a 15-min safety net. feed: poll activity every minute."""
        self.mode = mode
        if mode == MODE_WEBHOOK:
            self.update_interval = timedelta(minutes=WEBHOOK_REFRESH_MINUTES)
            self.async_stop_feed()
        else:
            self.update_interval = self._scan_interval
            if self._feed_unsub is None:
                self._feed_unsub = async_track_time_interval(
                    self.hass,
                    self.async_poll_feed,
                    timedelta(seconds=FEED_INTERVAL_SECONDS),
                    name=f"trello activity feed {self.board_title}",
                    cancel_on_shutdown=True,
                )
        self.async_update_listeners()

    @callback
    def async_stop_feed(self) -> None:
        if self._feed_unsub is not None:
            self._feed_unsub()
            self._feed_unsub = None

    async def _async_update_data(self) -> TrelloBoard:
        try:
            board = await self.client.get_board(self.board_id)
        except ConfigEntryAuthFailed:
            raise  # OAuth2TokenRequestReauthError: the refresh token was rejected; the user must sign in again
        except ClientError as err:
            # Any other failed token refresh (api.py wraps its own aiohttp errors, so these come from the token getter).
            raise UpdateFailed(f"Could not refresh the Trello token: {err}") from err
        except TrelloAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TrelloAccessError as err:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="board_inaccessible",
                translation_placeholders={"board": self.board_title},
            )
            raise UpdateFailed(str(err)) from err
        except TrelloRateLimitError as err:
            raise UpdateFailed(str(err), retry_after=err.retry_after or DEFAULT_RATE_LIMIT_BACKOFF) from err
        except TrelloError as err:
            raise UpdateFailed(str(err)) from err
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        if board.name and board.name != self.board_title:
            self._rename(board.name)
        return board

    def _rename(self, name: str) -> None:
        """Follow a board rename in Trello: sub-entry title, device name, messages. Titles don't trigger a reload."""
        self.board_title = name
        entry = self.config_entry
        if subentry := entry.subentries.get(self.subentry_id):
            self.hass.config_entries.async_update_subentry(entry, subentry, title=name)
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            if (DOMAIN, self.board_id) in device.identifiers:
                registry.async_update_device(device.id, name=name)

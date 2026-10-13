"""Trello for Home Assistant: boards as devices, lists as to-do lists."""
from __future__ import annotations

from typing import Any

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_entry_oauth2_flow, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TrelloAccessError, TrelloClient
from .const import CONF_BOARD_ID, DOMAIN, MODE_FEED, SUBENTRY_BOARD
from .coordinator import TrelloBoardCoordinator, TrelloConfigEntry, TrelloRuntimeData

PLATFORMS: list[Platform] = [Platform.TODO, Platform.EVENT]


def _board_settings(entry: TrelloConfigEntry) -> dict[str, dict[str, Any]]:
    return {sid: dict(sub.data) for sid, sub in entry.subentries.items() if sub.subentry_type == SUBENTRY_BOARD}


async def async_setup_entry(hass: HomeAssistant, entry: TrelloConfigEntry) -> bool:
    """Set up a Trello account and each of its boards."""
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(hass, entry)
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    async def get_token() -> str:
        await session.async_ensure_token_valid()
        return session.token["access_token"]

    client = TrelloClient(async_get_clientsession(hass), get_token)
    settings = _board_settings(entry)
    _delete_stale_issues(hass, {data[CONF_BOARD_ID] for data in settings.values()})
    boards = {sid: TrelloBoardCoordinator(hass, entry, client, entry.subentries[sid]) for sid in settings}

    # Boards load independently: one unreachable board must not keep the others down.
    for coordinator in boards.values():
        await coordinator.async_refresh()
    errors = [c.last_exception for c in boards.values() if not c.last_update_success]
    if any(isinstance(err, ConfigEntryAuthFailed) for err in errors):
        raise ConfigEntryAuthFailed("Trello no longer accepts the stored authorization")
    # Retry only when every board failed transiently; a deleted board loads as unavailable with a repair.
    if boards and len(errors) == len(boards) and not any(isinstance(e.__cause__, TrelloAccessError) for e in errors):
        raise ConfigEntryNotReady("No Trello board could be loaded")

    entry.runtime_data = TrelloRuntimeData(
        client=client, boards=boards, board_settings=settings, options=dict(entry.options)
    )
    for coordinator in boards.values():
        # Baseline: anything that happened before this start is not replayed as events.
        await coordinator.async_poll_feed()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    for coordinator in boards.values():
        entry.async_on_unload(coordinator.async_stop_feed)
        coordinator.set_mode(MODE_FEED)  # Task 6: the WebhookManager decides per board
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _delete_stale_issues(hass: HomeAssistant, board_ids: set[str]) -> None:
    """Drop "board inaccessible" repairs for boards that are no longer set up."""
    prefix = "board_inaccessible_"
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(prefix) and issue_id.removeprefix(prefix) not in board_ids:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def _async_update_listener(hass: HomeAssistant, entry: TrelloConfigEntry) -> None:
    """Reload when boards or options change, but not on the hourly token refresh."""
    runtime = entry.runtime_data
    if _board_settings(entry) != runtime.board_settings or dict(entry.options) != runtime.options:
        hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TrelloConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

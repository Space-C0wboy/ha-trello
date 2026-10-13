"""Diagnostics: tokens redacted, no card contents."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_BOARD_ID, SUBENTRY_BOARD
from .coordinator import TrelloConfigEntry

TO_REDACT = {"token"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: TrelloConfigEntry) -> dict[str, Any]:
    # runtime_data is missing while setup is retrying or failed, which is when diagnostics matter most.
    runtime = getattr(entry, "runtime_data", None)
    boards = []
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_BOARD:
            continue
        coordinator = runtime.boards.get(subentry_id) if runtime else None
        board = coordinator.data if coordinator else None
        boards.append(
            {
                "subentry_id": subentry_id,
                "board_id": subentry.data[CONF_BOARD_ID],
                "title": subentry.title,
                "settings": dict(subentry.data),
                "last_update_success": coordinator.last_update_success if coordinator else None,
                "lists": dict(board.lists) if board else None,
                "open_cards": {list_id: len(cards) for list_id, cards in board.cards.items()} if board else None,
            }
        )
    return {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT), "options": dict(entry.options)},
        "boards": boards,
    }

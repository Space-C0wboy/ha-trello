"""Card writes shared by the to-do lists and the calendar: run, map errors, mirror locally, refresh."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError, ServiceValidationError

from .api import TrelloAccessError, TrelloAuthError, TrelloBoard, TrelloCard, TrelloClient, TrelloError, reason_code
from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import TrelloBoardCoordinator

_LOGGER = logging.getLogger(__name__)


def apply_card_fields(board: TrelloBoard, card: TrelloCard, fields: dict[str, Any]) -> None:
    """Mirror a successful card update in the cached board until the next refresh."""
    for cards in board.cards.values():
        cards[:] = [c for c in cards if c.id != card.id]
    if fields.get("closed"):
        return
    list_id = fields.get("idList", card.list_id)
    target = board.cards.setdefault(list_id, [])
    positions = [c.pos for c in target]
    pos = fields.get("pos", card.pos)
    if pos == "top":
        pos = (min(positions) - 1) if positions else 0.0
    elif pos == "bottom":
        pos = (max(positions) + 1) if positions else 0.0
    target.append(
        replace(
            card,
            name=fields.get("name", card.name),
            desc=fields.get("desc", card.desc),
            due=fields.get("due", card.due),
            start=fields.get("start", card.start),
            due_complete=fields.get("dueComplete", card.due_complete),
            list_id=list_id,
            pos=float(pos),
        )
    )
    target.sort(key=lambda c: c.pos)


async def async_card_write(
    coordinator: TrelloBoardCoordinator,
    write: Callable[[TrelloClient], Awaitable[Any]],
    local: Callable[[TrelloBoard, Any], None] | None = None,
    *,
    card: str | None = None,
) -> None:
    """Run an API write, map its errors, mirror it locally, then refresh the board.

    With `card`, Trello's "not found / no access" means that card is gone (card_not_found).
    """
    try:
        result = await write(coordinator.client)
        if local is not None and (board := coordinator.data) is not None:
            try:
                local(board, result)
            except (KeyError, TypeError, ValueError) as err:  # the refresh below corrects the board anyway
                _LOGGER.debug("Could not mirror a Trello write locally: %s", err)
            else:
                coordinator.async_set_updated_data(board)
    except ConfigEntryAuthFailed as err:
        # OAuth2TokenRequestReauthError: OAuth2Session has already started reauth.
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed") from err
    except TrelloAuthError as err:
        coordinator.config_entry.async_start_reauth(coordinator.hass)
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed") from err
    except (TrelloError, ClientError) as err:  # ClientError: a failed token refresh
        if card is not None and isinstance(err, TrelloAccessError):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="card_not_found", translation_placeholders={"card": card}
            ) from err
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="api_error", translation_placeholders={"error": reason_code(err)}
        ) from err
    finally:
        await coordinator.async_request_refresh()

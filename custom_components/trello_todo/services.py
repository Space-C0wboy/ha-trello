"""Trello actions for automations: create, copy, move, archive/restore, due, comments, labels, members, checklists."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import wraps
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.util import dt as dt_util

from .api import TrelloAccessError, TrelloAuthError, TrelloClient, TrelloError
from .const import DOMAIN
from .coordinator import TrelloBoardCoordinator
from .resolve import (
    ResolveError,
    find_card_in_list,
    parse_card_ref,
    resolve_board,
    resolve_label,
    resolve_list,
    resolve_member,
)
from .todo import due_to_trello
from .webhooks import _reason

POSITIONS = ["top", "bottom"]
KEEP = ["all", "checklists", "labels", "members", "due", "none"]

CARD_REF = {
    vol.Optional("card"): cv.string,
    vol.Optional("entity_id"): cv.entity_id,
    vol.Optional("card_name"): cv.string,
}


def _one_card_ref(data: dict[str, Any]) -> dict[str, Any]:
    by_card = bool(data.get("card"))
    by_entity = bool(data.get("entity_id")) or bool(data.get("card_name"))
    if by_card == by_entity:
        raise vol.Invalid("Give either `card`, or `entity_id` together with `card_name`")
    if by_entity and not (data.get("entity_id") and data.get("card_name")):
        raise vol.Invalid("`entity_id` and `card_name` go together")
    return data


def _card_schema(extra: dict[Any, Any]) -> vol.Schema:
    return vol.Schema(vol.All({**CARD_REF, **extra}, _one_card_ref))


@dataclass
class CardTarget:
    coordinator: TrelloBoardCoordinator
    card_id: str
    list_id: str
    name: str
    url: str


# -- lookups -----------------------------------------------------------------------------------------
def _boards(hass: HomeAssistant) -> dict[str, TrelloBoardCoordinator]:
    boards: dict[str, TrelloBoardCoordinator] = {}
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        for coordinator in entry.runtime_data.boards.values():
            boards[coordinator.board_id] = coordinator
    return boards


def _with_data(coordinator: TrelloBoardCoordinator) -> TrelloBoardCoordinator:
    if coordinator.data is None:
        raise ResolveError("board_unavailable", board=coordinator.board_title)
    return coordinator


def _board_target(
    hass: HomeAssistant, value: str | None, default: TrelloBoardCoordinator | None
) -> TrelloBoardCoordinator:
    boards = _boards(hass)
    if value:
        board_id = resolve_board({bid: c.board_title for bid, c in boards.items()}, value)
        return _with_data(boards[board_id])
    if default is not None:
        return _with_data(default)
    if len(boards) == 1:
        return _with_data(next(iter(boards.values())))
    raise ResolveError("board_required")


def _todo_list(hass: HomeAssistant, entity_id: str) -> tuple[TrelloBoardCoordinator, str]:
    """A Trello to-do entity → (its board's coordinator, its list id)."""
    reg = er.async_get(hass).async_get(entity_id)
    if reg is None or reg.platform != DOMAIN or reg.domain != "todo":
        raise ResolveError("not_a_trello_list", entity_id=entity_id)
    entry = hass.config_entries.async_get_entry(reg.config_entry_id or "")
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise ResolveError("not_a_trello_list", entity_id=entity_id)
    coordinator = entry.runtime_data.boards.get(reg.config_subentry_id)
    if coordinator is None:
        raise ResolveError("not_a_trello_list", entity_id=entity_id)
    return _with_data(coordinator), reg.unique_id


async def _resolve_card(hass: HomeAssistant, data: dict[str, Any]) -> CardTarget:
    if data.get("card"):
        ref = parse_card_ref(data["card"])
        boards = _boards(hass)
        accounts = {id(c.client): c for c in boards.values()}  # one client per Trello account
        for coordinator in accounts.values():
            try:
                raw = await coordinator.client.get_card(ref)
            except TrelloAccessError:
                continue
            except (TrelloError, ClientError, HomeAssistantError) as err:
                _raise_api(hass, coordinator, err)
            target = boards.get(raw.get("idBoard", ""))
            if target is None:
                raise ResolveError("card_board_not_configured", card=data["card"])
            return CardTarget(
                _with_data(target), raw["id"], raw.get("idList", ""), raw.get("name") or "", raw.get("shortUrl") or ""
            )
        raise ResolveError("card_not_found", card=data["card"])
    coordinator, list_id = _todo_list(hass, data["entity_id"])
    card = find_card_in_list(coordinator.data.cards.get(list_id, []), data["card_name"])
    return CardTarget(coordinator, card.id, card.list_id, card.name, card.url)


# -- calls, errors, responses -------------------------------------------------------------------------
def _raise_api(hass: HomeAssistant, coordinator: TrelloBoardCoordinator, err: Exception) -> None:
    if isinstance(err, TrelloAuthError):
        coordinator.config_entry.async_start_reauth(hass)
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed") from err
    raise HomeAssistantError(
        translation_domain=DOMAIN, translation_key="api_error", translation_placeholders={"error": _reason(err)}
    ) from err


async def _api(
    hass: HomeAssistant,
    coordinator: TrelloBoardCoordinator,
    call: Callable[[TrelloClient], Awaitable[Any]],
    *,
    card: str = "",
    ok_statuses: tuple[int, ...] = (),
) -> Any:
    """Run one Trello call; map its errors. `ok_statuses` makes e.g. "label already on card" a success."""
    try:
        return await call(coordinator.client)
    except TrelloError as err:
        if err.status in ok_statuses:
            return None
        if isinstance(err, TrelloAccessError):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="card_not_found", translation_placeholders={"card": card}
            ) from err
        _raise_api(hass, coordinator, err)
    except (ClientError, HomeAssistantError) as err:  # a failed token refresh
        _raise_api(hass, coordinator, err)


def _info(coordinator: TrelloBoardCoordinator, raw: dict[str, Any], **extra: Any) -> dict[str, Any]:
    board = coordinator.data
    list_id = raw.get("idList") or ""
    return {
        "card_id": raw.get("id"),
        "card_url": raw.get("shortUrl") or raw.get("url"),
        "card_name": raw.get("name"),
        "list_id": list_id,
        "list_name": board.lists.get(list_id) if board else None,
        "board_id": coordinator.board_id,
        "board_name": coordinator.board_title,
        **extra,
    }


def _target_info(target: CardTarget, **extra: Any) -> dict[str, Any]:
    return _info(
        target.coordinator,
        {"id": target.card_id, "shortUrl": target.url, "name": target.name, "idList": target.list_id},
        **extra,
    )


Result = tuple[dict[str, Any], list[TrelloBoardCoordinator]]


def _handler(fn: Callable[[ServiceCall], Awaitable[Result]]) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    @wraps(fn)
    async def wrapper(call: ServiceCall) -> ServiceResponse:
        try:
            info, touched = await fn(call)
        except ResolveError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key=err.key, translation_placeholders=err.placeholders
            ) from err
        for coordinator in dict.fromkeys(touched):  # dedupe, keep order
            await coordinator.async_request_refresh()
        return info if call.return_response else None

    return wrapper


def _due(value: Any) -> str | None:
    """Date → local midnight; naive datetime → local time; empty → clear."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return due_to_trello(value)
    else:
        text = str(value).strip()
        if len(text) == 10 and (day := dt_util.parse_date(text)) is not None:
            return due_to_trello(day)
        if (parsed := dt_util.parse_datetime(text)) is None:
            raise ResolveError("invalid_due", value=text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    return due_to_trello(parsed)


# -- card actions -------------------------------------------------------------------------------------
CREATE_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): cv.entity_id,
        vol.Optional("board"): cv.string,
        vol.Optional("list"): cv.string,
        vol.Required("name"): cv.string,
        vol.Optional("description"): cv.string,
        vol.Optional("due"): vol.Any(None, cv.string),
        vol.Optional("labels"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("members"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("position", default="bottom"): vol.In(POSITIONS),
    }
)


@_handler
async def _create_card(call: ServiceCall) -> Result:
    hass, data = call.hass, call.data
    if data.get("entity_id"):
        if data.get("list") or data.get("board"):
            raise ResolveError("create_target_conflict")
        coordinator, list_id = _todo_list(hass, data["entity_id"])
    else:
        if not data.get("list"):
            raise ResolveError("list_required")
        coordinator = _board_target(hass, data.get("board"), None)
        list_id = resolve_list(coordinator.data, data["list"])
    board = coordinator.data
    fields: dict[str, Any] = {"idList": list_id, "name": data["name"], "pos": data["position"]}
    if data.get("description"):
        fields["desc"] = data["description"]
    if (due := _due(data.get("due"))) is not None:
        fields["due"] = due
    if data.get("labels"):
        fields["idLabels"] = ",".join(resolve_label(board, value) for value in data["labels"])
    if data.get("members"):
        fields["idMembers"] = ",".join(resolve_member(board, value) for value in data["members"])
    raw = await _api(hass, coordinator, lambda client: client.create_card(fields))
    return _info(coordinator, raw), [coordinator]


COPY_SCHEMA = _card_schema(
    {
        vol.Required("list"): cv.string,
        vol.Optional("board"): cv.string,
        vol.Optional("name"): cv.string,
        vol.Optional("keep", default="all"): vol.In(KEEP),
        vol.Optional("position", default="bottom"): vol.In(POSITIONS),
    }
)


@_handler
async def _copy_card(call: ServiceCall) -> Result:
    hass, data = call.hass, call.data
    target = await _resolve_card(hass, data)
    dest = _board_target(hass, data.get("board"), target.coordinator)
    fields: dict[str, Any] = {
        "idList": resolve_list(dest.data, data["list"]),
        "idCardSource": target.card_id,
        "keepFromSource": {"all": "all", "none": ""}.get(data["keep"], data["keep"]),
        "pos": data["position"],
    }
    if data.get("name"):
        fields["name"] = data["name"]
    raw = await _api(hass, dest, lambda client: client.create_card(fields), card=target.card_id)
    return _info(dest, raw), [dest]


MOVE_SCHEMA = _card_schema(
    {
        vol.Required("list"): cv.string,
        vol.Optional("board"): cv.string,
        vol.Optional("position", default="top"): vol.In(POSITIONS),
    }
)


@_handler
async def _move_card(call: ServiceCall) -> Result:
    hass, data = call.hass, call.data
    target = await _resolve_card(hass, data)
    dest = _board_target(hass, data.get("board"), target.coordinator)
    fields: dict[str, Any] = {"idList": resolve_list(dest.data, data["list"]), "pos": data["position"]}
    if dest.board_id != target.coordinator.board_id:
        fields["idBoard"] = dest.board_id
    raw = await _api(
        hass, target.coordinator, lambda client: client.update_card(target.card_id, fields), card=target.card_id
    )
    return _info(dest, raw), [target.coordinator, dest]


CARD_ONLY_SCHEMA = _card_schema({})


def _closed_handler(closed: bool) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    @_handler
    async def handle(call: ServiceCall) -> Result:
        target = await _resolve_card(call.hass, call.data)
        raw = await _api(
            call.hass,
            target.coordinator,
            lambda client: client.update_card(target.card_id, {"closed": closed}),
            card=target.card_id,
        )
        return _info(target.coordinator, raw), [target.coordinator]

    return handle


SET_DUE_SCHEMA = _card_schema(
    {vol.Optional("due"): vol.Any(None, cv.string), vol.Optional("due_complete"): cv.boolean}
)


@_handler
async def _set_due(call: ServiceCall) -> Result:
    hass, data = call.hass, call.data
    if "due" not in data and "due_complete" not in data:
        raise ResolveError("due_required")
    target = await _resolve_card(hass, data)
    fields: dict[str, Any] = {}
    if "due" in data:
        fields["due"] = _due(data["due"])
    if "due_complete" in data:
        fields["dueComplete"] = data["due_complete"]
    raw = await _api(
        hass, target.coordinator, lambda client: client.update_card(target.card_id, fields), card=target.card_id
    )
    return _info(target.coordinator, raw), [target.coordinator]


# -- registration -------------------------------------------------------------------------------------
SERVICES: dict[str, tuple[Callable[[ServiceCall], Awaitable[ServiceResponse]], vol.Schema]] = {
    "create_card": (_create_card, CREATE_SCHEMA),
    "copy_card": (_copy_card, COPY_SCHEMA),
    "move_card": (_move_card, MOVE_SCHEMA),
    "archive_card": (_closed_handler(True), CARD_ONLY_SCHEMA),
    "restore_card": (_closed_handler(False), CARD_ONLY_SCHEMA),
    "set_due": (_set_due, SET_DUE_SCHEMA),
}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    for name, (handler, schema) in SERVICES.items():
        hass.services.async_register(DOMAIN, name, handler, schema=schema, supports_response=SupportsResponse.OPTIONAL)

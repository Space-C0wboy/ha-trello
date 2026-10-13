"""One to-do entity per open Trello list, added and removed as the board changes."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date, datetime, time
from typing import Any

from aiohttp import ClientError
from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util, slugify

from .api import TrelloAuthError, TrelloBoard, TrelloCard, TrelloClient, TrelloError, parse_card
from .const import DOMAIN, DONE_ARCHIVE, DONE_MOVE, DONE_MOVE_AND_ARCHIVE, MAX_ITEMS_ATTRIBUTE
from .coordinator import TrelloBoardCoordinator, TrelloConfigEntry

PARALLEL_UPDATES = 1

# Gap left after the last card when moving a card to the bottom of a list.
POS_STEP = 16384.0


async def async_setup_entry(
    hass: HomeAssistant, entry: TrelloConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    for subentry_id, coordinator in entry.runtime_data.boards.items():
        tracker = _ListTracker(hass, entry, subentry_id, coordinator, async_add_entities)
        entry.async_on_unload(coordinator.async_add_listener(tracker.sync))
        tracker.sync()


class _ListTracker:
    """Keeps one entity per open list: adds new lists, removes archived ones (and their registry entries)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TrelloConfigEntry,
        subentry_id: str,
        coordinator: TrelloBoardCoordinator,
        async_add_entities: AddConfigEntryEntitiesCallback,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._subentry_id = subentry_id
        self._coordinator = coordinator
        self._add = async_add_entities
        self._known: set[str] = set()
        self._purged = False

    @callback
    def sync(self) -> None:
        board = self._coordinator.data
        if board is None:  # this board hasn't loaded yet; entities come with its first good refresh
            return
        registry = er.async_get(self._hass)
        if not self._purged:
            # Lists archived while HA was down still have registry entries; drop them once.
            self._purged = True
            for reg in er.async_entries_for_config_entry(registry, self._entry.entry_id):
                if reg.config_subentry_id == self._subentry_id and reg.unique_id not in board.lists:
                    registry.async_remove(reg.entity_id)
        new = []
        for list_id in board.lists:
            if list_id in self._known:
                continue
            owner = registry.async_get_entity_id("todo", DOMAIN, list_id)
            if owner and (reg := registry.async_get(owner)) and reg.config_subentry_id != self._subentry_id:
                # Still another board's list (mid "Move list"): take it over once that board lets go of it.
                continue
            new.append(list_id)
        if new:
            self._known.update(new)
            self._add(
                [TrelloTodoList(self._coordinator, list_id) for list_id in new],
                config_subentry_id=self._subentry_id,
            )
        for list_id in [list_id for list_id in self._known if list_id not in board.lists]:
            self._known.discard(list_id)
            entity_id = registry.async_get_entity_id("todo", DOMAIN, list_id)
            if entity_id and (reg := registry.async_get(entity_id)) and reg.config_subentry_id == self._subentry_id:
                registry.async_remove(entity_id)


def due_from_trello(due: str | None) -> date | datetime | None:
    """Local midnight means "a day"; anything else is a point in time."""
    if not due or (parsed := dt_util.parse_datetime(due)) is None:
        return None
    local = dt_util.as_local(parsed)
    return local.date() if local.time() == time() else local


def due_to_trello(due: date | datetime | None) -> str | None:
    if due is None:
        return None
    if isinstance(due, datetime):
        return dt_util.as_utc(due).isoformat()
    return dt_util.as_utc(dt_util.start_of_local_day(due)).isoformat()


def _due_iso(due: str | None) -> str | None:
    if not due or (parsed := dt_util.parse_datetime(due)) is None:
        return None
    return dt_util.as_utc(parsed).isoformat()


def _apply_card_fields(board: TrelloBoard, card: TrelloCard, fields: dict[str, Any]) -> None:
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
            list_id=list_id,
            pos=float(pos),
        )
    )
    target.sort(key=lambda c: c.pos)


class TrelloTodoList(CoordinatorEntity[TrelloBoardCoordinator], TodoListEntity):
    """A Trello list as a to-do list. Cards in the board's done list show as completed."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.MOVE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    # The dashboard reads these from state; they churn and don't belong in history.
    _unrecorded_attributes = frozenset({"items", "items_total"})

    def __init__(self, coordinator: TrelloBoardCoordinator, list_id: str) -> None:
        super().__init__(coordinator)
        self._list_id = list_id
        board = coordinator.data
        self._attr_unique_id = list_id
        self._attr_device_info = coordinator.device_info
        # Entity ids follow the list name alone (todo.chores), not "<board> <list>".
        # slugify() returns "unknown" for names with no sluggable characters; let HA name those.
        slug = slugify(board.lists[list_id])
        if slug and slug != "unknown":
            self.entity_id = f"todo.{slug}"
        self._update_from_board()

    @property
    def available(self) -> bool:
        board = self.coordinator.data
        return super().available and board is not None and self._list_id in board.lists

    @property
    def is_done_list(self) -> bool:
        return self._list_id == self.coordinator.done_list

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_board()
        super()._handle_coordinator_update()

    def _cards(self) -> list[TrelloCard]:
        board = self.coordinator.data
        return board.cards.get(self._list_id, []) if board else []

    def _card(self, uid: str | None) -> TrelloCard:
        for card in self._cards():
            if card.id == uid:
                return card
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="item_not_found", translation_placeholders={"uid": str(uid)}
        )

    def _update_from_board(self) -> None:
        board = self.coordinator.data
        if board and self._list_id in board.lists:
            self._attr_name = board.lists[self._list_id]
        status = TodoItemStatus.COMPLETED if self.is_done_list else TodoItemStatus.NEEDS_ACTION
        cards = self._cards()
        self._attr_todo_items = [
            TodoItem(
                uid=c.id,
                summary=c.name,
                description=c.desc or None,
                due=due_from_trello(c.due),
                status=status,
            )
            for c in cards
        ]
        self._attr_extra_state_attributes = {
            "items": [
                {
                    "uid": c.id,
                    "summary": c.name,
                    "due": _due_iso(c.due),
                    "labels": [{"name": lb.name, "color": lb.color} for lb in c.labels],
                    "members": list(c.members),
                    "url": c.url,
                }
                for c in cards[:MAX_ITEMS_ATTRIBUTE]
            ],
            "items_total": len(cards),
        }

    async def _write(
        self,
        write: Callable[[TrelloClient], Awaitable[Any]],
        local: Callable[[TrelloBoard, Any], None] | None = None,
    ) -> None:
        """Run an API write, map its errors, mirror it locally, then refresh this board."""
        try:
            result = await write(self.coordinator.client)
            if local is not None and (board := self.coordinator.data) is not None:
                local(board, result)
                self.coordinator.async_set_updated_data(board)
        except ConfigEntryAuthFailed as err:
            # OAuth2TokenRequestReauthError: OAuth2Session has already started reauth.
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed") from err
        except TrelloAuthError as err:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed") from err
        except (TrelloError, ClientError) as err:  # ClientError: a failed token refresh
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="api_error", translation_placeholders={"error": str(err)}
            ) from err
        finally:
            await self.coordinator.async_request_refresh()

    def _changed_fields(self, card: TrelloCard, item: TodoItem) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if item.summary is not None and item.summary != card.name:
            fields["name"] = item.summary
        if (item.description or "") != card.desc:
            fields["desc"] = item.description or ""
        if item.due != due_from_trello(card.due):
            fields["due"] = due_to_trello(item.due)
        return fields

    def _done_fields(self) -> dict[str, Any]:
        behavior = self.coordinator.done_behavior
        done_list = self.coordinator.done_list
        fields: dict[str, Any] = {}
        if behavior in (DONE_MOVE, DONE_MOVE_AND_ARCHIVE):
            if done_list not in self.coordinator.data.lists:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="done_list_missing",
                    translation_placeholders={"board": self.coordinator.board_title},
                )
            fields |= {"idList": done_list, "pos": "top"}
        if behavior in (DONE_ARCHIVE, DONE_MOVE_AND_ARCHIVE):
            fields["closed"] = True
        return fields

    async def _return_list(self, client: TrelloClient, card: TrelloCard) -> str:
        """Where an un-checked card goes: the list it came from, else the board's first other list."""
        lists = self.coordinator.data.lists
        previous = await client.get_last_list_move(card.id)
        if previous in lists and previous != self._list_id:
            return previous
        if fallback := next((list_id for list_id in lists if list_id != self._list_id), None):
            return fallback
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_return_list")

    async def async_create_todo_item(self, item: TodoItem) -> None:
        fields: dict[str, Any] = {"idList": self._list_id, "name": item.summary or "", "desc": item.description or ""}
        if item.due is not None:
            fields["due"] = due_to_trello(item.due)
        fields["pos"] = "bottom"
        await self._write(
            lambda client: client.create_card(fields),
            lambda board, raw: board.cards.setdefault(self._list_id, []).append(parse_card(raw, {}, {})),
        )

    async def async_update_todo_item(self, item: TodoItem) -> None:
        card = self._card(item.uid)
        fields = self._changed_fields(card, item)
        if self.is_done_list and item.status == TodoItemStatus.NEEDS_ACTION:

            async def reopen(client: TrelloClient) -> str:
                target = await self._return_list(client, card)
                await client.update_card(card.id, {**fields, "idList": target, "pos": "top"})
                return target

            await self._write(
                reopen,
                lambda board, target: _apply_card_fields(board, card, {**fields, "idList": target, "pos": "top"}),
            )
            return
        if not self.is_done_list and item.status == TodoItemStatus.COMPLETED:
            fields |= self._done_fields()
        if fields:
            await self._write(
                lambda client: client.update_card(card.id, fields),
                lambda board, _: _apply_card_fields(board, card, fields),
            )

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Archive, never hard-delete: archived cards can be restored in Trello."""
        cards = [self._card(uid) for uid in uids]

        async def archive(client: TrelloClient) -> None:
            for card in cards:
                await client.update_card(card.id, {"closed": True})

        await self._write(archive, lambda board, _: [_apply_card_fields(board, c, {"closed": True}) for c in cards])

    async def async_move_todo_item(self, uid: str, previous_uid: str | None = None) -> None:
        card = self._card(uid)
        if previous_uid == uid:
            return  # "move after itself" leaves the card where it is
        others = [c for c in self._cards() if c.id != uid]
        if previous_uid is None:
            pos = others[0].pos / 2 if others else POS_STEP
        else:
            prev = self._card(previous_uid)
            index = others.index(prev)
            pos = (prev.pos + others[index + 1].pos) / 2 if index + 1 < len(others) else prev.pos + POS_STEP
        await self._write(
            lambda client: client.update_card(card.id, {"pos": pos}),
            lambda board, _: _apply_card_fields(board, card, {"pos": pos}),
        )

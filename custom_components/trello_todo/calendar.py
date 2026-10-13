"""One calendar per board: every card with a due date is an event ending at its due time."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEntityFeature, CalendarEvent
from homeassistant.components.calendar.const import EVENT_END, EVENT_START
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import TrelloBoard, TrelloCard
from .const import DOMAIN
from .coordinator import TrelloBoardCoordinator, TrelloConfigEntry
from .dates import card_is_complete, parse_trello_time
from .entity import add_when_loaded
from .writes import apply_card_fields, async_card_write

PARALLEL_UPDATES = 1

DEFAULT_LENGTH = timedelta(minutes=30)
COMPLETE_PREFIX = "✓ "


async def async_setup_entry(
    hass: HomeAssistant, entry: TrelloConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    for subentry_id, coordinator in entry.runtime_data.boards.items():
        add_when_loaded(entry, subentry_id, coordinator, async_add_entities, lambda c: [TrelloDueCalendar(c)])


def card_event(board: TrelloBoard, card: TrelloCard, done_list: str | None) -> CalendarEvent | None:
    """The card as an event ending at its due time; None without a usable due date."""
    if (due := parse_trello_time(card.due)) is None:
        return None
    start = parse_trello_time(card.start)
    if start is None or start >= due:
        start = due - DEFAULT_LENGTH
    details = f"List: {board.lists.get(card.list_id, '')}\n{card.url}"
    return CalendarEvent(
        start=dt_util.as_local(start),
        end=dt_util.as_local(due),
        summary=(COMPLETE_PREFIX if card_is_complete(card, done_list) else "") + card.name,
        description=f"{card.desc}\n\n{details}" if card.desc else details,
        uid=card.id,
    )


class TrelloDueCalendar(CoordinatorEntity[TrelloBoardCoordinator], CalendarEntity):
    """Due dates on one board. Moving an event changes the due date; deleting it clears the due date."""

    _attr_has_entity_name = True
    _attr_translation_key = "due_dates"
    _attr_supported_features = CalendarEntityFeature.UPDATE_EVENT | CalendarEntityFeature.DELETE_EVENT

    def __init__(self, coordinator: TrelloBoardCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.board_id}_calendar"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    def _events(self) -> list[CalendarEvent]:
        board = self.coordinator.data
        if board is None:
            return []
        events = [
            event
            for cards in board.cards.values()
            for card in cards
            if (event := card_event(board, card, self.coordinator.done_list)) is not None
        ]
        return sorted(events, key=lambda e: e.start)

    @property
    def event(self) -> CalendarEvent | None:
        """The event in progress, else the next one."""
        now = dt_util.now()
        return next((e for e in self._events() if e.end > now), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [e for e in self._events() if e.start < end_date and e.end > start_date]

    def _card(self, uid: str) -> TrelloCard:
        if (board := self.coordinator.data) is None or (card := board.card(uid)) is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="card_not_found", translation_placeholders={"card": uid}
            )
        return card

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        card = self._card(uid)
        start, end = event.get(EVENT_START), event.get(EVENT_END)
        if not isinstance(start, datetime) or not isinstance(end, datetime) or start >= end:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_event_times")
        fields = {
            "due": dt_util.as_utc(end).isoformat(),
            "start": None if end - start == DEFAULT_LENGTH else dt_util.as_utc(start).isoformat(),
        }
        await self._write(card, fields)

    async def async_delete_event(
        self, uid: str, recurrence_id: str | None = None, recurrence_range: str | None = None
    ) -> None:
        await self._write(self._card(uid), {"due": None, "start": None})

    async def _write(self, card: TrelloCard, fields: dict[str, Any]) -> None:
        await async_card_write(
            self.coordinator,
            lambda client: client.update_card(card.id, fields),
            lambda board, _: apply_card_fields(board, card, fields),
            card=card.id,
        )

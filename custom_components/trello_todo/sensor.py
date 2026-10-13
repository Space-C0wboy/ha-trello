"""Due-date summary sensors per board: overdue, due today, due soon, next due."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import TrelloBoard, TrelloCard
from .const import DUE_SOON_DAYS, MAX_CARDS_ATTRIBUTE
from .coordinator import TrelloBoardCoordinator, TrelloConfigEntry
from .dates import card_is_complete, parse_trello_time
from .entity import add_when_loaded

PARALLEL_UPDATES = 0

TICK = timedelta(minutes=1)
UNASSIGNED = "Unassigned"
COUNT_KEYS = ("overdue", "due_today", "due_soon")

type Bucket = list[tuple[datetime, TrelloCard]]


async def async_setup_entry(
    hass: HomeAssistant, entry: TrelloConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    for subentry_id, coordinator in entry.runtime_data.boards.items():
        due = _BoardDue(coordinator)
        # Registered before the sensors' own listeners, so a board update invalidates first, then they recompute.
        entry.async_on_unload(coordinator.async_add_listener(due.invalidate))
        entry.async_on_unload(
            async_track_time_interval(
                hass, due.tick, TICK, name=f"trello due {coordinator.board_title}", cancel_on_shutdown=True
            )
        )
        add_when_loaded(
            entry,
            subentry_id,
            coordinator,
            async_add_entities,
            lambda c, due=due: [*(TrelloDueCountSensor(c, due, key) for key in COUNT_KEYS), TrelloNextDueSensor(c, due)],
        )


def due_buckets(board: TrelloBoard, done_list: str | None, now: datetime) -> dict[str, Bucket]:
    """Open cards with a due date, by window. "Today" ends at the next local midnight."""
    midnight = dt_util.start_of_local_day(dt_util.as_local(now).date() + timedelta(days=1))
    soon_end = now + timedelta(days=DUE_SOON_DAYS)
    cards = sorted(
        (
            (due, card)
            for list_cards in board.cards.values()
            for card in list_cards
            if not card_is_complete(card, done_list) and (due := parse_trello_time(card.due)) is not None
        ),
        key=lambda item: item[0],
    )
    return {
        "overdue": [(d, c) for d, c in cards if d <= now],
        "due_today": [(d, c) for d, c in cards if now < d < midnight],
        "due_soon": [(d, c) for d, c in cards if midnight <= d < soon_end],
        "upcoming": [(d, c) for d, c in cards if d > now],
    }


def _members(board: TrelloBoard, card: TrelloCard) -> list[str]:
    return [board.members.get(member_id, "") for member_id in card.member_ids]


class _BoardDue:
    """One board's due buckets, shared by its four sensors: computed once per board update and once a minute."""

    def __init__(self, coordinator: TrelloBoardCoordinator) -> None:
        self._coordinator = coordinator
        self._buckets: dict[str, Bucket] | None = None
        self.sensors: list[_DueSensor] = []

    @callback
    def invalidate(self) -> None:
        self._buckets = None

    def buckets(self) -> dict[str, Bucket] | None:
        if self._buckets is None and (board := self._coordinator.data) is not None:
            self._buckets = due_buckets(board, self._coordinator.done_list, dt_util.utcnow())
        return self._buckets

    @callback
    def tick(self, _now: datetime) -> None:
        """Time moves cards between windows even when nothing changes in Trello."""
        self._buckets = None
        for sensor in list(self.sensors):
            sensor.async_tick()


class _DueSensor(CoordinatorEntity[TrelloBoardCoordinator], SensorEntity):
    """Recomputes from the board's shared buckets on every board update and once a minute."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TrelloBoardCoordinator, due: _BoardDue, key: str) -> None:
        super().__init__(coordinator)
        self._due = due
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.board_id}_{key}"
        self._attr_device_info = coordinator.device_info
        self._recompute()

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._due.sensors.append(self)
        self.async_on_remove(lambda: self._due.sensors.remove(self))

    @callback
    def async_tick(self) -> None:
        before = (self._attr_native_value, self._attr_extra_state_attributes)
        self._recompute()
        if (self._attr_native_value, self._attr_extra_state_attributes) != before:
            self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._recompute()
        super()._handle_coordinator_update()

    def _recompute(self) -> None:
        board = self.coordinator.data
        if board is None or (buckets := self._due.buckets()) is None:
            return
        self._update(board, buckets)

    def _update(self, board: TrelloBoard, buckets: dict[str, Bucket]) -> None:
        raise NotImplementedError


class TrelloDueCountSensor(_DueSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT
    # The card list churns and doesn't belong in history.
    _unrecorded_attributes = frozenset({"cards"})

    def _update(self, board: TrelloBoard, buckets: dict[str, Bucket]) -> None:
        bucket = buckets[self._key]
        by_member: dict[str, int] = {}
        for _, card in bucket:
            for name in _members(board, card) or [UNASSIGNED]:
                by_member[name] = by_member.get(name, 0) + 1
        self._attr_native_value = len(bucket)
        self._attr_extra_state_attributes = {
            "by_member": dict(sorted(by_member.items())),
            "cards": [
                {
                    "name": card.name,
                    "due": due.isoformat(),
                    "list": board.lists.get(card.list_id, ""),
                    "members": _members(board, card),
                }
                for due, card in bucket[:MAX_CARDS_ATTRIBUTE]
            ],
        }


class TrelloNextDueSensor(_DueSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: TrelloBoardCoordinator, due: _BoardDue) -> None:
        super().__init__(coordinator, due, "next_due")

    def _update(self, board: TrelloBoard, buckets: dict[str, Bucket]) -> None:
        if not (upcoming := buckets["upcoming"]):
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        due, card = upcoming[0]
        self._attr_native_value = due
        self._attr_extra_state_attributes = {
            "card": card.name,
            "list": board.lists.get(card.list_id, ""),
            "members": _members(board, card),
            "url": card.url,
        }

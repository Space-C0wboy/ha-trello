"""One activity event entity per board: fires an event for each Trello activity."""
from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .activity import EVENT_TYPES
from .coordinator import TrelloBoardCoordinator, TrelloConfigEntry
from .entity import add_when_loaded

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: TrelloConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    for subentry_id, coordinator in entry.runtime_data.boards.items():
        add_when_loaded(entry, subentry_id, coordinator, async_add_entities, lambda c: [TrelloActivityEvent(c)])


class TrelloActivityEvent(CoordinatorEntity[TrelloBoardCoordinator], EventEntity):
    """Fires `card_created`, `comment_added`, `card_moved`, ... with the details as attributes."""

    _attr_has_entity_name = True
    _attr_translation_key = "activity"
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: TrelloBoardCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.board_id}_activity"
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.processor.add_listener(self._handle_activity))

    @callback
    def _handle_activity(self, event_type: str, attributes: dict[str, Any]) -> None:
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # Not CoordinatorEntity.available: a 429/5xx refresh must not swallow events arriving meanwhile.
        return self.coordinator.data is not None and not self.coordinator.inaccessible

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"source": self.coordinator.mode}

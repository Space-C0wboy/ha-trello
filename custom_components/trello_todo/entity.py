"""Entity setup shared by the per-board platforms."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrelloBoardCoordinator, TrelloConfigEntry


def add_when_loaded(
    entry: TrelloConfigEntry,
    subentry_id: str,
    coordinator: TrelloBoardCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    make: Callable[[TrelloBoardCoordinator], list[Entity]],
) -> None:
    """Per-board entities need the board's data (device name); a board that was down at startup gets them later."""
    if coordinator.data is not None:
        async_add_entities(make(coordinator), config_subentry_id=subentry_id)
        return
    remove = None

    @callback
    def _first_data() -> None:
        nonlocal remove
        if coordinator.data is None or remove is None:
            return
        remove()
        remove = None
        async_add_entities(make(coordinator), config_subentry_id=subentry_id)

    remove = coordinator.async_add_listener(_first_data)

    @callback
    def _cleanup() -> None:
        if remove is not None:
            remove()

    entry.async_on_unload(_cleanup)

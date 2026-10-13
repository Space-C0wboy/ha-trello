"""Due-date helpers shared by the calendar and the sensors."""
from __future__ import annotations

from datetime import datetime

from homeassistant.util import dt as dt_util

from .api import TrelloCard


def parse_trello_time(value: str | None) -> datetime | None:
    """Trello's ISO 8601 UTC string as an aware UTC datetime; None when missing or unparseable."""
    if not value:
        return None
    try:
        parsed = dt_util.parse_datetime(value)
    except ValueError:
        return None
    return dt_util.as_utc(parsed) if parsed is not None else None


def card_is_complete(card: TrelloCard, done_list: str | None) -> bool:
    """Ticked "due complete" in Trello, or sitting in the board's done list."""
    return card.due_complete or (done_list is not None and card.list_id == done_list)

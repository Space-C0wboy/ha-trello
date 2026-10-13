"""Turn names into Trello ids: cards, boards, lists, labels, members. No Home Assistant imports."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from .api import TrelloBoard, TrelloCard

_CARD_URL = re.compile(r"trello\.com/c/([A-Za-z0-9]+)")


class ResolveError(Exception):
    """A name that doesn't resolve; `key` is a translation key under `exceptions`."""

    def __init__(self, key: str, **placeholders: object) -> None:
        super().__init__(key)
        self.key = key
        self.placeholders = {k: str(v) for k, v in placeholders.items()}


def parse_card_ref(value: str) -> str:
    """A card id, short link, or trello.com/c/<shortLink>/... URL → what /cards/{id} accepts."""
    text = (value or "").strip()
    if match := _CARD_URL.search(text):
        return match.group(1)
    if not text:
        raise ResolveError("card_not_found", card=value)
    return text


def _pick(kind: str, value: str, candidates: Mapping[str, Iterable[str]], board: str) -> str:
    """candidates: id -> names it answers to. Exact id first, then one case-insensitive name match."""
    if value in candidates:
        return value
    wanted = value.strip().casefold()
    hits = [cid for cid, names in candidates.items() if any(n and n.casefold() == wanted for n in names)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ResolveError(f"{kind}_ambiguous", value=value, board=board)
    raise ResolveError(f"{kind}_not_found", value=value, board=board)


def resolve_board(boards: Mapping[str, str], value: str) -> str:
    return _pick("board", value, {bid: (name,) for bid, name in boards.items()}, board="")


def resolve_list(board: TrelloBoard, value: str) -> str:
    return _pick("list", value, {lid: (name,) for lid, name in board.lists.items()}, board.name)


def resolve_label(board: TrelloBoard, value: str) -> str:
    """Name or id; or a colour when exactly one unnamed label has it (Trello allows colour-only labels)."""
    try:
        return _pick("label", value, {lid: (lb.name,) for lid, lb in board.labels.items()}, board.name)
    except ResolveError as err:
        if err.key != "label_not_found":
            raise
    wanted = value.strip().casefold()
    unnamed = [lid for lid, lb in board.labels.items() if not lb.name and (lb.color or "").casefold() == wanted]
    if len(unnamed) == 1:
        return unnamed[0]
    if unnamed:
        raise ResolveError("label_ambiguous", value=value, board=board.name)
    raise ResolveError("label_not_found", value=value, board=board.name)


def resolve_member(board: TrelloBoard, value: str) -> str:
    candidates = {mid: (name, board.member_usernames.get(mid, "")) for mid, name in board.members.items()}
    return _pick("member", value, candidates, board.name)


def find_card_in_list(cards: list[TrelloCard], value: str) -> TrelloCard:
    for card in cards:
        if card.id == value:
            return card
    wanted = value.strip().casefold()
    hits = [card for card in cards if card.name.casefold() == wanted]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ResolveError("card_ambiguous", card=value, matches=", ".join(c.url for c in hits))
    raise ResolveError("card_not_found", card=value)

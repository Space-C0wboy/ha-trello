"""Shared test data, shaped like real Trello API responses."""
from __future__ import annotations

import re
import time

from homeassistant.config_entries import ConfigSubentryData
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

API = "https://api.trello.com/1"


def board_url(board_id: str) -> re.Pattern[str]:
    return re.compile(rf"^https://api\.trello\.com/1/boards/{board_id}\?.*$")


def lists_url(board_id: str) -> re.Pattern[str]:
    return re.compile(rf"^https://api\.trello\.com/1/boards/{board_id}/lists\?.*$")


def card_actions_url(card_id: str) -> re.Pattern[str]:
    return re.compile(rf"^https://api\.trello\.com/1/cards/{card_id}/actions\?.*$")


BOARD_URL = board_url("b1")
ME_URL = re.compile(r"^https://api\.trello\.com/1/members/me(\?.*)?$")
BOARDS_URL = re.compile(r"^https://api\.trello\.com/1/members/me/boards\?.*$")

ME = {"id": "m1", "username": "sam", "fullName": "Sam"}
BOARDS = [{"id": "b1", "name": "Household"}, {"id": "b2", "name": "Projects"}]
LISTS = [
    {"id": "l_alex", "name": "Alex", "pos": 1},
    {"id": "l_sam", "name": "Sam", "pos": 2},
    {"id": "l_done", "name": "Done & Archive", "pos": 3},
]
LISTS_B2 = [
    {"id": "l_venues", "name": "Venues", "pos": 1},
    {"id": "l_guests", "name": "Guests", "pos": 2},
]


def card(cid: str, name: str, list_id: str, pos: float, **extra) -> dict:
    return {
        "id": cid,
        "name": name,
        "desc": extra.get("desc", ""),
        "due": extra.get("due"),
        "start": extra.get("start"),
        "dueComplete": extra.get("due_complete", False),
        "idList": list_id,
        "idLabels": extra.get("labels", []),
        "idMembers": extra.get("members", []),
        "pos": pos,
        "shortUrl": f"https://trello.com/c/{cid}",
        "dateLastActivity": "2026-10-12T12:00:00.000Z",
    }


def board(
    cards: list[dict] | None = None,
    lists: list[dict] | None = None,
    board_id: str = "b1",
    name: str = "Household",
) -> dict:
    """A board as returned by GET /1/boards/{id} with nested lists/cards/labels/members."""
    if cards is None:
        cards = [
            # Out of pos order on purpose: the integration must sort.
            card("c2", "Call plumber", "l_alex", 32768, due="2026-10-15T22:30:00.000Z"),
            card(
                "c1", "Buy stamps", "l_alex", 16384,
                desc="Forever stamps", due="2026-10-15T05:00:00.000Z",
                labels=["lb1"], members=["m1", "m2"],
            ),
            card("c3", "Pay invoice", "l_alex", 65536),
            card("c4", "Mow lawn", "l_sam", 100),
            card("c9", "Old thing", "l_done", 1),
        ]
    return {
        "id": board_id,
        "name": name,
        "lists": LISTS if lists is None else lists,
        "cards": cards,
        "labels": [
            {"id": "lb1", "name": "Urgent", "color": "red"},
            {"id": "lb2", "name": "", "color": "green"},
        ],
        "members": [
            {"id": "m1", "fullName": "Sam", "initials": "S", "username": "sam"},
            {"id": "m2", "fullName": "Alex", "initials": "A", "username": "alex"},
        ],
    }


def second_board(cards: list[dict] | None = None, lists: list[dict] | None = None) -> dict:
    return board(
        cards=[card("w1", "Book venue", "l_venues", 1)] if cards is None else cards,
        lists=LISTS_B2 if lists is None else lists,
        board_id="b2",
        name="Projects",
    )


def calls(mock, method: str, url: str) -> list:
    """Requests aioresponses recorded for one method + exact URL (query ignored)."""
    return [
        c
        for (m, u), recorded in mock.requests.items()
        if m == method and URL(u).with_query(None) == URL(url)
        for c in recorded
    ]


def bodies(mock, method: str, url: str) -> list[dict]:
    return [c.kwargs["json"] for c in calls(mock, method, url)]


CLIENT_ID = "client-id"
CLIENT_SECRET = "client-secret-xyz"
ACCESS_TOKEN = "access-token-abc"
REFRESH_TOKEN = "refresh-token-def"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"


def token_data(expires_at: float, access_token: str = ACCESS_TOKEN) -> dict:
    return {
        "access_token": access_token,
        "refresh_token": REFRESH_TOKEN,
        "token_type": "Bearer",
        "expires_in": 3600,
        "expires_at": expires_at,
    }


def board_subentry(
    board_id: str = "b1",
    title: str = "Household",
    done_behavior: str = "move",
    done_list: str | None = "l_done",
) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={"board_id": board_id, "done_behavior": done_behavior, "done_list": done_list},
        subentry_type="board",
        title=title,
        unique_id=board_id,
    )


def make_entry(hass, *subentries: ConfigSubentryData, expires_at: float | None = None) -> MockConfigEntry:
    """An account entry. Defaults to one board sub-entry for Household."""
    entry = MockConfigEntry(
        domain="trello_todo",
        unique_id="m1",
        title="Sam",
        data={
            "auth_implementation": "trello_todo",
            "token": token_data(time.time() + 3600 if expires_at is None else expires_at),
        },
        options={"scan_interval": 120},
        subentries_data=list(subentries) or [board_subentry()],
    )
    entry.add_to_hass(hass)
    return entry


def subentry_id(entry, board_id: str) -> str:
    return next(sid for sid, sub in entry.subentries.items() if sub.unique_id == board_id)


def actions_url(board_id: str) -> re.Pattern[str]:
    return re.compile(rf"^https://api\.trello\.com/1/boards/{board_id}/actions(\?.*)?$")


WEBHOOKS_URL = f"{API}/webhooks"
PEOPLE = {"m1": ("Sam", "S"), "m2": ("Alex", "A")}


def action(aid: str, kind: str, *, by: str = "m2", date: str = "2026-10-13T12:00:00.000Z",
           member: dict | None = None, **data) -> dict:
    """A Trello action record, shaped like webhook payloads and /boards/{id}/actions entries."""
    name, initials = PEOPLE.get(by, ("Someone", "?"))
    record = {
        "id": aid,
        "type": kind,
        "date": date,
        "idMemberCreator": by,
        "memberCreator": {"id": by, "fullName": name, "initials": initials},
        "data": data,
    }
    if member is not None:
        record["member"] = member
    return record


def card_url(card_id: str) -> re.Pattern[str]:
    return re.compile(rf"^https://api\.trello\.com/1/cards/{card_id}(\?.*)?$")


def checklists_url(card_id: str) -> re.Pattern[str]:
    return re.compile(rf"^https://api\.trello\.com/1/cards/{card_id}/checklists(\?.*)?$")

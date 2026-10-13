"""Minimal typed async client for the Trello REST API (no Home Assistant imports)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

BASE_URL = "https://api.trello.com/1"
TIMEOUT = aiohttp.ClientTimeout(total=20)

BOARD_PARAMS = {
    "fields": "name",
    "lists": "open",
    "list_fields": "name,pos",
    "cards": "open",
    "card_fields": "name,desc,due,dueComplete,start,idList,idLabels,idMembers,pos,shortUrl,dateLastActivity",
    "labels": "all",
    "label_fields": "name,color",
    "members": "all",
    "member_fields": "fullName,initials,username",
}

TokenGetter = Callable[[], Awaitable[str]]
ACTIONS_PAGE_SIZE = 50
MAX_ACTION_PAGES = 5


class TrelloError(Exception):
    """Any failure talking to Trello. Messages never include the access token."""

    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TrelloAuthError(TrelloError):
    """The access token was rejected (HTTP 401)."""


class TrelloAccessError(TrelloError):
    """The board or card is gone, or the account can no longer see it (HTTP 403/404)."""


class TrelloWebhookExistsError(TrelloError):
    """Trello already has a webhook for this callback URL, board and token."""


class TrelloRateLimitError(TrelloError):
    """Trello returned HTTP 429."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("Trello rate limit hit", 429)
        self.retry_after = retry_after


class TrelloConnectionError(TrelloError):
    """Network failure, timeout or a 5xx from Trello."""


def reason_code(err: Exception) -> str:
    """A short code for errors shown to users and in diagnostics: Trello's own text can quote URLs."""
    if isinstance(err, TrelloRateLimitError):
        return "rate_limited"
    if isinstance(err, TrelloAuthError):
        return "auth"
    if isinstance(err, TrelloAccessError):
        return "access"
    if isinstance(err, (TrelloConnectionError, aiohttp.ClientError)):
        return "connection"
    if isinstance(err, TrelloError):
        return "rejected"
    return "error"


@dataclass(frozen=True, slots=True)
class TrelloLabel:
    name: str
    color: str | None


@dataclass(frozen=True, slots=True)
class TrelloCard:
    id: str
    name: str
    desc: str
    due: str | None
    list_id: str
    pos: float
    url: str
    labels: tuple[TrelloLabel, ...] = ()
    members: tuple[str, ...] = ()  # initials
    member_ids: tuple[str, ...] = ()
    due_complete: bool = False
    start: str | None = None


@dataclass(slots=True)
class TrelloBoard:
    id: str
    name: str
    lists: dict[str, str]  # list id -> name, in board order
    cards: dict[str, list[TrelloCard]] = field(default_factory=dict)  # list id -> cards in pos order
    members: dict[str, str] = field(default_factory=dict)  # member id -> full name
    labels: dict[str, TrelloLabel] = field(default_factory=dict)  # label id -> label (all board labels)
    member_usernames: dict[str, str] = field(default_factory=dict)  # member id -> username

    def card(self, card_id: str) -> TrelloCard | None:
        for cards in self.cards.values():
            for card in cards:
                if card.id == card_id:
                    return card
        return None


def parse_card(raw: dict[str, Any], labels: dict[str, TrelloLabel], initials: dict[str, str]) -> TrelloCard:
    return TrelloCard(
        id=raw["id"],
        name=raw.get("name") or "",
        desc=raw.get("desc") or "",
        due=raw.get("due"),
        list_id=raw["idList"],
        pos=float(raw.get("pos") or 0),
        url=raw.get("shortUrl") or "",
        labels=tuple(labels[i] for i in raw.get("idLabels", []) if i in labels),
        members=tuple(initials[i] for i in raw.get("idMembers", []) if i in initials),
        member_ids=tuple(i for i in raw.get("idMembers", []) if i in initials),
        due_complete=bool(raw.get("dueComplete")),
        start=raw.get("start"),
    )


def parse_board(raw: dict[str, Any]) -> TrelloBoard:
    """Turn the nested GET /boards/{id} response into a TrelloBoard."""
    labels = {lb["id"]: TrelloLabel(lb.get("name") or "", lb.get("color")) for lb in raw.get("labels", [])}
    initials = {m["id"]: m.get("initials") or "" for m in raw.get("members", [])}
    names = {m["id"]: m.get("fullName") or "" for m in raw.get("members", [])}
    lists = {lst["id"]: lst["name"] for lst in sorted(raw.get("lists", []), key=lambda lst: lst.get("pos", 0))}
    cards: dict[str, list[TrelloCard]] = {list_id: [] for list_id in lists}
    for c in raw.get("cards", []):
        if c["idList"] in cards:
            cards[c["idList"]].append(parse_card(c, labels, initials))
    for list_cards in cards.values():
        list_cards.sort(key=lambda card: card.pos)
    return TrelloBoard(
        id=raw["id"],
        name=raw.get("name") or "",
        lists=lists,
        cards=cards,
        members=names,
        labels=labels,
        member_usernames={m["id"]: m.get("username") or "" for m in raw.get("members", [])},
    )


class TrelloClient:
    """Talks to Trello with an OAuth 2.0 bearer token supplied by an async getter."""

    def __init__(self, session: aiohttp.ClientSession, get_token: TokenGetter) -> None:
        self._session = session
        self._get_token = get_token

    async def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None, json: dict[str, Any] | None = None
    ) -> Any:
        # Outside the try: a failed token refresh must reach the HA layer unchanged.
        # The token lives only in this header, never in the URL, so it can't leak into logs or exceptions.
        headers = {"Authorization": f"Bearer {await self._get_token()}"}
        try:
            async with self._session.request(
                method, f"{BASE_URL}{path}", params=params, json=json, headers=headers, timeout=TIMEOUT
            ) as resp:
                if resp.status == 401:
                    raise TrelloAuthError("Trello rejected the access token", 401)
                if resp.status in (403, 404):
                    raise TrelloAccessError(f"Trello returned HTTP {resp.status} for {method} {path}", resp.status)
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    raise TrelloRateLimitError(float(retry_after) if retry_after else None)
                if resp.status >= 500:
                    raise TrelloConnectionError(f"Trello returned HTTP {resp.status}", resp.status)
                if resp.status >= 400:
                    text = (await resp.text())[:200]
                    if resp.status == 400 and "already exists" in text:
                        raise TrelloWebhookExistsError("Trello already has this webhook", 400)
                    raise TrelloError(f"Trello returned HTTP {resp.status} for {method} {path}: {text}", resp.status)
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TrelloConnectionError(f"Error talking to Trello: {type(err).__name__}") from None

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/members/me", {"fields": "id,username,fullName"})

    async def get_boards(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/members/me/boards", {"filter": "open", "fields": "name"})

    async def get_lists(self, board_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/boards/{board_id}/lists", {"filter": "open", "fields": "name,pos"})

    async def get_board(self, board_id: str) -> TrelloBoard:
        return parse_board(await self._request("GET", f"/boards/{board_id}", BOARD_PARAMS))

    async def create_card(self, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/cards", json=fields)

    async def update_card(self, card_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", f"/cards/{card_id}", json=fields)

    async def get_last_list_move(self, card_id: str) -> str | None:
        """The list a card was most recently moved out of, from its action history."""
        actions = await self._request(
            "GET", f"/cards/{card_id}/actions", {"filter": "updateCard:idList", "limit": "1"}
        )
        for action in actions or []:
            if list_id := ((action.get("data") or {}).get("listBefore") or {}).get("id"):
                return list_id
        return None

    async def create_webhook(self, callback_url: str, board_id: str, description: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/webhooks", {"callbackURL": callback_url, "idModel": board_id, "description": description}
        )

    async def get_webhook(self, webhook_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/webhooks/{webhook_id}")

    async def delete_webhook(self, webhook_id: str) -> None:
        await self._request("DELETE", f"/webhooks/{webhook_id}")

    async def get_actions(self, board_id: str, since: str | None, action_types: Iterable[str]) -> list[dict[str, Any]]:
        params = {
            "filter": ",".join(action_types),
            "limit": str(ACTIONS_PAGE_SIZE),
            "fields": "type,date,data,idMemberCreator",
            "memberCreator_fields": "fullName,initials",
            "member": "true",
            "member_fields": "fullName",
        }
        if not since:  # startup baseline: only the newest page matters
            return await self._request("GET", f"/boards/{board_id}/actions", params)
        params["since"] = since
        actions: list[dict[str, Any]] = []
        for _ in range(MAX_ACTION_PAGES):  # after an outage, page back instead of skipping older activity
            page = await self._request("GET", f"/boards/{board_id}/actions", params)
            actions.extend(page)
            if len(page) < ACTIONS_PAGE_SIZE:
                break
            params = {**params, "before": page[-1]["id"]}
        return actions

    async def get_card(self, card_id: str, fields: str = "idBoard,idList,name,shortUrl,closed") -> dict[str, Any]:
        return await self._request("GET", f"/cards/{card_id}", {"fields": fields})

    async def add_comment(self, card_id: str, text: str) -> dict[str, Any]:
        return await self._request("POST", f"/cards/{card_id}/actions/comments", json={"text": text})

    async def add_label(self, card_id: str, label_id: str) -> None:
        await self._request("POST", f"/cards/{card_id}/idLabels", json={"value": label_id})

    async def remove_label(self, card_id: str, label_id: str) -> None:
        await self._request("DELETE", f"/cards/{card_id}/idLabels/{label_id}")

    async def add_member(self, card_id: str, member_id: str) -> None:
        await self._request("POST", f"/cards/{card_id}/idMembers", json={"value": member_id})

    async def remove_member(self, card_id: str, member_id: str) -> None:
        await self._request("DELETE", f"/cards/{card_id}/idMembers/{member_id}")

    async def get_checklists(self, card_id: str) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            f"/cards/{card_id}/checklists",
            {"fields": "name", "checkItems": "all", "checkItem_fields": "name,state"},
        )

    async def create_checklist(self, card_id: str, name: str) -> dict[str, Any]:
        return await self._request("POST", f"/cards/{card_id}/checklists", json={"name": name})

    async def add_check_item(self, checklist_id: str, name: str) -> dict[str, Any]:
        return await self._request("POST", f"/checklists/{checklist_id}/checkItems", json={"name": name})

    async def set_check_item(self, card_id: str, item_id: str, state: str) -> dict[str, Any]:
        return await self._request("PUT", f"/cards/{card_id}/checkItem/{item_id}", json={"state": state})

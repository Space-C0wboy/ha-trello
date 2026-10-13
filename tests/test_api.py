"""Tests for the HA-free Trello client."""
import re
import aiohttp
import pytest

from custom_components.trello_todo.api import (
    TrelloAccessError,
    TrelloAuthError,
    TrelloClient,
    TrelloConnectionError,
    TrelloError,
    TrelloRateLimitError,
)

from .common import API, BOARD_URL, board, bodies, calls, card_actions_url

TOKEN = "secret-access-token"


class TokenBroken(Exception):
    """Stands in for a failed OAuth token refresh."""


@pytest.fixture
async def client():
    async with aiohttp.ClientSession() as session:
        async def get_token() -> str:
            return TOKEN

        yield TrelloClient(session, get_token)


async def test_bearer_header_and_no_token_in_url(client, mock_trello):
    mock_trello.get(BOARD_URL, payload=board())
    await client.get_board("b1")
    (req,) = calls(mock_trello, "GET", f"{API}/boards/b1")
    assert req.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(req.kwargs["params"])


async def test_board_parsed_in_position_order(client, mock_trello):
    mock_trello.get(BOARD_URL, payload=board())
    result = await client.get_board("b1")
    assert list(result.lists) == ["l_alex", "l_sam", "l_done"]
    alex = result.cards["l_alex"]
    assert [c.id for c in alex] == ["c1", "c2", "c3"]
    assert (alex[0].labels[0].name, alex[0].labels[0].color) == ("Urgent", "red")
    assert alex[0].members == ("S", "A")
    assert result.card("c4").list_id == "l_sam"


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (401, TrelloAuthError),
        (403, TrelloAccessError),
        (404, TrelloAccessError),
        (500, TrelloConnectionError),
        (503, TrelloConnectionError),
        (400, TrelloError),
    ],
)
async def test_status_mapping(client, mock_trello, status, exc):
    mock_trello.get(BOARD_URL, status=status, body="nope")
    with pytest.raises(TrelloError) as info:
        await client.get_board("b1")
    assert type(info.value) is exc
    assert TOKEN not in str(info.value)


async def test_429_with_retry_after(client, mock_trello):
    mock_trello.get(BOARD_URL, status=429, headers={"Retry-After": "30"})
    with pytest.raises(TrelloRateLimitError) as info:
        await client.get_board("b1")
    assert info.value.retry_after == 30.0


async def test_429_without_retry_after(client, mock_trello):
    mock_trello.get(BOARD_URL, status=429)
    with pytest.raises(TrelloRateLimitError) as info:
        await client.get_board("b1")
    assert info.value.retry_after is None


async def test_network_error(client, mock_trello):
    mock_trello.get(BOARD_URL, exception=aiohttp.ClientConnectionError("boom"))
    with pytest.raises(TrelloConnectionError):
        await client.get_board("b1")


async def test_token_errors_propagate_unchanged(mock_trello):
    async with aiohttp.ClientSession() as session:
        async def get_token() -> str:
            raise TokenBroken

        with pytest.raises(TokenBroken):
            await TrelloClient(session, get_token).get_board("b1")
    assert calls(mock_trello, "GET", f"{API}/boards/b1") == []


async def test_last_list_move(client, mock_trello):
    mock_trello.get(
        card_actions_url("c9"),
        payload=[{"type": "updateCard", "data": {"listBefore": {"id": "l_sam"}, "listAfter": {"id": "l_done"}}}],
    )
    assert await client.get_last_list_move("c9") == "l_sam"
    (req,) = calls(mock_trello, "GET", f"{API}/cards/c9/actions")
    assert req.kwargs["params"] == {"filter": "updateCard:idList", "limit": "1"}


async def test_last_list_move_without_history(client, mock_trello):
    mock_trello.get(card_actions_url("c9"), payload=[])
    assert await client.get_last_list_move("c9") is None


async def test_writes_send_json(client, mock_trello):
    mock_trello.post(f"{API}/cards", payload={"id": "new"})
    mock_trello.put(f"{API}/cards/c1", payload={})
    await client.create_card({"idList": "l_alex", "name": "Milk"})
    await client.update_card("c1", {"closed": True})
    assert bodies(mock_trello, "POST", f"{API}/cards") == [{"idList": "l_alex", "name": "Milk"}]
    assert bodies(mock_trello, "PUT", f"{API}/cards/c1") == [{"closed": True}]


from custom_components.trello_todo.api import TrelloWebhookExistsError  # noqa: E402

from .common import WEBHOOKS_URL, actions_url  # noqa: E402


async def test_board_member_names(client, mock_trello):
    mock_trello.get(BOARD_URL, payload=board())
    result = await client.get_board("b1")
    assert result.members == {"m1": "Sam", "m2": "Alex"}


async def test_create_webhook(client, mock_trello):
    mock_trello.post(re.compile(r"^https://api\.trello\.com/1/webhooks(\?.*)?$"), payload={"id": "w1", "active": True})
    created = await client.create_webhook("https://hook.example/x", "b1", "Home Assistant: Household")
    assert created["id"] == "w1"
    (req,) = calls(mock_trello, "POST", WEBHOOKS_URL)
    assert req.kwargs["params"] == {
        "callbackURL": "https://hook.example/x",
        "idModel": "b1",
        "description": "Home Assistant: Household",
    }


async def test_create_webhook_already_exists(client, mock_trello):
    mock_trello.post(
        re.compile(r"^https://api\.trello\.com/1/webhooks(\?.*)?$"),
        status=400,
        body="A webhook with that callback, model, and token already exists",
    )
    with pytest.raises(TrelloWebhookExistsError):
        await client.create_webhook("https://hook.example/x", "b1", "d")


async def test_get_and_delete_webhook(client, mock_trello):
    mock_trello.get(f"{WEBHOOKS_URL}/w1", payload={"id": "w1", "active": True, "callbackURL": "u"})
    mock_trello.delete(f"{WEBHOOKS_URL}/w1", payload={"_value": None})
    assert (await client.get_webhook("w1"))["callbackURL"] == "u"
    await client.delete_webhook("w1")
    assert len(calls(mock_trello, "DELETE", f"{WEBHOOKS_URL}/w1")) == 1


async def test_get_actions(client, mock_trello):
    mock_trello.get(actions_url("b1"), payload=[{"id": "a1"}])
    assert await client.get_actions("b1", "a0", ["createCard", "commentCard"]) == [{"id": "a1"}]
    (req,) = calls(mock_trello, "GET", f"{API}/boards/b1/actions")
    assert req.kwargs["params"] == {
        "filter": "createCard,commentCard",
        "limit": "50",
        "fields": "type,date,data,idMemberCreator",
        "memberCreator_fields": "fullName,initials",
        "member": "true",
        "member_fields": "fullName",
        "since": "a0",
    }


async def test_get_actions_without_since(client, mock_trello):
    mock_trello.get(actions_url("b1"), payload=[])
    await client.get_actions("b1", None, ["createCard"])
    (req,) = calls(mock_trello, "GET", f"{API}/boards/b1/actions")
    assert "since" not in req.kwargs["params"]


async def test_get_actions_pages_back_until_short_page(client, mock_trello):
    """A full page (50) means there may be more since `since`: page back with `before`."""
    first = [{"id": f"p{n:02}", "date": f"2026-10-13T12:{59 - n:02}:00Z"} for n in range(50)]
    second = [{"id": "q00", "date": "2026-10-13T11:00:00Z"}]
    mock_trello.get(actions_url("b1"), payload=first)
    mock_trello.get(actions_url("b1"), payload=second)
    result = await client.get_actions("b1", "a0", ["createCard"])
    assert [a["id"] for a in result] == [a["id"] for a in first] + ["q00"]
    reqs = calls(mock_trello, "GET", f"{API}/boards/b1/actions")
    assert reqs[1].kwargs["params"]["before"] == "p49"
    assert reqs[1].kwargs["params"]["since"] == "a0"


async def test_get_actions_paging_is_capped(client, mock_trello):
    page = [{"id": f"x{n}", "date": "2026-10-13T12:00:00Z"} for n in range(50)]
    mock_trello.get(actions_url("b1"), payload=page, repeat=True)
    result = await client.get_actions("b1", "a0", ["createCard"])
    assert len(calls(mock_trello, "GET", f"{API}/boards/b1/actions")) == 5
    assert len(result) == 250


async def test_get_actions_without_since_does_not_page(client, mock_trello):
    """The startup baseline only needs the newest page."""
    page = [{"id": f"x{n}", "date": "2026-10-13T12:00:00Z"} for n in range(50)]
    mock_trello.get(actions_url("b1"), payload=page, repeat=True)
    await client.get_actions("b1", None, ["createCard"])
    assert len(calls(mock_trello, "GET", f"{API}/boards/b1/actions")) == 1

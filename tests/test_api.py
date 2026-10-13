"""Tests for the HA-free Trello client."""
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

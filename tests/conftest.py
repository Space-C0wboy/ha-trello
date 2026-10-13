"""Shared test fixtures."""
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import ClientResponse
from aioresponses import aioresponses
from homeassistant.components.application_credentials import ClientCredential, async_import_client_credential
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trello_todo.const import DOMAIN

from .common import BOARD_URL, CLIENT_ID, CLIENT_SECRET, board, board_url, make_entry, second_board


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load custom_components/trello_todo in every test."""
    yield


@pytest.fixture(autouse=True)
async def chicago(hass):
    """Pin the local time zone so due-date mapping is deterministic."""
    await hass.config.async_set_time_zone("America/Chicago")


class _Response(ClientResponse):
    """aioresponses 0.7.9 predates aiohttp 3.14's required `stream_writer` argument."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("stream_writer", SimpleNamespace(output_size=0))
        super().__init__(*args, **kwargs)


@pytest.fixture
def mock_trello() -> Generator[aioresponses]:
    # Localhost passes through so HA's test HTTP and websocket clients keep working.
    with patch("aioresponses.core.ClientResponse", _Response), aioresponses(passthrough=["http://127.0.0.1"]) as mock:
        yield mock


@pytest.fixture
async def setup_credentials(hass) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(hass, DOMAIN, ClientCredential(CLIENT_ID, CLIENT_SECRET), DOMAIN)


@pytest.fixture
def entry(hass, setup_credentials) -> MockConfigEntry:
    """The account entry with one board (Household), not set up."""
    return make_entry(hass)


@pytest.fixture
def load_entry(hass, setup_credentials, mock_trello):
    """Factory: create an entry with the given board sub-entries and set it up against mocked boards."""

    async def _load(*subentries, b1: dict | None = None, b2: dict | None = None, expires_at=None) -> MockConfigEntry:
        entry = make_entry(hass, *subentries, expires_at=expires_at)
        mock_trello.get(BOARD_URL, payload=board() if b1 is None else b1, repeat=True)
        mock_trello.get(board_url("b2"), payload=second_board() if b2 is None else b2, repeat=True)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _load


@pytest.fixture
async def loaded(load_entry) -> MockConfigEntry:
    """The default entry (Household, done = move to Done & Archive), set up."""
    return await load_entry()

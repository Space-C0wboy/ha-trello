"""Tests for the Trello OAuth 2.0 implementation."""
from homeassistant.components.application_credentials import ClientCredential
from yarl import URL

from custom_components.trello_todo.application_credentials import (
    async_get_auth_implementation,
    async_get_description_placeholders,
)
from custom_components.trello_todo.const import DOMAIN, OAUTH2_AUTHORIZE

from .common import CLIENT_ID, CLIENT_SECRET, TOKEN_URL, calls


async def _impl(hass):
    return await async_get_auth_implementation(hass, DOMAIN, ClientCredential(CLIENT_ID, CLIENT_SECRET))


async def test_authorize_url(hass, current_request_with_host):
    impl = await _impl(hass)
    url = URL(await impl.async_generate_authorize_url("flow-1"))
    assert str(url.with_query(None)) == OAUTH2_AUTHORIZE
    query = url.query
    assert query["client_id"] == CLIENT_ID
    assert query["response_type"] == "code"
    assert query["scope"] == "read:member:trello read:board:trello write:board:trello offline_access"
    assert query["prompt"] == "consent"
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"]
    assert CLIENT_SECRET not in str(url)


async def test_code_exchange_sends_secret_and_verifier(hass, mock_trello):
    impl = await _impl(hass)
    mock_trello.post(TOKEN_URL, payload={"access_token": "a", "refresh_token": "r", "expires_in": 3600, "token_type": "Bearer"})
    token = await impl.async_resolve_external_data(
        {"code": "abc", "state": {"redirect_uri": "https://example.com/auth/external/callback"}}
    )
    assert token["access_token"] == "a"
    (req,) = calls(mock_trello, "POST", TOKEN_URL)
    body = req.kwargs["data"]
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "abc"
    assert body["client_id"] == CLIENT_ID
    assert body["client_secret"] == CLIENT_SECRET
    assert body["code_verifier"] == impl.code_verifier
    assert body["redirect_uri"] == "https://example.com/auth/external/callback"


async def test_description_placeholders(hass):
    assert await async_get_description_placeholders(hass) == {
        "apps_url": "https://trello.com/apps/admin",
        "redirect_url": "https://my.home-assistant.io/redirect/oauth",
    }

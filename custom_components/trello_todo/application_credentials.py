"""Application credentials: Trello OAuth 2.0 (3LO) through auth.atlassian.com, with PKCE."""
from __future__ import annotations

from homeassistant.components.application_credentials import ClientCredential
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .const import MY_REDIRECT_URL, OAUTH2_AUTHORIZE, OAUTH2_SCOPES, OAUTH2_TOKEN, TRELLO_APPS_URL


class TrelloOAuth2Implementation(config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce):
    """Trello wants PKCE *and* the client secret (a confidential client)."""

    @property
    def extra_authorize_data(self) -> dict:
        return {"scope": " ".join(OAUTH2_SCOPES), "prompt": "consent", **super().extra_authorize_data}


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    return TrelloOAuth2Implementation(
        hass, auth_domain, credential.client_id, OAUTH2_AUTHORIZE, OAUTH2_TOKEN, credential.client_secret
    )


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    return {"apps_url": TRELLO_APPS_URL, "redirect_url": MY_REDIRECT_URL}

"""Config flow for Trello: OAuth sign-in, board picking, reauth, account options."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .api import TrelloClient, TrelloError
from .const import (
    CONF_BOARD_ID,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    SUBENTRY_BOARD,
)
from .subentry_flow import BoardSubentryFlow, configured_board_ids, default_done_settings


class TrelloFlowHandler(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Sign in with Atlassian, then pick boards."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._oauth_data: dict[str, Any] = {}
        self._title = ""
        self._boards: dict[str, str] = {}
        self._client: TrelloClient | None = None

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(__name__)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TrelloOptionsFlow:
        return TrelloOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_BOARD: BoardSubentryFlow}

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm", description_placeholders={"account": self._get_reauth_entry().title}
            )
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        access_token = data["token"]["access_token"]

        async def get_token() -> str:
            return access_token

        client = TrelloClient(async_get_clientsession(self.hass), get_token)
        try:
            me = await client.get_me()
        except TrelloError:
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(me["id"])
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        self._abort_if_unique_id_configured()
        try:
            boards = await client.get_boards()
        except TrelloError:
            return self.async_abort(reason="cannot_connect")
        if not boards:
            return self.async_abort(reason="no_boards")
        configured = configured_board_ids(self.hass)
        if not (boards := [b for b in boards if b["id"] not in configured]):
            return self.async_abort(reason="no_more_boards")
        self._oauth_data = data
        self._title = me.get("fullName") or me.get("username") or me["id"]
        self._boards = {b["id"]: b["name"] for b in boards}
        self._client = client
        return await self.async_step_boards()

    async def async_step_boards(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input["boards"]:
                errors["boards"] = "no_board_selected"
            else:
                assert self._client is not None
                try:
                    subentries = [
                        ConfigSubentryData(
                            data={CONF_BOARD_ID: board_id, **default_done_settings(await self._client.get_lists(board_id))},
                            subentry_type=SUBENTRY_BOARD,
                            title=self._boards[board_id],
                            unique_id=board_id,
                        )
                        for board_id in user_input["boards"]
                    ]
                except TrelloError:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=self._title,
                        data=self._oauth_data,
                        options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
                        subentries=subentries,
                    )
        options = [SelectOptionDict(value=bid, label=name) for bid, name in self._boards.items()]
        return self.async_show_form(
            step_id="boards",
            data_schema=vol.Schema(
                {vol.Required("boards", default=[]): SelectSelector(SelectSelectorConfig(options=options, multiple=True))}
            ),
            errors=errors,
        )


class TrelloOptionsFlow(OptionsFlow):
    """Account-wide options: the refresh interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])})
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=30,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

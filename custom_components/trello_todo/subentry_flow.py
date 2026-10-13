"""Board sub-entries: add a board, or change what "done" does on it."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryFlow, SubentryFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import TrelloClient, TrelloError
from .const import (
    CONF_BOARD_ID,
    CONF_DONE_BEHAVIOR,
    CONF_DONE_LIST,
    DOMAIN,
    DONE_ARCHIVE,
    DONE_BEHAVIORS,
    DONE_MOVE,
    SUBENTRY_BOARD,
)


_DONE_NAME = re.compile(r"^\W*done\b", re.IGNORECASE)


def configured_board_ids(hass: HomeAssistant) -> set[str]:
    """Boards already set up under any Trello account: a board belongs to one entry only."""
    return {
        sub.unique_id
        for entry in hass.config_entries.async_entries(DOMAIN)
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_BOARD and sub.unique_id
    }


def default_done_settings(lists: list[dict[str, Any]]) -> dict[str, Any]:
    """Pre-pick the first list (by position) whose name starts with "done"; with none, archive.

    Leading emoji/punctuation are ignored ("✅ Done"), but "To be done" or "Undone" don't count.
    """
    ordered = sorted(lists, key=lambda lst: lst.get("pos", 0))
    done_list = next((lst["id"] for lst in ordered if _DONE_NAME.match(lst["name"])), None)
    return {CONF_DONE_BEHAVIOR: DONE_MOVE if done_list else DONE_ARCHIVE, CONF_DONE_LIST: done_list}


def done_schema(lists: list[dict[str, Any]], current: Mapping[str, Any]) -> vol.Schema:
    options = [SelectOptionDict(value=lst["id"], label=lst["name"]) for lst in lists]
    return vol.Schema(
        {
            vol.Required(CONF_DONE_BEHAVIOR, default=current.get(CONF_DONE_BEHAVIOR, DONE_MOVE)): SelectSelector(
                SelectSelectorConfig(
                    options=DONE_BEHAVIORS, translation_key=CONF_DONE_BEHAVIOR, mode=SelectSelectorMode.LIST
                )
            ),
            vol.Optional(CONF_DONE_LIST, description={"suggested_value": current.get(CONF_DONE_LIST)}): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            ),
        }
    )


def done_settings_from_input(user_input: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate the done form. Returns (settings, errors)."""
    behavior = user_input[CONF_DONE_BEHAVIOR]
    done_list = user_input.get(CONF_DONE_LIST) or None
    if behavior != DONE_ARCHIVE and done_list is None:
        return {}, {CONF_DONE_LIST: "done_list_required"}
    return {CONF_DONE_BEHAVIOR: behavior, CONF_DONE_LIST: None if behavior == DONE_ARCHIVE else done_list}, {}


class BoardSubentryFlow(ConfigSubentryFlow):
    """Add a board to the account, or change a board's done settings."""

    def __init__(self) -> None:
        self._available: dict[str, str] = {}
        self._board_id = ""
        self._lists: list[dict[str, Any]] = []

    def _client(self) -> TrelloClient:
        return self._get_entry().runtime_data.client

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        if not self._available:
            try:
                boards = await self._client().get_boards()
            except (TrelloError, HomeAssistantError, ClientError):
                return self.async_abort(reason="cannot_connect")
            configured = configured_board_ids(self.hass)
            self._available = {b["id"]: b["name"] for b in boards if b["id"] not in configured}
            if not self._available:
                return self.async_abort(reason="no_more_boards")
        if user_input is not None:
            self._board_id = user_input["board"]
            try:
                self._lists = await self._client().get_lists(self._board_id)
            except (TrelloError, HomeAssistantError, ClientError):
                return self.async_abort(reason="cannot_connect")
            return await self.async_step_done()
        options = [SelectOptionDict(value=bid, label=name) for bid, name in self._available.items()]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("board"): SelectSelector(SelectSelectorConfig(options=options))}),
        )

    async def async_step_done(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        title = self._available[self._board_id]
        if user_input is not None:
            settings, errors = done_settings_from_input(user_input)
            if not errors:
                return self.async_create_entry(
                    title=title, data={CONF_BOARD_ID: self._board_id, **settings}, unique_id=self._board_id
                )
        schema = done_schema(self._lists, default_done_settings(self._lists))
        return self.async_show_form(
            step_id="done",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
            description_placeholders={"board": title},
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        errors: dict[str, str] = {}
        if user_input is not None:
            settings, errors = done_settings_from_input(user_input)
            if not errors:
                # The account's update listener sees the changed settings and reloads.
                return self.async_update_and_abort(entry, subentry, data_updates=settings)
        if not self._lists:
            try:
                self._lists = await self._client().get_lists(subentry.data[CONF_BOARD_ID])
            except (TrelloError, HomeAssistantError, ClientError):
                return self.async_abort(reason="cannot_connect")
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(done_schema(self._lists, subentry.data), user_input or {}),
            errors=errors,
            description_placeholders={"board": subentry.title},
        )

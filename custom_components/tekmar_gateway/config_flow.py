"""Config flow for the Tekmar Gateway integration.

Two-step user flow:
  1. user: email + password. We sign in to verify creds work, then fetch
     the building list. If exactly one building, we proceed directly. If
     multiple, we present a chooser.
  2. building (optional): pick a building UUID from the dropdown.

Also implements reauth, triggered by ConfigEntryAuthFailed from the
coordinator (e.g., user changed their tekmar password).
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TekmarApiClient, TekmarApiError, TekmarAuthError
from .const import CONF_BUILDING_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)


class TekmarConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Tekmar Gateway config flow."""

    VERSION = 1

    def __init__(self) -> None:
        # NB: do NOT add a _reauth_entry_id attribute here -- recent HA
        # versions define that name as a read-only property on the base
        # ConfigFlow class. Use self._get_reauth_entry() during reauth
        # instead of tracking it manually.
        self._username: str | None = None
        self._password: str | None = None
        self._buildings: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initial step - collect credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD]

            client = TekmarApiClient(
                async_get_clientsession(self.hass),
                self._username,
                self._password,
            )
            try:
                self._buildings = await client.list_buildings()
            except TekmarAuthError as err:
                _LOGGER.warning("Auth failed: %s", err)
                errors["base"] = "invalid_auth"
            except TekmarApiError as err:
                _LOGGER.error("API error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during config")
                errors["base"] = "unknown"
            else:
                if not self._buildings:
                    errors["base"] = "no_buildings"
                elif len(self._buildings) == 1:
                    return await self._async_create_or_update(
                        self._buildings[0]["uuid"]
                    )
                else:
                    return await self.async_step_building()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_building(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a building when the account has more than one."""
        if user_input is not None:
            return await self._async_create_or_update(
                user_input[CONF_BUILDING_ID]
            )

        options = {b["uuid"]: b.get("name", b["uuid"]) for b in self._buildings}
        return self.async_show_form(
            step_id="building",
            data_schema=vol.Schema(
                {vol.Required(CONF_BUILDING_ID): vol.In(options)}
            ),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Triggered when the coordinator raises ConfigEntryAuthFailed."""
        self._username = entry_data.get(CONF_USERNAME)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt the user to re-enter their password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            client = TekmarApiClient(
                async_get_clientsession(self.hass),
                self._username,  # type: ignore[arg-type]
                user_input[CONF_PASSWORD],
            )
            try:
                await client.list_buildings()
            except TekmarAuthError:
                errors["base"] = "invalid_auth"
            except TekmarApiError:
                errors["base"] = "cannot_connect"
            else:
                # Built-in helper: updates entry data, reloads it, and
                # aborts the flow with reason="reauth_successful".
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": self._username or ""},
            errors=errors,
        )

    async def _async_create_or_update(
        self, building_id: str
    ) -> ConfigFlowResult:
        """Finalize the flow - prevent duplicate entries by building UUID."""
        await self.async_set_unique_id(building_id)
        self._abort_if_unique_id_configured()

        title = next(
            (b.get("name", "Tekmar")
             for b in self._buildings if b["uuid"] == building_id),
            "Tekmar Gateway",
        )
        return self.async_create_entry(
            title=title,
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_BUILDING_ID: building_id,
            },
        )

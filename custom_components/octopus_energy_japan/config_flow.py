"""Config flow for Octopus Energy Japan."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KrakenAuthError, KrakenClient, KrakenConnectionError, KrakenError
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    LOGGER,
)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


class OctopusJapanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the email/password config flow for OEJP."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry_data: Mapping[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            result = await self._validate(email, password)
            if isinstance(result, dict):
                await self.async_set_unique_id(result[CONF_ACCOUNT_NUMBER])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Octopus Energy Japan ({result[CONF_ACCOUNT_NUMBER]})",
                    data=result,
                )
            errors["base"] = result

        return self.async_show_form(
            step_id="user", data_schema=_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._reauth_entry_data = entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._reauth_entry_data is not None
        email = self._reauth_entry_data[CONF_EMAIL]
        errors: dict[str, str] = {}

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            result = await self._validate(email, password)
            if isinstance(result, dict):
                entry = self._get_reauth_entry()
                if entry is not None:
                    self.hass.config_entries.async_update_entry(entry, data=result)
                    await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
            errors["base"] = result

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_REAUTH_SCHEMA,
            description_placeholders={"email": email},
            errors=errors,
        )

    def _get_reauth_entry(self):
        # Reauth flows expose the originating entry via context["entry_id"].
        entry_id = self.context.get("entry_id") if self.context else None
        if not entry_id:
            return None
        return self.hass.config_entries.async_get_entry(entry_id)

    async def _validate(self, email: str, password: str) -> dict[str, Any] | str:
        session = async_get_clientsession(self.hass)
        client = KrakenClient(session, email=email, password=password)
        try:
            await client.authenticate()
            account_number = await client.get_account_number()
        except KrakenAuthError as err:
            LOGGER.debug("OEJP auth failed: %s", err)
            return "invalid_auth"
        except KrakenConnectionError as err:
            LOGGER.debug("OEJP connection failed: %s", err)
            return "cannot_connect"
        except KrakenError as err:
            LOGGER.warning("Unexpected OEJP error: %s", err)
            return "unknown"

        return {
            CONF_EMAIL: email,
            CONF_PASSWORD: password,
            CONF_ACCOUNT_NUMBER: account_number,
            CONF_REFRESH_TOKEN: client.refresh_token,
        }

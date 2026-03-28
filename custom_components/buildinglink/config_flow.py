"""Config flow for BuildingLink integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BuildingLinkApi, BuildingLinkAuthError, BuildingLinkApiError
from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class BuildingLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BuildingLink."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate credentials by attempting login
            api = BuildingLinkApi(
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
            )
            try:
                await api.login()
                occupant = await api.get_occupant()
            except BuildingLinkAuthError:
                errors["base"] = "invalid_auth"
            except BuildingLinkApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during BuildingLink login")
                errors["base"] = "unknown"
            else:
                # Build a unique ID from occupant info
                first = occupant.get("firstName", "")
                last = occupant.get("lastName", "")
                unit = occupant.get("unit", {}).get("name", "")
                title = f"{first} {last}".strip() or user_input[CONF_USERNAME]
                if unit:
                    title = f"{title} ({unit})"

                await self._async_abort_or_unique_id(
                    user_input[CONF_USERNAME].lower()
                )

                return self.async_create_entry(title=title, data=user_input)
            finally:
                await api.close()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def _async_abort_or_unique_id(self, unique_id: str) -> None:
        """Set unique ID and abort if already configured."""
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

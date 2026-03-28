"""DataUpdateCoordinator for BuildingLink."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BuildingLinkApi, BuildingLinkApiError, BuildingLinkAuthError
from .const import CONF_PASSWORD, CONF_USERNAME, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class BuildingLinkCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch delivery data from BuildingLink."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.api = BuildingLinkApi(
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
        )
        self._entry = entry

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch deliveries from BuildingLink."""
        try:
            # Re-authenticate if needed (token may have expired)
            await self.api.login()
            deliveries = await self.api.get_deliveries()
        except BuildingLinkAuthError as err:
            # Force re-auth on next attempt
            self.api._cookies.clear()
            self.api._token = None
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except BuildingLinkApiError as err:
            raise UpdateFailed(f"Error fetching deliveries: {err}") from err

        return {
            "deliveries": deliveries,
            "count": len(deliveries),
        }

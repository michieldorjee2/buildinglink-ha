"""Sensor platform for BuildingLink deliveries."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BuildingLinkCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BuildingLink sensors from a config entry."""
    coordinator: BuildingLinkCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BuildingLinkDeliverySensor(coordinator, entry)])


class BuildingLinkDeliverySensor(
    CoordinatorEntity[BuildingLinkCoordinator], SensorEntity
):
    """Sensor showing the number of open deliveries."""

    _attr_has_entity_name = True
    _attr_name = "Deliveries"
    _attr_icon = "mdi:package-variant"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "packages"

    def __init__(
        self, coordinator: BuildingLinkCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_deliveries"
        self._entry = entry

    @property
    def native_value(self) -> int | None:
        """Return the number of open deliveries."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data["count"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return delivery details as attributes."""
        if self.coordinator.data is None:
            return {}

        deliveries = self.coordinator.data.get("deliveries", [])
        attrs: dict[str, Any] = {"deliveries": []}

        for d in deliveries:
            delivery_info: dict[str, Any] = {
                "id": d.get("Id"),
                "description": d.get("Description", ""),
                "is_open": d.get("IsOpen", True),
                "open_date": d.get("OpenDate") or d.get("OpenDateOld"),
            }

            location = d.get("Location")
            if location:
                delivery_info["location"] = location.get("Description", "")

            dtype = d.get("Type")
            if dtype:
                delivery_info["type"] = dtype.get("DescriptionLong", "")

            attrs["deliveries"].append(delivery_info)

        return attrs

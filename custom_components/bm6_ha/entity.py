"""Base entity for the BM6 battery monitor."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BM6Coordinator


class BM6Entity(CoordinatorEntity[BM6Coordinator]):
    """Common device info and naming."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BM6Coordinator, description: EntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        address = coordinator.address
        self._attr_unique_id = f"{address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
            identifiers={(DOMAIN, address)},
            name=coordinator.device_name,
            manufacturer="Leagend / DC",
            model="BM6 Battery Monitor",
            sw_version=coordinator.data.firmware if coordinator.data else None,
        )

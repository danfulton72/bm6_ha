"""The BM6 battery monitor integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_FULL,
    ATTR_STEP,
    CONF_BATTERY_TYPE,
    CONF_SOC_CURVE,
    DOMAIN,
    SERVICE_CHARGING_TEST,
    SERVICE_SYNC_HISTORY,
)
from .coordinator import BM6ConfigEntry, BM6Coordinator

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SENSOR,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ATTR_DEVICE_ID = "device_id"

SYNC_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_FULL, default=False): cv.boolean,
    }
)
CHARGING_TEST_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_STEP): vol.All(vol.Coerce(int), vol.In([1, 2, 3])),
    }
)


def _coordinator_for_device(hass: HomeAssistant, device_id: str) -> BM6Coordinator:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="unknown_device"
        )
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == DOMAIN and hasattr(entry, "runtime_data"):
            return entry.runtime_data
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_device")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services."""

    async def sync_history(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator_for_device(hass, call.data[ATTR_DEVICE_ID])
        return await coordinator.async_sync_history(call.data[ATTR_FULL])

    async def charging_test(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator_for_device(hass, call.data[ATTR_DEVICE_ID])
        result = await coordinator.async_charging_test_step(call.data[ATTR_STEP])
        return {"step": call.data[ATTR_STEP], "result": result}

    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC_HISTORY,
        sync_history,
        schema=SYNC_HISTORY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CHARGING_TEST,
        charging_test,
        schema=CHARGING_TEST_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BM6ConfigEntry) -> bool:
    """Set up a BM6 from a config entry."""
    address: str = entry.unique_id or entry.data[CONF_ADDRESS]
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="not_found",
            translation_placeholders={"address": address},
        )

    coordinator = BM6Coordinator(hass, entry, ble_device)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_start_advert_listener())
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    coordinator.options_snapshot = dict(entry.options)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: BM6ConfigEntry) -> None:
    """Apply option changes; only reload when connection settings changed."""
    coordinator = entry.runtime_data
    old = coordinator.options_snapshot
    new = dict(entry.options)
    coordinator.options_snapshot = new
    changed = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
    if changed and changed <= {CONF_BATTERY_TYPE, CONF_SOC_CURVE}:
        coordinator.mark_battery_type_pending()
        coordinator.async_update_listeners()
        # Write the new type straight away (not debounced).
        await coordinator.async_refresh()
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: BM6ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded

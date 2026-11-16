"""Battery type selection for the BM6 (written to the device)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import BATTERY_TYPE_OPTIONS, CONF_BATTERY_TYPE, CONF_SOC_CURVE, DOMAIN
from .coordinator import BM6ConfigEntry, parse_curve
from .entity import BM6Entity

DESCRIPTION = SelectEntityDescription(
    key="battery_type",
    translation_key="battery_type",
    entity_category=EntityCategory.CONFIG,
    options=list(BATTERY_TYPE_OPTIONS),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BM6ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([BM6BatteryTypeSelect(entry.runtime_data, DESCRIPTION)])


class BM6BatteryTypeSelect(BM6Entity, SelectEntity):
    """Battery chemistry used by the BM6 to calculate state of charge.

    Until a type is chosen here, the integration never writes one, so the
    setting made in the official app is left alone.
    """

    @property
    def available(self) -> bool:
        return True

    @property
    def current_option(self) -> str | None:
        return self.coordinator.battery_type_key

    async def async_select_option(self, option: str) -> None:
        btype = BATTERY_TYPE_OPTIONS[option]
        entry = self.coordinator.config_entry
        if btype.needs_curve:
            try:
                ok = parse_curve(entry.options.get(CONF_SOC_CURVE)) is not None
            except ValueError:
                ok = False
            if not ok:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="curve_required"
                )
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_BATTERY_TYPE: option}
        )

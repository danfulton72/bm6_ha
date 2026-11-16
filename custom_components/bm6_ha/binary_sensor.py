"""Binary sensors for the BM6 battery monitor (the app's warning indicators)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import protocol as p
from .coordinator import BM6ConfigEntry, BM6Coordinator, BM6Data
from .entity import BM6Entity


@dataclass(frozen=True, kw_only=True)
class BM6BinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[BM6Data, BM6Coordinator], bool | None]
    persistent: bool = False


def _voltage_problem(d: BM6Data, c: BM6Coordinator) -> bool | None:
    if d.voltage is None:
        return None
    limit = p.VOLTAGE_OK_LITHIUM if c.is_lithium else p.VOLTAGE_OK_LEAD
    return d.voltage < limit and d.status != p.Status.CHARGING


def _crank_problem(d: BM6Data, c: BM6Coordinator) -> bool | None:
    if d.crank_status is None:
        return None
    limit = p.CRANK_MIN_LITHIUM if c.is_lithium else p.CRANK_MIN_LEAD
    return d.crank_status == p.CrankStatus.LOW or (
        d.crank_voltage is not None and d.crank_voltage < limit
    )


def _charge_problem(d: BM6Data, c: BM6Coordinator) -> bool | None:
    if d.charge_status is None:
        return None
    return d.charge_status != p.ChargeStatus.OK


BINARY_SENSORS: tuple[BM6BinaryDescription, ...] = (
    BM6BinaryDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda d, c: None if d.status is None else d.status == p.Status.CHARGING,
    ),
    BM6BinaryDescription(
        key="low_battery",
        translation_key="low_battery",
        device_class=BinarySensorDeviceClass.BATTERY,
        value_fn=lambda d, c: None if d.status is None else d.status == p.Status.LOW,
    ),
    BM6BinaryDescription(
        key="voltage_problem",
        translation_key="voltage_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_voltage_problem,
    ),
    BM6BinaryDescription(
        key="temperature_problem",
        translation_key="temperature_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d, c: None
        if d.temperature is None
        else d.temperature > p.TEMPERATURE_MAX_OK,
    ),
    BM6BinaryDescription(
        key="crank_problem",
        translation_key="crank_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        persistent=True,
        value_fn=_crank_problem,
    ),
    BM6BinaryDescription(
        key="charging_system_problem",
        translation_key="charging_system_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        persistent=True,
        value_fn=_charge_problem,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BM6ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(BM6BinarySensor(coordinator, desc) for desc in BINARY_SENSORS)


class BM6BinarySensor(BM6Entity, BinarySensorEntity):
    """A BM6 binary sensor."""

    entity_description: BM6BinaryDescription

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data, self.coordinator)

    @property
    def available(self) -> bool:
        if self.entity_description.persistent and self.coordinator.data is not None:
            return self.is_on is not None
        return super().available and self.is_on is not None

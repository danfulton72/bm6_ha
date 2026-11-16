"""Sensors for the BM6 battery monitor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import protocol as p
from .coordinator import BM6ConfigEntry, BM6Data
from .entity import BM6Entity
from .statistics import statistic_id

STATUS_OPTIONS = ["normal", "low", "charging"]
CRANK_OPTIONS = ["ok", "low"]
CHARGE_OPTIONS = ["ok", "high", "low"]


def _status(value: int | None) -> str | None:
    return {0: "normal", 1: "low", 2: "charging"}.get(value) if value is not None else None


def _crank(value: int | None) -> str | None:
    return {1: "ok", 2: "low"}.get(value) if value is not None else None


def _charge(value: int | None) -> str | None:
    return {1: "ok", 2: "high", 3: "low"}.get(value) if value is not None else None


@dataclass(frozen=True, kw_only=True)
class BM6SensorDescription(SensorEntityDescription):
    value_fn: Callable[[BM6Data], Any]
    # Test results are kept (and stay available) when the car is out of range.
    persistent: bool = False


SENSORS: tuple[BM6SensorDescription, ...] = (
    BM6SensorDescription(
        key="voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.voltage,
    ),
    BM6SensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.soc,
    ),
    BM6SensorDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.temperature,
    ),
    BM6SensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUS_OPTIONS,
        value_fn=lambda d: _status(d.status),
    ),
    BM6SensorDescription(
        key="rapid_acceleration",
        translation_key="rapid_acceleration",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.rapid_acceleration,
    ),
    BM6SensorDescription(
        key="rapid_deceleration",
        translation_key="rapid_deceleration",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.rapid_deceleration,
    ),
    # Cranking (starting system) test
    BM6SensorDescription(
        key="crank_voltage",
        translation_key="crank_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        persistent=True,
        value_fn=lambda d: d.crank_voltage,
    ),
    BM6SensorDescription(
        key="crank_duration",
        translation_key="crank_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        persistent=True,
        value_fn=lambda d: d.crank_duration_ms,
    ),
    BM6SensorDescription(
        key="crank_result",
        translation_key="crank_result",
        device_class=SensorDeviceClass.ENUM,
        options=CRANK_OPTIONS,
        persistent=True,
        value_fn=lambda d: _crank(d.crank_status),
    ),
    BM6SensorDescription(
        key="crank_time",
        translation_key="crank_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        persistent=True,
        value_fn=lambda d: d.crank_time,
    ),
    # Charging system test
    BM6SensorDescription(
        key="charge_result",
        translation_key="charge_result",
        device_class=SensorDeviceClass.ENUM,
        options=CHARGE_OPTIONS,
        persistent=True,
        value_fn=lambda d: _charge(d.charge_status),
    ),
    BM6SensorDescription(
        key="charge_high_rpm_voltage",
        translation_key="charge_high_rpm_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        persistent=True,
        value_fn=lambda d: d.charge_load_voltage,
    ),
    BM6SensorDescription(
        key="charge_idle_voltage",
        translation_key="charge_idle_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        persistent=True,
        value_fn=lambda d: d.charge_no_load_voltage,
    ),
    BM6SensorDescription(
        key="charge_ripple",
        translation_key="charge_ripple",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        persistent=True,
        value_fn=lambda d: d.charge_ripple_mv,
    ),
    BM6SensorDescription(
        key="charge_time",
        translation_key="charge_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        persistent=True,
        value_fn=lambda d: d.charge_time,
    ),
    # Diagnostics
    BM6SensorDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        persistent=True,
        value_fn=lambda d: d.firmware,
    ),
    BM6SensorDescription(
        key="rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.rssi,
    ),
    BM6SensorDescription(
        key="last_update",
        translation_key="last_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        persistent=True,
        value_fn=lambda d: d.last_update,
    ),
    BM6SensorDescription(
        key="history_synced_until",
        translation_key="history_synced_until",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        persistent=True,
        value_fn=lambda d: d.history_synced_until,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BM6ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(BM6Sensor(coordinator, desc) for desc in SENSORS)


class BM6Sensor(BM6Entity, SensorEntity):
    """A BM6 sensor."""

    entity_description: BM6SensorDescription
    _unrecorded_attributes = frozenset({"waveform"})

    @property
    def available(self) -> bool:
        if self.entity_description.persistent and self.coordinator.data is not None:
            return self.native_value is not None
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        key = self.entity_description.key
        if data is None:
            return None
        if key == "crank_voltage" and data.crank_waveform:
            return {
                "waveform": data.crank_waveform,
                "sample_interval_ms": 10,
                "minimum_voltage": min(data.crank_waveform),
            }
        if key == "history_synced_until":
            addr = self.coordinator.address
            return {
                "statistic_ids": [
                    statistic_id(addr, k) for k in ("voltage", "soc", "temperature")
                ],
                "last_sync": data.history_last_sync,
                "records_in_last_sync": data.history_records,
            }
        if key == "charge_result" and data.charge_step_done:
            return {"steps_completed": data.charge_step_done}
        if key == "status":
            return {"engine_running": data.status == p.Status.CHARGING}
        return None

"""Buttons for the BM6: charging system test steps, history sync, refresh."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BM6ConfigEntry, BM6Coordinator
from .entity import BM6Entity


@dataclass(frozen=True, kw_only=True)
class BM6ButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[BM6Coordinator], Awaitable[Any]]


BUTTONS: tuple[BM6ButtonDescription, ...] = (
    BM6ButtonDescription(
        key="charge_test_step_1",
        translation_key="charge_test_step_1",
        press_fn=lambda c: c.async_charging_test_step(1),
    ),
    BM6ButtonDescription(
        key="charge_test_step_2",
        translation_key="charge_test_step_2",
        press_fn=lambda c: c.async_charging_test_step(2),
    ),
    BM6ButtonDescription(
        key="charge_test_step_3",
        translation_key="charge_test_step_3",
        press_fn=lambda c: c.async_charging_test_step(3),
    ),
    BM6ButtonDescription(
        key="sync_history",
        translation_key="sync_history",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda c: c.async_sync_history(False),
    ),
    BM6ButtonDescription(
        key="refresh",
        device_class=ButtonDeviceClass.UPDATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        translation_key="refresh",
        press_fn=lambda c: c.async_request_refresh(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BM6ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(BM6Button(entry.runtime_data, desc) for desc in BUTTONS)


class BM6Button(BM6Entity, ButtonEntity):
    """A BM6 action button."""

    entity_description: BM6ButtonDescription

    @property
    def available(self) -> bool:
        # Actions open their own connection, so allow them even when the last
        # poll failed.
        return True

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator)

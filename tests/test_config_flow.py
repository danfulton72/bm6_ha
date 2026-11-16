"""Config flow tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from bleak.backends.device import BLEDevice
from pytest_homeassistant.common import MockConfigEntry

from custom_components.bm6_ha.const import DOMAIN

from .conftest import ADDRESS

pytestmark = pytest.mark.usefixtures("enable_bluetooth")


def _info(name: str = "BM6", address: str = ADDRESS) -> BluetoothServiceInfoBleak:
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        source="local",
        device=BLEDevice(address, name, {}),
        advertisement=None,
        connectable=True,
        time=0,
        tx_power=None,
    )


async def test_bluetooth_discovery(hass: HomeAssistant) -> None:
    with patch("custom_components.bm6_ha.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_info()
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "bluetooth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "BM6 0203"
    assert result["data"] == {CONF_ADDRESS: ADDRESS}


async def test_bluetooth_not_bm6(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_info("Other")
    )
    assert result["type"] is FlowResultType.ABORT


async def test_user_flow(hass: HomeAssistant) -> None:
    with (
        patch(
            "custom_components.bm6_ha.config_flow.async_discovered_service_info",
            return_value=[_info(), _info("Phone", "11:22:33:44:55:66")],
        ),
        patch("custom_components.bm6_ha.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: ADDRESS}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_none(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.bm6_ha.config_flow.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_options_flow_validates_curve(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    base = {
        "poll_interval": 60,
        "keep_connected": False,
        "auto_history": True,
        "history_interval": 60,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "soc_curve": "12.5, 12"}
    )
    assert result["errors"] == {"soc_curve": "invalid_curve"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**base, "soc_curve": "12.9,12.8,12.7,12.6,12.5,12.4,12.3,12.2,12.1,12.0,11.9"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["poll_interval"] == 60

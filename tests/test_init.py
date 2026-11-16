"""End-to-end tests of the BM6 integration against a simulated device."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from bleak.backends.device import BLEDevice
import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
)

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from custom_components.bm6_ha import protocol as p
from custom_components.bm6_ha.const import CONF_BATTERY_TYPE, CONF_SOC_CURVE, DOMAIN
from custom_components.bm6_ha.statistics import statistic_id

from .conftest import ADDRESS, FakeBM6, history_record

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_bluetooth")


@pytest.fixture(autouse=True)
def mock_recorder_before_hass(async_test_recorder) -> None:
    """Make sure the recorder is set up before hass."""

BLE_DEVICE = BLEDevice(ADDRESS, "BM6", {})


@pytest.fixture
def ble_present():
    with (
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            return_value=BLE_DEVICE,
        ),
    ):
        yield


async def _setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="BM6 0203",
        data={CONF_ADDRESS: ADDRESS},
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.usefixtures("ble_present")
async def test_setup_reads_realtime_and_crank(hass: HomeAssistant, fake_bm6: FakeBM6) -> None:
    events = []
    hass.bus.async_listen("bm6_ha_crank_detected", events.append)
    entry = await _setup(hass, {"auto_history": False})
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get("sensor.bm6_0203_voltage").state == "12.5"
    assert hass.states.get("sensor.bm6_0203_battery").state == "80"
    assert hass.states.get("sensor.bm6_0203_temperature").state == "25"
    assert hass.states.get("sensor.bm6_0203_status").state == "normal"
    assert hass.states.get("binary_sensor.bm6_0203_charging").state == "off"
    assert hass.states.get("binary_sensor.bm6_0203_voltage_warning").state == "off"
    assert hass.states.get("sensor.bm6_0203_firmware").state == "1.8"

    crank = hass.states.get("sensor.bm6_0203_cranking_voltage")
    assert crank.state == "10.4"
    assert crank.attributes["waveform"] == [12.0, 10.0, 10.4]
    assert hass.states.get("sensor.bm6_0203_cranking_duration").state == "180"
    assert hass.states.get("sensor.bm6_0203_cranking_test").state == "ok"
    assert hass.states.get("binary_sensor.bm6_0203_cranking_warning").state == "off"
    assert len(events) == 1

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ADDRESS), entry.entry_id
    )
    assert device.sw_version == "1.8"

    # Battery type is never written unless chosen.
    assert not any(w.hex().upper().startswith("D15508") for w in fake_bm6.writes)
    # The handshake mirrors the app.
    assert fake_bm6.writes[0] == p.CMD_VERSION
    assert fake_bm6.writes[1] == p.CMD_GENUINE.rstrip(b"\x00")


@pytest.mark.usefixtures("ble_present")
async def test_engine_events_and_low_voltage(hass: HomeAssistant, fake_bm6: FakeBM6) -> None:
    started, stopped = [], []
    hass.bus.async_listen("bm6_ha_engine_started", started.append)
    hass.bus.async_listen("bm6_ha_engine_stopped", stopped.append)
    entry = await _setup(hass, {"auto_history": False})
    coordinator = entry.runtime_data

    fake_bm6.realtime = "D15507" + "00" + "1E" + "02" + "5A" + "0582" + "0003" + "0004"
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.bm6_0203_charging").state == "on"
    assert hass.states.get("sensor.bm6_0203_status").state == "charging"
    assert len(started) == 1

    fake_bm6.realtime = "D15507" + "01" + "05" + "01" + "28" + "0482" + "0003" + "0004"
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(stopped) == 1
    assert stopped[0].data["rapid_acceleration"] == 3
    assert hass.states.get("sensor.bm6_0203_temperature").state == "-5"
    assert hass.states.get("binary_sensor.bm6_0203_low_charge").state == "on"
    assert hass.states.get("binary_sensor.bm6_0203_voltage_warning").state == "on"


@pytest.mark.usefixtures("ble_present")
async def test_history_import(hass: HomeAssistant, fake_bm6: FakeBM6) -> None:
    now = dt_util.utcnow()
    ref = p.history_reference_time(now)
    # 6 hours of records, newest first; engine start then stop in the middle.
    recs = []
    for i in range(180):
        flags = 0
        if i == 50:
            flags = 0b100
        elif i == 80:
            flags = 0b010
        recs.append(history_record(12.0 + (i % 10) / 10, 70 + i % 5, 20 - i % 3, flags))
        if flags == 0b100:
            recs.append("00040001")
    fake_bm6.history_records = recs

    entry = await _setup(hass, {"auto_history": False})
    response = await hass.services.async_call(
        DOMAIN,
        "sync_history",
        {
            "device_id": dr.async_get(hass)
            .async_get_device_by_identifier((DOMAIN, ADDRESS), entry.entry_id)
            .id,
            "full": True,
        },
        blocking=True,
        return_response=True,
    )
    assert response["records"] == 180
    assert response["trips"][0]["rapid_acceleration"] == 4
    assert response["trips"][0]["start"] == (ref - timedelta(minutes=160)).isoformat()

    # Full download requests the 72-day maximum on extended firmware.
    req = next(w for w in fake_bm6.writes if w.hex().upper().startswith("D15505"))
    assert int(req.hex()[12:18], 16) == p.HISTORY_MAX_RECORDS_EXTENDED

    await hass.async_block_till_done()
    from homeassistant.components.recorder import get_instance

    await get_instance(hass).async_block_till_done()
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        ref - timedelta(hours=8),
        None,
        {statistic_id(ADDRESS, "voltage")},
        "hour",
        None,
        {"mean", "min", "max"},
    )
    rows = stats[statistic_id(ADDRESS, "voltage")]
    assert rows
    assert all(12.0 <= r["min"] <= r["mean"] <= r["max"] <= 12.9 for r in rows)
    synced = hass.states.get("sensor.bm6_0203_history_imported_until").state
    assert synced not in ("unknown", "unavailable")

    # Next sync is incremental.
    fake_bm6.writes.clear()
    await entry.runtime_data.async_sync_history(False)
    req = next(w for w in fake_bm6.writes if w.hex().upper().startswith("D15505"))
    assert int(req.hex()[12:18], 16) < 200


@pytest.mark.usefixtures("ble_present")
async def test_battery_type_select_writes_device(hass: HomeAssistant, fake_bm6: FakeBM6) -> None:
    entry = await _setup(hass, {"auto_history": False})
    fake_bm6.writes.clear()
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.bm6_0203_battery_type", "option": "agm"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_BATTERY_TYPE] == "agm"
    assert bytes.fromhex("D1550802") in fake_bm6.writes
    assert entry.state is ConfigEntryState.LOADED

    # Custom types need a curve first.
    from homeassistant.exceptions import ServiceValidationError

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.bm6_0203_battery_type", "option": "lithium_custom_voltage"},
            blocking=True,
        )
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_SOC_CURVE: "13.6,13.4,13.3,13.3,13.2,13.2,13.1,13.0,12.9,12.5,12.0",
        },
    )
    await hass.async_block_till_done()
    fake_bm6.writes.clear()
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.bm6_0203_battery_type", "option": "lithium_custom_voltage"},
        blocking=True,
    )
    await hass.async_block_till_done()
    frames = [w for w in fake_bm6.writes if w.hex().upper().startswith("D1550807")]
    assert len(frames) == 2


@pytest.mark.usefixtures("ble_present")
async def test_charging_test(hass: HomeAssistant, fake_bm6: FakeBM6) -> None:
    events = []
    hass.bus.async_listen("bm6_ha_charging_test_completed", events.append)
    await _setup(hass, {"auto_history": False})
    for step in (1, 2, 3):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": f"button.bm6_0203_charging_test_step_{step}_"
             + {1: "idle_loads_on", 2: "2500_3000_rpm", 3: "idle_ripple"}[step]},
            blocking=True,
        )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.bm6_0203_charging_test").state == "ok"
    assert hass.states.get("sensor.bm6_0203_idle_charging_voltage").state == "14.0"
    assert hass.states.get("sensor.bm6_0203_high_speed_charging_voltage").state == "14.5"
    assert hass.states.get("sensor.bm6_0203_diode_ripple_voltage").state == "40"
    assert hass.states.get("binary_sensor.bm6_0203_charging_system_warning").state == "off"
    assert len(events) == 1


async def test_not_in_range(hass: HomeAssistant, fake_bm6: FakeBM6) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, title="BM6 0203", data={CONF_ADDRESS: ADDRESS}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.usefixtures("ble_present")
async def test_unload(hass: HomeAssistant, fake_bm6: FakeBM6) -> None:
    entry = await _setup(hass, {"auto_history": False})
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not fake_bm6.connected


@pytest.mark.usefixtures("ble_present")
async def test_keep_connected_streams(hass: HomeAssistant, fake_bm6: FakeBM6) -> None:
    entry = await _setup(hass, {"auto_history": False, "keep_connected": True})
    assert fake_bm6.connected
    # The device pushes a new real-time frame on its own.
    fake_bm6._send("D15507" + "00" + "1A" + "00" + "4B" + "04D8" + "0000" + "0000")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.bm6_0203_voltage").state == "12.4"
    assert hass.states.get("sensor.bm6_0203_battery").state == "75"
    # A second poll reuses the connection (no new version handshake).
    versions = sum(1 for w in fake_bm6.writes if w == p.CMD_VERSION)
    await entry.runtime_data.async_refresh()
    assert sum(1 for w in fake_bm6.writes if w == p.CMD_VERSION) == versions


@pytest.mark.usefixtures("ble_present")
async def test_advert_fills_in_when_stale(hass: HomeAssistant, fake_bm6: FakeBM6) -> None:
    from homeassistant.components.bluetooth import BluetoothChange, BluetoothServiceInfoBleak

    entry = await _setup(hass, {"auto_history": False})
    coordinator = entry.runtime_data
    plain = bytes.fromhex("A4C138010203" + "0000" + "3C" + "01" + "00" + "03C0" + "01" + "0000")
    enc = p.encrypt(plain)
    info = BluetoothServiceInfoBleak(
        name="BM6", address=ADDRESS, rssi=-71,
        manufacturer_data={int.from_bytes(enc[:2], "little"): enc[2:]},
        service_data={}, service_uuids=[], source="local", device=BLE_DEVICE,
        advertisement=None, connectable=True, time=0, tx_power=None,
    )
    # Fresh poll data wins over adverts.
    coordinator._on_advert(info, BluetoothChange.ADVERTISEMENT)
    assert coordinator.data.soc == 80
    assert coordinator.data.rssi == -71
    # Once polling data is stale, adverts keep SoC/status current.
    coordinator.data.last_update = dt_util.utcnow() - timedelta(hours=1)
    coordinator._last_advert_push = 0
    coordinator._on_advert(info, BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.bm6_0203_battery").state == "60"
    assert hass.states.get("sensor.bm6_0203_status").state == "low"

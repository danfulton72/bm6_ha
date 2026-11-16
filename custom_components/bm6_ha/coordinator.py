"""Data update coordinator for the BM6 battery monitor."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import protocol as p
from .client import BM6Client, BM6Error, PollResult
from .const import (
    BATTERY_TYPE_OPTIONS,
    CONF_AUTO_HISTORY,
    CONF_BATTERY_TYPE,
    CONF_HISTORY_INTERVAL,
    CONF_KEEP_CONNECTED,
    CONF_POLL_INTERVAL,
    CONF_SOC_CURVE,
    DEFAULT_AUTO_HISTORY,
    DEFAULT_HISTORY_INTERVAL,
    DEFAULT_KEEP_CONNECTED,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    EVENT_CHARGE_TEST,
    EVENT_CRANK,
    EVENT_ENGINE_STARTED,
    EVENT_ENGINE_STOPPED,
    STORAGE_VERSION,
    STREAM_UPDATE_THROTTLE,
)
from .statistics import async_import_history

_LOGGER = logging.getLogger(__name__)

ADVERT_UPDATE_THROTTLE = 30.0

type BM6ConfigEntry = ConfigEntry[BM6Coordinator]


@dataclass
class BM6Data:
    """State exposed to entities."""

    voltage: float | None = None
    soc: int | None = None
    temperature: int | None = None
    status: int | None = None
    rapid_acceleration: int | None = None
    rapid_deceleration: int | None = None
    firmware: str | None = None
    rssi: int | None = None
    last_update: datetime | None = None
    crank_status: int | None = None
    crank_voltage: float | None = None
    crank_duration_ms: int | None = None
    crank_time: datetime | None = None
    crank_waveform: list[float] = field(default_factory=list)
    charge_status: int | None = None
    charge_no_load_voltage: float | None = None
    charge_load_voltage: float | None = None
    charge_ripple_mv: int | None = None
    charge_time: datetime | None = None
    charge_step_done: int | None = None
    history_synced_until: datetime | None = None
    history_last_sync: datetime | None = None
    history_records: int | None = None


def parse_curve(text: str | None) -> list[float] | None:
    """Parse a comma separated 11-point SoC curve."""
    if not text:
        return None
    values = [float(v) for v in text.replace(";", ",").split(",") if v.strip()]
    if len(values) != 11:
        raise ValueError("expected 11 voltages")
    if any(not 2.6 <= v <= 40.9 for v in values):
        raise ValueError("voltage out of range")
    return values


class BM6Coordinator(DataUpdateCoordinator[BM6Data]):
    """Polls one BM6 and listens to its adverts."""

    config_entry: BM6ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: BM6ConfigEntry, ble_device) -> None:
        self.address: str = entry.unique_id or entry.data["address"]
        self.device_name: str = entry.title
        interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {self.address}",
            update_interval=timedelta(seconds=interval),
        )
        self.client = BM6Client(ble_device, self.device_name)
        self.client.realtime_callback = self._on_stream_realtime
        self.keep_connected: bool = entry.options.get(
            CONF_KEEP_CONNECTED, DEFAULT_KEEP_CONNECTED
        )
        self.client.idle_timeout = None if self.keep_connected else 10.0
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._state = BM6Data()
        self._battery_type_pending = entry.options.get(CONF_BATTERY_TYPE) is not None
        self._last_success: float | None = None
        self._last_stream_push = 0.0
        self._last_advert_push = 0.0
        self._poll_lock = asyncio.Lock()
        self._unsub_adverts: CALLBACK_TYPE | None = None
        self.options_snapshot: dict[str, Any] = dict(entry.options)

    # ------------------------------------------------------------------ #
    # Setup / teardown
    # ------------------------------------------------------------------ #
    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        s = self._state
        if until := stored.get("history_synced_until"):
            s.history_synced_until = dt_util.parse_datetime(until)
        if last := stored.get("history_last_sync"):
            s.history_last_sync = dt_util.parse_datetime(last)
        if crank := stored.get("crank"):
            s.crank_status = crank.get("status")
            s.crank_voltage = crank.get("voltage")
            s.crank_duration_ms = crank.get("duration_ms")
            s.crank_waveform = crank.get("waveform") or []
            if ts := crank.get("time"):
                s.crank_time = dt_util.parse_datetime(ts)
        if charge := stored.get("charge"):
            s.charge_status = charge.get("status")
            s.charge_no_load_voltage = charge.get("no_load_voltage")
            s.charge_load_voltage = charge.get("load_voltage")
            s.charge_ripple_mv = charge.get("ripple_mv")
            if ts := charge.get("time"):
                s.charge_time = dt_util.parse_datetime(ts)
        s.firmware = stored.get("firmware")

    def _build_store(self) -> dict[str, Any]:
        s = self._state

        def _iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "history_synced_until": _iso(s.history_synced_until),
            "history_last_sync": _iso(s.history_last_sync),
            "firmware": s.firmware,
            "crank": {
                "status": s.crank_status,
                "voltage": s.crank_voltage,
                "duration_ms": s.crank_duration_ms,
                "waveform": s.crank_waveform,
                "time": _iso(s.crank_time),
            },
            "charge": {
                "status": s.charge_status,
                "no_load_voltage": s.charge_no_load_voltage,
                "load_voltage": s.charge_load_voltage,
                "ripple_mv": s.charge_ripple_mv,
                "time": _iso(s.charge_time),
            },
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._build_store, 2)

    @callback
    def async_start_advert_listener(self) -> CALLBACK_TYPE:
        self._unsub_adverts = bluetooth.async_register_callback(
            self.hass,
            self._on_advert,
            bluetooth.BluetoothCallbackMatcher(address=self.address),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
        return self._unsub_adverts

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.client.disconnect()
        await self._store.async_save(self._build_store())

    # ------------------------------------------------------------------ #
    # Options helpers
    # ------------------------------------------------------------------ #
    @property
    def battery_type_key(self) -> str | None:
        return self.config_entry.options.get(CONF_BATTERY_TYPE)

    @property
    def is_lithium(self) -> bool:
        key = self.battery_type_key
        return key is not None and BATTERY_TYPE_OPTIONS[key].is_lithium

    def mark_battery_type_pending(self) -> None:
        self._battery_type_pending = True

    def _battery_type_args(self) -> tuple[p.BatteryType | None, list[float] | None]:
        if not self._battery_type_pending or self.battery_type_key is None:
            return None, None
        btype = BATTERY_TYPE_OPTIONS[self.battery_type_key]
        curve = None
        if btype.needs_curve:
            try:
                curve = parse_curve(self.config_entry.options.get(CONF_SOC_CURVE))
            except ValueError:
                curve = None
        return btype, curve

    def _history_due(self) -> bool:
        opts = self.config_entry.options
        if not opts.get(CONF_AUTO_HISTORY, DEFAULT_AUTO_HISTORY):
            return False
        last = self._state.history_last_sync
        interval = timedelta(
            minutes=opts.get(CONF_HISTORY_INTERVAL, DEFAULT_HISTORY_INTERVAL)
        )
        return last is None or dt_util.utcnow() - last >= interval

    def _history_count(self, extended: bool, full: bool = False) -> int:
        since = None if full else self._state.history_synced_until
        return p.history_count_since(since, dt_util.utcnow(), extended)

    # ------------------------------------------------------------------ #
    # Callbacks
    # ------------------------------------------------------------------ #
    @callback
    def _on_advert(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        if service_info.connectable:
            self.client.set_ble_device(service_info.device)
        self._state.rssi = service_info.rssi
        try:
            adv = p.parse_advert(service_info.manufacturer_data, service_info.address)
        except Exception:  # noqa: BLE001 - malformed advert
            adv = None
        if adv is None or self.data is None:
            return
        # Only fill in from adverts when polling is not keeping data fresh.
        stale = self._state.last_update is None or (
            dt_util.utcnow() - self._state.last_update
            > 2 * (self.update_interval or timedelta(minutes=5))
        )
        if stale and 0 <= adv.soc <= 100:
            self._state.soc = adv.soc
            self._state.status = adv.status
        now = time.monotonic()
        if now - self._last_advert_push >= ADVERT_UPDATE_THROTTLE:
            self._last_advert_push = now
            self.async_update_listeners()

    @callback
    def _on_stream_realtime(self, rt: p.RealtimeData) -> None:
        """Real-time frames pushed while the connection is kept open."""
        self._apply_realtime(rt)
        now = time.monotonic()
        if self.data is not None and now - self._last_stream_push >= STREAM_UPDATE_THROTTLE:
            self._last_stream_push = now
            self.async_update_listeners()

    # ------------------------------------------------------------------ #
    # Processing
    # ------------------------------------------------------------------ #
    def _apply_realtime(self, rt: p.RealtimeData) -> None:
        s = self._state
        previous = s.status
        s.voltage = rt.voltage
        s.soc = rt.soc
        s.temperature = rt.temperature
        s.status = rt.status
        s.rapid_acceleration = rt.rapid_acceleration
        s.rapid_deceleration = rt.rapid_deceleration
        s.last_update = dt_util.utcnow()
        event_data = {
            "address": self.address,
            "name": self.device_name,
            "voltage": rt.voltage,
            "soc": rt.soc,
        }
        if previous is not None and previous != rt.status:
            if rt.status == p.Status.CHARGING:
                self.hass.bus.async_fire(EVENT_ENGINE_STARTED, event_data)
            elif previous == p.Status.CHARGING:
                self.hass.bus.async_fire(
                    EVENT_ENGINE_STOPPED,
                    event_data
                    | {
                        "rapid_acceleration": rt.rapid_acceleration,
                        "rapid_deceleration": rt.rapid_deceleration,
                    },
                )

    def _apply_crank(self, crank: p.CrankData) -> None:
        s = self._state
        ts = crank.timestamp(dt_util.utcnow())
        # The device reports age in 2-minute steps; treat anything within
        # 3 minutes of the stored crank as the same event.
        if s.crank_time and abs((ts - s.crank_time).total_seconds()) < 180:
            return
        if s.crank_time and ts < s.crank_time:
            return
        s.crank_status = crank.status
        s.crank_voltage = crank.voltage
        s.crank_duration_ms = crank.duration_ms
        s.crank_waveform = crank.waveform
        s.crank_time = ts
        self._save()
        self.hass.bus.async_fire(
            EVENT_CRANK,
            {
                "address": self.address,
                "name": self.device_name,
                "voltage": crank.voltage,
                "duration_ms": crank.duration_ms,
                "status": "ok" if crank.status == p.CrankStatus.OK else "low",
                "time": ts.isoformat(),
            },
        )

    async def _apply_history(self, result: PollResult, full: bool) -> dict[str, Any]:
        records = result.history or []
        reference = result.history_reference or dt_util.utcnow()
        since = None if full else self._state.history_synced_until
        imported_until = await async_import_history(
            self.hass, self.address, self.device_name, records, reference, since
        )
        s = self._state
        s.history_last_sync = dt_util.utcnow()
        s.history_records = len(records)
        if imported_until and (
            s.history_synced_until is None or imported_until > s.history_synced_until
        ):
            s.history_synced_until = imported_until
        self._save()
        trips = []
        start = None
        for rec in sorted(records, key=lambda r: r.time):
            if rec.event == "engine_start":
                start = rec.time
            elif rec.event == "engine_stop":
                trips.append(
                    {
                        "start": start.isoformat() if start else None,
                        "end": rec.time.isoformat(),
                        "rapid_acceleration": rec.rapid_acceleration,
                        "rapid_deceleration": rec.rapid_deceleration,
                    }
                )
                start = None
        return {
            "records": len(records),
            "first": min((r.time for r in records), default=None),
            "last": max((r.time for r in records), default=None),
            "imported_until": imported_until,
            "trips": trips,
        }

    async def _poll(self, force_history: bool | None = None) -> tuple[PollResult, dict[str, Any] | None]:
        """Serialize polling and service requests with their own history intent.

        ``None`` respects the auto-history schedule; a bool explicitly
        requests a history download, and True requests full history.
        """
        async with self._poll_lock:
            return await self._poll_locked(force_history)

    async def _poll_locked(
        self, force_history: bool | None = None
    ) -> tuple[PollResult, dict[str, Any] | None]:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise BM6Error("device not in range of a connectable Bluetooth adapter")
        self.client.set_ble_device(ble_device)
        want_history = force_history is not None or self._history_due()
        full = bool(force_history)
        btype, curve = self._battery_type_args()
        result = await self.client.poll(
            read_crank=True,
            history_count=(lambda extended: self._history_count(extended, full))
            if want_history else None,
            battery_type=btype,
            curve=curve,
        )
        if btype is not None:
            self._battery_type_pending = False
        if result.version and self._state.firmware != result.version.text:
            self._state.firmware = result.version.text
            self._save()
            registry = dr.async_get(self.hass)
            if device := registry.async_get_device(identifiers={(DOMAIN, self.address)}):
                registry.async_update_device(device.id, sw_version=result.version.text)
        if result.realtime:
            self._apply_realtime(result.realtime)
        if result.crank:
            self._apply_crank(result.crank)
        summary = None
        if result.history is not None:
            summary = await self._apply_history(result, full)
        for err in result.errors:
            _LOGGER.debug("%s: %s", self.device_name, err)
        self._last_success = time.monotonic()
        return result, summary

    async def _async_update_data(self) -> BM6Data:
        try:
            await self._poll()
        except BM6Error as err:
            # Bluetooth is flaky; ride out short outages with the last data.
            interval = (self.update_interval or timedelta(minutes=2)).total_seconds()
            if (
                self._last_success is not None
                and time.monotonic() - self._last_success < max(3 * interval, 600)
            ):
                _LOGGER.debug("%s: poll failed, keeping last data: %s", self.device_name, err)
                return self._state
            raise UpdateFailed(f"{self.device_name}: {err}") from err
        return self._state

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    async def async_sync_history(self, full: bool = False) -> dict[str, Any]:
        try:
            _result, summary = await self._poll(force_history=full)
        except BM6Error as err:
            raise HomeAssistantError(f"History sync failed: {err}") from err
        self.async_set_updated_data(self._state)
        if summary is None:
            raise HomeAssistantError("The device did not return any history")
        return {
            **summary,
            "first": summary["first"].isoformat() if summary["first"] else None,
            "last": summary["last"].isoformat() if summary["last"] else None,
            "imported_until": summary["imported_until"].isoformat()
            if summary["imported_until"]
            else None,
        }

    async def async_charging_test_step(self, step: int) -> dict[str, Any] | None:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is not None:
            self.client.set_ble_device(ble_device)
        try:
            res = await self.client.charging_test_step(step)
        except BM6Error as err:
            raise HomeAssistantError(f"Charging test step {step} failed: {err}") from err
        s = self._state
        s.charge_step_done = step
        if res is not None:
            s.charge_status = res.status
            s.charge_no_load_voltage = res.no_load_voltage
            s.charge_load_voltage = res.load_voltage
            s.charge_ripple_mv = res.ripple_mv
            s.charge_time = dt_util.utcnow()
            s.charge_step_done = None
            self._save()
            data = {"address": self.address, "name": self.device_name} | asdict(res)
            self.hass.bus.async_fire(EVENT_CHARGE_TEST, data)
        if self.data is not None:
            self.async_update_listeners()
        return asdict(res) if res else None

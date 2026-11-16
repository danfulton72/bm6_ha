"""Bluetooth session handling for the BM6 battery monitor."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
import logging
import time

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from . import protocol as p

_LOGGER = logging.getLogger(__name__)

REPLY_TIMEOUT = 5.0
REALTIME_TIMEOUT = 10.0
HISTORY_FRAME_TIMEOUT = 15.0
HISTORY_TOTAL_TIMEOUT = 600.0
CHARGE_TEST_TIMEOUT = 20.0
BATTERY_TYPE_FRAME_GAP = 0.5


class BM6Error(Exception):
    """Communication with the BM6 failed."""


@dataclass
class PollResult:
    """Everything gathered during one poll."""

    version: p.VersionInfo | None = None
    realtime: p.RealtimeData | None = None
    crank: p.CrankData | None = None
    history: list[p.HistoryRecord] | None = None
    history_reference: datetime | None = None
    extended_history: bool = False
    errors: list[str] = field(default_factory=list)


class BM6Client:
    """Talks to one BM6 over BLE.

    The connection is kept open for ``idle_timeout`` seconds after the last
    operation so that consecutive operations (for example the three steps of
    the charging test) share one connection.  ``idle_timeout`` of ``None``
    keeps the connection open permanently and the device then streams
    real-time data, which is delivered through ``realtime_callback``.
    """

    def __init__(self, ble_device: BLEDevice, name: str) -> None:
        self._ble_device = ble_device
        self._name = name
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._rx_buffer = b""
        self._disconnect_handle: asyncio.TimerHandle | None = None
        self._handshake_done = False
        self.idle_timeout: float | None = 10.0
        self.version: p.VersionInfo | None = None
        self.extended_history = False
        self.streaming = False
        self.realtime_callback: Callable[[p.RealtimeData], None] | None = None
        self.disconnect_callback: Callable[[], None] | None = None

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #
    def set_ble_device(self, ble_device: BLEDevice) -> None:
        self._ble_device = ble_device

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def _ensure_connected(self) -> BleakClient:
        self._cancel_disconnect_timer()
        if self.is_connected:
            assert self._client is not None
            return self._client
        _LOGGER.debug("%s: connecting", self._name)
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self._name,
                disconnected_callback=self._on_disconnected,
                ble_device_callback=lambda: self._ble_device,
            )
        except (BleakError, TimeoutError) as err:
            raise BM6Error(f"Could not connect: {err}") from err
        self._client = client
        self._handshake_done = False
        self.streaming = False
        self._rx_buffer = b""
        self._queue = asyncio.Queue()
        try:
            self.extended_history = (
                client.services.get_service(p.EXTENDED_SERVICE_UUID) is not None
            )
            await client.start_notify(p.NOTIFY_UUID, self._on_notify)
        except (BleakError, TimeoutError) as err:
            await self._disconnect()
            raise BM6Error(f"Could not subscribe to notifications: {err}") from err
        return client

    def _on_disconnected(self, _client: BleakClient) -> None:
        _LOGGER.debug("%s: disconnected", self._name)
        self._client = None
        self._handshake_done = False
        self.streaming = False
        if self.disconnect_callback:
            self.disconnect_callback()

    def _cancel_disconnect_timer(self) -> None:
        if self._disconnect_handle:
            self._disconnect_handle.cancel()
            self._disconnect_handle = None

    def _schedule_disconnect(self, delay: float | None = None) -> None:
        self._cancel_disconnect_timer()
        delay = self.idle_timeout if delay is None else delay
        if delay is None:
            return
        loop = asyncio.get_running_loop()
        self._disconnect_handle = loop.call_later(
            delay, lambda: asyncio.ensure_future(self._timed_disconnect())
        )

    async def _timed_disconnect(self) -> None:
        self._disconnect_handle = None
        if self._lock.locked():
            # A poll can outlive the timer; do not leave the GATT link open.
            self._schedule_disconnect(10.0)
            return
        async with self._lock:
            await self._disconnect()

    async def _disconnect(self) -> None:
        client = self._client
        self._client = None
        self._handshake_done = False
        self.streaming = False
        if client is None:
            return
        try:
            if client.is_connected:
                await client.disconnect()
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: error while disconnecting: %s", self._name, err)

    async def disconnect(self) -> None:
        self._cancel_disconnect_timer()
        async with self._lock:
            await self._disconnect()

    # ------------------------------------------------------------------ #
    # Low level I/O
    # ------------------------------------------------------------------ #
    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        buf = self._rx_buffer + bytes(data)
        usable = len(buf) - len(buf) % 16
        self._rx_buffer = buf[usable:]
        if not usable:
            return
        frame = p.decrypt(buf[:usable]).hex().upper()
        _LOGGER.debug("%s: rx %s", self._name, frame)
        opcode, _ = p.decode_frame(frame)
        if opcode == "07" and not p.is_realtime_ack(frame) and self.streaming:
            try:
                rt = p.parse_realtime(frame)
            except ValueError:
                return
            if self.realtime_callback:
                self.realtime_callback(rt)
        if self._lock.locked():
            # Only queue while an operation is waiting for replies, so the
            # real-time stream does not pile up between polls.
            self._queue.put_nowait(frame)

    async def _write(self, client: BleakClient, command: bytes) -> None:
        _LOGGER.debug("%s: tx %s", self._name, command.hex().upper())
        try:
            await client.write_gatt_char(p.WRITE_UUID, p.encrypt(command), response=False)
        except (BleakError, TimeoutError) as err:
            raise BM6Error(f"Write failed: {err}") from err

    def _drain(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    async def _next_frame(self, timeout: float) -> str:
        if not self.is_connected:
            raise BM6Error("Disconnected")
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError as err:
            raise BM6Error("Timed out waiting for the device") from err

    async def _wait_for(
        self,
        predicate: Callable[[str], bool],
        timeout: float = REPLY_TIMEOUT,
    ) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BM6Error("Timed out waiting for the device")
            frame = await self._next_frame(remaining)
            if predicate(frame):
                return frame

    async def _request(
        self, client: BleakClient, command: bytes, prefix: str, timeout: float = REPLY_TIMEOUT
    ) -> str:
        self._drain()
        await self._write(client, command)
        return await self._wait_for(lambda f: f.startswith(prefix), timeout)

    # ------------------------------------------------------------------ #
    # Protocol steps
    # ------------------------------------------------------------------ #
    async def _handshake(self, client: BleakClient) -> None:
        """Version query followed by the acknowledgement the app sends."""
        if self._handshake_done:
            return
        frame = await self._request(client, p.CMD_VERSION, "D15501")
        self.version = p.parse_version(frame)
        _LOGGER.debug("%s: firmware %s", self._name, self.version.text)
        try:
            await self._request(client, p.CMD_GENUINE, "D15502")
        except BM6Error:
            _LOGGER.debug("%s: no reply to acknowledgement", self._name)
        self._handshake_done = True

    async def _set_battery_type(
        self, client: BleakClient, battery_type: p.BatteryType, curve: list[float] | None
    ) -> None:
        frames = p.cmd_battery_type(battery_type, curve)
        if self.version and self.version.version < 1.7 and len(frames) > 1:
            # Old firmware only understands the two basic codes.
            frames = p.cmd_battery_type(
                p.BatteryType.LITHIUM if battery_type.is_lithium else p.BatteryType.LEAD_ACID
            )
        self._drain()
        for i, frame in enumerate(frames):
            if i:
                await asyncio.sleep(BATTERY_TYPE_FRAME_GAP)
            await self._write(client, frame)
        try:
            await self._wait_for(lambda f: f.startswith("D15508"))
        except BM6Error:
            _LOGGER.debug("%s: battery type not acknowledged", self._name)

    async def _read_crank(self, client: BleakClient) -> p.CrankData | None:
        frame = await self._request(client, p.CMD_CRANK, "D15503")
        if not p.crank_has_data(frame):
            return None
        collected = frame
        deadline = time.monotonic() + REPLY_TIMEOUT * 2
        while not (p.CRANK_WAVE_START in collected and p.HISTORY_END in collected):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                nxt = await self._next_frame(remaining)
            except BM6Error:
                break
            if nxt.startswith("D155"):
                continue
            collected += nxt
        return p.parse_crank(collected)

    async def _read_history(
        self, client: BleakClient, count: int
    ) -> tuple[list[p.HistoryRecord], datetime]:
        reference = p.history_reference_time(p.utcnow())
        header = await self._request(client, p.cmd_history(count), "D15505")
        expected = p.parse_history_header(header)
        _LOGGER.debug("%s: device will send %s history records", self._name, expected)
        if expected <= 0:
            return [], reference
        chunks: list[str] = []
        started = time.monotonic()
        while True:
            remaining = HISTORY_TOTAL_TIMEOUT - (time.monotonic() - started)
            if remaining <= 0:
                with contextlib.suppress(BM6Error):
                    await self._write(client, p.CMD_STOP_HISTORY)
                raise BM6Error("History download took too long")
            try:
                frame = await self._next_frame(min(HISTORY_FRAME_TIMEOUT, remaining))
            except BM6Error as err:
                with contextlib.suppress(BM6Error):
                    await self._write(client, p.CMD_STOP_HISTORY)
                # Never advance the sync cursor based on a partial download.
                raise BM6Error("History download interrupted") from err
            if frame.startswith("D155"):
                continue
            chunks.append(frame)
            # A six-hex-digit marker can straddle two notifications.
            if p.HISTORY_END in "".join(chunks[-2:]):
                break
        return p.parse_history("".join(chunks), reference), reference

    async def _read_realtime(self, client: BleakClient) -> p.RealtimeData:
        self._drain()
        await self._write(client, p.CMD_REALTIME)
        frame = await self._wait_for(
            lambda f: f.startswith("D15507") and not p.is_realtime_ack(f),
            REALTIME_TIMEOUT,
        )
        self.streaming = True
        return p.parse_realtime(frame)

    # ------------------------------------------------------------------ #
    # Public operations
    # ------------------------------------------------------------------ #
    async def poll(
        self,
        *,
        read_crank: bool = True,
        history_count: int | Callable[[bool], int] | None = None,
        battery_type: p.BatteryType | None = None,
        curve: list[float] | None = None,
    ) -> PollResult:
        """Connect (if needed) and collect a full set of data.

        ``history_count`` may be a callable receiving whether the device
        supports the extended (72 day) history.
        """
        result = PollResult()
        async with self._lock:
            try:
                client = await self._ensure_connected()
                await self._handshake(client)
                result.version = self.version
                result.extended_history = self.extended_history
                if battery_type is not None:
                    await self._set_battery_type(client, battery_type, curve)
                if history_count is not None:
                    count = (
                        history_count(self.extended_history)
                        if callable(history_count)
                        else history_count
                    )
                    if count > 0:
                        try:
                            result.history, result.history_reference = (
                                await self._read_history(client, count)
                            )
                        except BM6Error as err:
                            result.errors.append(f"history: {err}")
                if read_crank:
                    try:
                        result.crank = await self._read_crank(client)
                    except BM6Error as err:
                        result.errors.append(f"crank: {err}")
                result.realtime = await self._read_realtime(client)
            except BM6Error:
                await self._disconnect()
                raise
            except (BleakError, TimeoutError) as err:
                await self._disconnect()
                raise BM6Error(str(err)) from err
            self._schedule_disconnect()
        return result

    async def charging_test_step(
        self, step: int, hold_connection: float = 300.0
    ) -> p.ChargeTestData | None:
        """Run one step of the 3-step charging system test.

        The connection is held open for ``hold_connection`` seconds afterwards
        so the next step runs on the same connection, as in the app.
        """
        if step not in p.CMD_CHARGE_STEP:
            raise ValueError("step must be 1, 2 or 3")
        async with self._lock:
            try:
                client = await self._ensure_connected()
                await self._handshake(client)
                frame = await self._request(
                    client,
                    p.CMD_CHARGE_STEP[step],
                    f"D155040{step}",
                    CHARGE_TEST_TIMEOUT,
                )
            except BM6Error:
                await self._disconnect()
                raise
            except (BleakError, TimeoutError) as err:
                await self._disconnect()
                raise BM6Error(str(err)) from err
            if self.idle_timeout is None:
                self._schedule_disconnect(None)
            else:
                self._schedule_disconnect(max(self.idle_timeout, hold_connection))
        if step == 3:
            return p.parse_charge_test(frame)
        return None

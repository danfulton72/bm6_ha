"""Fixtures for BM6 tests: a simulated BM6 speaking the encrypted protocol."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from custom_components.bm6_ha import protocol as p

ADDRESS = "A4:C1:38:01:02:03"


def _blocks(hexstr: str) -> bytes:
    raw = bytes.fromhex(hexstr)
    if len(raw) % 16:
        raw += bytes(16 - len(raw) % 16)
    return raw


class FakeBM6:
    """Simulates the BM6 firmware behind the GATT characteristics."""

    def __init__(self) -> None:
        self.version = "D155010108" + "01"
        self.realtime = "D15507" + "00" + "19" + "00" + "50" + "04E2" + "0002" + "0001"
        self.crank: str | None = (
            "D15503" + "01" + "0410" + "00B4" + "0003" + "FFFFFE" + "4B0" + "3E8" + "410" + "000"
        )
        self.history_records: list[str] = []
        self.extended = True
        self.writes: list[bytes] = []
        self.notify_cb = None
        self.connected = False
        self.stream_task: asyncio.Task | None = None

    def _send(self, hexstr: str) -> None:
        data = p.encrypt(_blocks(hexstr))
        loop = asyncio.get_running_loop()
        loop.call_soon(self.notify_cb, None, bytearray(data))

    def handle(self, cmd: bytes) -> None:
        h = cmd.rstrip(b"\x00").hex().upper()
        self.writes.append(cmd.rstrip(b"\x00"))
        if h.startswith("D15501"):
            self._send(self.version)
        elif h.startswith("D15502"):
            self._send("D1550200")
        elif h.startswith("D15508"):
            self._send(h[:8])
        elif h.startswith("D15503"):
            if self.crank is None:
                self._send("D15503FF")
            else:
                # Split over several notifications like the real device.
                body = self.crank + p.HISTORY_END
                self._send(body[:32])
                for i in range(32, len(body), 32):
                    self._send(body[i : i + 32])
        elif h.startswith("D15505"):
            count = int(h[12:18], 16)
            recs = self.history_records[:count]
            self._send("D15505" + f"{len(recs):04X}")
            if recs:
                payload = "".join(recs) + p.HISTORY_END
                for i in range(0, len(payload), 32):
                    self._send(payload[i : i + 32])
        elif h.startswith("D15507"):
            self._send("D15507FF")
            self._send(self.realtime)
        elif h.startswith("D15504"):
            step = h[6:8]
            if step == "03":
                self._send("D1550403" + "01" + "0578" + "05AA" + "0028")
            else:
                self._send("D15504" + step)


class FakeClient:
    def __init__(self, device: FakeBM6, disconnected_callback) -> None:
        self.device = device
        self._dc = disconnected_callback
        device.connected = True
        self.services = MagicMock()
        self.services.get_service = lambda uuid: (
            object() if (uuid == p.EXTENDED_SERVICE_UUID and device.extended) else None
        )

    @property
    def is_connected(self) -> bool:
        return self.device.connected

    async def start_notify(self, uuid, cb) -> None:
        assert uuid == p.NOTIFY_UUID
        self.device.notify_cb = cb

    async def write_gatt_char(self, uuid, data, response=False) -> None:
        assert uuid == p.WRITE_UUID
        assert response is False
        self.device.handle(p.decrypt(bytes(data)))

    async def disconnect(self) -> None:
        self.device.connected = False


@pytest.fixture
def fake_bm6():
    device = FakeBM6()

    async def _establish(client_cls, ble_device, name, disconnected_callback=None, **kw):
        return FakeClient(device, disconnected_callback)

    with patch("custom_components.bm6_ha.client.establish_connection", _establish):
        yield device


def history_record(volt: float, soc: int, temp: int, flags: int = 0) -> str:
    if temp < 0:
        flags |= 0b1000
        temp = -temp
    return f"{round(volt * 100):03X}{soc:02X}{temp:02X}{flags:X}"


def fixed_now(dt: datetime):
    return patch("custom_components.bm6_ha.protocol.utcnow", return_value=dt)

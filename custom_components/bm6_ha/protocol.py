"""BM6 battery monitor BLE protocol.

Pure-python implementation (no Home Assistant imports) of the protocol used by
the official "BM6" Android app (com.dc.bm6, v2.6.3).

Transport
---------
* GATT service ``0000fff0-...``; commands are written (without response) to
  ``0000fff3-...`` and replies arrive as notifications on ``0000fff4-...``.
* Every frame in both directions is AES-128 encrypted with a fixed key and an
  all-zero IV, one 16-byte block at a time (so effectively AES-ECB).  Commands
  shorter than 16 bytes are zero padded.

All commands start with ``D1 55`` followed by an opcode:

====  =======================================================================
0x01  Firmware version.  Reply ``D15501 MM mm XX``
0x02  "Genuine device" acknowledgement the app sends after the version check
0x03  Read the last cranking (engine start) test
0x04  Charging-system test, steps 01 / 02 / 03 (result returned after 03)
0x05  Read stored history (one record every 2 minutes)
0x06  Stop reading history
0x07  Start real-time data
0x08  Set battery type / custom SoC curve
====  =======================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff4-0000-1000-8000-00805f9b34fb"
# Present on the newer ("intact"/A-type) firmware that stores 72 days of history.
EXTENDED_SERVICE_UUID = "0000fee0-0000-1000-8000-00805f9b34fb"

DEVICE_NAMES = ("BM6", "Battery Guard")

AES_KEY = bytes(
    [108, 101, 97, 103, 101, 110, 100, 0xFF, 0xFE, 48, 49, 48, 48, 48, 48, 57]
)  # b"leagend\xff\xfe0100009"

HEADER = bytes([0xD1, 0x55])

HISTORY_INTERVAL = timedelta(minutes=2)
HISTORY_MAX_RECORDS = 22320  # 31 days
HISTORY_MAX_RECORDS_EXTENDED = 51840  # 72 days

HISTORY_END = "FFFEFE"
HISTORY_EMPTY = "FEFEFE"
CRANK_WAVE_START = "FFFFFE"


class Status(IntEnum):
    """Battery status reported in real-time data and adverts."""

    NORMAL = 0
    LOW = 1
    CHARGING = 2


class CrankStatus(IntEnum):
    """Result of the cranking test."""

    OK = 1
    LOW = 2


class ChargeStatus(IntEnum):
    """Result of the charging system test."""

    OK = 1
    HIGH = 2
    LOW = 3


class BatteryType(IntEnum):
    """Battery type codes written with opcode 0x08."""

    LEAD_ACID = 1  # regular 12 V flooded, EFB and GEL all use this code
    AGM = 2
    LEAD_ACID_CUSTOM_SMART = 3
    LEAD_ACID_CUSTOM_VOLTAGE = 4
    LITHIUM = 5
    LITHIUM_CUSTOM_SMART = 6
    LITHIUM_CUSTOM_VOLTAGE = 7

    @property
    def is_lithium(self) -> bool:
        return self.value >= 5

    @property
    def needs_curve(self) -> bool:
        return self in CUSTOM_CURVE_TYPES


CUSTOM_CURVE_TYPES = {
    BatteryType.LEAD_ACID_CUSTOM_SMART,
    BatteryType.LEAD_ACID_CUSTOM_VOLTAGE,
    BatteryType.LITHIUM_CUSTOM_SMART,
    BatteryType.LITHIUM_CUSTOM_VOLTAGE,
}

# Default SoC curve used by the app (100 % ... 0 % in 10 % steps).
DEFAULT_SOC_CURVE = (12.9, 12.8, 12.7, 12.6, 12.5, 12.4, 12.3, 12.2, 12.1, 12.0, 11.9)

# Thresholds hard coded in the app UI.
VOLTAGE_OK_LEAD = 12.3
VOLTAGE_OK_LITHIUM = 13.13
TEMPERATURE_MAX_OK = 60
CRANK_MIN_LEAD = 9.6
CRANK_MIN_LITHIUM = 8.0
CHARGE_MIN_LEAD = 13.5
CHARGE_MIN_LITHIUM = 13.0
CHARGE_MAX = 14.7


# --------------------------------------------------------------------------- #
# Crypto
# --------------------------------------------------------------------------- #
def _cipher() -> Cipher:
    return Cipher(algorithms.AES(AES_KEY), modes.ECB())


def encrypt(data: bytes) -> bytes:
    """Encrypt a command (zero-padded to a multiple of 16 bytes)."""
    if len(data) % 16:
        data = data + bytes(16 - len(data) % 16)
    enc = _cipher().encryptor()
    return enc.update(data) + enc.finalize()


def decrypt(data: bytes) -> bytes:
    """Decrypt whole 16-byte blocks (any trailing partial block is ignored)."""
    usable = len(data) - len(data) % 16
    if not usable:
        return b""
    dec = _cipher().decryptor()
    return dec.update(data[:usable]) + dec.finalize()


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _cmd(*payload: int) -> bytes:
    return HEADER + bytes(payload)


CMD_VERSION = _cmd(0x01)
CMD_GENUINE = _cmd(0x02, 0x00)
CMD_CRANK = _cmd(0x03)
CMD_CHARGE_STEP = {1: _cmd(0x04, 0x01), 2: _cmd(0x04, 0x02), 3: _cmd(0x04, 0x03)}
CMD_STOP_HISTORY = _cmd(0x06)
CMD_REALTIME = _cmd(0x07)


def cmd_history(count: int, offset: int = 0) -> bytes:
    """Request ``count`` 2-minute records starting ``offset`` records back."""
    return _cmd(0x05) + offset.to_bytes(3, "big") + count.to_bytes(3, "big")


def cmd_battery_type(
    battery_type: BatteryType, curve: list[float] | tuple[float, ...] | None = None
) -> list[bytes]:
    """Build the frame(s) that set the battery type.

    Custom types need an 11 point SoC curve (voltage at 100 %, 90 % ... 0 %),
    sent as two frames of packed 12-bit centivolt values.
    """
    head = _cmd(0x08, int(battery_type)).hex().upper()
    if not battery_type.needs_curve:
        return [bytes.fromhex(head)]
    curve = list(curve or DEFAULT_SOC_CURVE)
    if len(curve) != 11:
        raise ValueError("custom SoC curve needs exactly 11 voltages")
    vals = [f"{round(v * 100):03x}" for v in curve]
    first = head + "01" + "".join(vals[:6])
    second = head + "02" + "".join(vals[6:]) + "000"
    return [bytes.fromhex(first), bytes.fromhex(second)]


def history_count_since(last: datetime | None, now: datetime, extended: bool) -> int:
    """Number of records to request (mirrors the app's incremental sync)."""
    maximum = HISTORY_MAX_RECORDS_EXTENDED if extended else HISTORY_MAX_RECORDS
    if last is None:
        return maximum
    count = int((history_reference_time(now) - last) / HISTORY_INTERVAL) + 10
    return max(10, min(count, maximum))


def history_reference_time(now: datetime) -> datetime:
    """Timestamp of the newest history record.

    The device stores a record on every odd minute; the app floors to the
    minute and steps back one minute when the minute is even.
    """
    ref = now.replace(second=0, microsecond=0)
    if ref.minute % 2 == 0:
        ref -= timedelta(minutes=1)
    return ref


# --------------------------------------------------------------------------- #
# Parsed data
# --------------------------------------------------------------------------- #
@dataclass
class RealtimeData:
    voltage: float
    soc: int
    temperature: int
    status: int
    rapid_acceleration: int
    rapid_deceleration: int


@dataclass
class VersionInfo:
    version: float
    flag: int

    @property
    def text(self) -> str:
        return f"{self.version:.1f}"


@dataclass
class CrankData:
    status: int
    voltage: float
    duration_ms: int
    age_records: int  # how many 2-minute periods ago the crank happened
    waveform: list[float] = field(default_factory=list)  # 10 ms samples

    def timestamp(self, now: datetime) -> datetime:
        return (now - self.age_records * HISTORY_INTERVAL).replace(microsecond=0)


@dataclass
class ChargeTestData:
    status: int
    no_load_voltage: float
    load_voltage: float
    ripple_mv: int


@dataclass
class HistoryRecord:
    time: datetime
    voltage: float
    soc: int
    temperature: int
    event: str | None = None  # "engine_start" | "engine_stop"
    rapid_acceleration: int | None = None
    rapid_deceleration: int | None = None


@dataclass
class AdvertData:
    soc: int
    status: int
    crank_voltage: float
    crank_status: int


def decode_frame(raw_hex: str) -> tuple[str, str]:
    """Return (opcode, hex) for a decrypted frame; opcode '' if not a reply."""
    raw_hex = raw_hex.upper()
    if raw_hex.startswith("D155"):
        return raw_hex[4:6], raw_hex
    return "", raw_hex


def parse_version(h: str) -> VersionInfo:
    major = int(h[6:8], 16)
    minor = int(h[8:10], 16)
    return VersionInfo(version=major + minor / 10, flag=int(h[10:12], 16))


def is_realtime_ack(h: str) -> bool:
    return h.upper().startswith("D15507FF")


def parse_realtime(h: str) -> RealtimeData:
    temp = int(h[8:10], 16)
    if int(h[6:8], 16) == 1:
        temp = -temp
    return RealtimeData(
        temperature=temp,
        status=int(h[10:12], 16),
        soc=int(h[12:14], 16),
        voltage=int(h[14:18], 16) / 100,
        rapid_acceleration=int(h[18:22], 16),
        rapid_deceleration=int(h[22:26], 16),
    )


def crank_has_data(h: str) -> bool:
    return h[6:8].upper() != "FF"


def parse_crank(h: str) -> CrankData:
    """Parse the concatenated crank frames (header .. FFFFFE wave FFFEFE)."""
    h = h.upper()
    data = CrankData(
        status=int(h[6:8], 16),
        voltage=int(h[8:12], 16) / 100,
        duration_ms=int(h[12:16], 16),
        age_records=int(h[16:20], 16),
    )
    start = h.find(CRANK_WAVE_START)
    end = h.find(HISTORY_END, start + 6 if start != -1 else 0)
    if start != -1 and end != -1:
        wave = h[start + 6 : end]
        for i in range(len(wave) // 3):
            val = int(wave[i * 3 : i * 3 + 3], 16) / 100
            if val:
                data.waveform.append(val)
    return data


def parse_charge_test(h: str) -> ChargeTestData:
    return ChargeTestData(
        status=int(h[8:10], 16),
        no_load_voltage=int(h[10:14], 16) / 100,
        load_voltage=int(h[14:18], 16) / 100,
        ripple_mv=int(h[18:22], 16),
    )


def parse_history_header(h: str) -> int:
    """Number of records the device is about to send."""
    return int(h[6:10], 16)


def parse_history(data_hex: str, reference: datetime) -> list[HistoryRecord]:
    """Parse the concatenated history payload (without the FFFEFE marker).

    Each record is 4 bytes: 12-bit voltage (centivolts), 8-bit SoC, 8-bit
    temperature, and a 4-bit flag nibble (bit 3 = negative temperature,
    bits 0-2 = event: 010 engine start, 100 engine stop).  An engine-stop
    record is followed by an extra 4-byte record holding the rapid
    acceleration / deceleration counters of the trip.  Records run newest
    first, 2 minutes apart, ending at ``reference``.
    """
    s = data_hex.upper()
    end = s.find(HISTORY_END)
    if end != -1:
        s = s[:end]
    while s.startswith("00000000"):
        s = s[8:]

    records: list[HistoryRecord] = []
    pos = 0
    index = 0
    while pos + 8 <= len(s):
        rec = s[pos : pos + 8]
        pos += 8
        try:
            volt = int(rec[0:3], 16) / 100
            soc = int(rec[3:5], 16)
            temp = int(rec[5:7], 16)
            flags = int(rec[7], 16)
        except ValueError:
            index += 1
            continue
        if flags & 0b1000:
            temp = -temp
        event_bits = flags & 0b0111
        item = HistoryRecord(
            time=reference - index * HISTORY_INTERVAL,
            voltage=volt,
            soc=soc,
            temperature=temp,
        )
        if event_bits == 0b010:
            item.event = "engine_start"
        elif event_bits == 0b100:
            item.event = "engine_stop"
            extra = s[pos : pos + 8]
            if len(extra) == 8:
                try:
                    item.rapid_acceleration = int(extra[0:4], 16)
                    item.rapid_deceleration = int(extra[4:8], 16)
                except ValueError:
                    pass
            pos += 8
        index += 1
        if volt != 0:
            records.append(item)
    return records


def parse_advert(manufacturer_data: dict[int, bytes], address: str) -> AdvertData | None:
    """Decode the encrypted manufacturer-specific advert.

    The 16 encrypted bytes are carried in an AD structure of type 0xFF, so the
    first two (encrypted) bytes show up as the "company id".
    """
    mac = address.replace(":", "").upper()
    for company_id, payload in manufacturer_data.items():
        raw = company_id.to_bytes(2, "little") + bytes(payload)
        if len(raw) != 16:
            continue
        h = decrypt(raw).hex().upper()
        if h[0:12] != mac:
            continue
        return AdvertData(
            soc=int(h[16:18], 16),
            status=int(h[18:20], 16),
            crank_voltage=int(h[22:26], 16) / 100,
            crank_status=int(h[26:28], 16),
        )
    return None


def utcnow() -> datetime:
    return datetime.now(UTC)

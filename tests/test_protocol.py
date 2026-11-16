"""Unit tests for the BM6 protocol module (no Home Assistant needed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "bm6_ha_protocol",
    Path(__file__).parents[1] / "custom_components" / "bm6_ha" / "protocol.py",
)
p = importlib.util.module_from_spec(_spec)
sys.modules["bm6_ha_protocol"] = p
_spec.loader.exec_module(p)


def test_known_ciphertext() -> None:
    # Widely published encrypted "start real-time data" command.
    assert p.encrypt(p.CMD_REALTIME).hex() == "697ea0b5d54cf024e794772355554114"


def test_roundtrip() -> None:
    frame = bytes.fromhex("D15507000F0050047E00030001000000")
    assert p.decrypt(p.encrypt(frame)) == frame


def test_decrypt_ignores_partial_block() -> None:
    frame = p.encrypt(bytes.fromhex("D15501"))
    assert p.decrypt(frame + b"\x01\x02") == p.decrypt(frame)
    assert p.decrypt(b"\x00" * 5) == b""


def test_parse_realtime() -> None:
    h = "D15507" + "00" + "19" + "02" + "5A" + "0546" + "0003" + "0001" + "000000"
    rt = p.parse_realtime(h)
    assert rt.temperature == 25
    assert rt.status == p.Status.CHARGING
    assert rt.soc == 90
    assert rt.voltage == pytest.approx(13.50)
    assert rt.rapid_acceleration == 3
    assert rt.rapid_deceleration == 1
    neg = p.parse_realtime("D15507" + "01" + "05" + "00" + "40" + "04D8" + "0000" + "0000")
    assert neg.temperature == -5
    assert p.is_realtime_ack("D15507FF000000")
    assert not p.is_realtime_ack(h)


def test_parse_version() -> None:
    v = p.parse_version("D155010108000000")
    assert v.version == pytest.approx(1.8)
    assert v.text == "1.8"


def test_battery_type_frames() -> None:
    assert p.cmd_battery_type(p.BatteryType.AGM) == [bytes.fromhex("D1550802")]
    frames = p.cmd_battery_type(p.BatteryType.LITHIUM_CUSTOM_VOLTAGE, [13.6, 13.4, 13.3, 13.3, 13.2, 13.2, 13.1, 13.0, 12.9, 12.5, 12.0])
    assert len(frames) == 2
    assert all(len(f) == 14 for f in frames)
    assert frames[0].hex().upper().startswith("D155080701550")
    assert frames[1].hex().upper().endswith("4B0000")
    with pytest.raises(ValueError):
        p.cmd_battery_type(p.BatteryType.LEAD_ACID_CUSTOM_SMART, [12.0] * 3)


def test_history_command_and_count() -> None:
    assert p.cmd_history(0x5730).hex().upper() == "D15505000000005730"
    now = datetime(2026, 1, 1, 12, 10, 30, tzinfo=UTC)
    ref = p.history_reference_time(now)
    assert ref == datetime(2026, 1, 1, 12, 9, tzinfo=UTC)
    assert p.history_count_since(None, now, False) == p.HISTORY_MAX_RECORDS
    assert p.history_count_since(None, now, True) == p.HISTORY_MAX_RECORDS_EXTENDED
    assert p.history_count_since(ref - timedelta(hours=1), now, False) == 40
    assert p.history_count_since(ref - timedelta(days=90), now, False) == p.HISTORY_MAX_RECORDS


def test_parse_history() -> None:
    ref = datetime(2026, 1, 1, 12, 9, tzinfo=UTC)
    recs = (
        "00000000"  # leading padding
        + "4E2" + "50" + "14" + "0"  # 12.50 V, 80 %, 20 C
        + "514" + "52" + "15" + "4"  # 13.00 V, engine stop
        + "00050002"  # trip counters for the stop record
        + "4B0" + "4F" + "05" + "A"  # 12.00 V, -5 C, engine start
        + "000" + "00" + "00" + "0"  # zero voltage -> skipped
        + p.HISTORY_END + "000000"
    )
    out = p.parse_history(recs, ref)
    assert [r.voltage for r in out] == [12.5, 13.0, 12.0]
    assert out[0].time == ref
    assert out[1].time == ref - timedelta(minutes=2)
    assert out[1].event == "engine_stop"
    assert (out[1].rapid_acceleration, out[1].rapid_deceleration) == (5, 2)
    assert out[2].time == ref - timedelta(minutes=4)
    assert out[2].event == "engine_start"
    assert out[2].temperature == -5


def test_parse_crank() -> None:
    h = "D15503" + "01" + "03E8" + "00C8" + "0002" + "FFFFFE" + "4B0" + "3E8" + "000" + "FFFEFE"
    c = p.parse_crank(h)
    assert c.status == p.CrankStatus.OK
    assert c.voltage == 10.0
    assert c.duration_ms == 200
    assert c.age_records == 2
    assert c.waveform == [12.0, 10.0]
    assert not p.crank_has_data("D15503FF")


def test_parse_charge_test() -> None:
    c = p.parse_charge_test("D1550403" + "01" + "0578" + "0564" + "0032")
    assert c.status == p.ChargeStatus.OK
    assert c.no_load_voltage == 14.0
    assert c.load_voltage == 13.8
    assert c.ripple_mv == 50


def test_parse_advert() -> None:
    mac = "A4:C1:38:01:02:03"
    plain = bytes.fromhex("A4C138010203" + "0000" + "4B" + "00" + "00" + "03C0" + "02" + "0000")
    enc = p.encrypt(plain)
    company = int.from_bytes(enc[:2], "little")
    adv = p.parse_advert({company: enc[2:]}, mac)
    assert adv is not None
    assert adv.soc == 75
    assert adv.status == p.Status.NORMAL
    assert adv.crank_voltage == 9.6
    assert adv.crank_status == p.CrankStatus.LOW
    assert p.parse_advert({company: enc[2:]}, "00:11:22:33:44:55") is None

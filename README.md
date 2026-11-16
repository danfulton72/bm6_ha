# BM6 Battery Monitor for Home Assistant

A local Bluetooth integration for the **BM6** 12 V car battery monitor (also sold as
"Battery Guard"). It reimplements what the official BM6 Android app (v2.6.3) does,
without the app or its cloud account. The protocol was taken from that app.

## What it does

| BM6 app feature | In Home Assistant |
|---|---|
| Live voltage, charge %, temperature, status | `Voltage`, `Battery`, `Temperature`, `Status` sensors (`Battery OK` / `Low charge` / `Charging`) |
| Live mode | Option **Stay connected**: keeps the link open so the BM6 streams readings |
| Cranking test (voltage, duration, waveform) | `Cranking voltage` (10 ms waveform in attributes), `Cranking duration`, `Cranking test`, `Last engine start`, plus a `bm6_ha_crank_detected` event |
| 3-step charging system test | Three buttons (or the `bm6_ha.charging_test_step` action) → `Charging test`, `Idle charging voltage`, `High-speed charging voltage`, `Diode ripple voltage` |
| Voltage history (logged every 2 min, 31 or 72 days on the device) | Downloaded automatically and imported as hourly long-term statistics (`bm6_ha:<mac>_voltage`, `_soc`, `_temperature`). This fills the gaps while the car was out of range |
| Trips (engine start/stop, harsh acceleration/braking) | `bm6_ha_engine_started` / `bm6_ha_engine_stopped` events; trips are returned by `bm6_ha.sync_history`; acceleration/deceleration counters (disabled by default) |
| Battery type (regular, AGM, EFB, GEL, lithium, custom SoC curve) | `Battery type` select, which is written to the device. Custom types use the SoC curve set in the options |
| Warnings / notifications | `Low charge`, `Voltage warning`, `Temperature warning`, `Cranking warning`, `Charging system warning` binary sensors. They use the app's thresholds (12.3 V lead-acid / 13.13 V lithium, 60 °C, crank 9.6 / 8.0 V) |
| Background advert decoding | SoC and status are decoded from the encrypted BLE adverts when polling data is stale |

Not reproduced: the app's cloud features (account, uploads, GPS trip maps, fuel cost, firmware updates).

## Install

<!-- ha-marketplace:exclude-start -->
1. On Home Assistant **2026.11+**, go to **Settings → Marketplace → Add from link**
   and add `https://github.com/danfulton72/bm6_ha`. Alternatively, copy
   `custom_components/bm6_ha` to `config/custom_components/bm6_ha`.
2. Restart Home Assistant and accept Bluetooth discovery, or add **BM6 Battery
   Monitor** under **Settings → Devices & services → Add integration**.
<!-- ha-marketplace:exclude-end -->

It needs a Bluetooth adapter on the HA host or an ESPHome Bluetooth proxy within range of the car.
Home Assistant 2026.11 or newer is required.

> Close the BM6 phone app while testing. The BM6 accepts only one connection at a time.

## Options

- **Polling interval** (default 120 s): how often HA connects, reads, and disconnects.
- **Stay connected**: live streaming, at the cost of permanently using one Bluetooth connection slot.
- **Download stored history automatically** / **interval** (default on, 60 min): the first download
  pulls everything stored on the device (up to ~90 KB, which can take a minute or two over BLE).
  Later downloads only fetch what is new.
- **Custom SoC curve**: 11 comma-separated voltages for 100 %, 90 % … 0 %. These are only used by
  the custom battery types.

The integration never writes a battery type until you choose one in the `Battery type` select,
so a setting you made in the app is left alone.

## Charging system test

Follow the same steps as the app:

1. Engine idling, all loads on (headlights, A/C, rear-window heater) → press **Charging test step 1**.
2. Loads off, hold the engine at 2500–3000 RPM → press **step 2**.
3. Back to idle, loads off → press **step 3**. The results appear in the sensors and a
   `bm6_ha_charging_test_completed` event is fired.

The connection is held open for 5 minutes between steps.

## Example automation

```yaml
automation:
  - alias: Car battery low
    triggers:
      - trigger: numeric_state
        entity_id: sensor.bm6_0203_battery
        below: 60
        for: "00:10:00"
    actions:
      - action: notify.mobile_app_phone
        data:
          message: "Car battery at {{ states('sensor.bm6_0203_battery') }}% ({{ states('sensor.bm6_0203_voltage') }} V)"
```

To chart the device history, use a **Statistics graph** card with the statistic
`bm6_ha:<mac without colons>_voltage`. The exact IDs are listed in the attributes of the
`History imported until` sensor.

## Protocol notes

- Service `FFF0`. Write (without response) to `FFF3`, notify on `FFF4`.
- AES-128, key `leagend\xff\xfe0100009`, zero IV, applied per 16-byte block, in both directions.
- Commands are `D1 55 <op> …`: `01` version, `02` ack, `03` crank, `04 0n` charge test,
  `05 <offset:3> <count:3>` history, `06` stop, `07` live data, `08 <type> [curve]` battery type.
- History records are 4 bytes: 12-bit centivolts, SoC, temperature, and flags. Bit 3 of the flags
  is the sign; `010` marks an engine start and `100` an engine stop, which is followed by a 4-byte
  record of trip counters. Records run newest first, 2 minutes apart, aligned to odd minutes.

See `custom_components/bm6_ha/protocol.py` for the full implementation.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
```

The tests run the real client, coordinator and entities against a simulated BM6 that speaks the
encrypted protocol.

## Compatibility and migration

The integration domain is `bm6_ha` to match this repository. If you previously
installed a `custom_components/bm6` integration, remove the old integration
entry and folder before installing this one; existing entity IDs, automations,
and custom history statistic IDs beginning with `bm6:` must be adjusted.

This integration communicates directly with a battery monitoring device. It has
not been tested here with physical BM6 hardware; test charging controls on the
actual device before relying on their results.

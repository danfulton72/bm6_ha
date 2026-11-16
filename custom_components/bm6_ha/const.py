"""Constants for the BM6 battery monitor integration."""

from __future__ import annotations

from .protocol import BatteryType

DOMAIN = "bm6_ha"

CONF_POLL_INTERVAL = "poll_interval"
CONF_KEEP_CONNECTED = "keep_connected"
CONF_AUTO_HISTORY = "auto_history"
CONF_HISTORY_INTERVAL = "history_interval"
CONF_BATTERY_TYPE = "battery_type"
CONF_SOC_CURVE = "soc_curve"

DEFAULT_POLL_INTERVAL = 120  # seconds
MIN_POLL_INTERVAL = 30
DEFAULT_HISTORY_INTERVAL = 60  # minutes
DEFAULT_AUTO_HISTORY = True
DEFAULT_KEEP_CONNECTED = False

# How often streamed real-time updates are pushed to Home Assistant.
STREAM_UPDATE_THROTTLE = 5.0

# Battery types shown to the user, mapped to the code the device understands.
# EFB and GEL share the regular lead-acid code, exactly as in the app.
BATTERY_TYPE_OPTIONS: dict[str, BatteryType] = {
    "lead_acid": BatteryType.LEAD_ACID,
    "agm": BatteryType.AGM,
    "efb": BatteryType.LEAD_ACID,
    "gel": BatteryType.LEAD_ACID,
    "lead_acid_custom_smart": BatteryType.LEAD_ACID_CUSTOM_SMART,
    "lead_acid_custom_voltage": BatteryType.LEAD_ACID_CUSTOM_VOLTAGE,
    "lithium": BatteryType.LITHIUM,
    "lithium_custom_smart": BatteryType.LITHIUM_CUSTOM_SMART,
    "lithium_custom_voltage": BatteryType.LITHIUM_CUSTOM_VOLTAGE,
}

EVENT_ENGINE_STARTED = f"{DOMAIN}_engine_started"
EVENT_ENGINE_STOPPED = f"{DOMAIN}_engine_stopped"
EVENT_CRANK = f"{DOMAIN}_crank_detected"
EVENT_CHARGE_TEST = f"{DOMAIN}_charging_test_completed"

SERVICE_SYNC_HISTORY = "sync_history"
SERVICE_CHARGING_TEST = "charging_test_step"
ATTR_FULL = "full"
ATTR_STEP = "step"

STORAGE_VERSION = 1

"""Import the BM6's on-device history into Home Assistant long-term statistics.

The BM6 logs voltage, state of charge and temperature every 2 minutes, even
when nothing is connected (31 days, or 72 days on newer firmware).  The app
downloads this log to draw its history charts.  Here it is rolled up into
hourly mean/min/max values and stored as external statistics, so the gaps
when the car was out of Bluetooth range are filled in.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import logging

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import PERCENTAGE, UnitOfElectricPotential, UnitOfTemperature
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .protocol import HistoryRecord

try:  # HA 2025.6+
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # pragma: no cover - older cores
    StatisticMeanType = None  # type: ignore[assignment,misc]

_LOGGER = logging.getLogger(__name__)

# key -> (attribute on HistoryRecord, unit, unit class, label)
SERIES = {
    "voltage": ("voltage", UnitOfElectricPotential.VOLT, "voltage", "voltage"),
    "soc": ("soc", PERCENTAGE, None, "state of charge"),
    "temperature": ("temperature", UnitOfTemperature.CELSIUS, "temperature", "temperature"),
}


def statistic_id(address: str, key: str) -> str:
    return f"{DOMAIN}:{address.replace(':', '').lower()}_{key}"


def _metadata(address: str, name: str, key: str) -> StatisticMetaData:
    _attr, unit, unit_class, label = SERIES[key]
    meta: dict = {
        "has_sum": False,
        "name": f"{name} {label} (device history)",
        "source": DOMAIN,
        "statistic_id": statistic_id(address, key),
        "unit_of_measurement": unit,
    }
    if StatisticMeanType is not None:
        meta["mean_type"] = StatisticMeanType.ARITHMETIC
        meta["unit_class"] = unit_class
    else:  # pragma: no cover
        meta["has_mean"] = True
    return meta  # type: ignore[return-value]


def hourly_buckets(
    records: list[HistoryRecord], reference: datetime, since: datetime | None
) -> dict[datetime, list[HistoryRecord]]:
    """Group records into complete hours between ``since`` and ``reference``."""
    current_hour = reference.replace(minute=0, second=0, microsecond=0)
    buckets: dict[datetime, list[HistoryRecord]] = defaultdict(list)
    for rec in records:
        hour = rec.time.replace(minute=0, second=0, microsecond=0)
        if hour >= current_hour:
            continue  # not finished yet
        if since is not None and hour < since:
            continue  # already imported
        buckets[hour].append(rec)
    if records and since is None and buckets:
        # The oldest hour of a full download is usually cut off part way.
        oldest = min(buckets)
        if len(buckets[oldest]) < 25:
            del buckets[oldest]
    return dict(sorted(buckets.items()))


async def async_import_history(
    hass: HomeAssistant,
    address: str,
    name: str,
    records: list[HistoryRecord],
    reference: datetime,
    since: datetime | None,
) -> datetime | None:
    """Import complete hours; return the end of the last imported hour."""
    buckets = hourly_buckets(records, reference, since)
    if not buckets:
        return None
    if "recorder" not in hass.config.components:
        _LOGGER.debug("Recorder not loaded; skipping history import")
        return None
    for key, (attr, *_rest) in SERIES.items():
        stats: list[StatisticData] = []
        for hour, recs in buckets.items():
            values = [getattr(r, attr) for r in recs]
            stats.append(
                StatisticData(
                    start=hour,
                    mean=round(sum(values) / len(values), 3),
                    min=min(values),
                    max=max(values),
                )
            )
        async_add_external_statistics(hass, _metadata(address, name, key), stats)
    _LOGGER.debug(
        "%s: imported %d hours of device history (%d records)",
        name,
        len(buckets),
        len(records),
    )
    return max(buckets) + timedelta(hours=1)

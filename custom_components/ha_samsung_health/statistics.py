"""External long-term statistics (statistic_id "ha_samsung_health:<key>").

Why external statistics and no state_class on the entities: see ha-workouts'
statistics_import.py. An entity-attached statistic is auto-compiled by the
recorder from the entity's own state history, which competes with imported
history; external statistics are never touched by that compile.

Every poll already holds the FULL history in memory (the cloud ignores time
filters), so every series is computed deterministically from scratch. Cumulative
`sum` values therefore need no database reads.

Full history is written ONCE. A fingerprint (hash) per statistic and calendar
month is persisted in .storage; on every poll, including the first one after a
Home Assistant restart, only rows from the first month whose fingerprint changed
are written (in practice the current month). Rows are upserts on (statistic_id,
start), so a rewrite never duplicates anything. If the recorder has no rows for a
statistic although fingerprints exist (statistics deleted, database replaced),
that statistic is rewritten in full.

Daily rows sit at 12:00 UTC of their day (as in ha-workouts) so the recorder's
local-day grouping never moves them across a day boundary. Body measurements
sit at the start of the UTC hour they were taken in.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import parsers
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StatSpec:
    key: str
    name: str
    unit: str | None
    cumulative: bool  # True: daily total with running sum; False: mean/min/max


SPECS: dict[str, StatSpec] = {
    s.key: s
    for s in (
        StatSpec("steps", "Samsung Health steps", "steps", True),
        StatSpec("active_calories", "Samsung Health active calories", "kcal", True),
        StatSpec("total_calories", "Samsung Health total calories (estimated)", "kcal", True),
        StatSpec("exercise_calories", "Samsung Health exercise calories", "kcal", True),
        StatSpec("weight", "Samsung Health weight", "kg", False),
        StatSpec("body_fat", "Samsung Health body fat", "%", False),
        StatSpec("body_fat_mass", "Samsung Health body fat mass", "kg", False),
        StatSpec("skeletal_muscle_mass", "Samsung Health skeletal muscle mass", "kg", False),
        StatSpec("body_water", "Samsung Health body water", "kg", False),
        StatSpec("bmr", "Samsung Health BMR", "kcal", False),
        StatSpec("sleep_duration", "Samsung Health sleep duration", "h", False),
        StatSpec("sleep_score", "Samsung Health sleep score", None, False),
        StatSpec("sleeping_heart_rate", "Samsung Health sleeping heart rate", "bpm", False),
        StatSpec("sleeping_hrv", "Samsung Health sleeping HRV", "ms", False),
        StatSpec("energy_score", "Samsung Health energy score", None, False),
    )
}

#: key -> [(row start, value)], sorted by start
Series = dict[str, list[tuple[datetime, float]]]


def statistic_id(key: str) -> str:
    return f"{DOMAIN}:{key}"


def day_bucket(day: date) -> datetime:
    return datetime.combine(day, time(12), tzinfo=UTC)


def hour_bucket(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def build_series(data, today: date, today_fraction: float) -> Series:
    """All values per statistic. `data` is a coordinator SamsungHealthData."""
    series: Series = {key: [] for key in SPECS}

    def add(key: str, start: datetime, value: float | None) -> None:
        if value is not None:
            series[key].append((start, float(value)))

    for day in sorted(data.days):
        a = data.days[day]
        bucket = day_bucket(day)
        add("steps", bucket, a.steps)
        add("active_calories", bucket, a.active_kcal)
        add("exercise_calories", bucket, a.exercise_kcal)
        fraction = today_fraction if day == today else 1.0
        add("total_calories", bucket,
            parsers.estimate_total_kcal(a.active_kcal, parsers.bmr_for_day(data.body, day), fraction))

    body_fields: dict[str, Callable[[parsers.BodyMeasurement], float | None]] = {
        "weight": lambda m: m.weight,
        "body_fat": lambda m: m.body_fat,
        "body_fat_mass": lambda m: m.body_fat_mass,
        "skeletal_muscle_mass": lambda m: m.skeletal_muscle_mass,
        "body_water": lambda m: m.total_body_water,
        "bmr": lambda m: m.bmr,
    }
    for m in data.body:
        for key, get in body_fields.items():
            add(key, hour_bucket(m.measured_at), get(m))

    for s in data.sleep:
        add("sleep_duration", day_bucket(s.day), s.duration_min / 60 if s.duration_min is not None else None)
        add("sleep_score", day_bucket(s.day), s.score)

    for day in sorted(data.recovery):
        r = data.recovery[day]
        add("sleeping_heart_rate", day_bucket(day), r.sleeping_hr)
        add("sleeping_hrv", day_bucket(day), r.sleeping_hrv)
        add("energy_score", day_bucket(day), r.energy_score)

    # Several values in one bucket (two weighings in an hour, two sleeps ending the
    # same day): keep the last one.
    return {key: sorted(dict(rows).items()) for key, rows in series.items()}


def to_rows(spec: StatSpec, values: list[tuple[datetime, float]]) -> list[StatisticData]:
    rows = []
    running = 0.0
    for start, value in values:
        if spec.cumulative:
            running += value
            rows.append(StatisticData(start=start, state=value, sum=running))
        else:
            rows.append(StatisticData(start=start, mean=value, min=value, max=value))
    return rows


def _month(row: StatisticData) -> str:
    return row["start"].strftime("%Y-%m")


def month_fingerprints(rows: list[StatisticData]) -> dict[str, str]:
    """Calendar month (UTC) -> hash of that month's rows."""
    months: dict[str, list] = {}
    for row in rows:
        months.setdefault(_month(row), []).append(
            [row["start"].timestamp(), row.get("state"), row.get("sum"), row.get("mean")]
        )
    return {
        month: hashlib.sha256(json.dumps(values).encode()).hexdigest()[:16]
        for month, values in months.items()
    }


def first_changed_month(old: dict[str, str] | None, new: dict[str, str]) -> str | None:
    """First month whose rows differ from what was written, or None if nothing changed.

    A change in one month also changes every later month of a cumulative series
    (its sums move), so writing from this month onwards is always enough.
    """
    for month in sorted(new):
        if old is None or old.get(month) != new[month]:
            return month
    return None


STORAGE_VERSION = 1


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.statistics"


def metadata(spec: StatSpec) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE if spec.cumulative else StatisticMeanType.ARITHMETIC,
        has_sum=spec.cumulative,
        name=spec.name,
        source=DOMAIN,
        statistic_id=statistic_id(spec.key),
        unit_class=None,
        unit_of_measurement=spec.unit,
    )


class StatisticsWriter:
    """Writes only months that changed since the last write, across restarts."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, store_key(entry_id))
        self._fingerprints: dict[str, dict[str, str]] | None = None

    def _keys_missing_in_recorder(self, keys: list[str]) -> list[str]:
        missing = []
        for key in keys:
            spec = SPECS.get(key)
            types = {"sum"} if spec and spec.cumulative else {"mean"}
            if not get_last_statistics(self.hass, 1, statistic_id(key), False, types):
                missing.append(key)
        return missing

    async def _async_load(self) -> dict[str, dict[str, str]]:
        stored = await self._store.async_load() or {}
        fingerprints: dict[str, dict[str, str]] = {
            k: v for k, v in stored.get("fingerprints", {}).items() if k in SPECS
        }
        if fingerprints:
            missing = await get_instance(self.hass).async_add_executor_job(
                self._keys_missing_in_recorder, list(fingerprints)
            )
            for key in missing:
                _LOGGER.info("Statistics for %s missing in the recorder; rewriting in full", key)
                fingerprints.pop(key)
        return fingerprints

    async def async_write(self, series: Series) -> int:
        if self._fingerprints is None:
            self._fingerprints = await self._async_load()
        total = 0
        for key, values in series.items():
            spec = SPECS[key]
            rows = to_rows(spec, values)
            fingerprints = month_fingerprints(rows)
            month = first_changed_month(self._fingerprints.get(key), fingerprints)
            if month is None:
                continue
            changed = [row for row in rows if _month(row) >= month]
            async_add_external_statistics(self.hass, metadata(spec), changed)
            self._fingerprints[key] = fingerprints
            total += len(changed)
        if total:
            # Only remember what was written once the recorder has committed it.
            await get_instance(self.hass).async_block_till_done()
            await self._store.async_save({"fingerprints": self._fingerprints})
            _LOGGER.debug("Wrote %d statistic rows", total)
        return total


async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
    await Store(hass, STORAGE_VERSION, store_key(entry_id)).async_remove()

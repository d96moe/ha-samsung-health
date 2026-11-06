"""External long-term statistics (statistic_id "ha_samsung_health:<key>").

Why external statistics and no state_class on the entities: see ha-workouts'
statistics_import.py. An entity-attached statistic is auto-compiled by the
recorder from the entity's own state history, which competes with imported
history; external statistics are never touched by that compile.

Every poll already holds the FULL history in memory (the cloud ignores time
filters), so every series is computed deterministically from scratch. Cumulative
`sum` values therefore need no database reads. After the first write following
startup, only rows from the earliest changed row onwards are written again.

Daily rows sit at 12:00 UTC of their day (as in ha-workouts) so the recorder's
local-day grouping never moves them across a day boundary. Body measurements
sit at the start of the UTC hour they were taken in.
"""
from __future__ import annotations

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
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant

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


def first_changed(old: list[StatisticData] | None, new: list[StatisticData]) -> int | None:
    """Index of the first row that differs, or None if nothing changed."""
    if old is None:
        return 0
    for i, row in enumerate(new):
        if i >= len(old) or old[i] != row:
            return i
    return None if len(old) == len(new) else len(new)


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
    """Remembers what it wrote since startup; writes only what changed."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._written: dict[str, list[StatisticData]] = {}

    async def async_write(self, series: Series) -> int:
        total = 0
        for key, values in series.items():
            spec = SPECS[key]
            rows = to_rows(spec, values)
            index = first_changed(self._written.get(key), rows)
            if index is None or index >= len(rows):
                continue
            async_add_external_statistics(self.hass, metadata(spec), rows[index:])
            self._written[key] = rows
            total += len(rows) - index
        if total:
            await get_instance(self.hass).async_block_till_done()
            _LOGGER.debug("Wrote %d statistic rows", total)
        return total

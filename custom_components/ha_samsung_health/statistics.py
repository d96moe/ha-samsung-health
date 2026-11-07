"""Long-term statistics with the full Samsung Health history.

Two kinds, because they behave differently:

- Measurements (weight, body composition, sleep, recovery) are imported onto the
  sensor entity's own statistic (statistic_id = the entity id, source "recorder";
  the entities have state_class measurement). So the full history shows up where
  you look for it: the entity's History and more-info graphs. The recorder's own
  hourly compile and the import don't conflict: both write the measured value,
  upserted per hour, and means have no running sum to keep consistent.
- Daily totals (steps, calories) stay external statistics
  ("ha_samsung_health:<key>"), and those entities have no state_class. Their
  value resets at midnight, so the recorder would keep its own running sum on the
  entity, and importing history whenever the phone syncs a past day late would
  break that sum (see ha-workouts' statistics_import.py).

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

from homeassistant.components.recorder import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    STATISTIC_UNIT_TO_UNIT_CONVERTER,
    async_add_external_statistics,
    async_import_statistics,
    get_last_statistics,
)
from homeassistant.components.sensor.const import UNIT_CONVERTERS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from . import parsers
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StatSpec:
    key: str  # also the sensor's key (unique_id suffix)
    name: str
    unit: str | None  # the sensor's native unit
    #: True: daily total with running sum, external statistic.
    #: False: measurement (mean/min/max) on the sensor entity's own statistic.
    cumulative: bool
    device_class: str | None = None


SPECS: dict[str, StatSpec] = {
    s.key: s
    for s in (
        StatSpec("steps", "Samsung Health steps", "steps", True),
        StatSpec("active_calories", "Samsung Health active calories", "kcal", True),
        StatSpec("total_calories", "Samsung Health total calories (estimated)", "kcal", True),
        StatSpec("exercise_calories", "Samsung Health exercise calories", "kcal", True),
        StatSpec("weight", "Samsung Health weight", "kg", False, "weight"),
        StatSpec("body_fat", "Samsung Health body fat", "%", False),
        StatSpec("body_fat_mass", "Samsung Health body fat mass", "kg", False, "weight"),
        StatSpec("skeletal_muscle_mass", "Samsung Health skeletal muscle mass", "kg", False, "weight"),
        StatSpec("body_water", "Samsung Health body water", "kg", False, "weight"),
        StatSpec("bmr", "Samsung Health BMR", "kcal", False),
        StatSpec("sleep_duration", "Samsung Health sleep duration", "h", False, "duration"),
        StatSpec("sleep_score", "Samsung Health sleep score", None, False),
        StatSpec("sleeping_heart_rate", "Samsung Health sleeping heart rate", "bpm", False),
        StatSpec("sleeping_hrv", "Samsung Health sleeping HRV", "ms", False),
        StatSpec("energy_score", "Samsung Health energy score", None, False),
    )
}

#: Keys whose history lives on the sensor entity (state_class measurement).
ENTITY_KEYS = frozenset(k for k, s in SPECS.items() if not s.cumulative)

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
#: 1: everything external (v0.1.x). 2: measurements on the sensor entities.
LAYOUT = 2


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.statistics"


def unit_class(spec: StatSpec) -> str | None:
    """Same rule as the sensor recorder's own compile, so the two never disagree."""
    if (
        spec.device_class
        and (conv := UNIT_CONVERTERS.get(spec.device_class))
        and spec.unit in conv.VALID_UNITS
    ):
        return conv.UNIT_CLASS
    if conv := STATISTIC_UNIT_TO_UNIT_CONVERTER.get(spec.unit):
        return conv.UNIT_CLASS
    return None


def metadata(spec: StatSpec, entity_id: str | None = None) -> StatisticMetaData:
    """External metadata for daily totals; the entity's own for measurements."""
    if spec.cumulative:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=spec.name,
            source=DOMAIN,
            statistic_id=statistic_id(spec.key),
            unit_class=None,
            unit_of_measurement=spec.unit,
        )
    assert entity_id is not None
    return StatisticMetaData(
        mean_type=StatisticMeanType.ARITHMETIC,
        has_sum=False,
        name=None,
        source=RECORDER_DOMAIN,
        statistic_id=entity_id,
        unit_class=unit_class(spec),
        unit_of_measurement=spec.unit,
    )


class StatisticsWriter:
    """Writes only months that changed since the last write, across restarts."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, store_key(entry_id))
        self._fingerprints: dict[str, dict[str, str]] | None = None

    def _entity_id(self, key: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id("sensor", DOMAIN, f"{self.entry_id}_{key}")

    def _target(self, key: str) -> str | None:
        """The statistic_id a key is written to; None if its entity doesn't exist."""
        return self._entity_id(key) if key in ENTITY_KEYS else statistic_id(key)

    def _keys_missing_in_recorder(self, targets: dict[str, str]) -> list[str]:
        missing = []
        for key, target in targets.items():
            types = {"mean"} if key in ENTITY_KEYS else {"sum"}
            if not get_last_statistics(self.hass, 1, target, False, types):
                missing.append(key)
        return missing

    async def _async_load(self) -> dict[str, dict[str, str]]:
        stored = await self._store.async_load() or {}
        fingerprints: dict[str, dict[str, str]] = {
            k: v for k, v in stored.get("fingerprints", {}).items() if k in SPECS
        }
        if stored and stored.get("layout", 1) < LAYOUT:
            # v0.1.x wrote measurements as external statistics. They now live on the
            # sensor entities: drop the old copies and write the history there.
            _LOGGER.info("Moving Samsung Health measurement history onto the sensor entities")
            get_instance(self.hass).async_clear_statistics([statistic_id(k) for k in sorted(ENTITY_KEYS)])
            for key in ENTITY_KEYS:
                fingerprints.pop(key, None)
        targets = {k: t for k in fingerprints if (t := self._target(k))}
        if targets:
            missing = await get_instance(self.hass).async_add_executor_job(
                self._keys_missing_in_recorder, targets
            )
            for key in missing:
                _LOGGER.info("Statistics for %s missing in the recorder; rewriting in full", key)
                fingerprints.pop(key)
        return fingerprints

    def _unit_matches(self, spec: StatSpec, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        unit = state.attributes.get("unit_of_measurement") if state else spec.unit
        if unit != spec.unit:
            # The user picked another display unit; importing in the native unit would
            # make the recorder flag a unit change. Leave that entity's history alone.
            _LOGGER.warning(
                "Not importing history into %s: its unit is %s, history is in %s", entity_id, unit, spec.unit
            )
            return False
        return True

    async def async_write(self, series: Series) -> int:
        # Save after the first load even if nothing is written, to record the layout.
        first_load = self._fingerprints is None
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
            if spec.cumulative:
                async_add_external_statistics(self.hass, metadata(spec), changed)
            else:
                if (entity_id := self._entity_id(key)) is None or not self._unit_matches(spec, entity_id):
                    continue
                async_import_statistics(self.hass, metadata(spec, entity_id), changed)
            self._fingerprints[key] = fingerprints
            total += len(changed)
        if total or first_load:
            # Only remember what was written once the recorder has committed it.
            await get_instance(self.hass).async_block_till_done()
            await self._store.async_save({"layout": LAYOUT, "fingerprints": self._fingerprints})
            _LOGGER.debug("Wrote %d statistic rows", total)
        return total


async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
    await Store(hass, STORAGE_VERSION, store_key(entry_id)).async_remove()

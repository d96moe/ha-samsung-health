"""Long-term statistics with the full Samsung Health history.

Two kinds, because they behave differently:

- Measurements (weight, body composition, sleep, recovery) are imported onto the
  sensor entity's own statistic (statistic_id = the entity id, source "recorder";
  the entities have state_class measurement). So the full history shows up where
  you look for it: the entity's History and more-info graphs. The import upserts,
  but the recorder's own hourly compile only inserts: a row imported into an hour
  it hasn't compiled yet makes that compile fail and roll back the hour for every
  entity. So rows are only imported for hours already compiled (COMPILE_MARGIN);
  newer ones wait for a later poll.
- Daily totals (steps, calories, ...) are not written here any more: they live on
  the "today" sensors (state_class total), see daily_history.py. Their old
  external statistics ("ha_samsung_health:<key>") are legacy (repairs.py).

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
from datetime import UTC, date, datetime, time, timedelta

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
from homeassistant.util import dt as dt_util

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
        StatSpec("time_in_bed", "Samsung Health time in bed", "h", False, "duration"),
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
    """Row start for a daily value: 12:00 UTC, which is on the same local day from
    UTC-11 to UTC+11 (so existing rows never move). Further east or west it would
    land on another local day; there, local noon (on the hour) is used instead."""
    noon_utc = datetime.combine(day, time(12), tzinfo=UTC)
    if dt_util.as_local(noon_utc).date() == day:
        return noon_utc
    local_noon = datetime.combine(day, time(12), tzinfo=dt_util.get_default_time_zone())
    return hour_bucket(local_noon)


def hour_bucket(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def build_series(data, today: date, today_fraction: float) -> Series:
    """All values per statistic. `data` is a coordinator SamsungHealthData."""
    series: Series = {key: [] for key in SPECS}

    def add(key: str, start: datetime, value: float | None) -> None:
        if value is not None:
            series[key].append((start, float(value)))

    bmr = parsers.bmr_by_day(data.body, data.days)  # one pass, not one scan per day
    for day in sorted(data.days):
        a = data.days[day]
        bucket = day_bucket(day)
        add("steps", bucket, a.steps)
        add("active_calories", bucket, a.active_kcal)
        add("exercise_calories", bucket, a.exercise_kcal)
        fraction = today_fraction if day == today else 1.0
        add("total_calories", bucket,
            parsers.estimate_total_kcal(a.active_kcal, bmr[day], fraction))

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
        add("time_in_bed", day_bucket(s.day), s.time_in_bed_min / 60 if s.time_in_bed_min is not None else None)
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


def month_buckets(rows: list[StatisticData]) -> dict[str, list[int]]:
    """Calendar month (UTC) -> hours since that month's start of its rows (compact)."""
    out: dict[str, list[int]] = {}
    for row in rows:
        start = row["start"]
        month_start = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        out.setdefault(_month(row), []).append(int((start - month_start).total_seconds() // 3600))
    return out


def first_changed_month(old: dict[str, str] | None, new: dict[str, str]) -> str | None:
    """First month whose rows differ from what was written, or None if nothing changed.

    A change in one month also changes every later month of a cumulative series
    (its sums move), so writing from this month onwards is always enough.
    """
    for month in sorted(new):
        if old is None or old.get(month) != new[month]:
            return month
    return None


#: The recorder compiles an hour a few minutes after it ends; leave it this long.
COMPILE_MARGIN = timedelta(minutes=15)


def compiled_hours_end(now: datetime) -> datetime:
    """Start of the first hour the recorder may not have compiled yet.

    Hour H is compiled once now >= H + 1 h + margin, i.e. H < floor(now - margin).
    """
    return hour_bucket(now - COMPILE_MARGIN)


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


def metadata(spec: StatSpec, entity_id: str | None = None, unit: str | None = None) -> StatisticMetaData:
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
        unit_of_measurement=unit or spec.unit,
    )


class StatisticsWriter:
    """Writes only months that changed since the last write, across restarts."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, store_key(entry_id))
        self._fingerprints: dict[str, dict[str, str]] | None = None
        #: key -> month -> hours since the month's start of the rows written there
        self._buckets: dict[str, dict[str, list[int]]] = {}
        self._unit_warned: set[str] = set()

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
        self._buckets = {k: v for k, v in stored.get("buckets", {}).items() if k in fingerprints}
        if stored and stored.get("layout", 1) < LAYOUT:
            # v0.1.x wrote measurements as external statistics. They now live on the
            # sensor entities: drop the old copies and write the history there.
            _LOGGER.info("Moving Samsung Health measurement history onto the sensor entities")
            get_instance(self.hass).async_clear_statistics([statistic_id(k) for k in sorted(ENTITY_KEYS)])
            for key in ENTITY_KEYS:
                fingerprints.pop(key, None)
                self._buckets.pop(key, None)
        return fingerprints

    async def _async_forget_keys_missing_in_recorder(self) -> None:
        """Statistics deleted in the recorder (or a replaced database): write them in full."""
        assert self._fingerprints is not None
        targets = {k: t for k, f in self._fingerprints.items() if f and (t := self._target(k))}
        if not targets:
            return
        missing = await get_instance(self.hass).async_add_executor_job(self._keys_missing_in_recorder, targets)
        for key in missing:
            _LOGGER.info("Statistics for %s missing in the recorder; rewriting in full", key)
            self._fingerprints.pop(key)
            self._buckets.pop(key, None)

    def _entity_unit(self, spec: StatSpec, entity_id: str) -> tuple[str | None, Callable[[float], float] | None]:
        """The unit the entity's statistics are in, and how to convert to it (None: skip)."""
        state = self.hass.states.get(entity_id)
        unit = state.attributes.get("unit_of_measurement") if state else spec.unit
        if unit == spec.unit:
            return unit, lambda value: value
        converter = (UNIT_CONVERTERS.get(spec.device_class) if spec.device_class else None) \
            or STATISTIC_UNIT_TO_UNIT_CONVERTER.get(spec.unit)
        if converter and spec.unit in converter.VALID_UNITS and unit in converter.VALID_UNITS:
            # The user picked another display unit (e.g. lb): import in that unit.
            return unit, converter.converter_factory(spec.unit, unit)
        if entity_id not in self._unit_warned:
            self._unit_warned.add(entity_id)
            _LOGGER.warning(
                "Not importing history into %s: its unit %s can't be converted from %s", entity_id, unit, spec.unit
            )
        return None, None

    async def async_write(self, series: Series) -> int:
        # Save after the first load even if nothing is written, to record the layout.
        first_load = self._fingerprints is None
        if self._fingerprints is None:
            self._fingerprints = await self._async_load()
        await self._async_forget_keys_missing_in_recorder()
        compiled_before = compiled_hours_end(dt_util.utcnow())
        total, save = 0, first_load
        for key, values in series.items():
            spec = SPECS[key]
            if spec.cumulative:
                # Daily totals live on the "today" sensors now (daily_history.py); the
                # old ha_samsung_health:<key> series are legacy (see repairs.py).
                continue
            entity_id = unit = None
            if not spec.cumulative:
                if (entity_id := self._entity_id(key)) is None:
                    continue
                unit, convert = self._entity_unit(spec, entity_id)
                if convert is None:
                    continue
                # Rows on the sensor entity's own statistic must not reach an hour the
                # recorder hasn't compiled yet: its hourly compile inserts (no upsert),
                # so it would hit our row, fail and roll back that hour for EVERY entity.
                # Held-back rows aren't fingerprinted, so a later poll writes them.
                values = [(start, convert(value)) for start, value in values if start < compiled_before]
            rows = to_rows(spec, values)
            fingerprints = month_fingerprints(rows)
            buckets = month_buckets(rows)
            old_buckets = self._buckets.get(key)
            if old_buckets is None and key in self._fingerprints:
                # Stored before buckets were kept (v0.4.1 and older): start keeping them.
                self._buckets[key] = buckets
                save = True
            month = first_changed_month(self._fingerprints.get(key), fingerprints)
            removed = old_buckets is not None and any(
                set(hours) - set(buckets.get(m, ())) for m, hours in old_buckets.items()
            )
            if month is None and not removed:
                continue
            target = statistic_id(key) if spec.cumulative else entity_id
            if removed:
                # A record was deleted in Samsung Health. Rows can't be deleted one by
                # one, so clear this statistic and write it again in full.
                _LOGGER.info("Rows removed from %s; rewriting its statistics in full", key)
                get_instance(self.hass).async_clear_statistics([target])
                changed = rows
            else:
                changed = [row for row in rows if _month(row) >= month]
            if changed:
                if spec.cumulative:
                    async_add_external_statistics(self.hass, metadata(spec), changed)
                else:
                    async_import_statistics(self.hass, metadata(spec, entity_id, unit), changed)
            self._fingerprints[key] = fingerprints
            self._buckets[key] = buckets
            total += len(changed)
            save = True
        if save:
            # Only remember what was written once the recorder has committed it.
            await get_instance(self.hass).async_block_till_done()
            await self._store.async_save(
                {"layout": LAYOUT, "fingerprints": self._fingerprints, "buckets": self._buckets}
            )
            _LOGGER.debug("Wrote %d statistic rows", total)
        return total


async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
    await Store(hass, STORAGE_VERSION, store_key(entry_id)).async_remove()

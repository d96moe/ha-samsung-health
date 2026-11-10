"""A daily-total sensor's own statistics, reconciled with the integration's daily truth.

The "today" sensors are totals that start over at local midnight (state_class
total), so the recorder compiles their statistics itself, hour by hour. This
module gives them their whole history and keeps it right when a past day changes
(a late phone sync, a backfilled or corrected day) without ever inserting a row
into an hour the recorder compiles itself (that made its compile fail for every
entity, see statistics.COMPILE_MARGIN):

- days the recorder compiled get a *sum adjustment* from the day's last row on
  (Recorder.async_adjust_statistics: shifts that row and every later one, in the
  hourly and 5-minute tables, so the curve stays continuous);
- days it didn't (before the sensor existed, or Home Assistant was down) are
  *imported*, one row per day at the day's bucket. A run of such days is written
  as one import (re-importing upserts our own earlier rows) plus one adjustment
  of everything after it, so correcting thousands of old days stays cheap.

The recorder itself is the ledger: each sync reads the sensor's rows and only
applies differences, so it is idempotent and heals deleted statistics. Only
completed days the recorder has compiled are touched; today is the sensor's.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_short_term_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .statistics import compiled_hours_end, day_bucket

_LOGGER = logging.getLogger(__name__)

TOLERANCE = 0.01
#: guard_removals: refuse a sync that would zero this share of the days with rows
REMOVAL_GUARD_SHARE = 0.2
REMOVAL_GUARD_MIN_DAYS = 10
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class SyncResult:
    imported_rows: int = 0
    adjustments: int = 0
    #: The recorder hasn't compiled the sensor yet: nothing was done, try again later.
    waiting: bool = False
    #: The truth lost many days that have history (a bad download?): nothing was done.
    suspicious: bool = False

    @property
    def in_sync(self) -> bool:
        """Checked, and nothing needed changing."""
        return not (self.waiting or self.suspicious) and self.imported_rows == 0 and self.adjustments == 0


def _read_rows(hass: HomeAssistant, entity_id: str) -> list[tuple[datetime, float]]:
    rows = statistics_during_period(hass, _EPOCH, None, {entity_id}, "hour", None, {"sum"}).get(entity_id, [])
    return [(dt_util.utc_from_timestamp(r["start"]), r["sum"] or 0.0) for r in rows]


def _has_short_term(hass: HomeAssistant, entity_id: str) -> bool:
    return bool(get_last_short_term_statistics(hass, 1, entity_id, False, {"sum"}).get(entity_id))


async def async_statistics_unit(hass: HomeAssistant, entity_id: str) -> tuple[str | None, str | None] | None:
    """(unit, unit_class) of the entity's existing statistics, or None if it has none.

    The recorder keeps a statistic's unit when the display unit changes (it converts
    states into it), so history must be written in this unit, not the state's.
    """
    metadata = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={entity_id})
    )
    if entity_id not in metadata:
        return None
    meta = metadata[entity_id][1]
    return meta["unit_of_measurement"], meta.get("unit_class")


async def async_sync_daily_history(
    hass: HomeAssistant,
    entity_id: str,
    unit: str | None,
    unit_class: str | None,
    daily: Mapping[date, float],
    now: datetime,
    *,
    guard_removals: bool = False,
) -> SyncResult:
    """Make the sensor's daily change equal `daily` on every completed, compiled day."""
    instance = get_instance(hass)
    # The recorder continues a sensor's sum from its own last 5-minute row. Until it
    # has compiled the sensor at least once there is no such row, and it would start
    # again from 0 below imported history: wait for it.
    if not await instance.async_add_executor_job(_has_short_term, hass, entity_id):
        _LOGGER.debug("%s: waiting for the recorder's first compile before adding history", entity_id)
        return SyncResult(waiting=True)
    rows = await instance.async_add_executor_job(_read_rows, hass, entity_id)
    last_start: dict[date, datetime] = {}
    end_sum: dict[date, float] = {}
    row_count: dict[date, int] = {}
    for start, total in rows:  # sorted by start
        day = dt_util.as_local(start).date()
        last_start[day], end_sum[day] = start, total
        row_count[day] = row_count.get(day, 0) + 1

    today = dt_util.as_local(now).date()
    compiled_before = compiled_hours_end(now)

    def eligible(day: date) -> bool:
        return day < today and dt_util.as_utc(dt_util.start_of_local_day(day + timedelta(days=1))) <= compiled_before

    def imported_day(day: date) -> bool:
        """A day whose only row is our own import (or that has no row at all)."""
        return day not in end_sum or (row_count[day] == 1 and last_start[day] == day_bucket(day))

    days = sorted(d for d in set(daily) | set(end_sum) if eligible(d))
    if guard_removals:
        removed = [d for d in days if d in end_sum and d not in daily]
        with_rows = sum(1 for d in days if d in end_sum)
        if len(removed) >= REMOVAL_GUARD_MIN_DAYS and len(removed) > REMOVAL_GUARD_SHARE * with_rows:
            _LOGGER.warning(
                "%s: the source no longer has %d of %d days with history; not removing them "
                "(an incomplete download?)", entity_id, len(removed), with_rows,
            )
            return SyncResult(suspicious=True)

    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE, has_sum=True, name=None, source="recorder",
        statistic_id=entity_id, unit_class=unit_class, unit_of_measurement=unit,
    )
    result = SyncResult()
    offset = 0.0  # shift already queued for every row after the processed point
    current_end = 0.0  # actual sum at the processed point, after queued operations
    segment: list[date] = []

    def flush_segment() -> None:
        """Write a run of imported/missing days: changed rows re-imported, one shift after."""
        nonlocal offset, current_end
        if not segment:
            return
        old_end = new_end = current_end
        dirty = False
        new_rows: list[StatisticData] = []
        for day in segment:
            value = float(daily.get(day, 0.0))
            new_end += value
            if day in end_sum:
                actual = end_sum[day] + offset
                dirty = dirty or abs(actual - new_end) >= TOLERANCE
                old_end = actual
            else:
                dirty = True
            if dirty:
                new_rows.append(StatisticData(
                    start=day_bucket(day), state=value, sum=new_end,
                    last_reset=dt_util.as_utc(dt_util.start_of_local_day(day)),
                ))
        if new_rows:
            async_import_statistics(hass, metadata, new_rows)
            result.imported_rows += len(new_rows)
            shift = new_end - old_end
            if abs(shift) >= TOLERANCE:
                # Everything after the run, including 5-minute rows the recorder
                # continues from (a no-op if there are none).
                instance.async_adjust_statistics(entity_id, day_bucket(segment[-1]) + timedelta(hours=1), shift, unit)
                result.adjustments += 1
                offset += shift
        current_end = new_end
        segment.clear()

    for day in days:
        if imported_day(day):
            if day in end_sum or day in daily:
                segment.append(day)
            continue
        flush_segment()
        value = float(daily.get(day, 0.0))
        actual_end = end_sum[day] + offset
        delta = value - (actual_end - current_end)
        if abs(delta) >= TOLERANCE:
            instance.async_adjust_statistics(entity_id, last_start[day], delta, unit)
            result.adjustments += 1
            offset += delta
            actual_end += delta
        current_end = actual_end
    flush_segment()

    if not result.in_sync:
        await instance.async_block_till_done()
    return result

"""A daily-total sensor's own statistics, reconciled with the integration's daily truth.

The "today" sensors are totals that start over at local midnight (state_class
total), so the recorder compiles their statistics itself, hour by hour. This
module gives them their whole history and keeps it right when a past day changes
(a late phone sync, a backfilled or corrected day) without ever inserting a row
into an hour the recorder compiles itself (that made its compile fail for every
entity, see statistics.COMPILE_MARGIN):

- days the sensor already has rows for get a *sum adjustment* from the day's last
  row on (Recorder.async_adjust_statistics: shifts that row and every later one,
  in the hourly and 5-minute tables, so the curve stays continuous);
- runs of days without rows (before the sensor existed, or Home Assistant was
  down) get one *import* of a row per day at the day's bucket, plus one
  adjustment of everything after the run.

The recorder itself is the ledger: each sync reads the sensor's rows and only
applies differences, so it is idempotent and heals deleted statistics. Only
completed days the recorder has compiled are touched; today is the sensor's.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_import_statistics, statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .statistics import compiled_hours_end, day_bucket

TOLERANCE = 0.01
SHORT_TERM_WINDOW = timedelta(days=10)  # the recorder keeps 5-minute rows this long
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class SyncResult:
    imported_rows: int = 0
    adjustments: int = 0
    #: The recorder hasn't compiled the sensor yet: nothing was done, try again later.
    waiting: bool = False

    @property
    def in_sync(self) -> bool:
        """Checked, and nothing needed changing."""
        return not self.waiting and self.imported_rows == 0 and self.adjustments == 0


def _read_rows(hass: HomeAssistant, entity_id: str) -> list[tuple[datetime, float]]:
    rows = statistics_during_period(hass, _EPOCH, None, {entity_id}, "hour", None, {"sum"}).get(entity_id, [])
    return [(dt_util.utc_from_timestamp(r["start"]), r["sum"] or 0.0) for r in rows]


def _has_short_term(hass: HomeAssistant, entity_id: str, now: datetime) -> bool:
    rows = statistics_during_period(hass, now - SHORT_TERM_WINDOW, None, {entity_id}, "5minute", None, {"sum"})
    return bool(rows.get(entity_id))


async def async_sync_daily_history(
    hass: HomeAssistant,
    entity_id: str,
    unit: str | None,
    unit_class: str | None,
    daily: Mapping[date, float],
    now: datetime,
) -> SyncResult:
    """Make the sensor's daily change equal `daily` on every completed, compiled day."""
    instance = get_instance(hass)
    # The recorder continues a sensor's sum from its own last 5-minute row. Until it
    # has compiled the sensor at least once there is no such row, and it would start
    # again from 0 below imported history: wait for it.
    if not await instance.async_add_executor_job(_has_short_term, hass, entity_id, now):
        return SyncResult(waiting=True)
    rows = await instance.async_add_executor_job(_read_rows, hass, entity_id)
    last_start: dict[date, datetime] = {}
    end_sum: dict[date, float] = {}
    for start, total in rows:  # sorted by start
        day = dt_util.as_local(start).date()
        last_start[day], end_sum[day] = start, total

    today = dt_util.as_local(now).date()
    compiled_before = compiled_hours_end(now)

    def eligible(day: date) -> bool:
        return day < today and dt_util.as_utc(dt_util.start_of_local_day(day + timedelta(days=1))) <= compiled_before

    days = sorted(d for d in set(daily) | set(end_sum) if eligible(d))
    latest_row = rows[-1][0] if rows else None
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE, has_sum=True, name=None, source="recorder",
        statistic_id=entity_id, unit_class=unit_class, unit_of_measurement=unit,
    )
    result = SyncResult()
    offset = 0.0  # shift already queued for every row after the processed point
    current_end = 0.0  # actual sum at the processed point, after queued operations
    run: list[tuple[date, float]] = []

    def flush_run() -> None:
        nonlocal offset, current_end
        if not run:
            return
        new_rows, running = [], current_end
        for day, value in run:
            running += value
            new_rows.append(StatisticData(
                start=day_bucket(day), state=value, sum=running,
                last_reset=dt_util.as_utc(dt_util.start_of_local_day(day)),
            ))
        async_import_statistics(hass, metadata, new_rows)
        total = running - current_end
        after = day_bucket(run[-1][0]) + timedelta(hours=1)
        if total and latest_row is not None and latest_row >= after:
            instance.async_adjust_statistics(entity_id, after, total, unit)
            result.adjustments += 1
        result.imported_rows += len(new_rows)
        offset += total
        current_end = running
        run.clear()

    for day in days:
        value = float(daily.get(day, 0.0))
        if day not in end_sum:
            if day in daily:
                run.append((day, value))
            continue
        flush_run()
        actual_end = end_sum[day] + offset
        delta = value - (actual_end - current_end)
        if abs(delta) >= TOLERANCE:
            instance.async_adjust_statistics(entity_id, last_start[day], delta, unit)
            result.adjustments += 1
            offset += delta
        current_end = actual_end + (delta if abs(delta) >= TOLERANCE else 0.0)
    flush_run()

    if not result.in_sync:
        await instance.async_block_till_done()
    return result

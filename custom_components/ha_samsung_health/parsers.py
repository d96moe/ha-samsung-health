"""Pure functions: Samsung cloud document envelopes -> typed daily/point series.

Field semantics were verified against the Samsung Health app's own final daily
values (29 days, 2026-09-29..10-27). See docs/library-analysis.md.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from .const import (
    BMR_MEDIAN_WINDOW,
    SLEEP_STAGE_AWAKE,
    TOTAL_KCAL_ACTIVE_FACTOR,
    TOTAL_KCAL_OFFSET,
)

# step_daily_trend: -2 is Samsung's deduplicated "all sources" row.
STEPS_ALL_SOURCES = -2


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def records(envelopes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unwrap envelopes; skip E2EE records; one record per id, newest update_time wins."""
    newest: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for envelope in envelopes:
        if not isinstance(envelope, dict) or envelope.get("needToDecrypt") is True:
            continue
        data = envelope.get("data")
        if not isinstance(data, dict):
            continue
        key = next(
            (data[k] for k in ("datauuid", "uuid", "id") if isinstance(data.get(k), str) and data[k]),
            None,
        )
        if key is None:
            anonymous.append(data)
            continue
        current = newest.get(key)
        if current is None or (data.get("update_time") or 0) >= (current.get("update_time") or 0):
            newest[key] = data
    return [*newest.values(), *anonymous]


def local_datetime(record: dict[str, Any], field: str = "start_time") -> datetime | None:
    """Aware datetime in the offset the record was made in (time_offset, ms)."""
    ms = _num(record.get(field))
    if ms is None or ms <= 0:
        return None
    offset = _num(record.get("time_offset")) or 0.0
    return datetime.fromtimestamp(ms / 1000, timezone(timedelta(milliseconds=offset)))


def summary_day(record: dict[str, Any]) -> date | None:
    """Daily-summary types store day_time as UTC midnight of the local calendar day."""
    ms = _num(record.get("day_time"))
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).date()


def _newest_per_day(recs: Iterable[dict[str, Any]]) -> dict[date, dict[str, Any]]:
    out: dict[date, dict[str, Any]] = {}
    for rec in recs:
        day = summary_day(rec)
        if day is None:
            continue
        current = out.get(day)
        if current is None or (rec.get("update_time") or 0) >= (current.get("update_time") or 0):
            out[day] = rec
    return out


# --------------------------------------------------------------------------- models
@dataclass(slots=True)
class DailyActivity:
    day: date
    steps: int | None = None
    active_kcal: float | None = None
    step_kcal: float | None = None
    exercise_kcal: float = 0.0
    active_time_min: float | None = None
    distance_m: float | None = None


@dataclass(slots=True)
class BodyMeasurement:
    measured_at: datetime
    weight: float | None
    body_fat: float | None
    body_fat_mass: float | None
    skeletal_muscle_mass: float | None
    total_body_water: float | None
    bmr: float | None


@dataclass(slots=True)
class SleepSession:
    day: date  # wake-up day
    start: datetime
    end: datetime
    duration_min: float | None  # actual sleep: the app's "verklig sovtid"
    score: float | None
    time_in_bed_min: float | None = None  # the app's total time


@dataclass(slots=True)
class RecoveryDay:
    day: date
    sleeping_hr: float | None
    sleeping_hrv: float | None
    energy_score: float | None


@dataclass(slots=True)
class ExerciseSession:
    source_id: str
    exercise_type: int | None
    start: datetime
    duration_s: float
    calories: float | None
    distance_m: float | None
    mean_hr: float | None
    max_hr: float | None


# --------------------------------------------------------------------------- parsers
def parse_exercises(envelopes: Iterable[dict[str, Any]]) -> list[ExerciseSession]:
    out = []
    for rec in records(envelopes):
        start = local_datetime(rec)
        source_id = rec.get("datauuid")
        if start is None or not isinstance(source_id, str):
            continue
        duration_ms = _num(rec.get("duration"))
        if duration_ms is None:
            end = _num(rec.get("end_time"))
            duration_ms = (end - _num(rec["start_time"])) if end else 0.0
        exercise_type = rec.get("exercise_type")
        out.append(
            ExerciseSession(
                source_id=source_id,
                exercise_type=exercise_type if isinstance(exercise_type, int) else None,
                start=start,
                duration_s=max(0.0, duration_ms / 1000),
                calories=_num(rec.get("calorie")),
                distance_m=_num(rec.get("distance")),
                mean_hr=_num(rec.get("mean_heart_rate")) or None,
                max_hr=_num(rec.get("max_heart_rate")) or None,
            )
        )
    return sorted(out, key=lambda s: s.start)


def parse_daily_activity(
    activity_envelopes: Iterable[dict[str, Any]],
    step_envelopes: Iterable[dict[str, Any]],
    exercises: Iterable[ExerciseSession],
) -> dict[date, DailyActivity]:
    days: dict[date, DailyActivity] = {}

    def day_of(d: date) -> DailyActivity:
        return days.setdefault(d, DailyActivity(day=d))

    for d, rec in _newest_per_day(records(activity_envelopes)).items():
        item = day_of(d)
        item.active_kcal = _num(rec.get("calorie"))
        active_ms = _num(rec.get("active_time"))
        item.active_time_min = active_ms / 60000 if active_ms is not None else None
        item.distance_m = _num(rec.get("distance"))
        if item.steps is None and _num(rec.get("step_count")) is not None:
            item.steps = int(rec["step_count"])

    steps = [r for r in records(step_envelopes) if r.get("source_type") == STEPS_ALL_SOURCES]
    for d, rec in _newest_per_day(steps).items():
        item = day_of(d)
        if _num(rec.get("count")) is not None:
            item.steps = int(rec["count"])  # step_daily_trend wins over day_summary
        item.step_kcal = _num(rec.get("calorie"))

    for session in exercises:
        if session.calories:
            day_of(session.start.date()).exercise_kcal += session.calories
    return days


def parse_body(envelopes: Iterable[dict[str, Any]]) -> list[BodyMeasurement]:
    out = []
    for rec in records(envelopes):
        measured_at = local_datetime(rec)
        if measured_at is None:
            continue
        out.append(
            BodyMeasurement(
                measured_at=measured_at,
                weight=_num(rec.get("weight")),
                body_fat=_num(rec.get("body_fat")),
                body_fat_mass=_num(rec.get("body_fat_mass")),
                skeletal_muscle_mass=_num(rec.get("skeletal_muscle_mass")),
                total_body_water=_num(rec.get("total_body_water")),
                bmr=_num(rec.get("basal_metabolic_rate")),
            )
        )
    return sorted(out, key=lambda m: m.measured_at)


SLEEP_DAY_SHIFT = timedelta(hours=6)


def asleep_minutes_by_segment(stage_envelopes: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Segment datauuid -> minutes in a non-awake stage, for segments that have stages."""
    asleep_ms: dict[str, float] = defaultdict(float)
    for rec in records(stage_envelopes):
        sleep_id = rec.get("sleep_id")
        start, end = _num(rec.get("start_time")), _num(rec.get("end_time"))
        if not isinstance(sleep_id, str) or start is None or end is None or end <= start:
            continue
        asleep_ms[sleep_id] += 0.0 if rec.get("stage") == SLEEP_STAGE_AWAKE else end - start
    return {sleep_id: ms / 60000 for sleep_id, ms in asleep_ms.items()}


def parse_sleep(
    sleep_envelopes: Iterable[dict[str, Any]], stage_envelopes: Iterable[dict[str, Any]] = ()
) -> list[SleepSession]:
    """One night per wake-up day: that day's longest segment, as the app shows it.

    A night is often several segments (woke up, fell asleep again). The Samsung
    Health app shows the longest one, with its own score and times; naps lose
    to the night's main sleep. Actual sleep is the segment's non-awake stage time,
    or time in bed x efficiency for old segments without stages.
    """
    asleep = asleep_minutes_by_segment(stage_envelopes)
    nights: dict[date, SleepSession] = {}
    for rec in records(sleep_envelopes):
        start, end = local_datetime(rec, "start_time"), local_datetime(rec, "end_time")
        if start is None or end is None or end <= start:
            continue
        in_bed = (end - start).total_seconds() / 60
        segment_id = rec.get("datauuid")
        if isinstance(segment_id, str) and segment_id in asleep:
            actual = asleep[segment_id]
        elif (efficiency := _num(rec.get("efficiency"))) is not None and efficiency > 0:
            actual = in_bed * efficiency / 100
        else:
            actual = None
        session = SleepSession(
            # Wake-up day of the night a segment belongs to: an evening segment
            # ending 23:11 belongs to the next morning's night, a nap ending 16:00
            # to today's (where it loses to the longer night).
            day=(end + SLEEP_DAY_SHIFT).date(),
            start=start,
            end=end,
            duration_min=actual,
            score=_num(rec.get("sleep_score")),
            time_in_bed_min=in_bed,
        )
        current = nights.get(session.day)
        if current is None or in_bed > (current.time_in_bed_min or 0):
            nights[session.day] = session
    return sorted(nights.values(), key=lambda s: s.end)


def parse_recovery(envelopes: Iterable[dict[str, Any]]) -> dict[date, RecoveryDay]:
    out = {}
    for d, rec in _newest_per_day(records(envelopes)).items():
        out[d] = RecoveryDay(
            day=d,
            sleeping_hr=_num(rec.get("shr_value")),
            sleeping_hrv=_num(rec.get("shrv_value")),
            energy_score=_num(rec.get("total_score")),
        )
    return out


def newest_update_ms(*envelope_lists: Iterable[dict[str, Any]]) -> int | None:
    newest = 0
    for envelopes in envelope_lists:
        for envelope in envelopes:
            data = envelope.get("data") if isinstance(envelope, dict) else None
            if isinstance(data, dict):
                newest = max(newest, int(_num(data.get("update_time")) or 0))
    return newest or None


# --------------------------------------------------------------------------- total kcal
def bmr_for_day(body: list[BodyMeasurement], day: date) -> float | None:
    """Median BMR of the last weighings up to and including `day`.

    The per-weighing BMR swings with the watch's BIA reading (about +-3 % within
    two weeks), so a rolling median is used. Before the first weighing, the earliest
    known value is used.
    """
    values = [m.bmr for m in body if m.bmr and m.measured_at.date() <= day]
    if not values:
        values = [m.bmr for m in body if m.bmr][:1]
    if not values:
        return None
    return statistics.median(values[-BMR_MEDIAN_WINDOW:])


def bmr_by_day(body: list[BodyMeasurement], days: Iterable[date]) -> dict[date, float | None]:
    """bmr_for_day for many days in one pass over the weighings (body is sorted)."""
    values = [(m.measured_at.date(), m.bmr) for m in body if m.bmr]
    first = [values[0][1]] if values else []
    seen: list[float] = []
    out: dict[date, float | None] = {}
    i = 0
    for day in sorted(set(days)):
        while i < len(values) and values[i][0] <= day:
            seen.append(values[i][1])
            i += 1
        window = seen[-BMR_MEDIAN_WINDOW:] or first
        out[day] = statistics.median(window) if window else None
    return out


def estimate_total_kcal(active_kcal: float | None, bmr: float | None, day_fraction: float = 1.0) -> float | None:
    """Samsung's total burn is not in the cloud data; this is an empirical fit."""
    if active_kcal is None or bmr is None:
        return None
    fraction = min(max(day_fraction, 0.0), 1.0)
    return bmr * fraction + TOTAL_KCAL_ACTIVE_FACTOR * active_kcal + TOTAL_KCAL_OFFSET * fraction


def exercise_types(sessions: Iterable[ExerciseSession]) -> dict[int | None, int]:
    counts: dict[int | None, int] = defaultdict(int)
    for s in sessions:
        counts[s.exercise_type] += 1
    return dict(counts)

"""LLM API "Samsung Health": read-only health tools (plus Sync now) for AI agents.

Home Assistant's MCP server publishes every registered LLM API at its own URL,
/api/mcp/<api id>, so an MCP client can be given this API alone:
it sees health data and nothing else in Home Assistant.

Everything is answered from the coordinator's in-memory data, which holds the
full history after every poll, so no recorder queries are needed.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from . import parsers
from .const import DOMAIN
from .coordinator import SamsungHealthConfigEntry, SamsungHealthData
from .sensor import day_fraction
from .statistics import SPECS, build_series

try:  # Home Assistant 2026.10+ describes tool parameters with probatio
    import probatio as schema_lib
except ImportError:  # older releases use voluptuous
    import voluptuous as schema_lib  # type: ignore[no-redef]

API_NAME = "Samsung Health"
SYNC_TIMEOUT = 180  # seconds; a full download normally takes 10-20 s

API_PROMPT = (
    "Samsung Health data (steps, calories, body composition, sleep, recovery and "
    "workouts) synced from the user's phone through Samsung Cloud. Dates are "
    "YYYY-MM-DD in the user's local time zone. Data is only as fresh as the phone's "
    "last sync to Samsung Cloud: check data_freshness, and if it looks old, call "
    "sync_now (it fetches whatever the phone has synced; it can't make the phone "
    "sync). A night of sleep is its longest segment, as in the app: actual sleep "
    "excludes time awake, time in bed is the whole segment. Total calories are an estimate (BMR + 0.82 x active calories); steps, "
    "active calories and workout calories match the Samsung Health app exactly."
)

#: Samsung Health predefined exercise types (exercise_type).
EXERCISE_TYPES = {
    1001: "walking",
    1002: "running",
    11007: "cycling",
    13001: "hiking",
    13002: "climbing",
    14001: "swimming",
}

PERIODS = ("day", "week", "month")
BODY_KEYS = ("weight", "body_fat", "body_fat_mass", "skeletal_muscle_mass", "body_water", "bmr")
_BODY_FIELDS: dict[str, Callable[[parsers.BodyMeasurement], float | None]] = {
    "weight": lambda m: m.weight,
    "body_fat": lambda m: m.body_fat,
    "body_fat_mass": lambda m: m.body_fat_mass,
    "skeletal_muscle_mass": lambda m: m.skeletal_muscle_mass,
    "body_water": lambda m: m.total_body_water,
    "bmr": lambda m: m.bmr,
}
MAX_HISTORY_ROWS = 400
MAX_WORKOUTS = 50

_READ_ONLY: dict[str, Any] = {}
_SYNC: dict[str, Any] = {}
if hasattr(llm, "ToolAnnotations"):  # 2026.10+: forwarded to MCP clients as tool hints
    _READ_ONLY = {"annotations": llm.ToolAnnotations(
        read_only=True, destructive=False, idempotent=True, open_world=False)}
    _SYNC = {"annotations": llm.ToolAnnotations(
        read_only=False, destructive=False, idempotent=True, open_world=True)}


class ToolError(Exception):
    """A problem to report back to the agent as the tool's result."""


def _r(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _parse_date(value: str | None, name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{name} must be a date like 2026-11-08, got {value!r}") from exc


def _result(data: dict[str, Any], error: bool = False):
    if hasattr(llm, "ToolResult"):
        return llm.ToolResult(data=data, error=error)
    return data


# ---------------------------------------------------------------- data shaping
def freshness(entry: SamsungHealthConfigEntry, now: datetime) -> dict[str, Any]:
    data = entry.runtime_data.data
    last = data.last_cloud_update if data else None
    return {
        "last_cloud_sync": last.isoformat() if last else None,
        "hours_since_cloud_sync": _r((now - last).total_seconds() / 3600) if last else None,
        "sync_in_progress": entry.runtime_data.syncing,
    }


def workout(s: parsers.ExerciseSession) -> dict[str, Any]:
    return {
        "type": EXERCISE_TYPES.get(s.exercise_type, "other") if s.exercise_type is not None else "other",
        "exercise_type_code": s.exercise_type,
        "start": s.start.isoformat(),
        "end": (s.start + timedelta(seconds=s.duration_s)).isoformat(),
        "duration_min": _r(s.duration_s / 60),
        "calories_kcal": _r(s.calories, 0),
        "mean_heart_rate_bpm": _r(s.mean_hr, 0),
        "max_heart_rate_bpm": _r(s.max_hr, 0),
        "distance_km": _r(s.distance_m / 1000, 2) if s.distance_m else None,
    }


def summary(data: SamsungHealthData, day: date, today: date, now: datetime) -> dict[str, Any]:
    a = data.days.get(day)
    fraction = day_fraction(now) if day == today else 1.0
    total = parsers.estimate_total_kcal(
        a.active_kcal if a else 0.0, parsers.bmr_for_day(data.body, day), fraction
    )
    activity = {
        "steps": a.steps if a else None,
        "active_calories_kcal": _r(a.active_kcal, 0) if a else None,
        "total_calories_estimated_kcal": _r(total, 0),
        "exercise_calories_kcal": _r(a.exercise_kcal, 0) if a else 0,
        "active_time_min": _r(a.active_time_min, 0) if a else None,
        "distance_km": _r(a.distance_m / 1000, 2) if a and a.distance_m else None,
    }
    if day == today:
        activity["note"] = "today so far; total calories include BMR only up to now"

    body = next((m for m in reversed(data.body) if m.measured_at.date() <= day), None)
    sleep = next((s for s in reversed(data.sleep) if s.day <= day), None)
    recovery_day = max((d for d in data.recovery if d <= day), default=None)
    recovery = data.recovery.get(recovery_day) if recovery_day else None
    return {
        "date": day.isoformat(),
        "activity": activity,
        "body_latest_weighing": body and {
            "measured_at": body.measured_at.isoformat(),
            "weight_kg": _r(body.weight),
            "body_fat_pct": _r(body.body_fat),
            "body_fat_mass_kg": _r(body.body_fat_mass),
            "skeletal_muscle_mass_kg": _r(body.skeletal_muscle_mass),
            "body_water_kg": _r(body.total_body_water),
            "bmr_kcal": _r(body.bmr, 0),
        },
        "sleep_latest_night": sleep and {
            "wake_up_date": sleep.day.isoformat(),
            "bedtime": sleep.start.isoformat(),
            "wake_time": sleep.end.isoformat(),
            "actual_sleep_h": _r(sleep.duration_min / 60, 2) if sleep.duration_min is not None else None,
            "time_in_bed_h": _r(sleep.time_in_bed_min / 60, 2) if sleep.time_in_bed_min is not None else None,
            "sleep_score": _r(sleep.score, 0),
        },
        "recovery_latest_day": recovery and {
            "date": recovery.day.isoformat(),
            "sleeping_heart_rate_bpm": _r(recovery.sleeping_hr, 0),
            "sleeping_hrv_ms": _r(recovery.sleeping_hrv, 0),
            "energy_score": _r(recovery.energy_score, 0),
        },
        "workouts": [workout(s) for s in data.exercises if s.start.date() == day],
    }


def daily_values(data: SamsungHealthData, metric: str, today: date, now: datetime) -> list[tuple[date, float]]:
    """(local day, value) for every recorded value of a metric, oldest first."""
    if metric in BODY_KEYS:
        get = _BODY_FIELDS[metric]
        return [(m.measured_at.date(), v) for m in data.body if (v := get(m)) is not None]
    # Daily series sit at 12:00 UTC of their own day, so .date() is that day.
    return [(start.date(), v) for start, v in build_series(data, today, day_fraction(now))[metric]]


def period_start(day: date, period: str) -> date:
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    return day


def history(values: list[tuple[date, float]], cumulative: bool, start: date, end: date, period: str) -> list[dict]:
    groups: dict[date, list[float]] = defaultdict(list)
    for day, value in values:
        if start <= day <= end:
            groups[period_start(day, period)].append(value)
    rows = []
    for first in sorted(groups):
        vals = groups[first]
        row: dict[str, Any] = {"period_start": first.isoformat()}
        if cumulative:
            row["total"] = _r(sum(vals), 0)
            row["days_with_data"] = len(vals)
            if period != "day":
                row["daily_average"] = _r(sum(vals) / len(vals), 0)
        else:
            row["mean"] = _r(sum(vals) / len(vals), 2)
            if len(vals) > 1:
                row["min"] = _r(min(vals), 2)
                row["max"] = _r(max(vals), 2)
            row["count"] = len(vals)
        rows.append(row)
    return rows


# ---------------------------------------------------------------- tools
class _Tool(llm.Tool):
    hints: dict[str, Any] = _READ_ONLY

    def __init__(self, entry: SamsungHealthConfigEntry) -> None:
        self.entry = entry
        self.integration = DOMAIN
        for key, value in self.hints.items():
            setattr(self, key, value)

    def _data(self) -> SamsungHealthData:
        data = self.entry.runtime_data.data
        if data is None:
            raise ToolError("No Samsung Health data yet: the first download after a restart is still running.")
        return data

    async def async_call(self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext):
        try:
            return _result(await self._async_run(dict(tool_input.tool_args), dt_util.now()))
        except ToolError as exc:
            return _result({"error": str(exc)}, error=True)

    async def _async_run(self, args: dict[str, Any], now: datetime) -> dict[str, Any]:
        raise NotImplementedError


class SummaryTool(_Tool):
    name = "get_health_summary"
    title = "Health summary for a day"
    description = (
        "One day's health picture: steps, active, exercise and estimated total calories, "
        "active time and distance for that day; the latest weighing (weight and body "
        "composition), night of sleep and recovery values on or before that day; and "
        "that day's workouts. Defaults to today."
    )
    parameters = schema_lib.Schema({
        schema_lib.Optional("date", description="YYYY-MM-DD; default today"): str,
    })

    async def _async_run(self, args, now):
        data = self._data()
        today = now.date()
        day = _parse_date(args.get("date"), "date") or today
        return {**summary(data, day, today, now), "data_freshness": freshness(self.entry, dt_util.utcnow())}


class HistoryTool(_Tool):
    name = "get_history"
    title = "History of one metric"
    description = (
        "History of one metric between two dates, per day, week (starting Monday) or "
        "month. Daily totals (steps, calories) give the total and the daily average per "
        "period; measurements (weight, body composition, sleep, recovery) give the mean, "
        "min and max. History goes back to the first Samsung Health record."
    )
    parameters = schema_lib.Schema({
        schema_lib.Required("metric", description="What to show"): schema_lib.In(sorted(SPECS)),
        schema_lib.Optional("start", description="YYYY-MM-DD; default 30 days before end"): str,
        schema_lib.Optional("end", description="YYYY-MM-DD; default today"): str,
        schema_lib.Optional("period", description="day, week or month; default day"): schema_lib.In(list(PERIODS)),
    })

    async def _async_run(self, args, now):
        data = self._data()
        metric = args["metric"]
        if metric not in SPECS:
            raise ToolError(f"Unknown metric {metric!r}; use one of {', '.join(sorted(SPECS))}")
        period = args.get("period") or "day"
        if period not in PERIODS:
            raise ToolError("period must be day, week or month")
        today = now.date()
        end = _parse_date(args.get("end"), "end") or today
        start = _parse_date(args.get("start"), "start") or end - timedelta(days=29)
        if start > end:
            raise ToolError("start is after end")
        spec = SPECS[metric]
        rows = history(daily_values(data, metric, today, now), spec.cumulative, start, end, period)
        truncated = len(rows) > MAX_HISTORY_ROWS
        result = {
            "metric": metric,
            "unit": spec.unit,
            "kind": "daily total" if spec.cumulative else "measurement",
            "period": period,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "rows": rows[-MAX_HISTORY_ROWS:],
        }
        if truncated:
            result["note"] = f"Only the last {MAX_HISTORY_ROWS} periods; use a longer period or a shorter range."
        if metric == "total_calories":
            result["estimate"] = "BMR + 0.82 x active calories; today counts BMR only up to now"
        return result


class WorkoutsTool(_Tool):
    name = "get_workouts"
    title = "Workouts"
    description = (
        "Workouts recorded in Samsung Health, newest first: type, start and end, "
        "duration, calories burned (as shown in Samsung Health), mean and max heart rate "
        "and distance. Without dates: the latest ones."
    )
    parameters = schema_lib.Schema({
        schema_lib.Optional("start", description="YYYY-MM-DD, first day to include"): str,
        schema_lib.Optional("end", description="YYYY-MM-DD, last day to include"): str,
        schema_lib.Optional("limit", description=f"Most workouts to return (1-{MAX_WORKOUTS}); default 5"): int,
    })

    async def _async_run(self, args, now):
        data = self._data()
        start = _parse_date(args.get("start"), "start")
        end = _parse_date(args.get("end"), "end")
        try:
            limit = max(1, min(MAX_WORKOUTS, int(args.get("limit") or 5)))
        except (TypeError, ValueError) as exc:
            raise ToolError("limit must be a number") from exc
        sessions = [
            s for s in reversed(data.exercises)
            if (start is None or s.start.date() >= start) and (end is None or s.start.date() <= end)
        ]
        return {
            "workouts": [workout(s) for s in sessions[:limit]],
            "matching": len(sessions),
            "data_freshness": freshness(self.entry, dt_util.utcnow()),
        }


class SyncNowTool(_Tool):
    name = "sync_now"
    title = "Sync now"
    description = (
        "Download the latest data from Samsung Cloud now and wait for it (usually "
        "10-30 seconds). Only gets what the phone has already synced to Samsung Cloud."
    )
    parameters = schema_lib.Schema({})
    hints = _SYNC

    async def _async_run(self, args, now):
        coordinator = self.entry.runtime_data
        try:
            await asyncio.wait_for(coordinator.async_sync_and_wait(), SYNC_TIMEOUT)
        except TimeoutError as exc:
            raise ToolError("The download is still running; try again in a minute.") from exc
        if not coordinator.last_update_success:
            raise ToolError("The download from Samsung Cloud failed; the previous data is still available.")
        return {"synced": True, "data_freshness": freshness(self.entry, dt_util.utcnow())}


class SamsungHealthAPI(llm.API):
    """Registered by the config entry (single_config_entry)."""

    def __init__(self, hass: HomeAssistant, entry: SamsungHealthConfigEntry) -> None:
        super().__init__(hass=hass, id=DOMAIN, name=API_NAME)
        self.entry = entry

    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        tools = [cls(self.entry) for cls in (SummaryTool, HistoryTool, WorkoutsTool, SyncNowTool)]
        return llm.APIInstance(api=self, api_prompt=API_PROMPT, llm_context=llm_context, tools=tools)

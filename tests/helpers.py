"""Synthetic Samsung cloud envelopes. No real health data in this repository."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

CET = 3_600_000  # time_offset in ms
CEST = 7_200_000


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def utc_midnight(y: int, m: int, d: int) -> int:
    return ms(datetime(y, m, d, tzinfo=UTC))


def env(data: dict[str, Any], **envelope: Any) -> dict[str, Any]:
    return {"data": data, **envelope}


def day_summary(y: int, m: int, d: int, *, calorie: float, steps: int, update: int = 1, uuid: str | None = None) -> dict:
    return env({
        "datauuid": uuid or f"act-{y}{m:02}{d:02}-{update}",
        "day_time": utc_midnight(y, m, d),
        "calorie": calorie,
        "step_count": steps,
        "active_time": 60 * 60_000,
        "distance": 5000.0,
        "update_time": update,
    })


def step_trend(y: int, m: int, d: int, *, count: int, calorie: float, source_type: int = -2, update: int = 1) -> dict:
    return env({
        "datauuid": f"st-{y}{m:02}{d:02}-{source_type}-{update}",
        "day_time": utc_midnight(y, m, d),
        "count": count,
        "calorie": calorie,
        "source_type": source_type,
        "update_time": update,
    })


def exercise(start_utc: datetime, *, minutes: float, calorie: float, exercise_type: int = 1001,
             offset: int = CET, uuid: str | None = None) -> dict:
    return env({
        "datauuid": uuid or f"ex-{ms(start_utc)}",
        "start_time": ms(start_utc),
        "end_time": ms(start_utc + timedelta(minutes=minutes)),
        "duration": int(minutes * 60_000),
        "time_offset": offset,
        "calorie": calorie,
        "exercise_type": exercise_type,
        "distance": 1000.0,
        "mean_heart_rate": 120.0,
        "max_heart_rate": 160.0,
        "update_time": ms(start_utc),
    })


def weight(at_utc: datetime, *, kg: float, bmr: int, offset: int = CET) -> dict:
    return env({
        "datauuid": f"w-{ms(at_utc)}",
        "start_time": ms(at_utc),
        "time_offset": offset,
        "weight": kg,
        "body_fat": 25.0,
        "body_fat_mass": kg / 4,
        "skeletal_muscle_mass": 36.0,
        "total_body_water": 49.0,
        "basal_metabolic_rate": bmr,
        "update_time": ms(at_utc),
    })


def sleep(start_utc: datetime, end_utc: datetime, *, minutes: int, score: int, offset: int = CET) -> dict:
    return env({
        "datauuid": f"sl-{ms(start_utc)}",
        "start_time": ms(start_utc),
        "end_time": ms(end_utc),
        "time_offset": offset,
        "sleep_duration": minutes,
        "sleep_score": score,
        "update_time": ms(end_utc),
    })


def vitality(y: int, m: int, d: int, *, shr: float, shrv: float, score: float) -> dict:
    return env({
        "datauuid": f"v-{y}{m:02}{d:02}",
        "day_time": utc_midnight(y, m, d),
        "shr_value": shr,
        "shrv_value": shrv,
        "total_score": score,
        "update_time": utc_midnight(y, m, d) + 1,
    })

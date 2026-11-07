from datetime import UTC, date, datetime

from custom_components.ha_samsung_health import parsers
from custom_components.ha_samsung_health.coordinator import SamsungHealthData
from custom_components.ha_samsung_health.statistics import (
    SPECS,
    build_series,
    day_bucket,
    first_changed_month,
    metadata,
    month_fingerprints,
    statistic_id,
    to_rows,
)

from .helpers import day_summary, step_trend, weight


def _data(*, extra_day: bool = False) -> SamsungHealthData:
    acts = [day_summary(2026, 10, 1, calorie=300, steps=5000), day_summary(2026, 10, 2, calorie=500, steps=8000)]
    steps = [step_trend(2026, 10, 1, count=5000, calorie=250), step_trend(2026, 10, 2, count=8000, calorie=400)]
    if extra_day:
        acts.append(day_summary(2026, 10, 3, calorie=100, steps=1000))
    body = parsers.parse_body([weight(datetime(2026, 10, 1, 6, 30, tzinfo=UTC), kg=90.0, bmr=1800)])
    return SamsungHealthData(days=parsers.parse_daily_activity(acts, steps, []), body=body)


def test_series_cumulative_sum_is_monotonic_and_daily_rows_at_noon_utc():
    series = build_series(_data(), today=date(2026, 10, 10), today_fraction=0.5)
    rows = to_rows(SPECS["steps"], series["steps"])
    assert [r["start"] for r in rows] == [day_bucket(date(2026, 10, 1)), day_bucket(date(2026, 10, 2))]
    assert rows[0]["start"].hour == 12
    assert [r["state"] for r in rows] == [5000, 8000]
    assert [r["sum"] for r in rows] == [5000, 13000]


def test_total_calories_uses_fraction_only_for_today():
    full = build_series(_data(), today=date(2026, 10, 10), today_fraction=0.5)["total_calories"]
    partial = build_series(_data(), today=date(2026, 10, 2), today_fraction=0.5)["total_calories"]
    assert partial[0] == full[0]
    assert partial[1][1] < full[1][1]


def test_body_rows_are_mean_at_measurement_hour():
    series = build_series(_data(), today=date(2026, 10, 10), today_fraction=1)
    [row] = to_rows(SPECS["weight"], series["weight"])
    assert row["start"] == datetime(2026, 10, 1, 6, tzinfo=UTC)
    assert row["mean"] == row["min"] == row["max"] == 90.0
    assert "sum" not in row


def test_month_fingerprints_and_first_changed_month():
    old = month_fingerprints(to_rows(SPECS["steps"], build_series(_data(), date(2026, 10, 10), 1)["steps"]))
    new = month_fingerprints(
        to_rows(SPECS["steps"], build_series(_data(extra_day=True), date(2026, 10, 10), 1)["steps"])
    )
    assert list(old) == ["2026-10"]
    assert first_changed_month(None, new) == "2026-10"
    assert first_changed_month(old, old) is None
    assert first_changed_month(old, new) == "2026-10"
    assert first_changed_month({"2026-09": "x", **old}, old) is None  # removed month: nothing to write


def test_metadata():
    assert statistic_id("steps") == "ha_samsung_health:steps"
    steps = metadata(SPECS["steps"])
    assert steps["has_sum"] is True and steps["source"] == "ha_samsung_health"
    # measurements go onto the sensor entity's own statistic
    weight = metadata(SPECS["weight"], "sensor.samsung_health_weight")
    assert weight["has_sum"] is False
    assert weight["source"] == "recorder"
    assert weight["statistic_id"] == "sensor.samsung_health_weight"
    assert weight["unit_class"] == "mass"
    assert metadata(SPECS["sleep_duration"], "sensor.x")["unit_class"] == "duration"
    assert metadata(SPECS["sleep_score"], "sensor.x")["unit_class"] == "unitless"  # as HA's own compile

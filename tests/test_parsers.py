from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.ha_samsung_health import parsers

from .helpers import (
    CEST,
    day_summary,
    env,
    exercise,
    sleep,
    sleep_stage,
    step_trend,
    vitality,
    weight,
)


def test_records_skips_encrypted_and_keeps_newest_update():
    envs = [
        env({"datauuid": "a", "update_time": 1, "v": 1}),
        env({"datauuid": "a", "update_time": 5, "v": 2}),
        env({"datauuid": "a", "update_time": 3, "v": 3}),
        env({"datauuid": "b", "v": 9}, needToDecrypt=True),
        {"no": "data"},
    ]
    assert [r["v"] for r in parsers.records(envs)] == [2]


def test_summary_day_reads_utc_date_not_local():
    # day_time is UTC midnight of the local calendar day; local time would shift it.
    rec = day_summary(2026, 10, 5, calorie=1, steps=1)["data"]
    assert parsers.summary_day(rec) == date(2026, 10, 5)


def test_daily_activity_merges_sources():
    acts = [day_summary(2026, 10, 5, calorie=500, steps=9_000, update=1),
            day_summary(2026, 10, 5, calorie=650, steps=9_100, update=2)]
    steps = [step_trend(2026, 10, 5, count=9_200, calorie=403),
             step_trend(2026, 10, 5, count=99_999, calorie=999, source_type=0)]
    ex = parsers.parse_exercises([
        exercise(datetime(2026, 10, 5, 6, 0, tzinfo=UTC), minutes=30, calorie=200),
        exercise(datetime(2026, 10, 5, 17, 0, tzinfo=UTC), minutes=60, calorie=187),
    ])
    day = parsers.parse_daily_activity(acts, steps, ex)[date(2026, 10, 5)]
    assert day.active_kcal == 650  # newest update_time wins
    assert day.steps == 9_200  # step_daily_trend, only source_type -2
    assert day.step_kcal == 403
    assert day.exercise_kcal == 387
    assert day.active_time_min == 60


def test_exercise_local_day_uses_time_offset():
    # 23:30 UTC is 01:30 the next day in CEST.
    [s] = parsers.parse_exercises([
        exercise(datetime(2026, 7, 1, 23, 30, tzinfo=UTC), minutes=20, calorie=100, offset=CEST)
    ])
    assert s.start.date() == date(2026, 7, 2)
    assert s.start.hour == 1
    assert s.duration_s == 1200
    days = parsers.parse_daily_activity([], [], [s])
    assert days[date(2026, 7, 2)].exercise_kcal == 100


def test_sleep_counts_for_wake_up_day():
    [s] = parsers.parse_sleep([
        sleep(datetime(2026, 10, 21, 21, 0, tzinfo=UTC), datetime(2026, 10, 22, 5, 0, tzinfo=UTC),
              minutes=480, score=75, offset=CEST, efficiency=90)
    ])
    assert s.day == date(2026, 10, 22)
    assert s.time_in_bed_min == 480
    assert s.duration_min == 432  # no stages: time in bed x efficiency


def test_split_night_is_its_longest_segment_with_stage_sleep_time():
    # Like the night to 2026-11-05 checked in the app: two segments; the app shows
    # the longer one (score 45), actual sleep = its non-awake stages.
    first = datetime(2026, 11, 4, 21, 3, tzinfo=UTC)
    main = datetime(2026, 11, 5, 0, 32, tzinfo=UTC)
    segments = [
        sleep(first, first + timedelta(minutes=68), minutes=68, score=22),
        sleep(main, main + timedelta(minutes=326), minutes=326, score=45),
        # an afternoon nap: shorter, so not the night
        sleep(datetime(2026, 11, 5, 13, 0, tzinfo=UTC), datetime(2026, 11, 5, 13, 40, tzinfo=UTC),
              minutes=40, score=50),
    ]
    stages = [
        sleep_stage(main, main, main + timedelta(minutes=31), stage=40001),  # awake
        sleep_stage(main, main + timedelta(minutes=31), main + timedelta(minutes=200), stage=40002),
        sleep_stage(main, main + timedelta(minutes=200), main + timedelta(seconds=326 * 60), stage=40004),
        sleep_stage(first, first, first + timedelta(minutes=68), stage=40002),  # other segment
    ]
    [night] = parsers.parse_sleep(segments, stages)
    assert night.day == date(2026, 11, 5)
    assert night.score == 45
    assert night.time_in_bed_min == 326
    assert night.duration_min == 295
    assert night.start.hour == 1 and night.start.minute == 32  # local time


def test_recovery_and_body():
    rec = parsers.parse_recovery([vitality(2026, 11, 5, shr=60, shrv=40, score=70.0)])
    assert rec[date(2026, 11, 5)].sleeping_hr == 60
    body = parsers.parse_body([weight(datetime(2026, 11, 6, 6, 8, tzinfo=UTC), kg=80.5, bmr=1700)])
    assert body[0].weight == 80.5 and body[0].measured_at.hour == 7


def test_bmr_rolling_median_and_before_first_weighing():
    body = parsers.parse_body([
        weight(datetime(2026, 10, d, 6, 0, tzinfo=UTC), kg=90, bmr=b)
        for d, b in [(1, 1800), (2, 1900), (3, 1700), (4, 1810)]
    ])
    assert parsers.bmr_for_day(body, date(2026, 10, 3)) == 1800
    assert parsers.bmr_for_day(body, date(2026, 9, 1)) == 1800  # earliest known
    assert parsers.bmr_for_day([], date(2026, 9, 1)) is None


@pytest.mark.parametrize(("active", "bmr"), [(0, 1700), (300, 1700), (1200, 1650)])
def test_total_estimate_formula(active, bmr):
    # The fit itself is validated against real app values outside the repo
    # (spike/private/validate.py); here only the formula is pinned.
    assert parsers.estimate_total_kcal(active, bmr) == pytest.approx(bmr + 0.819 * active + 4)


def test_total_estimate_partial_day_and_missing_inputs():
    assert parsers.estimate_total_kcal(0, 1800, day_fraction=0.5) == pytest.approx(902)
    assert parsers.estimate_total_kcal(None, 1800) is None
    assert parsers.estimate_total_kcal(100, None) is None


def test_newest_update_ms():
    assert parsers.newest_update_ms([env({"update_time": 5})], [env({"update_time": 9})]) == 9
    assert parsers.newest_update_ms([]) is None

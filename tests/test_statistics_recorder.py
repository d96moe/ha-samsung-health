"""StatisticsWriter against a real (test) recorder: history is written once."""
from datetime import UTC, date, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.ha_samsung_health import parsers
from custom_components.ha_samsung_health.const import DOMAIN
from custom_components.ha_samsung_health.coordinator import SamsungHealthData
from custom_components.ha_samsung_health.statistics import (
    SPECS,
    StatisticsWriter,
    build_series,
    metadata,
    statistic_id,
    store_key,
)

from .helpers import day_summary, step_trend, weight

TODAY = date(2026, 10, 10)


def _series(days):
    acts = [day_summary(2026, m, d, calorie=100 * i, steps=1000 * i) for i, (m, d) in enumerate(days, 1)]
    steps = [step_trend(2026, m, d, count=1000 * i, calorie=50 * i) for i, (m, d) in enumerate(days, 1)]
    data = SamsungHealthData(days=parsers.parse_daily_activity(acts, steps, []))
    return build_series(data, TODAY, 1.0)


async def _rows(hass, key):
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2000, 1, 1, tzinfo=UTC), None,
        {statistic_id(key)}, "hour", None, {"state", "sum"},
    )
    return result.get(statistic_id(key), [])


async def test_history_written_once_across_restarts(hass):
    entity_id = _weight_entity(hass)
    series = _weight_series(90.0, 85.0, 84.0)
    assert await StatisticsWriter(hass, "e1").async_write(series) > 0
    await async_wait_recording_done(hass)
    assert await _means(hass, entity_id) == [90.0, 85.0, 84.0]

    # "Restart": a new writer with the same persisted fingerprints writes nothing.
    restarted = StatisticsWriter(hass, "e1")
    assert await restarted.async_write(series) == 0

    # One more weighing: only its month is written; the rest stays as written.
    assert await restarted.async_write(_weight_series(90.0, 85.0, 84.0, 83.0)) == 1
    await async_wait_recording_done(hass)
    assert await _means(hass, entity_id) == [90.0, 85.0, 84.0, 83.0]


def _weight_series(*kgs):
    body = parsers.parse_body(
        [weight(datetime(2016 + i, 3, 1, 6, 30, tzinfo=UTC), kg=kg, bmr=1700) for i, kg in enumerate(kgs)]
    )
    return build_series(SamsungHealthData(body=body), TODAY, 1.0)


def _weight_entity(hass, unit="kg") -> str:
    entity_id = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "e1_weight", suggested_object_id="samsung_health_weight"
    ).entity_id
    hass.states.async_set(entity_id, "80.0", {"unit_of_measurement": unit, "state_class": "measurement"})
    return entity_id


async def _means(hass, statistic):
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2000, 1, 1, tzinfo=UTC), None,
        {statistic}, "hour", None, {"mean"},
    )
    return [r["mean"] for r in result.get(statistic, [])]


async def test_measurement_history_goes_onto_the_entity(hass):
    entity_id = _weight_entity(hass)
    await StatisticsWriter(hass, "e1").async_write(_weight_series(90.0, 85.0))
    await async_wait_recording_done(hass)
    assert await _means(hass, entity_id) == [90.0, 85.0]
    assert await _means(hass, statistic_id("weight")) == []
    meta = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={entity_id})
    )
    assert meta[entity_id][1]["source"] == "recorder"
    assert meta[entity_id][1]["unit_class"] == "mass"


async def test_old_external_measurements_are_moved_to_the_entity(hass, hass_storage):
    # v0.1.x: weight written as external statistic, fingerprints without layout.
    old_meta = metadata(SPECS["steps"]) | {
        "statistic_id": statistic_id("weight"), "unit_of_measurement": "kg",
        "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False,
    }
    async_add_external_statistics(
        hass, old_meta, [StatisticData(start=datetime(2016, 3, 1, 6, tzinfo=UTC), mean=90.0, min=90.0, max=90.0)]
    )
    await async_wait_recording_done(hass)
    hass_storage[store_key("e1")] = {"version": 1, "key": store_key("e1"),
                                     "data": {"fingerprints": {"weight": {"2016-03": "x"}}}}
    entity_id = _weight_entity(hass)

    await StatisticsWriter(hass, "e1").async_write(_weight_series(90.0))
    await async_wait_recording_done(hass)
    assert await _means(hass, statistic_id("weight")) == []  # old copy gone
    assert await _means(hass, entity_id) == [90.0]
    assert hass_storage[store_key("e1")]["data"]["layout"] == 2


async def test_statistics_deleted_in_recorder_are_rewritten(hass):
    entity_id = _weight_entity(hass)
    series = _weight_series(90.0, 85.0)
    await StatisticsWriter(hass, "e1").async_write(series)
    await async_wait_recording_done(hass)
    get_instance(hass).async_clear_statistics([entity_id])
    await async_wait_recording_done(hass)
    assert await StatisticsWriter(hass, "e1").async_write(series) == 2  # missing: written in full
    await async_wait_recording_done(hass)
    assert await _means(hass, entity_id) == [90.0, 85.0]


# --------------------------------------------------------------- recorder compile
HOUR = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


def _weighing_at(moment: datetime, kg: float = 80.0):
    body = parsers.parse_body([weight(moment, kg=kg, bmr=1700)])
    return build_series(SamsungHealthData(body=body), TODAY, 1.0)


async def test_import_never_collides_with_the_recorders_hourly_compile(hass, freezer):
    """Importing into an hour the recorder hasn't compiled yet made its compile fail,
    dropping that hour's statistics for EVERY entity (seen live 2026-11-09)."""
    await async_setup_component(hass, "sensor", {})
    other = "sensor.other_temperature"
    freezer.move_to(HOUR + timedelta(minutes=5))
    hass.states.async_set(other, "21.0", {"unit_of_measurement": "°C", "state_class": "measurement"})
    _weight_entity(hass)
    await async_wait_recording_done(hass)

    freezer.move_to(HOUR + timedelta(minutes=30))  # weighed 10:10, polled 10:30
    await StatisticsWriter(hass, "e1").async_write(_weighing_at(HOUR + timedelta(minutes=10)))
    await async_wait_recording_done(hass)

    freezer.move_to(HOUR + timedelta(hours=1, minutes=1))
    for minute in range(0, 60, 5):  # the 5-minute runs; the :55 one also compiles the hour
        do_adhoc_statistics(hass, start=HOUR + timedelta(minutes=minute))
    await async_wait_recording_done(hass)
    assert await _means(hass, other) == [21.0]


async def test_rows_for_uncompiled_hours_are_written_by_a_later_poll(hass, freezer):
    entity_id = _weight_entity(hass)
    writer = StatisticsWriter(hass, "e1")
    series = _weighing_at(HOUR + timedelta(minutes=10))

    freezer.move_to(HOUR + timedelta(minutes=30))
    await writer.async_write(series)
    await async_wait_recording_done(hass)
    assert await _means(hass, entity_id) == []  # 10:00 not compiled yet: held back

    freezer.move_to(HOUR + timedelta(hours=1, minutes=20))
    assert await writer.async_write(series) == 1
    await async_wait_recording_done(hass)
    assert await _means(hass, entity_id) == [80.0]


async def test_deleted_weighing_disappears_from_the_history(hass):
    """A weighing deleted in Samsung Health (a guest on the scale) must not stay."""
    entity_id = _weight_entity(hass)
    writer = StatisticsWriter(hass, "e1")
    both = parsers.parse_body([weight(datetime(2016, 3, 1, 6, 30, tzinfo=UTC), kg=90.0, bmr=1700),
                               weight(datetime(2016, 3, 5, 6, 30, tzinfo=UTC), kg=55.0, bmr=1200)])
    await writer.async_write(build_series(SamsungHealthData(body=both), TODAY, 1.0))
    await async_wait_recording_done(hass)
    assert await _means(hass, entity_id) == [90.0, 55.0]
    await writer.async_write(build_series(SamsungHealthData(body=both[:1]), TODAY, 1.0))
    await async_wait_recording_done(hass)
    assert await _means(hass, entity_id) == [90.0]




async def test_statistics_deleted_in_recorder_are_rewritten_on_the_next_poll(hass):
    entity_id = _weight_entity(hass)
    writer = StatisticsWriter(hass, "e1")  # the same writer: no restart in between
    series = _weight_series(90.0, 85.0)
    await writer.async_write(series)
    await async_wait_recording_done(hass)
    get_instance(hass).async_clear_statistics([entity_id])
    await async_wait_recording_done(hass)
    await writer.async_write(series)
    await async_wait_recording_done(hass)
    assert await _means(hass, entity_id) == [90.0, 85.0]


async def test_entity_with_another_display_unit_gets_converted_history(hass):
    entity_id = _weight_entity(hass, unit="lb")
    writer = StatisticsWriter(hass, "e1")
    assert await writer.async_write(_weight_series(90.0)) == 1
    await async_wait_recording_done(hass)
    [mean] = await _means(hass, entity_id)
    assert abs(mean - 90.0 / 0.45359237) < 1e-6



async def test_daily_totals_are_not_written_as_external_statistics(hass):
    assert await StatisticsWriter(hass, "e1").async_write(_series([(10, 1), (10, 2)])) == 0
    await async_wait_recording_done(hass)
    assert await _rows(hass, "steps") == []

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import probatio
import pytest
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_samsung_health.const import (
    CONF_STORAGE_KEY,
    DOMAIN,
    MANIFEST_ACTIVITY,
    MANIFEST_EXERCISE,
    MANIFEST_SLEEP,
    MANIFEST_SLEEP_STAGE,
    MANIFEST_STEPS,
    MANIFEST_VITALITY,
    MANIFEST_WEIGHT,
)

from .helpers import CET, day_summary, exercise, sleep, sleep_stage, step_trend, vitality, weight
from .test_init import NOW, FakeApi

SLEEP_START = datetime(2026, 11, 5, 22, 0, tzinfo=UTC)

DOCS = {
    MANIFEST_ACTIVITY: [
        day_summary(2026, 11, 2, calorie=500, steps=8000),
        day_summary(2026, 11, 3, calorie=300, steps=4000),
        day_summary(2026, 11, 6, calorie=400, steps=6000),
    ],
    MANIFEST_STEPS: [step_trend(2026, 11, 6, count=6100, calorie=300)],
    MANIFEST_EXERCISE: [
        exercise(datetime(2026, 11, 1, 17, 0, tzinfo=UTC), minutes=45, calorie=300, exercise_type=1002),
        exercise(datetime(2026, 11, 5, 17, 0, tzinfo=UTC), minutes=120, calorie=588, exercise_type=13002),
        exercise(datetime(2026, 11, 6, 6, 0, tzinfo=UTC), minutes=30, calorie=150, exercise_type=99999),
    ],
    MANIFEST_WEIGHT: [
        weight(datetime(2026, 11, 2, 6, 0, tzinfo=UTC), kg=81.0, bmr=1700),
        weight(datetime(2026, 11, 3, 6, 0, tzinfo=UTC), kg=80.0, bmr=1700),
        weight(datetime(2026, 11, 6, 6, 8, tzinfo=UTC), kg=80.5, bmr=1700),
    ],
    MANIFEST_SLEEP: [sleep(datetime(2026, 11, 5, 22, 0, tzinfo=UTC), datetime(2026, 11, 6, 5, 30, tzinfo=UTC),
                           minutes=420, score=81, offset=CET)],
    MANIFEST_SLEEP_STAGE: [  # 30 min awake, then 7 h asleep
        sleep_stage(SLEEP_START, SLEEP_START, SLEEP_START + timedelta(minutes=30), stage=40001),
        sleep_stage(SLEEP_START, SLEEP_START + timedelta(minutes=30), datetime(2026, 11, 6, 5, 30, tzinfo=UTC), stage=40002),
    ],
    MANIFEST_VITALITY: [vitality(2026, 11, 6, shr=60, shrv=40, score=70.0)],
}

CONTEXT = llm.LLMContext(platform="test", context=None, language="en", assistant="conversation", device_id=None)


@pytest.fixture
async def api(hass, freezer):
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Stockholm")
    FakeApi.error = None
    FakeApi.instances = []
    FakeApi.polls = 0
    FakeApi.gate = None
    FakeApi.docs = DOCS
    entry = MockConfigEntry(domain=DOMAIN, unique_id="u", data={CONF_STORAGE_KEY: "a" * 32})
    entry.add_to_hass(hass)
    with patch("custom_components.ha_samsung_health.coordinator.SamsungHealthApi", FakeApi), \
         patch("custom_components.ha_samsung_health.statistics.async_add_external_statistics"), \
         patch("custom_components.ha_samsung_health.statistics.async_import_statistics"):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        yield entry, await llm.async_get_api(hass, DOMAIN, CONTEXT)


async def _spin():
    """Let pending tasks run. The clock is frozen, so asyncio.sleep(>0) would never return."""
    for _ in range(20):
        await asyncio.sleep(0)


async def call(instance, name, **args):
    """Call a tool the way the MCP server does, with validated arguments.

    Not via APIInstance.async_call_tool: that imports the conversation integration
    (for its trace), which needs packages the test environment doesn't install.
    """
    [tool] = [t for t in instance.tools if t.name == name]
    tool_args = tool.parameters(args)
    result = await tool.async_call(instance.api.hass, llm.ToolInput(tool_name=name, tool_args=tool_args), CONTEXT)
    return result.data if hasattr(result, "data") else result


async def test_registered_as_its_own_api(hass, api):
    entry, instance = api
    assert {t.name for t in instance.tools} == {"get_health_summary", "get_history", "get_workouts", "sync_now"}
    assert DOMAIN in {a.id for a in llm.async_get_apis(hass)}
    for tool in instance.tools:  # the MCP server converts every schema like this
        assert probatio.to_openapi(tool.parameters, openapi_version="3.1.0")["type"] == "object"
    await hass.config_entries.async_unload(entry.entry_id)
    assert DOMAIN not in {a.id for a in llm.async_get_apis(hass)}


async def test_summary_today(api):
    _, instance = api
    data = await call(instance, "get_health_summary")
    assert data["date"] == "2026-11-06"
    assert data["activity"]["steps"] == 6100
    assert data["activity"]["active_calories_kcal"] == 400
    assert "note" in data["activity"]
    assert data["body_latest_weighing"]["weight_kg"] == 80.5
    assert data["sleep_latest_night"]["actual_sleep_h"] == 7.0
    assert data["sleep_latest_night"]["time_in_bed_h"] == 7.5
    assert data["recovery_latest_day"]["sleeping_heart_rate_bpm"] == 60
    assert data["workouts"][0]["type"] == "other" and data["workouts"][0]["exercise_type_code"] == 99999
    assert data["data_freshness"]["last_cloud_sync"] is not None


async def test_summary_past_day_uses_values_from_then(api):
    _, instance = api
    data = await call(instance, "get_health_summary", date="2026-11-03")
    assert data["activity"]["steps"] == 4000
    assert data["body_latest_weighing"]["weight_kg"] == 80.0
    assert data["sleep_latest_night"] is None  # first night recorded is later
    assert "note" not in data["activity"]


async def test_bad_date_is_reported_to_the_agent(api):
    _, instance = api
    data = await call(instance, "get_health_summary", date="yesterday")
    assert "2026-11-08" in data["error"]


async def test_history_daily_totals_per_week(api):
    _, instance = api
    data = await call(instance, "get_history", metric="steps", start="2026-11-01", end="2026-11-08", period="week")
    # week of Mon 2026-10-26 holds 11-01; week of Mon 11-02 holds 11-02, 11-03, 11-06
    rows = {r["period_start"]: r for r in data["rows"]}
    assert rows["2026-11-02"]["total"] == 8000 + 4000 + 6100
    assert rows["2026-11-02"]["days_with_data"] == 3
    assert data["kind"] == "daily total"


async def test_history_measurement_mean_min_max(api):
    _, instance = api
    data = await call(instance, "get_history", metric="weight", start="2026-11-01", end="2026-11-08", period="month")
    [row] = data["rows"]
    assert row == {"period_start": "2026-11-01", "mean": 80.5, "min": 80.0, "max": 81.0, "count": 3}


async def test_workouts_newest_first_with_heart_rate(api):
    _, instance = api
    data = await call(instance, "get_workouts", limit=2)
    assert [w["type"] for w in data["workouts"]] == ["other", "climbing"]
    climb = data["workouts"][1]
    assert climb["calories_kcal"] == 588
    assert climb["mean_heart_rate_bpm"] == 120 and climb["max_heart_rate_bpm"] == 160
    assert climb["duration_min"] == 120
    assert data["matching"] == 3
    ranged = await call(instance, "get_workouts", start="2026-11-01", end="2026-11-01")
    assert [w["type"] for w in ranged["workouts"]] == ["running"]


async def test_sync_now_waits_for_the_download(hass, api):
    entry, instance = api
    polls = FakeApi.polls
    data = await call(instance, "sync_now")
    assert data["synced"] is True
    assert FakeApi.polls == polls + 1


async def test_sync_now_joins_a_running_download(hass, api):
    entry, instance = api
    polls = FakeApi.polls
    FakeApi.gate = threading.Event()
    entry.runtime_data.async_sync_now()
    await _spin()
    task = hass.async_create_task(call(instance, "sync_now"))
    await _spin()
    assert not task.done()
    FakeApi.gate.set()
    data = await task
    assert data["synced"] is True
    assert FakeApi.polls == polls + 1  # one download, not two

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_samsung_health import exercise_api
from custom_components.ha_samsung_health.api import FetchResult, SamsungHealthAuthError
from custom_components.ha_samsung_health.const import (
    CONF_STORAGE_KEY,
    DOMAIN,
    MANIFEST_ACTIVITY,
    MANIFEST_EXERCISE,
    MANIFEST_SLEEP,
    MANIFEST_STEPS,
    MANIFEST_VITALITY,
    MANIFEST_WEIGHT,
    SIGNAL_EXERCISE_UPDATED,
)
from custom_components.ha_samsung_health.diagnostics import async_get_config_entry_diagnostics

from .helpers import CET, day_summary, exercise, sleep, step_trend, vitality, weight

NOW = datetime(2026, 11, 6, 11, 0, tzinfo=UTC)  # 12:00 in Stockholm

DOCS = {
    MANIFEST_ACTIVITY: [day_summary(2026, 11, 6, calorie=400, steps=6000)],
    MANIFEST_STEPS: [step_trend(2026, 11, 6, count=6100, calorie=300)],
    MANIFEST_EXERCISE: [exercise(datetime(2026, 11, 6, 6, 0, tzinfo=UTC), minutes=60, calorie=500, exercise_type=13002)],
    MANIFEST_WEIGHT: [weight(datetime(2026, 11, 6, 6, 8, tzinfo=UTC), kg=80.5, bmr=1700)],
    MANIFEST_SLEEP: [sleep(datetime(2026, 11, 5, 22, 0, tzinfo=UTC), datetime(2026, 11, 6, 5, 30, tzinfo=UTC),
                           minutes=420, score=81, offset=CET)],
    MANIFEST_VITALITY: [vitality(2026, 11, 6, shr=60, shrv=40, score=70.0)],
}


class FakeApi:
    error: Exception | None = None
    instances: list["FakeApi"] = []

    def __init__(self, paths, http=None):
        self.paths = paths
        self.closed = False
        FakeApi.instances.append(self)

    def initialize(self):
        if FakeApi.error:
            raise FakeApi.error

    def fetch(self, manifest, revision):
        return FetchResult(manifest, revision, DOCS[manifest], 0.1)

    def close(self):
        self.closed = True


@pytest.fixture
async def setup(hass, freezer):
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Stockholm")
    FakeApi.error = None
    FakeApi.instances = []
    written = []
    entry = MockConfigEntry(domain=DOMAIN, unique_id="u", data={CONF_STORAGE_KEY: "a" * 32})
    entry.add_to_hass(hass)
    with patch("custom_components.ha_samsung_health.coordinator.SamsungHealthApi", FakeApi), \
         patch("custom_components.ha_samsung_health.statistics.async_add_external_statistics",
               side_effect=lambda h, meta, rows: written.append((meta["statistic_id"], rows))):
        yield entry, written


async def _load(hass, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_creates_sensors(hass, setup):
    entry, _ = setup
    await _load(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    def state(name):
        return hass.states.get(f"sensor.samsung_health_{name}")

    assert state("steps").state == "6100"  # step_daily_trend wins
    assert float(state("active_calories").state) == 400
    assert float(state("exercise_calories").state) == 500
    assert float(state("weight").state) == 80.5
    assert state("weight").attributes["measured_at"].startswith("2026-11-06T07:08")
    assert float(state("sleeping_heart_rate").state) == 60
    assert float(state("sleep_score").state) == 81
    # BMR * 0.5 day + 0.819 * 400 + 4 * 0.5
    assert float(state("total_calories_estimated").state) == pytest.approx(1700 * 0.5 + 0.819 * 400 + 2, abs=1)
    assert "state_class" not in state("steps").attributes
    assert hass.states.get("binary_sensor.samsung_health_cloud_sync_stale").state == "off"


async def test_statistics_written_once_then_only_changes(hass, setup):
    entry, written = setup
    await _load(hass, entry)
    ids = {sid for sid, _ in written}
    assert {"ha_samsung_health:steps", "ha_samsung_health:weight", "ha_samsung_health:sleep_duration"} <= ids
    written.clear()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    # Same data: only today's total (time moved? no) -> nothing rewritten.
    assert written == []


async def test_auth_error_starts_reauth(hass, setup):
    entry, _ = setup
    FakeApi.error = SamsungHealthAuthError("authorize failed with HTTP 401")
    await _load(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"


async def test_exercise_api_and_signal(hass, setup):
    entry, _ = setup
    calls = []
    async_dispatcher_connect(hass, SIGNAL_EXERCISE_UPDATED, lambda: calls.append(1))
    await _load(hass, entry)
    assert calls == [1]
    assert exercise_api.is_available(hass)
    [session] = exercise_api.get_exercise_sessions(hass)
    assert session.exercise_type == 13002 and session.calories == 500
    assert exercise_api.get_exercise_sessions(hass, start=datetime(2026, 11, 7).date()) == []
    # unchanged sessions: no new signal
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert calls == [1]


async def test_diagnostics_have_no_values(hass, setup):
    entry, _ = setup
    await _load(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["counts"]["exercise_sessions"] == 1
    assert diag["fetch"][MANIFEST_WEIGHT]["documents"] == 1
    assert "80.5" not in str(diag)


async def test_unload_closes_client_and_remove_deletes_tokens(hass, setup):
    entry, _ = setup
    await _load(hass, entry)
    directory = FakeApi.instances[0].paths.directory
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "master.json").write_text("{}")
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert FakeApi.instances[0].closed
    assert not directory.exists()

import threading
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import State
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache_with_extra_data

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
from custom_components.ha_samsung_health.coordinator import Prefetched, prefetched_store
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
    polls = 0
    gate: threading.Event | None = None  # blocks the download until set
    instances: ClassVar[list["FakeApi"]] = []

    def __init__(self, paths, http=None):
        self.paths = paths
        self.closed = False
        FakeApi.instances.append(self)

    def initialize(self):
        FakeApi.polls += 1
        if FakeApi.error:
            raise FakeApi.error

    def fetch(self, manifest, revision):
        if FakeApi.gate is not None:
            FakeApi.gate.wait(5)
        return FetchResult(manifest, revision, DOCS[manifest], 0.1)

    def close(self):
        self.closed = True


@pytest.fixture
async def setup(hass, freezer):
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Stockholm")
    FakeApi.error = None
    FakeApi.instances = []
    FakeApi.polls = 0
    FakeApi.gate = None
    written = []
    entry = MockConfigEntry(domain=DOMAIN, unique_id="u", data={CONF_STORAGE_KEY: "a" * 32})
    entry.add_to_hass(hass)

    def record(h, meta, rows):
        written.append((meta["statistic_id"], rows))

    with patch("custom_components.ha_samsung_health.coordinator.SamsungHealthApi", FakeApi), \
         patch("custom_components.ha_samsung_health.statistics.async_add_external_statistics", side_effect=record), \
         patch("custom_components.ha_samsung_health.statistics.async_import_statistics", side_effect=record):
        yield entry, written


async def _load(hass, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    # the first download runs as a background task
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_does_not_wait_for_the_download(hass, setup):
    entry, _ = setup
    FakeApi.gate = threading.Event()
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED  # before any data arrived
    assert hass.states.get("sensor.samsung_health_weight").state == "unknown"
    FakeApi.gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert float(hass.states.get("sensor.samsung_health_weight").state) == 80.5


async def test_sync_now_button_downloads_once_at_a_time(hass, setup):
    entry, _ = setup
    await _load(hass, entry)
    polls = FakeApi.polls
    FakeApi.gate = threading.Event()
    for _ in range(2):  # second press while the first download runs: ignored
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.samsung_health_sync_now"}, blocking=True
        )
    assert entry.runtime_data.syncing
    FakeApi.gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert FakeApi.polls == polls + 1
    assert not entry.runtime_data.syncing


async def test_sensors_restore_until_the_download_finishes(hass, setup):
    entry, _ = setup
    mock_restore_cache_with_extra_data(hass, [
        (State("sensor.samsung_health_weight", "79.0", {"measured_at": "2026-11-01T07:00:00+01:00"}),
         {"native_value": 79.0, "native_unit_of_measurement": "kg"}),
        # "today" value from yesterday: not restored
        (State("sensor.samsung_health_steps", "9000", {"date": "2026-11-05"}),
         {"native_value": 9000, "native_unit_of_measurement": "steps"}),
    ])
    FakeApi.gate = threading.Event()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    weight = hass.states.get("sensor.samsung_health_weight")
    assert float(weight.state) == 79.0
    assert weight.attributes["measured_at"] == "2026-11-01T07:00:00+01:00"
    assert hass.states.get("sensor.samsung_health_steps").state == "unknown"
    FakeApi.gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert float(hass.states.get("sensor.samsung_health_weight").state) == 80.5


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
    assert "state_class" not in state("steps").attributes  # daily total: external statistics
    assert state("weight").attributes["state_class"] == "measurement"  # history on the entity
    assert float(state("sleep_duration").state) == 7.0
    assert state("sleep_duration").attributes["unit_of_measurement"] == "h"
    assert hass.states.get("binary_sensor.samsung_health_cloud_sync_stale").state == "off"


async def test_statistics_written_once_then_only_changes(hass, setup):
    entry, written = setup
    await _load(hass, entry)
    ids = {sid for sid, _ in written}
    assert {"ha_samsung_health:steps", "sensor.samsung_health_weight", "sensor.samsung_health_sleep_duration"} <= ids
    assert "ha_samsung_health:weight" not in ids
    written.clear()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    # Same data: only today's total (time moved? no) -> nothing rewritten.
    assert written == []


async def test_first_refresh_uses_download_from_config_flow(hass, setup):
    entry, _ = setup
    revisions = {m: 99 for m in DOCS}
    prefetched_store(hass)["a" * 32] = Prefetched(dict(DOCS), revisions, {MANIFEST_WEIGHT: (99, 1, 0.1)})
    await _load(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert FakeApi.instances == []  # nothing downloaded again
    assert float(hass.states.get("sensor.samsung_health_weight").state) == 80.5
    assert prefetched_store(hass) == {}
    # later polls download as usual, with the healed revisions
    await entry.runtime_data.async_refresh()
    assert len(FakeApi.instances) == 1
    assert entry.runtime_data.data.fetch_stats[MANIFEST_WEIGHT][0] == 99


async def test_interval_change_applies_without_reload_or_download(hass, setup):
    entry, _ = setup
    await _load(hass, entry)
    polls = FakeApi.polls
    coordinator = entry.runtime_data
    hass.config_entries.async_update_entry(entry, options={"scan_interval_minutes": 15})
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator  # not reloaded
    assert coordinator.update_interval.total_seconds() == 15 * 60
    assert FakeApi.polls == polls  # nothing downloaded
    assert hass.states.get("sensor.samsung_health_steps").state == "6100"  # never unavailable


async def test_auth_error_starts_reauth(hass, setup):
    entry, _ = setup
    FakeApi.error = SamsungHealthAuthError("authorize failed with HTTP 401")
    await _load(hass, entry)
    assert entry.state is ConfigEntryState.LOADED  # setup never waits for Samsung
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

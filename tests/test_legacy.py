"""Removing the pre-0.5.0 daily statistics: a repair, never automatic."""
from datetime import UTC, datetime

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics, get_metadata
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.ha_samsung_health import repairs
from custom_components.ha_samsung_health.const import DOMAIN
from custom_components.ha_samsung_health.sensor_history import SyncResult

from .test_init import _load

LEGACY = ["ha_samsung_health:steps", "ha_samsung_health:active_calories",
          "ha_samsung_health:total_calories", "ha_samsung_health:exercise_calories"]
IN_SYNC = {"steps": SyncResult(), "distance": SyncResult()}


async def _legacy(hass, ids=LEGACY):
    for sid in ids:
        meta = StatisticMetaData(mean_type=StatisticMeanType.NONE, has_sum=True, name=sid, source=DOMAIN,
                                 statistic_id=sid, unit_class=None, unit_of_measurement="kcal")
        async_add_external_statistics(hass, meta, [StatisticData(start=datetime(2026, 1, 1, 12, tzinfo=UTC),
                                                                 state=1, sum=1)])
    await async_wait_recording_done(hass)


async def _existing(hass) -> set[str]:
    meta = await get_instance(hass).async_add_executor_job(lambda: get_metadata(hass, statistic_ids=set(LEGACY)))
    return set(meta)


def _issue(hass):
    return ir.async_get(hass).async_get_issue(DOMAIN, repairs.ISSUE_ID)


async def test_issue_only_when_in_sync_and_legacy_exists(hass):
    await repairs.async_check_legacy(hass, IN_SYNC)
    assert _issue(hass) is None  # nothing to remove
    await _legacy(hass)
    await repairs.async_check_legacy(hass, {**IN_SYNC, "steps": SyncResult(adjustments=3)})
    assert _issue(hass) is None  # not verified yet: still changing
    await repairs.async_check_legacy(hass, {**IN_SYNC, "steps": SyncResult(waiting=True)})
    assert _issue(hass) is None
    await repairs.async_check_legacy(hass, {})
    assert _issue(hass) is None  # no sensors checked
    await repairs.async_check_legacy(hass, IN_SYNC)
    issue = _issue(hass)
    assert issue is not None and issue.is_fixable and issue.severity is ir.IssueSeverity.WARNING


async def test_fix_flow_removes_only_the_legacy_series(hass):
    await _legacy(hass)
    other = StatisticMetaData(mean_type=StatisticMeanType.NONE, has_sum=True, name="x", source=DOMAIN,
                              statistic_id="ha_samsung_health:something_else", unit_class=None,
                              unit_of_measurement="kcal")
    async_add_external_statistics(hass, other, [StatisticData(start=datetime(2026, 1, 1, 12, tzinfo=UTC),
                                                              state=1, sum=1)])
    await async_wait_recording_done(hass)
    await repairs.async_check_legacy(hass, IN_SYNC)
    flow = await repairs.async_create_fix_flow(hass, repairs.ISSUE_ID, None)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == "form" and result["step_id"] == "confirm"
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    await async_wait_recording_done(hass)
    assert await _existing(hass) == set()
    meta = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={"ha_samsung_health:something_else"}))
    assert meta  # untouched
    assert _issue(hass) is None


async def test_service_removes_the_legacy_series(hass, setup):
    entry, _ = setup
    await _load(hass, entry)
    await _legacy(hass)
    await hass.services.async_call(DOMAIN, "remove_legacy_statistics", {}, blocking=True)
    await async_wait_recording_done(hass)
    assert await _existing(hass) == set()

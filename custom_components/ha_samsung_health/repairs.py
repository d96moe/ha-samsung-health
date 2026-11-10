"""Removing the daily statistics of versions before 0.5.0 (ha_samsung_health:steps …).

Daily totals now live on the "today" sensors (daily_history.py), so the old
series are no longer updated. They are only removed when the user confirms the
repair (or calls the service): never automatically, and only after a poll found
every daily sensor's history already right (the move is verified).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .sensor_history import SyncResult

ISSUE_ID = "legacy_statistics"
LEGACY_IDS = [f"{DOMAIN}:{key}" for key in ("steps", "active_calories", "total_calories", "exercise_calories")]


async def _async_existing(hass: HomeAssistant) -> list[str]:
    meta = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids=set(LEGACY_IDS))
    )
    return sorted(meta)


async def async_check_legacy(hass: HomeAssistant, results: Mapping[str, SyncResult]) -> None:
    """Offer the removal once every daily sensor was checked and needed no change."""
    if not results or not all(r.in_sync for r in results.values()):
        return
    if not await _async_existing(hass):
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)
        return
    ir.async_create_issue(
        hass, DOMAIN, ISSUE_ID, is_fixable=True, is_persistent=False,
        severity=ir.IssueSeverity.WARNING, translation_key=ISSUE_ID,
    )


async def async_remove_legacy(hass: HomeAssistant) -> None:
    if existing := await _async_existing(hass):
        get_instance(hass).async_clear_statistics(existing)
        await get_instance(hass).async_block_till_done()
    ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)


async def async_handle_remove_service(call: ServiceCall) -> None:
    await async_remove_legacy(call.hass)


class LegacyStatisticsFlow(RepairsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        if user_input is not None:
            await async_remove_legacy(self.hass)
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    return LegacyStatisticsFlow()

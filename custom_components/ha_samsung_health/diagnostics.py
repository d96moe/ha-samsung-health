"""Diagnostics: counts, revisions and status only. No health values, ids or tokens."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import SamsungHealthConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SamsungHealthConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "last_update_success": coordinator.last_update_success,
        "last_exception": type(coordinator.last_exception).__name__ if coordinator.last_exception else None,
        "last_cloud_update": data.last_cloud_update.isoformat() if data and data.last_cloud_update else None,
        "fetch": {
            manifest: {"revision": rev, "documents": count, "seconds": seconds}
            for manifest, (rev, count, seconds) in (data.fetch_stats if data else {}).items()
        },
        "counts": {
            "days": len(data.days),
            "body_measurements": len(data.body),
            "sleep_sessions": len(data.sleep),
            "recovery_days": len(data.recovery),
            "exercise_sessions": len(data.exercises),
        } if data else None,
    }

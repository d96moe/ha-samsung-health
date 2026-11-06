"""Public in-process API for other integrations (ha-workouts' Samsung Health source).

Exercise sessions are already downloaded on every poll, so a second login or a
second download by another integration is unnecessary. Listen to
SIGNAL_EXERCISE_UPDATED (homeassistant.helpers.dispatcher) to learn when the set
of sessions has changed, then call get_exercise_sessions().
"""
from __future__ import annotations

from datetime import date

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, SIGNAL_EXERCISE_UPDATED
from .parsers import ExerciseSession

__all__ = ["SIGNAL_EXERCISE_UPDATED", "ExerciseSession", "get_exercise_sessions", "is_available"]


@callback
def is_available(hass: HomeAssistant) -> bool:
    return any(
        entry.runtime_data.data is not None
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
    )


@callback
def get_exercise_sessions(
    hass: HomeAssistant, start: date | None = None, end: date | None = None
) -> list[ExerciseSession]:
    """Sessions whose local start date is within [start, end] (inclusive)."""
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        data = entry.runtime_data.data
        if data is None:
            continue
        return [
            s for s in data.exercises
            if (start is None or s.start.date() >= start) and (end is None or s.start.date() <= end)
        ]
    return []

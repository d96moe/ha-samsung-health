"""Constants for the Samsung Health integration."""
from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "ha_samsung_health"

CONF_STORAGE_KEY: Final = "storage_key"
CONF_COUNTRY: Final = "country"
CONF_LOCALE: Final = "locale"
DEFAULT_COUNTRY: Final = "se"
DEFAULT_LOCALE: Final = "sv-SE"
# ISO 3166-1 alpha-3, used in the SCSP device profile.
DEFAULT_PROFILE_COUNTRY: Final = "SWE"

#: Each poll downloads the full history (~15-20 GETs), so polling is kept modest;
#: data is never fresher than the phone's own sync to Samsung Cloud anyway.
CONF_SCAN_INTERVAL: Final = "scan_interval_minutes"
SCAN_INTERVAL_OPTIONS: Final = (15, 30, 60, 120)
DEFAULT_SCAN_INTERVAL: Final = 60
# Cloud data older than this means the phone has stopped syncing to Samsung Cloud
# (seen in practice: the phone's cloud sync toggle was off for 10 days).
STALE_AFTER: Final = timedelta(hours=24)
# Days rewritten on every poll; older days are only rewritten when a gap is found.
STATS_REWRITE_DAYS: Final = 7

# Manifest id -> last known server schema revision (verified 2026-11-06). A stale
# value self-heals: Samsung's HTTP 400 body names the current revision.
MANIFEST_ACTIVITY: Final = "com.samsung.shealth.activity.day_summary"
MANIFEST_STEPS: Final = "com.samsung.shealth.step_daily_trend"
MANIFEST_EXERCISE: Final = "com.samsung.health.exercise"
MANIFEST_WEIGHT: Final = "com.samsung.health.weight"
MANIFEST_VITALITY: Final = "com.samsung.shealth.vitality_score"
# Sleep segments, one per continuous sleep. Not "shealth.sleep_combined": that only
# held nights Samsung had merged from several segments, and it stopped getting new
# nights on 2026-10-29 (verified 2026-11-08). The app shows a night's longest segment.
MANIFEST_SLEEP: Final = "com.samsung.health.sleep"
# Sleep stages per segment (sleep_id = the segment's datauuid). The app's "actual
# sleep time" is the segment's non-awake stage time (verified against two nights).
MANIFEST_SLEEP_STAGE: Final = "com.samsung.health.sleep_stage"
SLEEP_STAGE_AWAKE: Final = 40001

MANIFESTS: Final[dict[str, int]] = {
    MANIFEST_ACTIVITY: 5,
    MANIFEST_STEPS: 5,
    MANIFEST_EXERCISE: 36,
    MANIFEST_WEIGHT: 12,
    MANIFEST_VITALITY: 3,
    MANIFEST_SLEEP: 19,
    MANIFEST_SLEEP_STAGE: 6,
}

# Empirical fit against 29 days of final values from the Samsung Health app
# (2026-09-29..10-27): total ~= BMR + 0.82 * active + 4, residual sd 24 kcal,
# max 53 kcal. Samsung does not expose total kcal in its cloud data.
TOTAL_KCAL_ACTIVE_FACTOR: Final = 0.819
TOTAL_KCAL_OFFSET: Final = 4.0
BMR_MEDIAN_WINDOW: Final = 7

SIGNAL_EXERCISE_UPDATED: Final = f"{DOMAIN}_exercise_updated"

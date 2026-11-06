# samsung-re-health: library analysis for a Home Assistant integration

Analysis date: 2026-11-06, of `charlesbel/samsung-re-health` **0.7.2** (MIT). The analysis read the code only and ran nothing.

The goal is a HA custom integration that uses the library **unmodified** from PyPI and stores its data in HA's recorder, not in the library's SQLite mirror.

## Key findings

### 1. The cache-free call path works, but not through the public facade
- `SamsungHealthClient.__init__` always creates the SQLite cache (`client.py:35-38`, `cache.py:14-18`). **Don't use it.**
- Use the standalone building blocks instead. The wiring is in `client.py:311-322`.
  - `httpx.Client(timeout=30, follow_redirects=False)`
  - `StateStore(path)`
  - `HealthAuth(http)`
  - `ScspIdentity(http, profile=DeviceProfile(country_code="SWE"))`
  - `SamsungHealthService(store=, master_state_path=, health_auth=, scsp=)`, then `.initialize()`, which returns `HealthState`
  - `CloudDataClient(http, profile=)`, then `.iter_documents(manifest_id, schema_revision, cloud_authorization=state.cloud_token, page_size=1000, start_time, end_time)`
  - `load_bundled_manifests()` (catalog: manifest id and schema revision)
- These classes are **semi-internal**. They are not in the package `__all__` and have no documented stability promise.
  - Pin the version exactly: `samsung-re-health==0.7.2`. Don't use `>=0.7.2`, because 0.7.2 fixed stale schema revisions.
  - Fallback if the service changes: copy `service.py:48-128` (about 80 lines) and call `HealthAuth` and `ScspIdentity` directly.

### 2. Token lifecycle
- Hierarchy: master `userauth_token` (`master.json`, no expiry logic), then Health OAuth access token, then SCSP `registration_id` plus `cloud_token` (`"{tokenType} {accessToken}"`).
- All refresh happens in `SamsungHealthService._initialize_locked` (`service.py:52-128`), and **only when `initialize()` is called**. Call it at the start of every poll.
- **The refresh token is never used.** Every re-issue goes through the master token.
- **There is no 401 retry.** If a data GET returns 401, clear `cloud_token` / `cloud_expires_at` in the state, call `initialize()` again, and retry once.
- `state.json` is written atomically, mode 0600, up to 5 times per `initialize()`. The lock is a sidecar `.lock` file using flock.
- **Directory requirement (POSIX):** the parent directory must be owned by the current uid, mode **0700**, and not a symlink. `master.json` must be 0600.
  - `/config` (0755) fails, so use a dedicated per-entry directory.
- **The master state must be a file on disk.** The service has no in-memory path.
- **A revoked or expired master token raises no dedicated exception.** It comes through as `SamsungHealthCloudError("authorize failed with HTTP 4xx")`.
  - Parse the status from the message (`HTTP (\d{3})`) and map 400/401/403 on authorize/token to HA reauth.
  - Which status Samsung actually returns is **unknown** until tested with a real login.

### 3. Login bootstrap fits a HA config flow
- `AccountBootstrap(master_path=, pending_path=)` with **explicit paths**. Never pass `None`, because then env vars or defaults take over.
- `.start(country, locale)` returns the login URL and writes `pending.json`, which is valid for 15 minutes.
- `.complete(pasted_redirect_uri)` takes the **pasted string directly**. No handler or `capture_redirect` is needed.
  - It requires a `ms-app://s-1-15-2-…` URL with `state`, `code`, `auth_server_url` and `retValue` (query or fragment).
  - It writes `master.json` with a new generation, which clears the derived state.
- The `ms-app://` URL **can't be opened in a browser**. The user copies it from the address bar or dev tools.
  - Exactly how that looks in a real browser is **unverified**.
- Errors: `AuthenticationError` for an expired pending state or a bad URL; `SamsungHealthCloudError` for network or HTTP failures. Catch the base class.

### 4. Data model, and the most important gap
| Need | Manifest | Comment |
|---|---|---|
| Steps per day | `com.samsung.shealth.step_daily_trend` (rev 14) | `day_time` (UTC midnight), `count`, `distance`, `calorie`. **Only `source_type == -2`** (deduplicated across all sources); take the newest `update_time` per day |
| Exercise | `com.samsung.health.exercise` (rev 36) | `start_time`/`end_time`/`duration` (ms), `calorie`, `distance`, pulse. `exercise_type` is an int **with no mapping** |
| Sleep | `com.samsung.health.sleep` (+ `sleep_stage`) | intervals; stages 40001-40004 |
| Pulse | `com.samsung.health.heart_rate` | samples; **no resting heart rate anywhere** |
| Weight | `com.samsung.health.weight` | kg; latest by `start_time` |
| Daily summary? | `com.samsung.shealth.tracker.pedometer_day_summary` (rev 7) | **No semantics in the library.** Contents unknown |

- **⚠ The library provides neither active nor total kcal per day.** `step_daily_trend.calorie` is calories attributed to steps only. That was the main reason for this project, so it has to be verified with a real login:
  - Does `pedometer_day_summary` or another type carry active/total kcal?
  - As a fallback, active kcal could be summed from exercise sessions plus step calories. That would be an approximation.
- Documents arrive as **envelopes**; the data is in `env["data"]`. Skip records with `needToDecrypt: true` (E2EE; the library can't decrypt them).
- The library's analysis functions are **SQL-bound to the cache**. The aggregation rules have to be re-implemented on in-memory data:
  - Steps: `source_type -2`, newest `update_time` per day (`analytics.py:50`, `analysis_engine.py:638-648`).
  - Sleep: union of intervals from the previous day 12:00 to 18:00 local time (`analytics.py:81-97`).
  - Weight: newest `start_time` / `create_time` (`:176-180`).
  - Exercise: sum of `duration`, `distance` and `calorie` (`analysis_engine.py:736-739`).
- Document identity: `datauuid`, then `uuid`, then `id`, then sha256 of the canonical data (`cache.py:79-92`). Copy this rather than import it.

### 5. Time handling
- `start_time` / `end_time` are ms epoch. The server **may ignore them**, so always filter on the client side and budget for full downloads.
- Paging uses `nextPageToken` with a loop guard. `page_size` is 1-1000. The schema revision must come from the catalog, or the request fails with HTTP 400.
- `step_daily_trend.day_time` is UTC midnight. Read it as a **UTC date** rather than windowing it in local time.
- Exercise and sleep have `time_offset` (ms), which the library doesn't use.

### 6. Errors and rate limits
- In practice everything is raised as `SamsungHealthCloudError("<op> failed with HTTP <code>")` or as "network request failed".
  - `RateLimitError`, `NetworkError`, `DataError` and `StateError` are **never raised** on our code paths.
- The library has **no retry, no backoff and no `Retry-After` handling**. A 429 arrives as plain text.
- The integration does its own mapping:
  - network errors and 5xx: `UpdateFailed`
  - auth statuses on authorize/token: `ConfigEntryAuthFailed`
  - 404: the type is unavailable, so skip it
  - 429: back off

### 7. I/O and isolation
- **Everything is synchronous** (httpx plus blocking file I/O plus flock). Every call goes through `hass.async_add_executor_job`.
  - Create the `httpx.Client` in the executor and close it on unload.
  - Importing the package pulls in sqlite3 and platformdirs (no I/O at import), so import in an executor too.
- Serialise `initialize()` per config entry with an `asyncio.Lock`.
- **Env vars that leak between entries:** `SAMSUNG_ACCOUNT_MASTER_STATE`, `SAMSUNG_ACCOUNT_PENDING_PATH`, `SAMSUNG_HEALTH_*`, plus a legacy fallback to `samsung-find/state.json`.
  - **Always pass explicit, absolute per-entry paths. Never construct `HealthConfig()`.**
- The library has no `logging` and doesn't print secrets.

### 8. Dependencies
- Python >= 3.11, `cryptography>=43`, **`httpx>=0.27,<0.29`** (the biggest clash risk with HA core's pins; check `package_constraints.txt` first), `platformdirs>=4`, `tzdata`.
- Don't install the `mcp` extra.

### Privacy (HA logs and diagnostics)
- **Never log or put in diagnostics:**
  - tokens, `login_id`, `user_id`, `cdid`, `registration_id`, `master_generation`
  - `datauuid`, `deviceuuid`, URLs, `downloadUrls`, GPS, device ids
- Diagnostics show counts and statuses only, never health values.
- Reuse `serialization.redact_secrets` / `redact_raw_records`.

## Verified with a real login (2026-11-06)
- **Bug in 0.7.2: EU login fails in `complete()`.** The error is "Samsung returned an untrusted authentication server".
  - Samsung returns `auth_server_url` as a **bare host** (`eu-auth2.samsungosp.com`) with no scheme.
  - The domain is on the allowlist, but `_trusted_auth_server_url` (`account_auth.py:73-96`) requires `scheme == "https"`.
  - `api_server_url` in the callback has the same ciphertext, so it is the same host.
  - Workaround without a fork: wrap the module function to add `https://` before validation (see `spike/spike.py`). After that, `master.json` stores `https://…`, so `auth.py`'s check at `issue()` passes.
  - This fix is worth an upstream issue/PR, because it is one line of normalisation.
- After the workaround, `initialize()` takes 0.8 s and the cloud token is valid for **about 5 days**. No records needed decrypting (`needToDecrypt` = 0 everywhere).
- **The server ignores the time window completely.**
  - It ignores `start_time`/`end_time` and every variant tested (`startTime`, `since`, `updateTime`, `modifiedAfter`, `order`/`sort`, …). The result is identical to the baseline.
  - The order is roughly oldest first, but it is not strictly sorted.
  - Each page has these keys: `documents`, `hasNext`, `nextPageToken`, `pagingStartedAt`. `pagingStartedAt` might be a hook for an incremental cursor; that is untested.
  - **Every poll is therefore a full download.** Full history for the daily types took about 20 s and 20k documents in total:

    | Manifest | Docs | Time |
    |---|---|---|
    | `activity.day_summary` | 3386 | 3 s |
    | `pedometer_day_summary` | 7538 | 9–10 s |
    | `step_daily_trend` | 7526 | 2 s |
    | `exercise` | 1691 | 4 s |
    | `weight` | 69 | <1 s |
    | `vitality_score` | 441 | <1 s |
- **Many bundled schema revisions are stale or wrong.**
  - Affected: `step_daily_trend` (14, server says 5), `heart_rate`, `sleep_stage`, `stress`, `oxygen_saturation`, `blood_glucose`, `body_temperature`, `caffeine_intake`, `food_info`, `water_intake`.
  - Samsung's HTTP 400 body names the correct revision: `{"rcode":4001001,"rmsg":"The schemaRevision is invalid … (server revision: N)"}`. The integration should parse it and retry, so we don't depend on the catalog.
  - The library discards that body, so the integration needs its own GET or a raw-response hook.
- **Manifests outside the bundled catalog that exist:**

  | Manifest | Rev | Contents |
  |---|---|---|
  | `com.samsung.shealth.activity.day_summary` | 5 | `calorie`, `active_time` (ms), `step_count`, `distance`, `walk_time`/`run_time`, `score`; 1 doc per day |
  | `com.samsung.shealth.vitality_score` | 3 | daily: `shr_value` (sleeping HR, a candidate for resting HR), `shrv_value` (HRV), sleep/activity scores |
  | `com.samsung.shealth.exercise.recovery_heart_rate` | 1 | — |
  | `com.samsung.health.floors_climbed` | 6 | — |
  | `com.samsung.shealth.stand_day_summary` | 1 | empty |

  - `calories_burned.details` and `tracker.heart_rate` return 404 ("Content not found"). `activity.goal` returns 400 with "server revision: 0".
- **Verified against the Samsung Health app's final daily values** (29 consecutive days, read in the app):
  - **Active kcal = `activity.day_summary.calorie`.** It matches the app's "activity" kcal exactly (to the whole kcal). It equals step kcal on days without exercise, and is lower than steps plus exercise on exercise days (no double counting).
  - **Steps** = `step_daily_trend` (rev 5), `source_type -2`. It matches exactly on every day.
  - **Exercise kcal per session** = `exercise.calorie`. It matches exactly.
  - **Total kcal is not in the cloud data.** Empirically, `total ≈ BMR + 0.82 × active + 4`, where BMR is `weight.basal_metabolic_rate` (median of the last 7 weighings). The fit is r = 0.997 with a residual sd of about 24 kcal and a maximum error of about 54 kcal. `BMR + active` overestimates by about 130 kcal on average.
    - The per-weighing BMR swings by about ±3 % with the watch's BIA reading. Samsung probably uses a steadier BMR, hence the rolling median.
  - `exercise_type` 13002 = climbing (bouldering), 1001 = walking. Both are verified against the app.
  - `com.samsung.shealth.calories_burned.details` does not exist on the server (404).
- **Weight and body composition:** `com.samsung.health.weight` has one doc per weighing: `weight`, `body_fat` (%), `body_fat_mass`, `skeletal_muscle_mass`, `total_body_water`, `fat_free_mass`, `basal_metabolic_rate`, `height`. The values match the app.
- **Sleep:** `sleep_combined.sleep_duration` is in **minutes**, and `time_offset` is the recording's UTC offset in ms. `vitality_score.sleep_duration` is in ms.
- **Freshness depends on the phone's cloud sync.** In the test, the cloud data was ten days old because Samsung Cloud sync was switched off on the phone. Once it was switched on, the data up to the same day arrived within minutes. Sleep data appeared to sync later than the other types.
- `pedometer_day_summary` has 3 docs per day (all `com.sec.android.app.shealth`), and the newest one does not always match `step_daily_trend`. Use `step_daily_trend` for steps.
- The callback parameters come in the **query** (`?code=…&api_server_url=…&auth_server_url=…&retValue=…&code_expires_in=300&state=…&status=new`). The `code` lives 300 s.

## Open questions
Answered by the real-login test:
- Daily active kcal: **yes** (`activity.day_summary`). Total kcal: **no**, it is estimated. Resting HR: no, but sleeping HR is available (`vitality_score.shr_value`).
- The server **ignores** `start_time` / `end_time` for every manifest tested.
- The callback parameters come in the query.
- `needToDecrypt` did not occur (E2EE off on the test account; requesting `x-sc-e2ee-state: ON` returns HTTP 400).
- HA core's pins accept the library (httpx 0.28.1, cryptography 48–50).

Still open:
1. Which HTTP status a revoked master token, or an expired cloud token, produces (400, 401 or 403). The integration treats all three as "sign in again" on authorize/token calls.
2. 429 / `Retry-After` behaviour and real limits.
3. Whether re-issuing from the master token on every expiry triggers throttling or security emails from Samsung.
4. Whether reusing `device_id` avoids duplicate "devices" in the Samsung account.
5. The remaining `exercise_type` codes (only 1001 and 13002 are verified).
6. Whether the partial-day total-kcal estimate (BMR scaled by the elapsed part of the day) matches what the app shows during the day.

## Files to read (in order)
`service.py`, `data.py`, `account_auth.py` (+ `capture_redirect.py`, `constants.py`), `credentials.py`, `state.py`, `file_lock.py`, `auth.py`, `scsp.py`, `exceptions.py`, `analytics.py` / `analysis_engine.py` / `semantics.py`, `cache.py:79-92`, `filters.py`, `models.py`, `catalog.py`, `resources/manifests.json`, `serialization.py`, `docs/authentication.md`, `docs/privacy.md`, `tests/test_data.py`, `tests/test_sync.py`.

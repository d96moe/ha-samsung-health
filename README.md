# Samsung Health for Home Assistant

A custom integration that brings your **Samsung Health** data into Home Assistant:
steps, active calories, body composition, sleep and recovery. It includes your full
history as long-term statistics.

Samsung Health writes incomplete data to Android's Health Connect: active calories
are missing entirely and the totals are off. So instead of Health Connect, this
integration reads the same cloud sync that the Samsung Health app itself uses, through
[samsung-re-health](https://github.com/charlesbel/samsung-re-health).

> **Unofficial.** Samsung's sync protocol is private and was reverse-engineered by
> samsung-re-health. Samsung can change or block it at any time, and using it may be
> against Samsung's terms. Access is read-only: nothing is ever written to your
> Samsung account.

## What you get

| Group | Sensors | Value shown |
|---|---|---|
| Activity | Steps, Active calories, Total calories (estimated), Exercise calories, Active time, Distance | Today so far |
| Body | Weight, Body fat, Body fat mass, Skeletal muscle mass, Body water, BMR | Latest weighing |
| Sleep | Sleep duration, Sleep score, Bedtime, Wake-up time | Last night |
| Recovery | Sleeping heart rate, Sleeping HRV, Energy score | Latest day |
| Status | Last cloud sync, Cloud sync stale (problem) | — |

- **Exact values.** Steps, active calories and per-workout calories match the Samsung
  Health app exactly. This was checked against the app's final daily values over 29 days.
- **Estimated total calories.** Samsung doesn't store total calories in its cloud data.
  The integration estimates it as `BMR + 0.82 × active calories`, using the median BMR
  of your last 7 weighings. Over those 29 days it was within ±54 kcal of the app, with
  a typical error of ±25 kcal.

**History.** Every value is also written as an external statistic
(`ha_samsung_health:steps`, `ha_samsung_health:weight`, …), going back as far as your
Samsung Health data does. Use them in Statistics Graph or Statistic cards. The sensors
have no `state_class` on purpose: the statistics carry the history, including the
years before you installed the integration.

## Requirements

- **Cloud sync on your phone.** Samsung Health must be syncing to Samsung Cloud. Check
  Samsung Health → Settings → *Sync with Samsung Cloud*. If it's off, this integration
  sees nothing new; the *Cloud sync stale* sensor turns on after 24 hours without new
  data.
- **Home Assistant 2025.12 or newer.**

## Installation

1. In Home Assistant, open Marketplace (formerly HACS) and add this repository as a
   custom repository (type *Integration*).
2. Install **Samsung Health** and restart Home Assistant.
3. Go to Settings → Devices & services → Add integration → **Samsung Health**.

## Signing in

1. Open the sign-in link that the setup dialog shows, and sign in to your Samsung
   account.
2. At the end, the browser tries to open an address starting with `ms-app://`. That is
   the Windows Samsung Health app's callback, so the browser shows an error or a blank
   page. **Copy that whole address.**
   - If the address bar doesn't show it, open the browser's developer tools (F12)
     before you sign in, and copy the address from the Network or Console tab.
3. Paste it into the setup dialog within 15 minutes.

The sign-in is stored under `/config/.storage/ha_samsung_health/` and is refreshed
automatically. If Samsung stops accepting it, Home Assistant asks you to sign in again.

## Workouts in ha-workouts

Individual workouts (type, duration, Samsung's calories, heart rate) are available to
the [ha-workouts](https://github.com/d96moe/ha-workouts) fork as its *Samsung Health*
source:

- Set up this integration first, then add the source in ha-workouts.
- No second sign-in is needed: ha-workouts reads the sessions this integration has
  already downloaded.

## How it works and its limits

- **Polling.** The integration polls every 30 minutes.
- **Full download on every poll.** Samsung's server ignores date filters, so each poll
  downloads your full history for six data types: about 13,000 records and roughly
  10 seconds for ten years of data.
- **Freshness.** Data is only as fresh as the phone's sync to Samsung Cloud.
- **Library workarounds.** Two problems in samsung-re-health 0.7.2 are worked around
  without modifying it:
  - EU sign-in fails because Samsung returns its sign-in server without `https://`.
  - Some data types have outdated schema revisions. Samsung's error reply names the
    current revision, and the integration retries with it.
- **Diagnostics.** The diagnostics download contains only counts and statuses: no
  health values, ids or tokens.

## Development

```
python -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest tests
```

The Home Assistant test plugin needs Linux. See [ci/README.md](ci/README.md).

## License

MIT

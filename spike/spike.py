"""Spike: answer the open questions in docs/library-analysis.md with a real login.

Read-only (GET only). By default it prints STRUCTURE only (field names, types,
fill ratio, counts), never health values. Use --kcal to opt in to per-day sums of
calorie-like fields, for comparison with the Samsung Health app.

Run it in your own terminal (login is interactive):

    .venv\\Scripts\\python spike\\spike.py login
    .venv\\Scripts\\python spike\\spike.py probe            # 22 bundled manifests
    .venv\\Scripts\\python spike\\spike.py probe-extra      # candidate manifests (calories etc.)
    .venv\\Scripts\\python spike\\spike.py probe --kcal     # + per-day kcal sums

Tokens live in spike/.state/ (gitignored). Results are written to spike/out/.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from samsung_health_cloud import account_auth
from samsung_health_cloud.account_auth import AccountBootstrap
from samsung_health_cloud.auth import HealthAuth
from samsung_health_cloud.catalog import load_bundled_manifests
from samsung_health_cloud.data import DATA_URL, CloudDataClient
from samsung_health_cloud.exceptions import SamsungHealthCloudError
from samsung_health_cloud.models import DeviceProfile
from samsung_health_cloud.scsp import APP_VERSION, ScspIdentity
from samsung_health_cloud.service import SamsungHealthService
from samsung_health_cloud.state import StateStore

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
OUT_DIR = ROOT / "out"
MASTER = STATE_DIR / "master.json"
PENDING = STATE_DIR / "pending.json"
STATE = STATE_DIR / "state.json"
PROFILE = DeviceProfile(country_code="SWE")

# Unverified guesses, taken from file names in Samsung Health's "Download personal data"
# CSV export. Not in the library's bundled catalog, so the schema revision is unknown.
CANDIDATES = [
    "com.samsung.shealth.calories_burned.details",
    "com.samsung.shealth.activity.day_summary",
    "com.samsung.shealth.activity.goal",
    "com.samsung.shealth.tracker.heart_rate",
    "com.samsung.shealth.exercise.recovery_heart_rate",
    "com.samsung.shealth.stand_day_summary",
    "com.samsung.health.floors_climbed",
    "com.samsung.shealth.vitality_score",
]

# Small integer enums whose value distribution is safe to print (no health values).
SMALL_ENUM_KEYS = ("source_type", "exercise_type", "stage")

# Workaround for samsung-re-health 0.7.2: Samsung returns auth_server_url as a bare
# host for EU accounts ("eu-auth2.samsungosp.com", verified 2026-11-06), but
# _trusted_auth_server_url() requires scheme https and rejects it. Add the scheme
# before validation; the host allowlist check still applies. complete() looks the
# function up as a module global at call time, so patching the module attribute works.
_orig_trusted = account_auth._trusted_auth_server_url
account_auth._trusted_auth_server_url = lambda v: _orig_trusted(
    v if "://" in v else f"https://{v}"
)


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=False)


# --------------------------------------------------------------------------- login
def cmd_login(args: argparse.Namespace) -> None:
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    with AccountBootstrap(master_path=MASTER, pending_path=PENDING) as boot:
        url = boot.start(country=args.country, locale=args.locale)
        print("\n1. Open this URL in a browser and sign in to your Samsung account:\n")
        print(url)
        print(
            "\n2. After sign-in the browser tries to open an ms-app://... URL and fails."
            "\n   Copy that whole URL (address bar, or DevTools > Network/Console if the"
            "\n   address bar doesn't show it). Note HOW you got it - that is open question 5."
            "\n   The pending login expires after 15 minutes.\n"
        )
        redirect = input("Paste the ms-app:// URL here: ").strip()
        where = "fragment" if "#" in redirect else "query"
        result = boot.complete(redirect)
    print(f"\nOK. Callback parameters came in the URL {where}. Status: {result}")


# --------------------------------------------------------------------------- session
def _session(http: httpx.Client) -> tuple[str, CloudDataClient]:
    service = SamsungHealthService(
        store=StateStore(STATE),
        master_state_path=MASTER,
        health_auth=HealthAuth(http),
        scsp=ScspIdentity(http, profile=PROFILE),
    )
    t0 = time.monotonic()
    state = service.initialize()
    print(f"initialize() OK in {time.monotonic() - t0:.1f}s, cloud token expires "
          f"{datetime.fromtimestamp(state.cloud_expires_at, UTC):%Y-%m-%d %H:%M} UTC")
    return state.cloud_token, CloudDataClient(http, profile=PROFILE)


# --------------------------------------------------------------------------- structure
def _type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    return "dict"


def _doc_time_ms(data: dict) -> int | None:
    for k in ("day_time", "start_time", "create_time"):
        v = data.get(k)
        if isinstance(v, int | float) and v > 0:
            return int(v)
    return None


def summarise(manifest: str, docs: list[dict], window: tuple[int, int] | None,
              want_kcal: bool) -> dict:
    env_keys: Counter = Counter()
    fields: dict[str, Counter] = defaultdict(Counter)
    filled: Counter = Counter()
    enums: dict[str, Counter] = defaultdict(Counter)
    encrypted = 0
    outside = 0
    times: list[int] = []
    kcal: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for env in docs:
        env_keys.update(env.keys())
        if env.get("needToDecrypt") is True:
            encrypted += 1
            continue
        data = env.get("data") if isinstance(env.get("data"), dict) else {}
        for k, v in data.items():
            fields[k][_type(v)] += 1
            if v not in (None, "", 0, [], {}):
                filled[k] += 1
            if k in SMALL_ENUM_KEYS and isinstance(v, int):
                enums[k][v] += 1
        t = _doc_time_ms(data)
        if t is not None:
            times.append(t)
            if window and not (window[0] <= t <= window[1]):
                outside += 1
        if want_kcal and t is not None:
            day = datetime.fromtimestamp(t / 1000, UTC).date().isoformat()
            if manifest == "com.samsung.shealth.step_daily_trend" and data.get("source_type") != -2:
                continue
            for k, v in data.items():
                if "calor" in k.lower() and isinstance(v, int | float):
                    kcal[k][day] += v

    n = len(docs)
    out: dict[str, Any] = {
        "manifest": manifest,
        "docs": n,
        "encrypted_needToDecrypt": encrypted,
        "envelope_keys": sorted(env_keys),
        "fields": {
            k: {"types": dict(c), "filled_pct": round(100 * filled[k] / max(1, n - encrypted))}
            for k, c in sorted(fields.items())
        },
        "enum_counts": {k: dict(c) for k, c in enums.items()},
    }
    if times:
        out["time_range_utc"] = [
            datetime.fromtimestamp(min(times) / 1000, UTC).isoformat(timespec="minutes"),
            datetime.fromtimestamp(max(times) / 1000, UTC).isoformat(timespec="minutes"),
        ]
    if window:
        out["docs_outside_requested_window"] = outside
    if want_kcal and kcal:
        out["kcal_per_day_utc"] = {k: dict(sorted(d.items())) for k, d in kcal.items()}
    return out


def _fetch(data: CloudDataClient, token: str, manifest: str, rev: int,
           window: tuple[int, int] | None, max_docs: int) -> list[dict]:
    docs: list[dict] = []
    for doc in data.iter_documents(
        manifest_id=manifest, schema_revision=rev, cloud_authorization=token,
        page_size=min(1000, max_docs),
        start_time=window[0] if window else None,
        end_time=window[1] if window else None,
    ):
        docs.append(doc)
        if len(docs) >= max_docs:
            break
    return docs


def _print_summary(s: dict) -> None:
    print(f"\n== {s['manifest']}  docs={s['docs']}  encrypted={s['encrypted_needToDecrypt']}"
          f"  outside_window={s.get('docs_outside_requested_window', '-')}"
          f"  range={s.get('time_range_utc', '-')}")
    for k, f in s["fields"].items():
        print(f"   {k:<32} {','.join(f['types']):<12} {f['filled_pct']:>3}% filled")
    for k, c in s["enum_counts"].items():
        print(f"   [{k}] value counts: {c}")
    for k, d in s.get("kcal_per_day_utc", {}).items():
        print(f"   kcal {k}: " + ", ".join(f"{day}={v:.0f}" for day, v in d.items()))


def _window(days: int) -> tuple[int, int]:
    now = datetime.now(UTC)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000), int(now.timestamp() * 1000)


def _status(exc: Exception) -> str:
    m = re.search(r"HTTP (\d{3})", str(exc))
    return m.group(1) if m else type(exc).__name__


_SERVER_REV = re.compile(r"server revision: (\d+)")


def _server_revision(http: httpx.Client, token: str, manifest: str, rev: int) -> int | None:
    """Samsung answers a stale schemaRevision with HTTP 400 and names the right one."""
    r = _raw_get(http, token, manifest, rev)
    if r.status_code == 200:
        return rev
    m = _SERVER_REV.search(r.text)
    return int(m.group(1)) if m else None


def _fetch_auto(http: httpx.Client, data: CloudDataClient, token: str, manifest: str,
                rev: int, window: tuple[int, int] | None, max_docs: int) -> tuple[list[dict], int]:
    try:
        return _fetch(data, token, manifest, rev, window, max_docs), rev
    except SamsungHealthCloudError as exc:
        if _status(exc) != "400":
            raise
        fixed = _server_revision(http, token, manifest, rev)
        if fixed is None or fixed == rev:
            raise
        print(f"   ({manifest}: bundled rev {rev} stale, server rev {fixed})")
        return _fetch(data, token, manifest, fixed, window, max_docs), fixed


def cmd_probe(args: argparse.Namespace) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    window = None if args.no_window else _window(args.days)
    results = []
    with _client() as http:
        token, data = _session(http)
        manifests = load_bundled_manifests()
        if args.only:
            manifests = [m for m in manifests if args.only in m.manifest_id]
        for m in manifests:
            t0 = time.monotonic()
            try:
                docs, rev = _fetch_auto(http, data, token, m.manifest_id, m.schema_revision,
                                        window, args.max_docs)
            except SamsungHealthCloudError as exc:
                print(f"\n== {m.manifest_id}  ERROR HTTP/{_status(exc)}: {exc}")
                results.append({"manifest": m.manifest_id, "error": str(exc)})
                continue
            s = summarise(m.manifest_id, docs, window, args.kcal)
            s["revision"] = rev
            s["seconds"] = round(time.monotonic() - t0, 1)
            s["hit_max_docs"] = len(docs) >= args.max_docs
            _print_summary(s)
            results.append(s)
    path = OUT_DIR / f"probe-{datetime.now():%Y%m%d-%H%M}.json"
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {path}")


# --------------------------------------------------------------------------- extra
def _raw_get(http: httpx.Client, token: str, manifest: str, rev: int,
             limit: int = 1, extra: dict[str, str] | None = None) -> httpx.Response:
    return http.get(
        f"{DATA_URL}/data/v3/{manifest}/documents",
        params={"schemaRevision": str(rev), "limit": str(limit), **(extra or {})},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"{PROFILE.model_name}; {PROFILE.client_tag}; "
                          f"com.sec.android.app.shealth={APP_VERSION}",
            "authorization": token,
            "x-sc-e2ee-state": "OFF",
        },
    )


def cmd_probe_extra(args: argparse.Namespace) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    window = _window(args.days)
    results = []
    names = args.manifest or CANDIDATES
    with _client() as http:
        token, data = _session(http)
        for name in names:
            print(f"\n-- {name}")
            seen: set[tuple[int, str]] = set()
            found = None
            for rev in range(args.rev_min, args.rev_max + 1):
                r = _raw_get(http, token, name, rev)
                body = r.text[:240].replace("\n", " ")
                if r.status_code == 200:
                    found = rev
                    print(f"   rev {rev}: 200 OK")
                    break
                if (r.status_code, body) not in seen:
                    seen.add((r.status_code, body))
                    print(f"   rev {rev}: HTTP {r.status_code}  {body}")
                if r.status_code == 429:
                    print("   rate limited, stopping"); break
                time.sleep(0.3)
            entry: dict[str, Any] = {"manifest": name, "revision": found,
                                     "errors": [list(x) for x in seen]}
            if found is not None:
                docs = _fetch(data, token, name, found, window, args.max_docs)
                s = summarise(name, docs, window, args.kcal)
                _print_summary(s)
                entry.update(s)
            results.append(entry)
    path = OUT_DIR / f"probe-extra-{datetime.now():%Y%m%d-%H%M}.json"
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {path}")


# --------------------------------------------------------------------------- kcal
# Full history (the server ignores start/end_time), then only the last N days are shown.
DAILY = [
    ("com.samsung.shealth.activity.day_summary", 5),
    ("com.samsung.shealth.tracker.pedometer_day_summary", 7),
    ("com.samsung.shealth.step_daily_trend", 14),
    ("com.samsung.health.exercise", 36),
    ("com.samsung.health.weight", 12),
    ("com.samsung.shealth.vitality_score", 3),
]


def _num(v: Any) -> str:
    return f"{v:.0f}" if isinstance(v, int | float) else "-"


def _newest(recs: list[dict]) -> dict:
    return max(recs, key=lambda d: d.get("update_time") or 0)


def _cell(manifest: str, recs: list[dict]) -> str:
    if not recs:
        return "-"
    if manifest.endswith("step_daily_trend"):
        recs = [d for d in recs if d.get("source_type") == -2]
        if not recs:
            return "no source_type -2"
        n = _newest(recs)
        return f"kcal={_num(n.get('calorie'))} steps={_num(n.get('count'))}"
    if manifest.endswith("exercise"):
        cal = sum(d.get("calorie") or 0 for d in recs)
        tot = sum(d.get("total_calorie") or 0 for d in recs)
        types = Counter(d.get("exercise_type") for d in recs)
        return f"sessions={len(recs)} kcal={cal:.0f} total_calorie={tot:.0f} types={dict(types)}"
    if manifest.endswith("weight"):
        return f"bmr={_num(_newest(recs).get('basal_metabolic_rate'))}"
    if manifest.endswith("vitality_score"):
        n = _newest(recs)
        return f"shr={_num(n.get('shr_value'))} shrv={_num(n.get('shrv_value'))} max_hr={_num(n.get('max_hr'))}"
    # day summaries
    n = _newest(recs)
    pkgs = Counter(d.get("source_package_name") or d.get("pkg_name") for d in recs)
    max_cal = max((d.get("calorie") or 0) for d in recs)
    return (f"docs={len(recs)} newest: kcal={_num(n.get('calorie'))} steps={_num(n.get('step_count'))} "
            f"active_time={_num(n.get('active_time'))} | max kcal={max_cal:.0f} pkgs={dict(pkgs)}")


def _record_day(d: dict) -> Any:
    if isinstance(d.get("day_time"), int | float):  # UTC midnight
        return datetime.fromtimestamp(d["day_time"] / 1000, UTC).date()
    if isinstance(d.get("start_time"), int | float):  # local day via time_offset
        off = d.get("time_offset") if isinstance(d.get("time_offset"), int | float) else 0
        return datetime.fromtimestamp((d["start_time"] + off) / 1000, UTC).date()
    return None


def cmd_kcal(args: argparse.Namespace) -> None:
    today = datetime.now().date()
    days = [today - timedelta(days=i) for i in range(args.days)]
    rows: dict[Any, dict[str, str]] = defaultdict(dict)
    cost = []
    with _client() as http:
        token, data = _session(http)
        for manifest, rev in DAILY:
            t0 = time.monotonic()
            try:
                docs, rev = _fetch_auto(http, data, token, manifest, rev, None, args.max_docs)
            except SamsungHealthCloudError as exc:
                print(f"{manifest}: ERROR {exc}")
                continue
            per_day: dict[Any, list[dict]] = defaultdict(list)
            latest = None
            for env in docs:
                if env.get("needToDecrypt") is True or not isinstance(env.get("data"), dict):
                    continue
                day = _record_day(env["data"])
                if day is not None and (latest is None or day > latest):
                    latest = day
                if day in days:
                    per_day[day].append(env["data"])
            cost.append((manifest, rev, len(docs), time.monotonic() - t0, latest))
            for day in days:
                rows[day][manifest.rsplit(".", 1)[-1]] = _cell(manifest, per_day.get(day, []))
    print("\nFull-history download cost (= poll cost if nothing incremental exists):")
    for manifest, rev, n, sec, latest in cost:
        print(f"   {manifest:<52} rev={rev:<3} docs={n:>6} {sec:5.1f}s  latest day={latest}")
    for day in days:
        print(f"\n{day}")
        for col, cell in rows[day].items():
            print(f"   {col:<22} {cell}")


# --------------------------------------------------------------------------- filter-test
def cmd_filter_test(args: argparse.Namespace) -> None:
    """Does any parameter make the server filter or reorder? Structure/dates only."""
    manifest, rev = args.manifest, args.rev
    since = str(int((datetime.now(UTC) - timedelta(days=30)).timestamp() * 1000))
    variants = [
        ("baseline", {}),
        ("start_time", {"start_time": since}),
        ("startTime", {"startTime": since}),
        ("start", {"start": since}),
        ("since", {"since": since}),
        ("updateTime", {"updateTime": since}),
        ("update_time", {"update_time": since}),
        ("modifiedAfter", {"modifiedAfter": since}),
        ("fromTime", {"fromTime": since}),
        ("order=desc", {"order": "desc"}),
        ("sort=desc", {"sort": "desc"}),
        ("sortOrder=DESC", {"sortOrder": "DESC"}),
    ]

    def d(ms: Any) -> str:
        return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d") if isinstance(ms, int | float) else "-"

    with _client() as http:
        token, _ = _session(http)
        print(f"\n{manifest} rev {rev}, limit=50, 'since' = 30 days ago\n")
        for name, extra in variants:
            r = _raw_get(http, token, manifest, rev, limit=50, extra=extra)
            if r.status_code != 200:
                print(f"   {name:<16} HTTP {r.status_code} {r.text[:150]}")
                continue
            page = r.json()
            docs = [x.get("data") or {} for x in page.get("documents", [])]
            st = [x.get("start_time") for x in docs]
            ut = [x.get("update_time") for x in docs]
            asc = lambda xs: all(a is not None and b is not None and a <= b for a, b in zip(xs, xs[1:]))
            print(f"   {name:<16} n={len(docs):<3} start {d(st[0] if st else None)}..{d(st[-1] if st else None)}"
                  f" asc_start={asc(st)} asc_update={asc(ut)} update {d(min(filter(None, ut), default=None))}"
                  f"..{d(max(filter(None, ut), default=None))}  page_keys={sorted(page)}")
            time.sleep(0.3)


# --------------------------------------------------------------------------- main
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("login")
    lg.add_argument("--country", default="se")
    lg.add_argument("--locale", default="sv-SE")
    lg.set_defaults(func=cmd_login)

    for name, func in (("probe", cmd_probe), ("probe-extra", cmd_probe_extra)):
        sp = sub.add_parser(name)
        sp.add_argument("--days", type=int, default=3, help="time window to request (default 3)")
        sp.add_argument("--max-docs", type=int, default=500, help="stop per manifest after N docs")
        sp.add_argument("--kcal", action="store_true", help="opt-in: print per-day kcal sums")
        sp.set_defaults(func=func)
        if name == "probe":
            sp.add_argument("--no-window", action="store_true",
                            help="don't send start/end_time (tests open question 3)")
            sp.add_argument("--only", help="substring filter on manifest id")
        else:
            sp.add_argument("--manifest", action="append", help="probe this id (repeatable)")
            sp.add_argument("--rev-min", type=int, default=1)
            sp.add_argument("--rev-max", type=int, default=15)

    kc = sub.add_parser("kcal", help="opt-in VALUES: last N days of daily kcal/steps/BMR/sleep HR")
    kc.add_argument("--days", type=int, default=7)
    kc.add_argument("--max-docs", type=int, default=100_000)
    kc.set_defaults(func=cmd_kcal)

    ft = sub.add_parser("filter-test", help="probe filter/order params (dates only)")
    ft.add_argument("--manifest", default="com.samsung.health.exercise")
    ft.add_argument("--rev", type=int, default=36)
    ft.set_defaults(func=cmd_filter_test)

    args = p.parse_args()
    if args.cmd != "login" and not MASTER.exists():
        sys.exit("No master.json - run `login` first.")
    try:
        args.func(args)
    except SamsungHealthCloudError as exc:
        sys.exit(f"Samsung error (HTTP/{_status(exc)}): {exc}")


if __name__ == "__main__":
    main()

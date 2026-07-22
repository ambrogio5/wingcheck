"""
kitesailing_weather.py - live weather reading from the dedicated "Weather &
Watersports" page at
https://www.kitesailing.ch/en/spot/weather-watersports.

There is no documented public API and no client-visible JSON endpoint for
this widget (confirmed by inspecting the page's network traffic and DOM: no
XHR/fetch call, no iframe, no data-* attributes - it's rendered fully
server-side by kitesailing.ch, and the "livemeteo.ch" vendor name found in
its CSS classes doesn't even resolve as a domain). So this scrapes the
rendered HTML instead, matching the widget's own text labels
(German: "Windspitzen" = gust, "Mittelwind" = average wind, "Windrichtung" =
direction, "Feuchtigkeit" = humidity, "Luftdruck" = pressure).

This needs a real browser (Playwright + Chromium), unlike the rest of the
pipeline which only depends on `requests` - that's a much heavier
dependency, so this is kept standalone rather than wired into
forecast_and_log.py / backtest.py. To use it:

    pip install playwright
    playwright install --with-deps chromium
    python kitesailing_weather.py

Two readings taken ~20 minutes apart during discovery (12:10 -> 20.5°C,
12:30 -> 20.9°C) suggest the underlying station/cache refreshes roughly
every 10-20 minutes.

Sampled every 15 minutes by the local collector, 05:00-21:45 Europe/Zurich
(the widget itself has no notion of a "collection window" - this module's own
is_within_collection_window() is what the scheduled workflow checks before
actually attempting a fetch, since GitHub Actions cron is UTC-only and the
CET/CEST offset shifts twice a year). The 12:00-18:30 window is this
project's actual scored range and the one collection genuinely can't
afford to miss - see docs/DATA_ARCHITECTURE.md.

This is the model's PRIMARY ground truth (see verify_and_learn.py),
scraped by .github/workflows/kitesailing-sampler.yml (a separate workflow
file from the main wingcheck.yml, with its own concurrency group - see
that file's header) into logs/kitesailing_observations.jsonl. Unlike
Samedan, there is no historical archive for this station - history only
exists from whenever scraping started, so backtest.py's historical
retrain still has to use Samedan (the only source with a multi-year
archive).

Every attempt (success or failure) writes one row to
logs/kitesailing_ingestion_health.jsonl via attempt_reading() - this is
the operational entry point the workflow calls, NOT fetch_current_reading()
directly. On failure, a screenshot and a compact copy of the page HTML are
saved under logs/kitesailing_failure_artifacts/<timestamp>/ for the
workflow to upload as a short-lived (7-day) GitHub Actions artifact - never
a fake/fallback observation. An anti-bot challenge page (Cloudflare
interstitial, CAPTCHA, etc.) is explicitly detected and reported as a
distinct failure category - this module never attempts to solve or bypass
one.

retrieved_at vs source_observed_at: the dedicated page publishes a local
date/time beside the reading. source_observed_at and the backward-compatible
observed_at field use that real station timestamp converted to UTC;
retrieved_at remains this project's fetch time.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

URL = "https://www.kitesailing.ch/en/spot/weather-watersports"
LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "kitesailing_observations.jsonl")
HEALTH_LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "kitesailing_ingestion_health.jsonl")
FAILURE_ARTIFACT_DIR = os.path.join(os.path.dirname(__file__), "logs", "kitesailing_failure_artifacts")

ZURICH_TZ = ZoneInfo("Europe/Zurich")
COLLECTION_WINDOW_START = dtime(5, 0)   # Europe/Zurich
COLLECTION_WINDOW_END = dtime(21, 45)
PRIORITY_WINDOW_START = dtime(12, 0)    # this project's actually-scored window
PRIORITY_WINDOW_END = dtime(18, 30)

_ANTI_BOT_MARKERS = (
    "just a moment", "attention required", "cloudflare", "captcha",
    "access denied", "checking your browser", "are you a robot", "bot detection",
    "unusual traffic", "verify you are human",
)

_TEMP_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*°C")
_GUST_RE = re.compile(r"Windspitzen.*?(\d+(?:\.\d+)?)\s*km/h.*?\(([\d.]+)\s*kn\)", re.S)
_HUMIDITY_RE = re.compile(r"Feuchtigkeit:\s*(\d+(?:\.\d+)?)\s*%")
_PRESSURE_RE = re.compile(r"Luftdruck:\s*(\d+(?:\.\d+)?)\s*hPa")
_DIRECTION_RE = re.compile(r"Windrichtung:\s*([A-Z]+)\s*\(([\d.]+)\s*°\)")
_AVG_WIND_RE = re.compile(r"Mittelwind:\s*(\d+(?:\.\d+)?)\s*km/h\s*\((\d+)\s*Bft\)")
_SOURCE_TIME_RE = re.compile(
    r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s*\(\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})\s*\)"
)
_COMPASS_TRANSLATIONS = {
    "N": "N", "NO": "NE", "NE": "NE", "O": "E", "E": "E",
    "SO": "SE", "SE": "SE", "S": "S", "SW": "SW", "W": "W", "NW": "NW",
}

# Fields compared for duplicate/unchanged-reading detection - deliberately
# excludes timestamps (which always differ between attempts).
_DEDUP_FIELDS = ("temp_c", "gust_kmh", "avg_wind_kmh", "wind_dir_deg", "humidity_pct", "pressure_hpa")


def is_within_collection_window(now_utc: datetime = None) -> bool:
    """The Python-side local-time filter behind a broad UTC cron - handles
    the CET/CEST transition correctly (zoneinfo, not a fixed UTC offset)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    local_time = now_utc.astimezone(ZURICH_TZ).time()
    return COLLECTION_WINDOW_START <= local_time <= COLLECTION_WINDOW_END


def is_in_priority_window(now_utc: datetime = None) -> bool:
    """This project's actually-scored 12:00-18:30 Europe/Zurich range -
    used only to tag a reading, not to gate collection (the full
    05:00-21:45 window is always sampled)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    local_time = now_utc.astimezone(ZURICH_TZ).time()
    return PRIORITY_WINDOW_START <= local_time <= PRIORITY_WINDOW_END


def _detect_anti_bot_challenge(page_title: str, page_content: str):
    """Returns a short category string if the page looks like an anti-bot
    challenge (Cloudflare interstitial, CAPTCHA, etc.) rather than the
    real widget, or None if it looks normal. Detection only - never
    attempts to solve or bypass a detected challenge, so a real challenge
    fails visibly instead of silently producing garbage data."""
    haystack = f"{page_title or ''} {page_content or ''}".lower()
    for marker in _ANTI_BOT_MARKERS:
        if marker in haystack:
            return f"anti_bot_challenge_detected:{marker}"
    return None


def _parse_reading(today_text: str, details_text: str, retrieved_at: datetime) -> dict:
    """Pure parsing logic (no browser) - raises ValueError if the page's
    markup doesn't match the expected labels (fail loudly rather than
    silently producing a wrong reading)."""
    temp_m = _TEMP_RE.search(today_text)
    gust_m = _GUST_RE.search(today_text)
    humidity_m = _HUMIDITY_RE.search(details_text)
    pressure_m = _PRESSURE_RE.search(details_text)
    direction_m = _DIRECTION_RE.search(details_text)
    avg_wind_m = _AVG_WIND_RE.search(details_text)
    source_time_m = _SOURCE_TIME_RE.search(today_text)

    missing = [
        name for name, m in [
            ("temp", temp_m), ("gust", gust_m), ("humidity", humidity_m),
            ("pressure", pressure_m), ("direction", direction_m), ("avg_wind", avg_wind_m),
            ("source_time", source_time_m),
        ] if m is None
    ]
    if missing:
        raise ValueError(
            f"could not parse fields {missing} from the widget - kitesailing.ch's "
            f"markup or wording may have changed. today_text={today_text!r} "
            f"details_text={details_text!r}"
        )

    day, month, year, hour, minute, second = map(int, source_time_m.groups())
    source_local = datetime(year, month, day, hour, minute, second, tzinfo=ZURICH_TZ)
    source_utc = source_local.astimezone(timezone.utc)
    retrieved_at_iso = retrieved_at.isoformat()
    source_observed_iso = source_utc.isoformat()
    raw_compass = direction_m.group(1)
    return {
        "observed_at": source_observed_iso,  # backward-compatible canonical timestamp
        "retrieved_at": retrieved_at_iso,
        "source_observed_at": source_observed_iso,
        "source_url": URL,
        "temp_c": float(temp_m.group(1)),
        "gust_kmh": float(gust_m.group(1)),
        "gust_kn": float(gust_m.group(2)),
        "avg_wind_kmh": float(avg_wind_m.group(1)),
        "avg_wind_bft": int(avg_wind_m.group(2)),
        "wind_dir_compass": _COMPASS_TRANSLATIONS.get(raw_compass, raw_compass),
        "wind_dir_compass_raw": raw_compass,
        "wind_dir_deg": float(direction_m.group(2)),
        "humidity_pct": float(humidity_m.group(1)),
        "pressure_hpa": float(pressure_m.group(1)),
        "in_priority_window": is_in_priority_window(source_utc),
    }


def fetch_current_reading(headless: bool = True) -> dict:
    """Scrapes the live-weather widget and returns one reading. Raises
    ValueError if the page's markup doesn't match the expected labels.
    This is the low-level, no-health-logging primitive - the operational
    entry point is attempt_reading(), not this function directly.

    Imports playwright lazily: load_observations()/closest_observation()
    below are pure log-file readers used by verify_and_learn.py, which
    must stay importable in the (playwright-free) `learn` job - only the
    actual scraping job needs the browser dependency."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.goto(URL, timeout=30000, wait_until="networkidle")
        page.wait_for_selector(".lmw-weather-today-temp", timeout=15000)

        today_text = page.eval_on_selector(".lmw-weather-today", "el => el.textContent")
        details_text = page.eval_on_selector(".lmw-weather-details", "el => el.textContent")
        browser.close()

    return _parse_reading(today_text, details_text, datetime.now(timezone.utc))


def load_observations() -> list:
    """All accumulated readings, oldest first. No network - reads the local
    log that the scheduled scrape job appends to."""
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH) as f:
        obs = [json.loads(line) for line in f if line.strip()]
    obs.sort(key=lambda o: o["observed_at"])
    return obs


def closest_observation(observations: list, target_utc: datetime, tolerance_minutes: float = 30) -> dict:
    """The reading nearest target_utc, or None if nothing falls within
    tolerance_minutes - readings only exist for whenever the scrape job
    happened to run, not every hour, so callers must handle a miss."""
    best, best_diff = None, None
    tolerance = timedelta(minutes=tolerance_minutes)
    for obs in observations:
        obs_dt = datetime.fromisoformat(obs["observed_at"])
        diff = abs(obs_dt - target_utc)
        if diff <= tolerance and (best_diff is None or diff < best_diff):
            best, best_diff = obs, diff
    return best


def _is_duplicate_reading(reading: dict) -> bool:
    """True if every dedup field exactly matches the most recent logged
    observation - the underlying station/cache hasn't actually updated
    since the last successful scrape."""
    existing = load_observations()
    if not existing:
        return False
    last = existing[-1]
    return all(last.get(f) == reading.get(f) for f in _DEDUP_FIELDS)


def _append_observation(reading: dict):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(reading) + "\n")


def _write_health_row(row: dict):
    os.makedirs(os.path.dirname(HEALTH_LOG_PATH), exist_ok=True)
    with open(HEALTH_LOG_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")


def _save_failure_artifacts(screenshot_bytes, html_text, attempted_at_iso: str) -> str:
    """Saves a screenshot + a bounded ("compact") copy of the page HTML
    under logs/kitesailing_failure_artifacts/<timestamp>/ for the workflow
    to upload as a short-lived GitHub Actions artifact. Returns the
    directory path."""
    safe_ts = attempted_at_iso.replace(":", "-")
    subdir = os.path.join(FAILURE_ARTIFACT_DIR, safe_ts)
    os.makedirs(subdir, exist_ok=True)
    if screenshot_bytes:
        with open(os.path.join(subdir, "screenshot.png"), "wb") as f:
            f.write(screenshot_bytes)
    if html_text:
        compact = html_text[:20000]  # bounded - "compact", not the full raw page
        with open(os.path.join(subdir, "page.html"), "w") as f:
            f.write(compact)
    return subdir


def attempt_reading(headless: bool = True) -> dict:
    """The OPERATIONAL entry point (what the workflow calls, not
    fetch_current_reading() directly). Runs one full sampling attempt,
    always writes a logs/kitesailing_ingestion_health.jsonl row, and on
    failure saves a screenshot + compact HTML for later inspection -
    NEVER writes a fake/fallback observation. Returns
    {"success": bool, "reading": dict|None, "failure_category": str|None,
     "duplicate": bool}."""
    attempted_at = datetime.now(timezone.utc)
    final_url = None
    page_title = None
    reading = None
    failure_category = None
    duplicate = False

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page()
            page.goto(URL, timeout=30000, wait_until="networkidle")
            final_url = page.url
            page_title = page.title()
            page_content = page.content()

            challenge = _detect_anti_bot_challenge(page_title, page_content)
            if challenge:
                failure_category = challenge
                screenshot = page.screenshot()
                _save_failure_artifacts(screenshot, page_content, attempted_at.isoformat())
            else:
                try:
                    page.wait_for_selector(".lmw-weather-today-temp", timeout=15000)
                    today_text = page.eval_on_selector(".lmw-weather-today", "el => el.textContent")
                    details_text = page.eval_on_selector(".lmw-weather-details", "el => el.textContent")
                    reading = _parse_reading(today_text, details_text, attempted_at)
                except Exception as e:
                    failure_category = f"parse_error:{e}"
                    screenshot = page.screenshot()
                    html = page.content()
                    _save_failure_artifacts(screenshot, html, attempted_at.isoformat())
            browser.close()
    except Exception as e:
        failure_category = f"navigation_error:{e}"

    observation_written = False
    latest_observation_timestamp = None
    if reading is not None:
        latest_observation_timestamp = reading["retrieved_at"]
        if _is_duplicate_reading(reading):
            duplicate = True
        else:
            _append_observation(reading)
            observation_written = True

    runtime_s = round((datetime.now(timezone.utc) - attempted_at).total_seconds(), 2)
    _write_health_row({
        "attempted_at": attempted_at.isoformat(),
        "success": reading is not None,
        "failure_category": failure_category,
        "runtime_s": runtime_s,
        "final_url": final_url,
        "page_title": page_title,
        "observation_written": observation_written,
        "duplicate": duplicate,
        "latest_observation_timestamp": latest_observation_timestamp,
    })

    return {"success": reading is not None, "reading": reading, "failure_category": failure_category, "duplicate": duplicate}


def main():
    if not is_within_collection_window():
        print("[skip] outside the 05:00-21:45 Europe/Zurich collection window - not attempting a fetch")
        return 0

    result = attempt_reading()
    if result["success"]:
        status = "duplicate (unchanged reading, not re-appended)" if result["duplicate"] else "new observation written"
        print(f"[ok] {status}: {json.dumps(result['reading'], indent=2)}")
        return 0
    print(f"[failed] {result['failure_category']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

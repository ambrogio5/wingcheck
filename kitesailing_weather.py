"""
kitesailing_weather.py - live weather reading from the "LiveMeteo" widget
embedded on https://www.kitesailing.ch/en/spot/webcam.

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
every 10-20 minutes - poll on a similar interval, not faster.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

URL = "https://www.kitesailing.ch/en/spot/webcam"
LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "kitesailing_observations.jsonl")

_TEMP_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*°C")
_GUST_RE = re.compile(r"Windspitzen.*?(\d+(?:\.\d+)?)\s*km/h.*?\(([\d.]+)\s*kn\)", re.S)
_HUMIDITY_RE = re.compile(r"Feuchtigkeit:\s*(\d+(?:\.\d+)?)\s*%")
_PRESSURE_RE = re.compile(r"Luftdruck:\s*(\d+(?:\.\d+)?)\s*hPa")
_DIRECTION_RE = re.compile(r"Windrichtung:\s*([A-Z]+)\s*\(([\d.]+)\s*°\)")
_AVG_WIND_RE = re.compile(r"Mittelwind:\s*(\d+(?:\.\d+)?)\s*km/h\s*\((\d+)\s*Bft\)")


def fetch_current_reading(headless: bool = True) -> dict:
    """Scrapes the live-weather widget and returns one reading. Raises
    ValueError if the page's markup doesn't match the expected labels
    (fail loudly - a silently wrong scrape is worse than a crash)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.goto(URL, timeout=30000, wait_until="networkidle")
        page.wait_for_selector(".lmw-weather-today-temp", timeout=15000)

        today_text = page.eval_on_selector(".lmw-weather-today", "el => el.textContent")
        details_text = page.eval_on_selector(".lmw-weather-details", "el => el.textContent")
        browser.close()

    temp_m = _TEMP_RE.search(today_text)
    gust_m = _GUST_RE.search(today_text)
    humidity_m = _HUMIDITY_RE.search(details_text)
    pressure_m = _PRESSURE_RE.search(details_text)
    direction_m = _DIRECTION_RE.search(details_text)
    avg_wind_m = _AVG_WIND_RE.search(details_text)

    missing = [
        name for name, m in [
            ("temp", temp_m), ("gust", gust_m), ("humidity", humidity_m),
            ("pressure", pressure_m), ("direction", direction_m), ("avg_wind", avg_wind_m),
        ] if m is None
    ]
    if missing:
        raise ValueError(
            f"could not parse fields {missing} from the widget - kitesailing.ch's "
            f"markup or wording may have changed. today_text={today_text!r} "
            f"details_text={details_text!r}"
        )

    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "temp_c": float(temp_m.group(1)),
        "gust_kmh": float(gust_m.group(1)),
        "gust_kn": float(gust_m.group(2)),
        "avg_wind_kmh": float(avg_wind_m.group(1)),
        "avg_wind_bft": int(avg_wind_m.group(2)),
        "wind_dir_compass": direction_m.group(1),
        "wind_dir_deg": float(direction_m.group(2)),
        "humidity_pct": float(humidity_m.group(1)),
        "pressure_hpa": float(pressure_m.group(1)),
    }


def main():
    reading = fetch_current_reading()
    print(json.dumps(reading, indent=2))

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(reading) + "\n")


if __name__ == "__main__":
    sys.exit(main())

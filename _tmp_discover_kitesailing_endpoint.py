"""
One-off reconnaissance script: find the live-weather data source behind
https://www.kitesailing.ch/en/spot/webcam.

Launches headless Chromium, loads the page, and logs every XHR/fetch
response so we can spot the polling endpoint (likely JSON, likely fired
repeatedly on an interval). Also dumps any inline <script> JSON blobs and
elements that look like live weather readouts, in case the data is
server-rendered rather than fetched client-side.

Usage:
    pip install playwright
    playwright install chromium
    python discover_kitesailing_endpoint.py
"""

import json
import re
import sys

from playwright.sync_api import sync_playwright

URL = "https://www.kitesailing.ch/en/spot/webcam"
WEATHER_HINTS = re.compile(
    r"(api|weather|wind|station|sensor|ajax|json|live|meteo|data)", re.I
)


def main():
    seen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(resp):
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            ct = resp.headers.get("content-type", "")
            entry = {
                "method": req.method,
                "url": resp.url,
                "status": resp.status,
                "content_type": ct,
            }
            if "json" in ct:
                try:
                    body = resp.json()
                    entry["body_preview"] = json.dumps(body)[:2000]
                except Exception as e:
                    entry["body_error"] = str(e)
            seen.append(entry)
            print(f"[xhr] {req.method} {resp.status} {resp.url}  ct={ct}")

        page.on("response", on_response)

        print(f"Navigating to {URL} ...")
        page.goto(URL, timeout=30000, wait_until="networkidle")

        # Give any polling interval a chance to fire at least once more.
        page.wait_for_timeout(8000)

        print("\n--- All XHR/fetch responses seen ---")
        for e in seen:
            print(json.dumps(e, indent=2)[:1500])

        candidates = [e for e in seen if WEATHER_HINTS.search(e["url"])]
        print("\n--- Candidates matching weather-ish keywords in the URL ---")
        for e in candidates:
            print(json.dumps(e, indent=2))

        print("\n--- Looking for inline <script> JSON blobs ---")
        scripts = page.eval_on_selector_all(
            "script:not([src])", "els => els.map(e => e.textContent)"
        )
        for i, s in enumerate(scripts):
            if s and WEATHER_HINTS.search(s) and len(s) < 5000:
                print(f"[inline script {i}] {s[:1000]}")

        print("\n--- Elements whose id/class hints at live weather values ---")
        els = page.eval_on_selector_all(
            "[id], [class]",
            """els => els
                .filter(e => /temp|wind|gust|humid|pressure|dir/i.test(e.id + ' ' + e.className))
                .map(e => ({tag: e.tagName, id: e.id, class: e.className, text: e.textContent.trim().slice(0, 80)}))
            """,
        )
        for e in els[:40]:
            print(json.dumps(e))

        browser.close()

    if not seen:
        print("\nNo XHR/fetch requests captured at all - data may be fully "
              "server-rendered, or the page needs more time/interaction.")


if __name__ == "__main__":
    sys.exit(main())

"""
One-off reconnaissance script: find the live-weather data source behind
https://www.kitesailing.ch/en/spot/webcam.

v2: the first pass showed the live weather DOM (classes like
lmw-weather-today-*, bdtw-*, meteo-windstaerke) has no matching top-level
XHR/fetch - meaning it's very likely rendered inside an <iframe> from a
third-party widget host, whose own document/XHR requests aren't tagged as
"xhr"/"fetch" at the top frame. This version:
  - logs every request regardless of resource_type, across every frame
  - explicitly walks page.frames() to find the frame whose DOM holds the
    weather elements, and prints that frame's URL (the widget host)
  - lists all <iframe src=...> and <script src=...> in every frame

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
    r"(api|weather|wind|station|sensor|ajax|json|live|meteo|data|lmw|bdtw)", re.I
)


def main():
    seen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(resp):
            req = resp.request
            ct = resp.headers.get("content-type", "")
            entry = {
                "resource_type": req.resource_type,
                "method": req.method,
                "url": resp.url,
                "status": resp.status,
                "content_type": ct,
                "frame_url": req.frame.url if req.frame else None,
            }
            if "json" in ct:
                try:
                    body = resp.json()
                    entry["body_preview"] = json.dumps(body)[:1500]
                except Exception as e:
                    entry["body_error"] = str(e)
            seen.append(entry)

        page.on("response", on_response)

        print(f"Navigating to {URL} ...")
        page.goto(URL, timeout=30000, wait_until="networkidle")
        page.wait_for_timeout(8000)

        print("\n--- All frames on the page ---")
        for f in page.frames:
            print(f"frame: {f.url}")

        print("\n--- Requests that are NOT plain document/script/stylesheet/image/font ---")
        for e in seen:
            if e["resource_type"] not in ("stylesheet", "image", "font", "media"):
                print(json.dumps(e, indent=2)[:1200])

        print("\n--- Requests matching weather-ish keywords (any resource_type) ---")
        for e in seen:
            if WEATHER_HINTS.search(e["url"]):
                print(json.dumps(e, indent=2))

        print("\n--- Which frame holds the live-weather elements? ---")
        for f in page.frames:
            try:
                els = f.eval_on_selector_all(
                    "[class]",
                    """els => els
                        .filter(e => /lmw|bdtw|meteo-wind/i.test(e.className))
                        .map(e => ({class: e.className, text: e.textContent.trim().slice(0,80)}))
                    """,
                )
            except Exception as e:
                els = []
            if els:
                print(f"\nFRAME {f.url} contains {len(els)} weather elements:")
                for el in els[:15]:
                    print(" ", json.dumps(el))

        print("\n--- <iframe src> across all frames ---")
        for f in page.frames:
            try:
                srcs = f.eval_on_selector_all("iframe", "els => els.map(e => e.src)")
            except Exception:
                srcs = []
            for s in srcs:
                print(f"  iframe in {f.url} -> {s}")

        print("\n--- <script src> across all frames (filtered to weather-ish) ---")
        for f in page.frames:
            try:
                srcs = f.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
            except Exception:
                srcs = []
            for s in srcs:
                if WEATHER_HINTS.search(s):
                    print(f"  script in {f.url} -> {s}")

        browser.close()


if __name__ == "__main__":
    sys.exit(main())

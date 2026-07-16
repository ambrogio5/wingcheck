"""
One-off reconnaissance script, v3: the live weather block on
https://www.kitesailing.ch/en/spot/webcam is a "LiveMeteo" widget
(class livemeteo-widget-lmw-weather, id 4270bd8) rendered fully
server-side - no iframe, no client-visible XHR/fetch call, confirmed by
v1/v2 runs. This pass:
  - dumps the widget's full outerHTML (any data-* attributes might name
    the vendor's own API/config)
  - checks whether livemeteo.ch (the literal vendor name found in the
    CSS classes) is reachable and whether IT exposes a client-visible
    API for the same widget id, since kitesailing.ch's own page doesn't

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


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print(f"Navigating to {URL} ...")
        page.goto(URL, timeout=30000, wait_until="networkidle")
        page.wait_for_timeout(3000)

        print("\n--- Full outerHTML of the livemeteo widget wrapper ---")
        html = page.eval_on_selector(
            ".livemeteo-element",
            "el => el.outerHTML",
        )
        print(html[:6000] if html else "(selector not found)")

        print("\n--- All data-* attributes anywhere under the widget ---")
        attrs = page.eval_on_selector_all(
            ".livemeteo-element, .livemeteo-element *",
            """els => els.flatMap(e =>
                Array.from(e.attributes)
                    .filter(a => a.name.startsWith('data-'))
                    .map(a => a.name + '=' + a.value)
            )""",
        )
        for a in set(attrs):
            print(" ", a)

        browser.close()

    print("\n--- Trying livemeteo.ch directly (vendor name found in CSS classes) ---")
    for candidate in ["https://www.livemeteo.ch", "https://livemeteo.ch"]:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            seen = []
            page.on("response", lambda resp: seen.append(resp.url) if resp.request.resource_type in ("xhr", "fetch") else None)
            try:
                resp = page.goto(candidate, timeout=20000, wait_until="domcontentloaded")
                print(f"{candidate} -> status {resp.status if resp else None}, title: {page.title()!r}")
                page.wait_for_timeout(3000)
                for u in seen:
                    print("  xhr/fetch:", u)
            except Exception as e:
                print(f"{candidate} -> ERROR: {e}")
            browser.close()


if __name__ == "__main__":
    sys.exit(main())

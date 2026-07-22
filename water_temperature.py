"""Fetch the current estimated surface temperature for Lake Silvaplana.

The source labels its value as water temperature, but does not document a
physical sensor or measurement time.  Wingcheck therefore stores and displays
it as an estimate, with the retrieval time and source URL kept alongside it.
"""
from __future__ import annotations

import html as html_module
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

URL = "https://www.wassertemperatur.org/seen/schweiz/silvaplanersee/"
LATEST_PATH = Path(__file__).resolve().parent / "logs" / "water_temperature_latest.json"
_TEMPERATURE_RE = re.compile(
    r"Aktuelle\s+Wassertemperatur\s+im\s+Silvaplanersee\s*:\s*"
    r"<[^>]+>\s*|Aktuelle\s+Wassertemperatur\s+im\s+Silvaplanersee\s*:\s*",
    re.IGNORECASE,
)
_VALUE_RE = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*(?:°|&deg;)\s*C", re.IGNORECASE)


def parse_temperature(page_html: str) -> float:
    """Extract the explicitly labelled Silvaplanersee water temperature."""
    text = html_module.unescape(page_html).replace("\xa0", " ")
    label = _TEMPERATURE_RE.search(text)
    if not label:
        raise ValueError("Silvaplanersee water-temperature label not found")
    value = _VALUE_RE.search(text, label.start(), min(len(text), label.end() + 250))
    if not value:
        raise ValueError("Silvaplanersee water-temperature value not found")
    temperature = float(value.group(1).replace(",", "."))
    if not -1.0 <= temperature <= 30.0:
        raise ValueError(f"implausible lake-water temperature: {temperature}")
    return temperature


def fetch_current_reading(now: datetime | None = None) -> dict:
    response = requests.get(
        URL,
        timeout=20,
        headers={"User-Agent": "Wingcheck/1.0 (+local weather dashboard)"},
    )
    response.raise_for_status()
    return {
        "temp_c": parse_temperature(response.text),
        "retrieved_at": (now or datetime.now(timezone.utc)).isoformat(),
        "source_url": URL,
        "estimated": True,
        "source_note": "Source does not publish sensor provenance or a measurement timestamp.",
    }


def write_latest(reading: dict, path: Path = LATEST_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(reading, indent=2) + "\n")
    os.replace(temporary, path)


def main() -> int:
    try:
        reading = fetch_current_reading()
        write_latest(reading)
    except (requests.RequestException, ValueError, OSError) as exc:
        print(f"[failed] water-temperature estimate: {exc}", file=sys.stderr)
        return 1
    print(f"[ok] estimated lake-water temperature: {reading['temp_c']:.1f}°C")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

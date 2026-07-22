"""Download the official MeteoSwiss localized forecast for Silvaplana."""
from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

COLLECTION = "ch.meteoschweiz.ogd-local-forecasting"
STAC_ITEMS_URL = f"https://data.geo.admin.ch/api/stac/v1/collections/{COLLECTION}/items?limit=20"
SOURCE_URL = "https://opendatadocs.meteoswiss.ch/e-forecast-data/e4-local-forecast-data"
POINT_ID = "751300"  # MeteoSwiss postal-code point for 7513 Silvaplana.
PARAMETERS = {
    "tre200h0": "temp_c",
    "fu3010h0": "wind_kmh",
    "fu3010h1": "gust_kmh",
    "dkl010h0": "wind_direction_deg",
    "rre150h0": "precipitation_mm",
    "rp0003i0": "precipitation_probability_pct",
    "jww003i0": "weather_code",
}
ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = Path(os.environ.get("WINGCHECK_RUNTIME_DIR", ROOT)) / "logs" / "meteoswiss_local_forecast.json"


def _latest_assets(catalog: dict) -> tuple[str, dict[str, str]]:
    candidates: dict[str, tuple[str, str]] = {}
    for feature in catalog.get("features", []):
        for asset in feature.get("assets", {}).values():
            href = asset.get("href", "")
            match = re.search(r"\.(\d{12})\.([a-z0-9]+)\.csv$", href)
            if not match or match.group(2) not in PARAMETERS:
                continue
            issued, parameter = match.groups()
            if parameter not in candidates or issued > candidates[parameter][0]:
                candidates[parameter] = (issued, href)
    if not candidates:
        raise RuntimeError("MeteoSwiss catalogue contains no localized forecast assets")
    common_issuance = max(issued for issued, _ in candidates.values())
    urls = {parameter: href for parameter, (issued, href) in candidates.items() if issued == common_issuance}
    missing = set(PARAMETERS) - set(urls)
    if missing:
        raise RuntimeError(f"Latest MeteoSwiss issuance is missing: {', '.join(sorted(missing))}")
    return common_issuance, urls


def _point_values(csv_text: str, parameter: str) -> dict[str, float]:
    values = {}
    for row in csv.DictReader(io.StringIO(csv_text), delimiter=";"):
        if row.get("point_id") != POINT_ID:
            continue
        raw = row.get(parameter)
        if raw not in (None, "", "NA"):
            values[row["Date"]] = float(raw)
    return values


def fetch_forecast(session=requests) -> dict:
    catalog_response = session.get(STAC_ITEMS_URL, timeout=30)
    catalog_response.raise_for_status()
    issuance, urls = _latest_assets(catalog_response.json())
    by_parameter = {}
    for parameter, url in urls.items():
        response = session.get(url, timeout=45)
        response.raise_for_status()
        by_parameter[parameter] = _point_values(response.text, parameter)

    timestamps = sorted(set().union(*(values.keys() for values in by_parameter.values())))
    hours = []
    for stamp in timestamps:
        row = {"time": datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=timezone.utc).isoformat()}
        for parameter, field in PARAMETERS.items():
            value = by_parameter[parameter].get(stamp)
            if value is not None:
                row[field] = round(value, 1)
        if row.keys() - {"time"}:
            hours.append(row)
    return {
        "location": "Silvaplana",
        "point_id": POINT_ID,
        "source": "MeteoSwiss Open Data localized forecast",
        "source_url": SOURCE_URL,
        "issued_at": datetime.strptime(issuance, "%Y%m%d%H%M").replace(tzinfo=timezone.utc).isoformat(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "hours": hours,
    }


def main() -> None:
    payload = fetch_forecast()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(OUTPUT_PATH)
    print(f"Saved {len(payload['hours'])} MeteoSwiss forecast hours for Silvaplana")


if __name__ == "__main__":
    main()

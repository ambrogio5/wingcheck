"""
manual_station_import.py - parsers for one-off, user-provided historical
weather data files (no live API involved, unlike meteoswiss.py).

Some real stations near the lake (e.g. Sils/Segl, station_id "sils" in
config/stations.json) have no confirmed MeteoSwiss/Open-Meteo API access,
but real historical data for them can still arrive as a manually-provided
export file. This module turns such a file into the same
{datetime_utc: {normalized_field_name: value}} shape that
historical_data.normalize_generic_observations() already expects from
meteoswiss.fetch_station_observations() - so no new normalize function or
storage format is needed, only a new parser feeding the existing pipeline.

Parsers are registered by FORMAT NAME (not station name) in PARSERS, since
a future upload for a different station may share the exact same file
layout (the same export tool, a different station's readings).

Currently supported format: "semicolon_weather" - a semicolon-delimited,
double-quoted CSV with columns "date/time (local)", "wind direction
[degrees]", "wind speed [kts]", "air temperature [°C]", "air pressure
[hPa]", "clouds". Confirmed from a real uploaded file
(weatherdata_silser_see_*.csv, 2014-04-02, 22 hourly rows). Units/mapping
decided from the data itself:
  - wind speed is in KNOTS (not km/h like MeteoSwiss's feed) - a distinct
    conversion factor from the km/h ones used elsewhere in this codebase.
  - air pressure (~845-848 hPa here) is STATION-LEVEL (QFE), not
    sea-level-reduced (QFF): physically implausible as QFF at any real
    elevation, but exactly right for a ~1800m lake-side station, and
    consistent with pressure_station_hpa's plausible floor (800 hPa) in
    data_quality.py vs pressure_sea_level_hpa's floor (930 hPa).
  - "clouds" is a free-form METAR-style code (e.g. "SKC", "SCT000") with no
    existing NORMALIZED_FIELDS slot of its own - preserved as-is into the
    additive `clouds_raw` field rather than discarded (empty string -> None).
  - timestamps are naive Europe/Zurich local wall-clock (no UTC offset in
    the source file) - converted to aware UTC via ZoneInfo, matching the
    pattern already used in verify_and_learn.py/backtest.py/features.py.
"""

import csv
import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ZURICH_TZ = ZoneInfo("Europe/Zurich")

_KTS_TO_MS = 0.514444


def parse_semicolon_weather_csv(text: str) -> dict:
    """Parses the semicolon-delimited, quoted lake-station CSV format into
    {datetime_utc (aware): {normalized_field_name: value}}. Rows with an
    unparseable timestamp are skipped (never raises on a single bad row);
    a completely empty/header-only input returns {}."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";", quotechar='"')
    result = {}
    for row in reader:
        raw_ts = (row.get("date/time (local)") or "").strip()
        if not raw_ts:
            continue
        try:
            local_dt = datetime.fromisoformat(raw_ts).replace(tzinfo=ZURICH_TZ)
        except ValueError:
            continue
        dt_utc = local_dt.astimezone(timezone.utc)

        vals = {}
        wind_dir = row.get("wind direction [degrees]")
        if wind_dir not in (None, ""):
            vals["wind_direction_deg"] = float(wind_dir)
        wind_speed_kts = row.get("wind speed [kts]")
        if wind_speed_kts not in (None, ""):
            vals["wind_speed_ms"] = round(float(wind_speed_kts) * _KTS_TO_MS, 3)
        temp_c = row.get("air temperature [°C]")
        if temp_c not in (None, ""):
            vals["temperature_c"] = float(temp_c)
        pressure_hpa = row.get("air pressure [hPa]")
        if pressure_hpa not in (None, ""):
            vals["pressure_station_hpa"] = float(pressure_hpa)
        clouds = (row.get("clouds") or "").strip()
        if clouds:
            vals["clouds_raw"] = clouds

        result[dt_utc] = vals
    return result


PARSERS = {
    "semicolon_weather": parse_semicolon_weather_csv,
}

"""
meteoswiss.py - shared access to MeteoSwiss's official open data (SwissMetNet).

Real, licensed, free station observations - data.geo.admin.ch, no API key.
Used as ground truth: what actually happened, as opposed to another model's
guess. Station SAM (Samedan) is ~10km from Silvaplana; MeteoSwiss's own
writeup on valley winds notes the Malojawind reaches that far.

Also provides real sea-level pressure from Lugano (station "lug") and
Zurich/Fluntern (station "sma") - confirmed against the live API on
2026-07-16 (station codes and the real column name, pp0qffh0, not the
plausible-looking but wrong pp0qffs0 an earlier docstring guessed). This
feeds features.py's pressure_nowcast_score, a NOWCAST feature (current
measured pressure gradient) - it is NOT a substitute for pressure_signal,
which is deliberately Open-Meteo FORECAST data: pressure_signal scores a
1-3 day-ahead target hour, and a real observation can't exist yet for a
future hour.

Instead of guessing file names (which vary and change), we ask the official
STAC catalog API which data files exist for the station, then download every
hourly CSV it lists. This survives MeteoSwiss renaming or re-splitting files.
"""

import csv
import io
from datetime import datetime, timezone

import requests

# Labeling threshold measured AT SAMEDAN. The Malojawind weakens as it runs
# down-valley, so wind at Samedan understates wind at the Silvaplana lake.
# 8kt at SAM is a first-guess proxy for "~10kt+ rideable at the lake".
# Tune this after comparing a few real sessions: if the model misses days
# you actually rode, lower it; if it flags days that were dead, raise it.
SAM_PROXY_KT = 8.0

LUGANO_STATION = "lug"
ZURICH_STATION = "sma"


def _stac_item_url(station: str) -> str:
    return f"https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-smn/items/{station}"


def _recent_url(station: str) -> str:
    return f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/{station}/ogd-smn_{station}_h_recent.csv"


def _parse_wind_csv(text: str) -> dict:
    """Returns {datetime_utc: {"speed_kmh":..., "gust_kmh":...}}."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    obs = {}
    for row in reader:
        row = {k.lower(): v for k, v in row.items()}
        ts_raw = row.get("reference_timestamp") or row.get("time")
        if not ts_raw:
            continue
        try:
            dt = datetime.strptime(ts_raw.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        try:
            speed = float(row.get("fu3010h0", "") or "nan")
        except ValueError:
            continue
        try:
            gust = float(row.get("fu3010h1", "") or "nan")
        except ValueError:
            gust = float("nan")
        if speed != speed:  # NaN check
            continue
        obs[dt] = {"speed_kmh": speed, "gust_kmh": gust}
    return obs


def _parse_pressure_csv(text: str) -> dict:
    """Returns {datetime_utc: {"pressure_hpa": ...}} from the sea-level
    (QFF) pressure column."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    obs = {}
    for row in reader:
        row = {k.lower(): v for k, v in row.items()}
        ts_raw = row.get("reference_timestamp") or row.get("time")
        if not ts_raw:
            continue
        try:
            dt = datetime.strptime(ts_raw.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        try:
            pressure = float(row.get("pp0qffh0", "") or "nan")
        except ValueError:
            continue
        if pressure != pressure:  # NaN check
            continue
        obs[dt] = {"pressure_hpa": pressure}
    return obs


def _fetch_csv(url: str, parser) -> dict:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return parser(r.content.decode("utf-8", errors="replace"))


def _discover_hourly_urls(station: str) -> list:
    """Ask the STAC catalog which files exist for the station; return the
    URLs of all hourly (_h) CSV assets, historical files first."""
    r = requests.get(_stac_item_url(station), timeout=60)
    r.raise_for_status()
    assets = r.json().get("assets", {})
    urls = []
    for name, asset in assets.items():
        href = asset.get("href", "")
        lname = name.lower()
        # hourly granularity files look like ogd-smn_<station>_h*.csv
        if f"_{station}_h" in lname and lname.endswith(".csv"):
            urls.append(href)
    # historical files first so 'recent' overwrites overlaps last
    urls.sort(key=lambda u: ("recent" in u, "now" in u, u))
    return urls


def _fetch_station_observations(station: str, parser, include_historical: bool) -> dict:
    recent_url = _recent_url(station)
    if not include_historical:
        try:
            return _fetch_csv(recent_url, parser)
        except requests.RequestException as e:
            print(f"[warn] could not fetch {station} recent data: {e}")
            return {}

    obs = {}
    try:
        urls = _discover_hourly_urls(station)
    except requests.RequestException as e:
        print(f"[warn] {station} STAC catalog lookup failed ({e}); falling back to recent file only")
        urls = [recent_url]

    if not urls:
        print(f"[warn] {station} catalog listed no hourly files; falling back to recent file")
        urls = [recent_url]

    for url in urls:
        try:
            part = _fetch_csv(url, parser)
            obs.update(part)
            print(f"  loaded {len(part)} hours from {url.rsplit('/', 1)[-1]}")
        except requests.RequestException as e:
            print(f"[warn] could not fetch {url}: {e}")

    return obs


def fetch_sam_hourly_observations(include_historical: bool = True) -> dict:
    """Returns {datetime_utc: {"speed_kmh":..., "gust_kmh":...}} for Samedan
    (SAM) - the model's ground-truth fallback (see kitesailing_weather.py
    for the primary one) and, via features.py, a wind nowcast feature.

    include_historical=False: just the rolling recent file (fast, used by
    the daily verification job).
    include_historical=True: everything the catalog lists (used by the
    backtest to cover 2024+)."""
    return _fetch_station_observations("sam", _parse_wind_csv, include_historical)


def fetch_pressure_observations(station: str, include_historical: bool = True) -> dict:
    """Returns {datetime_utc: {"pressure_hpa": ...}} for the given real
    SwissMetNet station (use LUGANO_STATION / ZURICH_STATION). Feeds
    features.py's pressure_nowcast_score - see this module's docstring for
    why that's a nowcast feature, not a replacement for pressure_signal."""
    return _fetch_station_observations(station, _parse_pressure_csv, include_historical)


# ---------------------------------------------------------------------------
# Generic multi-variable MeteoSwiss station support (added for Corvatsch/COV
# and any future confirmed station beyond the original sam/lug/sma). Unlike
# the role-specific parsers above (one variable each, hand-picked column),
# this fetches every documented SwissMetNet hourly parameter a station
# happens to publish and fails LOUDLY if a variable the caller explicitly
# asked for isn't in the CSV header - it never silently guesses what an
# unrecognized column means.
#
# CONFIRMED_COLUMNS lists the exact column codes this repo has verified
# against a live fetch (fu3010h0/fu3010h1/pp0qffh0 - see this module's
# original docstring). Every other column below (tre200h0, ure200h0,
# dkl010h0, rre150h0, gre000h0, sre000h0) is a documented, standard
# SwissMetNet parameter abbreviation but has NOT yet been independently
# confirmed against a live COV fetch in this repo - that confirmation only
# happens the first time `historical_data.py sync --station cov` actually
# runs somewhere with real network access (see docs/DATA_ARCHITECTURE.md).
# `parse_generic_station_csv()`'s `raw_column_map`/`confirmed_columns`/
# `unconfirmed_columns` output makes this distinction visible in the
# archive's own provenance metadata, not just in this comment.
# ---------------------------------------------------------------------------

METADATA_CSV_URL = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/ogd-smn_meta_stations.csv"

CONFIRMED_COLUMNS = ("fu3010h0", "fu3010h1", "pp0qffh0")

# normalized field name -> (raw SwissMetNet column code, unit of the raw column)
GENERIC_FIELD_COLUMNS = {
    "temperature_c": ("tre200h0", "degC"),
    "relative_humidity_pct": ("ure200h0", "percent"),
    "dew_point_c": ("tde200h0", "degC"),
    "wind_speed_ms": ("fu3010h0", "km/h"),   # converted to m/s during parsing
    "wind_gust_ms": ("fu3010h1", "km/h"),    # converted to m/s during parsing
    "wind_direction_deg": ("dkl010h0", "degrees"),
    "precipitation_mm": ("rre150h0", "mm"),
    "global_radiation_wm2": ("gre000h0", "W/m2"),
    "sunshine_duration_min": ("sre000h0", "min"),
    "pressure_sea_level_hpa": ("pp0qffh0", "hPa"),
}
_KMH_TO_MS_FIELDS = ("wind_speed_ms", "wind_gust_ms")
PARSER_VERSION = 1

# 10-minute ("t") granularity counterpart of GENERIC_FIELD_COLUMNS above -
# same normalized field names, "z0/z1/s0"-suffixed raw columns instead of
# "h0/h1". Confirmed against a real uploaded SIA ogd-smn_sia_t_recent.csv
# file (2026-07-17): every column below was independently verified in
# config/... via ogdsmn_meta_parameters.csv's own English descriptions, not
# guessed from the hourly table by pattern-matching alone - see
# docs/DATA_ARCHITECTURE.md's "SIA 10-minute ingestion" section.
# pressure_sea_level_hpa's column (pp0qffs0, QFF) exists in the schema but
# was NOT populated for SIA in that real file (SIA reports QFE/prestas0 and
# QNH/pp0qnhs0 instead) - left mapped here since another station may
# populate it; for SIA it will simply parse to None, which is honest, not
# a bug.
CONFIRMED_COLUMNS_10MIN = ("fu3010z0", "fu3010z1", "dkl010z0", "tre200s0", "prestas0")

GENERIC_FIELD_COLUMNS_10MIN = {
    "temperature_c": ("tre200s0", "degC"),
    "relative_humidity_pct": ("ure200s0", "percent"),
    "dew_point_c": ("tde200s0", "degC"),
    "wind_speed_ms": ("fu3010z0", "km/h"),   # converted to m/s during parsing
    "wind_gust_ms": ("fu3010z1", "km/h"),    # converted to m/s during parsing
    "wind_direction_deg": ("dkl010z0", "degrees"),
    "precipitation_mm": ("rre150z0", "mm"),
    "global_radiation_wm2": ("gre000z0", "W/m2"),
    "sunshine_duration_min": ("sre000z0", "min"),
    "pressure_station_hpa": ("prestas0", "hPa"),
    "pressure_sea_level_hpa": ("pp0qffs0", "hPa"),
}


def fetch_station_metadata(station_id: str = None) -> dict:
    """Fetches the official MeteoSwiss station metadata CSV
    (METADATA_CSV_URL) and returns either one station's metadata dict
    (station_id given, case-insensitive on the official abbreviation) or
    the full {station_abbr_lower: {...}} dict (station_id=None).

    Raises ValueError if station_id is given but not found in the
    official metadata - never silently returns a guessed/empty result for
    a station that should exist."""
    r = requests.get(METADATA_CSV_URL, timeout=60)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    stations = {}
    for row in reader:
        row = {k.lower(): v for k, v in row.items()}
        abbr = (row.get("station_abbr") or "").strip().lower()
        if not abbr:
            continue
        stations[abbr] = {
            "station_abbr": row.get("station_abbr"),
            "name": row.get("station_name"),
            "canton": row.get("station_canton"),
            "latitude": _safe_float(row.get("station_coordinates_wgs84_lat")),
            "longitude": _safe_float(row.get("station_coordinates_wgs84_lon")),
            "elevation_m": _safe_float(row.get("station_height_masl")),
            "data_since": row.get("station_data_since"),
            "wigos_id": row.get("station_wigos_id"),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source_url": METADATA_CSV_URL,
        }
    if station_id:
        key = station_id.strip().lower()
        if key not in stations:
            raise ValueError(f"station {station_id!r} not found in official MeteoSwiss metadata CSV")
        return stations[key]
    return stations


def search_stations_by_name(query: str) -> dict:
    """Fetches the full official metadata CSV and returns every station
    whose name or abbreviation contains `query` (case-insensitive) -
    {abbr: metadata_dict, ...}, possibly empty.

    This is the ONLY sanctioned way to discover whether a real MeteoSwiss
    station exists under a given name - it is never acceptable to guess a
    plausible-looking abbreviation and try it directly (see
    docs/STATION_RESEARCH.md's explicit prohibition, and the "cor" vs
    "cov" mixup that prohibition exists because of)."""
    all_stations = fetch_station_metadata(station_id=None)
    q = query.strip().lower()
    return {
        abbr: meta for abbr, meta in all_stations.items()
        if q in abbr or q in (meta.get("name") or "").lower()
    }


def _safe_float(raw):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN check


def _parse_generic_station_csv(text: str, field_columns: dict, confirmed_columns: tuple,
                                requested_variables=None) -> dict:
    """Shared core behind parse_generic_station_csv (hourly, 'h0/h1'
    columns) and parse_generic_station_csv_10min (10-minute, 'z0/z1/s0'
    columns) - identical parsing/validation logic, only the field->column
    table differs. requested_variables=None parses whatever's available
    (best-effort); pass an explicit list of normalized field names to
    REQUIRE them - a variable requested but whose column isn't in this
    CSV's header raises ValueError immediately rather than silently
    returning None for it forever.

    Returns {"observations": {datetime_utc: {field: value}},
             "raw_column_map": {field: raw_column_code},
             "units": {field: unit_string},
             "confirmed_columns": [...], "unconfirmed_columns": [...]}."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    header_lower = {h.lower() for h in (reader.fieldnames or [])}

    wanted = requested_variables if requested_variables is not None else list(field_columns)
    raw_column_map = {}
    missing = []
    for field in wanted:
        spec = field_columns.get(field)
        if spec is None:
            missing.append(field)  # not a recognized normalized field name at all
            continue
        column, _unit = spec
        if column.lower() not in header_lower:
            missing.append(field)
            continue
        raw_column_map[field] = column

    if requested_variables is not None and missing:
        raise ValueError(
            f"requested core variable(s) not present in this station's CSV header: {missing} "
            f"(available columns: {sorted(header_lower)})"
        )

    obs = {}
    for row in reader:
        row = {k.lower(): v for k, v in row.items()}
        ts_raw = row.get("reference_timestamp") or row.get("time")
        if not ts_raw:
            continue
        try:
            dt = datetime.strptime(ts_raw.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        record = {}
        for field, column in raw_column_map.items():
            val = _safe_float(row.get(column.lower()))
            if val is not None and field in _KMH_TO_MS_FIELDS:
                val = val * (1000.0 / 3600.0)
            record[field] = val
        obs[dt] = record

    used_columns = set(raw_column_map.values())
    return {
        "observations": obs,
        "raw_column_map": raw_column_map,
        "units": {f: ("m/s" if f in _KMH_TO_MS_FIELDS else field_columns[f][1]) for f in raw_column_map},
        "confirmed_columns": sorted(used_columns & set(confirmed_columns)),
        "unconfirmed_columns": sorted(used_columns - set(confirmed_columns)),
        "parser_version": PARSER_VERSION,
    }


def parse_generic_station_csv(text: str, requested_variables=None) -> dict:
    """Generic multi-variable HOURLY ('h0/h1'-suffixed columns) parser -
    see _parse_generic_station_csv for the shared behaviour/return shape."""
    return _parse_generic_station_csv(text, GENERIC_FIELD_COLUMNS, CONFIRMED_COLUMNS, requested_variables)


def parse_generic_station_csv_10min(text: str, requested_variables=None) -> dict:
    """Generic multi-variable 10-MINUTE ('z0/z1/s0'-suffixed columns)
    parser, for MeteoSwiss's ogd-smn_<station>_t_*.csv files - see
    _parse_generic_station_csv for the shared behaviour/return shape."""
    return _parse_generic_station_csv(text, GENERIC_FIELD_COLUMNS_10MIN, CONFIRMED_COLUMNS_10MIN,
                                       requested_variables)


def fetch_station_observations(station_id: str, include_historical: bool = True, variables=None) -> dict:
    """Generic version of _fetch_station_observations: fetches whatever
    hourly CSV file(s) the STAC catalog lists for this station (same
    discovery mechanism as fetch_sam_hourly_observations/
    fetch_pressure_observations) and parses every requested variable with
    parse_generic_station_csv(). Returns
    {"observations": {...}, "raw_column_map": {...}, "units": {...},
     "confirmed_columns": [...], "unconfirmed_columns": [...],
     "source_assets": [urls actually fetched], "parser_version": ...}."""
    recent_url = _recent_url(station_id)
    urls = [recent_url]
    if include_historical:
        try:
            urls = _discover_hourly_urls(station_id)
            if not urls:
                urls = [recent_url]
        except requests.RequestException as e:
            print(f"[warn] {station_id} STAC catalog lookup failed ({e}); falling back to recent file only")
            urls = [recent_url]

    merged_obs = {}
    raw_column_map = {}
    units = {}
    confirmed_columns = set()
    unconfirmed_columns = set()
    source_assets = []
    for url in urls:
        try:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            text = r.content.decode("utf-8", errors="replace")
        except requests.RequestException as e:
            print(f"[warn] could not fetch {url}: {e}")
            continue
        result = parse_generic_station_csv(text, requested_variables=variables)
        merged_obs.update(result["observations"])
        raw_column_map.update(result["raw_column_map"])
        units.update(result["units"])
        confirmed_columns.update(result["confirmed_columns"])
        unconfirmed_columns.update(result["unconfirmed_columns"])
        source_assets.append(url)
        print(f"  loaded {len(result['observations'])} hours from {url.rsplit('/', 1)[-1]}")

    return {
        "observations": merged_obs,
        "raw_column_map": raw_column_map,
        "units": units,
        "confirmed_columns": sorted(confirmed_columns),
        "unconfirmed_columns": sorted(unconfirmed_columns),
        "source_assets": source_assets,
        "parser_version": PARSER_VERSION,
    }

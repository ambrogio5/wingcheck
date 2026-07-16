"""
station_registry.py - loads and validates config/stations.json, the
machine-readable registry of every weather station this project knows
about (in production use or under research).

This module holds ONLY metadata plus small lookup helpers - it does not
fetch data. Provider-specific fetch logic lives in meteoswiss.py (and any
future provider would get its own module); historical_data.py and
station_features.py turn this registry's entries into actual fetch calls,
but only for stations with enabled=true.

VERIFICATION IS NOT DECORATIVE. Only stations with `enabled: true` in
config/stations.json have been confirmed against a live provider fetch and
have real historical data already in this repo (sam, lug, sma - the same
three stations already used by the production model). Every other entry
is `enabled: false` and `verification: "unverified"` - a plausible station
code proposed from general knowledge of the Swiss station network, never
confirmed. See docs/STATION_RESEARCH.md for the full narrative, including
why each unverified candidate is still unconfirmed.

Do not flip `enabled` to true, and do not invent a new station_id, without
an actual successful fetch inspected by a human - see PROMOTION note in
docs/STATION_RESEARCH.md.
"""

import json
import os
from typing import NamedTuple, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(BASE_DIR, "config", "stations.json")

VALID_ROLES = (
    "source_region",
    "pass",
    "target_region",
    "down_valley",
    "summit",
    "synoptic_pressure",
    "competing_flow",
    "ground_truth",
)

VALID_VERIFICATIONS = ("confirmed", "unverified")


class Station(NamedTuple):
    station_id: str
    name: str
    provider: str
    latitude: Optional[float]
    longitude: Optional[float]
    elevation_m: Optional[float]
    roles: tuple
    available_variables: tuple
    historical_available: Optional[bool]
    live_available: Optional[bool]
    licence: str
    reporting_delay_minutes: Optional[float]
    enabled: bool
    verification: str
    notes: str


def _station_from_dict(d: dict) -> Station:
    return Station(
        station_id=d["station_id"],
        name=d["name"],
        provider=d["provider"],
        latitude=d.get("latitude"),
        longitude=d.get("longitude"),
        elevation_m=d.get("elevation_m"),
        roles=tuple(d.get("roles", [])),
        available_variables=tuple(d.get("available_variables", [])),
        historical_available=d.get("historical_available"),
        live_available=d.get("live_available"),
        licence=d.get("licence", "unknown"),
        reporting_delay_minutes=d.get("reporting_delay_minutes"),
        enabled=bool(d.get("enabled", False)),
        verification=d.get("verification", "unverified"),
        notes=d.get("notes", ""),
    )


def load_registry(path: str = None) -> dict:
    """Returns {station_id: Station} for every entry in config/stations.json."""
    path = path or REGISTRY_PATH
    with open(path) as f:
        raw = json.load(f)
    stations = {}
    for entry in raw.get("stations", []):
        s = _station_from_dict(entry)
        stations[s.station_id] = s
    return stations


def validate_registry(stations: dict) -> list:
    """Returns a list of human-readable problem strings (empty if the
    registry is well-formed). Does not raise - callers decide severity."""
    problems = []
    for sid, s in stations.items():
        if not s.station_id or not s.name:
            problems.append(f"{sid}: missing station_id or name")
        if not s.roles:
            problems.append(f"{sid}: no roles assigned")
        for role in s.roles:
            if role not in VALID_ROLES:
                problems.append(f"{sid}: unknown role {role!r}")
        if s.verification not in VALID_VERIFICATIONS:
            problems.append(f"{sid}: unknown verification {s.verification!r}")
        if s.verification != "confirmed" and s.enabled:
            problems.append(f"{sid}: enabled=true but verification is not 'confirmed' - "
                             f"a station must be confirmed by a real fetch before it can be enabled")
        if s.enabled and not (s.historical_available or s.live_available):
            problems.append(f"{sid}: enabled=true but neither historical_available nor live_available is true")
    return problems


def enabled_station_ids(stations: dict = None) -> tuple:
    stations = stations if stations is not None else load_registry()
    return tuple(sid for sid, s in stations.items() if s.enabled)


def stations_by_role(role: str, stations: dict = None) -> dict:
    stations = stations if stations is not None else load_registry()
    return {sid: s for sid, s in stations.items() if role in s.roles}


def stations_by_provider(provider: str, stations: dict = None) -> dict:
    stations = stations if stations is not None else load_registry()
    return {sid: s for sid, s in stations.items() if s.provider == provider}


def get_station(station_id: str, stations: dict = None) -> Station:
    stations = stations if stations is not None else load_registry()
    return stations[station_id]


def merge_official_metadata(entry: Station, official_metadata: dict) -> Station:
    """Applies a real fetch's official metadata (meteoswiss.fetch_station_metadata())
    onto a provisional registry entry - official values ALWAYS override
    whatever provisional placeholder was there before (typically None),
    never the other way around. Does not flip `enabled`/`verification`
    itself - a human still reviews the result and updates
    config/stations.json by hand (see docs/STATION_RESEARCH.md's
    promotion process); this only computes what the merged entry WOULD
    look like."""
    return entry._replace(
        latitude=official_metadata.get("latitude", entry.latitude),
        longitude=official_metadata.get("longitude", entry.longitude),
        elevation_m=official_metadata.get("elevation_m", entry.elevation_m),
        name=official_metadata.get("name") or entry.name,
    )

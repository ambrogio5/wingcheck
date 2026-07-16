"""
stations.py - the station metadata registry: what stations exist (or are
believed to exist) around Silvaplana/Maloja/Upper Engadin/Bregaglia, and
what we actually know about each one.

This module holds ONLY metadata - coordinates, provider, roles, and
crucially a `verification` status. It deliberately does NOT contain
provider-specific fetch logic (that stays in meteoswiss.py, and any future
provider gets its own module) - see historical_data.py for how metadata
here is turned into actual sync calls.

VERIFICATION STATUS IS NOT DECORATIVE. Three stations in this registry
have real, successfully fetched and parsed data sitting in
logs/raw_cache/: "sam" (Samedan wind), "lug" (Lugano pressure), "sma"
(Zurich/Fluntern pressure) - these are marked verification="confirmed".
Every other entry was added during a research session (2026-07-16) in a
sandboxed environment where outbound network access to data.geo.admin.ch
was blocked at the gateway level (confirmed via both `requests` and the
WebFetch tool - see docs/STATION_RESEARCH.md for the exact evidence) and
WebFetch was blocked for effectively all external domains. That session
could not verify station codes, coordinates, or data availability for any
station beyond the three above against a live source. Those entries carry
verification="candidate_unconfirmed" (a specific, plausible station code
is proposed, from general knowledge of the Swiss/Engadin station network)
or verification="needs_discovery" (no reliable station code could be
proposed at all) - see each entry's `confidence`/`verification_note`.

DO NOT treat a "candidate_unconfirmed" entry as ground truth. A wrong
guessed station code fails loudly (the STAC API 404s / returns no assets)
rather than silently returning wrong data, so it is safe to attempt a
real sync against these - but no station-family analysis in this
repository is allowed to report a "promising" result for a station that
has never been confirmed to return real data. See station_analysis.py's
requirement to check `verification` before treating any correlation as
meaningful.
"""

from typing import NamedTuple


class Station(NamedTuple):
    name: str
    provider: str                  # "meteoswiss", "arpa_lombardia", "slf_imis", "unknown"
    latitude: float | None
    longitude: float | None
    elevation_m: float | None
    variables: tuple                # e.g. ("wind_speed", "wind_gust", "pressure_sea_level")
    roles: tuple                    # e.g. ("historical_label_proxy", "morning_nowcast")
    verification: str               # "confirmed" | "candidate_unconfirmed" | "needs_discovery"
    confidence: str                 # "high" | "moderate" | "low" | "n/a"
    historical_archive: bool | None  # True/False/None (unknown)
    suitable_for_live_retrieval: bool | None
    suitable_for_backtesting: bool | None
    licence: str
    verification_note: str
    rejected: bool = False
    rejection_reason: str = ""


# --- Confirmed stations: real data already fetched and cached in this repo ---

_CONFIRMED = {
    "sam": Station(
        name="Samedan", provider="meteoswiss",
        latitude=46.5335, longitude=9.8794, elevation_m=1705,
        variables=("wind_speed", "wind_gust"),
        roles=("historical_label_proxy", "morning_nowcast", "wind_feature_family"),
        verification="confirmed", confidence="high",
        historical_archive=True, suitable_for_live_retrieval=True, suitable_for_backtesting=True,
        licence="MeteoSwiss Open Data (opendata.swiss terms of use - attribution required, "
                "commercial use permitted, see https://www.meteoswiss.admin.ch/services-and-publications/service/open-data.html)",
        verification_note=(
            "fu3010h0 (wind speed) / fu3010h1 (gust) columns confirmed against the live "
            "STAC v1 API on 2026-07-16 (see meteoswiss.py's docstring); 399,181 hourly "
            "records cached in logs/raw_cache/samedan_archive.json as of this session. "
            "10km from the Silvaplana target lake - the historical labeling proxy since "
            "kitesailing.ch has no archive; correlation with the real lake reading is "
            "only ~0.52 (see CLAUDE.md's accuracy-ceiling note), an acknowledged ceiling."
        ),
    ),
    "lug": Station(
        name="Lugano", provider="meteoswiss",
        latitude=46.0037, longitude=8.9511, elevation_m=273,
        variables=("pressure_sea_level",),
        roles=("pressure_gradient_south",),
        verification="confirmed", confidence="high",
        historical_archive=True, suitable_for_live_retrieval=True, suitable_for_backtesting=True,
        licence="MeteoSwiss Open Data (opendata.swiss terms of use)",
        verification_note=(
            "pp0qffh0 (QFF sea-level pressure) column confirmed live on 2026-07-16; "
            "196,835 hourly records cached in logs/raw_cache/pressure_lug.json. Feeds "
            "pressure_signal (forecast) and pressure_nowcast_score (real observation)."
        ),
    ),
    "sma": Station(
        name="Zürich / Fluntern", provider="meteoswiss",
        latitude=47.3769, longitude=8.5417, elevation_m=556,
        variables=("pressure_sea_level",),
        roles=("pressure_gradient_north",),
        verification="confirmed", confidence="high",
        historical_archive=True, suitable_for_live_retrieval=True, suitable_for_backtesting=True,
        licence="MeteoSwiss Open Data (opendata.swiss terms of use)",
        verification_note=(
            "pp0qffh0 confirmed live on 2026-07-16; 196,770 hourly records cached in "
            "logs/raw_cache/pressure_sma.json."
        ),
    ),
}

# --- Candidate stations: proposed from general knowledge, NOT verified this session ---
# (network to data.geo.admin.ch and all WebFetch targets was blocked - see module docstring)

_UNVERIFIED_NOTE = (
    "Proposed from general knowledge of the Swiss/SwissMetNet station network, NOT "
    "confirmed against a live API this session (network blocked - see stations.py's "
    "module docstring and docs/STATION_RESEARCH.md). Station code, exact coordinates, "
    "elevation, and variable availability all require confirmation via a real "
    "`historical_data.py sync` run before any result concerning this station may be "
    "reported as anything other than exploratory/unverified."
)

_CANDIDATES = {
    # --- High-altitude / ridge stations (Phase 3 priority list) ---
    "cor": Station(
        name="Corvatsch (summit)", provider="meteoswiss",
        latitude=46.4145, longitude=9.8215, elevation_m=3315,
        variables=("wind_speed", "wind_gust", "wind_direction", "air_temperature"),
        roles=("summit_wind_family", "vertical_shear_family"),
        verification="candidate_unconfirmed", confidence="low",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown - presumed MeteoSwiss Open Data if it is a SwissMetNet station",
        verification_note=_UNVERIFIED_NOTE + (
            " Corvatsch has a cable-car-served summit station used for avalanche/ski "
            "operations; whether it is a full SwissMetNet member (vs. an SLF/IMIS-only "
            "or ski-operator-only sensor) is exactly the open question a real sync "
            "must answer."
        ),
    ),
    "piz_nair": Station(
        name="Piz Nair (near St. Moritz)", provider="unknown",
        latitude=46.4917, longitude=9.8354, elevation_m=3057,
        variables=("wind_speed", "wind_gust", "wind_direction"),
        roles=("summit_wind_family", "vertical_shear_family"),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown",
        verification_note=_UNVERIFIED_NOTE + (
            " No station code could be proposed with even low confidence - Piz Nair is "
            "a well-known ski/panorama summit above St. Moritz, but no MeteoSwiss "
            "SwissMetNet code is reliably known; it may only be instrumented by "
            "SLF/IMIS or the local ski operator, neither of which was reachable to "
            "confirm this session."
        ),
    ),
    "diavolezza": Station(
        name="Diavolezza", provider="unknown",
        latitude=46.4227, longitude=9.9716, elevation_m=2973,
        variables=("wind_speed", "wind_gust", "wind_direction"),
        roles=("summit_wind_family", "eastern_flow_suppression"),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown",
        verification_note=_UNVERIFIED_NOTE + (
            " Diavolezza is a cable-car station near Bernina; likely candidate for an "
            "SLF/IMIS avalanche-network sensor rather than MeteoSwiss SwissMetNet, but "
            "this is unconfirmed."
        ),
    ),
    "bernina_hospiz": Station(
        name="Bernina Hospiz / Bernina Pass", provider="meteoswiss",
        latitude=46.4103, longitude=10.0231, elevation_m=2253,
        variables=("wind_speed", "wind_gust", "air_temperature"),
        roles=("eastern_flow_suppression", "pass_gradient"),
        verification="candidate_unconfirmed", confidence="low",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown - presumed MeteoSwiss Open Data",
        verification_note=_UNVERIFIED_NOTE,
    ),
    "julier": Station(
        name="Julier Pass", provider="meteoswiss",
        latitude=46.4711, longitude=9.7157, elevation_m=2284,
        variables=("wind_speed", "wind_gust", "air_temperature"),
        roles=("pass_gradient",),
        verification="candidate_unconfirmed", confidence="low",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown - presumed MeteoSwiss Open Data",
        verification_note=_UNVERIFIED_NOTE,
    ),
    "albula": Station(
        name="Albula Pass", provider="unknown",
        latitude=46.5814, longitude=9.6828, elevation_m=2312,
        variables=("wind_speed", "wind_gust", "air_temperature"),
        roles=("pass_gradient",),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown",
        verification_note=_UNVERIFIED_NOTE,
    ),
    "buffalora": Station(
        name="Buffalora / Ofen Pass", provider="meteoswiss",
        latitude=46.6467, longitude=10.2683, elevation_m=1970,
        variables=("wind_speed", "wind_gust", "air_temperature"),
        roles=("far_east_context",),
        verification="candidate_unconfirmed", confidence="low",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown - presumed MeteoSwiss Open Data",
        verification_note=_UNVERIFIED_NOTE + (
            " Far from Silvaplana (Ofen Pass, near the Italian border past Zernez); "
            "included only because the task's candidate list named it - physical "
            "relevance to the Maloja wind is doubtful and should be weighed against "
            "that before any real investment in fetching it."
        ),
    ),

    # --- Direct/valley-level Engadin stations ---
    "st_moritz": Station(
        name="St. Moritz", provider="unknown",
        latitude=46.4908, longitude=9.8355, elevation_m=1822,
        variables=("air_temperature",),
        roles=("valley_context",),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown",
        verification_note=_UNVERIFIED_NOTE + (
            " St. Moritz may be served by the Samedan station for SwissMetNet purposes "
            "rather than having its own distinct automatic station - unconfirmed."
        ),
    ),
    "sils": Station(
        name="Sils / Segl", provider="unknown",
        latitude=46.4297, longitude=9.7514, elevation_m=1797,
        variables=("air_temperature", "wind_speed"),
        roles=("closest_upstream_valley_station",),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown",
        verification_note=_UNVERIFIED_NOTE + (
            " Sils/Segl is the lake immediately upstream of Silvaplana and would be an "
            "extremely valuable station if it exists with real data - highest-priority "
            "target for the next real sync attempt."
        ),
    ),
    "bever": Station(
        name="Bever", provider="unknown", latitude=46.5766, longitude=9.8964, elevation_m=1712,
        variables=(), roles=("valley_context",),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown", verification_note=_UNVERIFIED_NOTE,
    ),
    "zuoz": Station(
        name="Zuoz", provider="unknown", latitude=46.5989, longitude=9.9583, elevation_m=1716,
        variables=(), roles=("valley_context",),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown", verification_note=_UNVERIFIED_NOTE,
    ),
    "pontresina": Station(
        name="Pontresina", provider="unknown", latitude=46.4903, longitude=9.9006, elevation_m=1805,
        variables=(), roles=("valley_context",),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown", verification_note=_UNVERIFIED_NOTE,
    ),

    # --- Upper Bregaglia / southern source region ---
    "vicosoprano": Station(
        name="Vicosoprano", provider="unknown",
        latitude=46.3603, longitude=9.6398, elevation_m=1067,
        variables=(), roles=("bregaglia_heating_family",),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown",
        verification_note=_UNVERIFIED_NOTE + (
            " IMPORTANT: these coordinates are already used in features.py as an "
            "Open-Meteo FORECAST-MODEL grid point (a lat/lon query, not a real "
            "station). No evidence was found this session that Vicosoprano hosts an "
            "actual MeteoSwiss ground station - do not conflate the two. A real "
            "Bregaglia ground station (if one exists) would be a materially different, "
            "and potentially very valuable, addition."
        ),
    ),
    "bondo": Station(
        name="Bondo", provider="unknown", latitude=46.3389, longitude=9.6008, elevation_m=823,
        variables=(), roles=("bregaglia_heating_family",),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown", verification_note=_UNVERIFIED_NOTE,
    ),
    "soglio": Station(
        name="Soglio", provider="unknown", latitude=46.3389, longitude=9.5794, elevation_m=1097,
        variables=(), roles=("bregaglia_heating_family",),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown", verification_note=_UNVERIFIED_NOTE,
    ),
    "castasegna": Station(
        name="Castasegna", provider="unknown", latitude=46.3364, longitude=9.5508, elevation_m=697,
        variables=(), roles=("bregaglia_heating_family", "lower_valley_entrance"),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown", verification_note=_UNVERIFIED_NOTE,
    ),
    "maloja": Station(
        name="Maloja", provider="meteoswiss",
        latitude=46.4030, longitude=9.6880, elevation_m=1815,
        variables=("air_temperature", "precipitation"),
        roles=("bregaglia_heating_family", "pass_summit"),
        verification="candidate_unconfirmed", confidence="moderate",
        historical_archive=True, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown - presumed MeteoSwiss Open Data",
        verification_note=_UNVERIFIED_NOTE + (
            " Maloja is one of Switzerland's oldest continuously operated climate "
            "stations (long precipitation/temperature record) - moderate confidence a "
            "MeteoSwiss station exists here under some code, higher confidence than "
            "most other unconfirmed candidates, but the exact station code used by the "
            "current Open Data API was not confirmed this session."
        ),
    ),

    # --- Pressure-gradient context (beyond the 2 already in production) ---
    "locarno_monti": Station(
        name="Locarno / Monti", provider="meteoswiss",
        latitude=46.1712, longitude=8.7873, elevation_m=366,
        variables=("pressure_sea_level", "air_temperature"),
        roles=("pressure_gradient_west", "ticino_context"),
        verification="candidate_unconfirmed", confidence="moderate",
        historical_archive=True, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown - presumed MeteoSwiss Open Data",
        verification_note=_UNVERIFIED_NOTE,
    ),
    "poschiavo": Station(
        name="Poschiavo", provider="meteoswiss",
        latitude=46.3247, longitude=10.0389, elevation_m=1078,
        variables=("air_temperature", "pressure_sea_level"),
        roles=("pressure_gradient_east", "eastern_flow_suppression"),
        verification="candidate_unconfirmed", confidence="low",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown - presumed MeteoSwiss Open Data",
        verification_note=_UNVERIFIED_NOTE,
    ),
    "davos": Station(
        name="Davos", provider="meteoswiss",
        latitude=46.8133, longitude=9.8433, elevation_m=1594,
        variables=("air_temperature", "pressure_sea_level", "wind_speed"),
        roles=("pressure_gradient_context", "northeast_context"),
        verification="candidate_unconfirmed", confidence="moderate",
        historical_archive=True, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown - presumed MeteoSwiss Open Data",
        verification_note=_UNVERIFIED_NOTE + (
            " Davos is one of the best-known long-record Swiss climate stations - "
            "moderate confidence it exists in the current API under some code."
        ),
    ),
    "chur": Station(
        name="Chur", provider="meteoswiss",
        latitude=46.8499, longitude=9.5300, elevation_m=555,
        variables=("air_temperature", "pressure_sea_level"),
        roles=("pressure_gradient_context", "north_valley_context"),
        verification="candidate_unconfirmed", confidence="moderate",
        historical_archive=True, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown - presumed MeteoSwiss Open Data",
        verification_note=_UNVERIFIED_NOTE,
    ),

    # --- Italian side (different provider entirely) ---
    "chiavenna": Station(
        name="Chiavenna (Italy)", provider="arpa_lombardia",
        latitude=46.3183, longitude=9.3919, elevation_m=333,
        variables=(), roles=("bregaglia_lower_valley_context",),
        verification="needs_discovery", confidence="n/a",
        historical_archive=None, suitable_for_live_retrieval=None, suitable_for_backtesting=None,
        licence="unknown - ARPA Lombardia open data licence not reviewed this session",
        verification_note=_UNVERIFIED_NOTE + (
            " ARPA Lombardia is a completely different provider/API/licensing regime "
            "from MeteoSwiss; no adapter exists in this codebase yet (see "
            "historical_data.py's PROVIDER_ADAPTERS). This is the lowest-priority "
            "candidate to actually implement, given the extra integration cost for a "
            "station this far down-valley from the thermal source region."
        ),
    ),
}

STATIONS = {**_CONFIRMED, **_CANDIDATES}


# --- Explicitly rejected candidates (documented per Phase 3's requirement to
# record even rejected stations, with a reason) ---

REJECTED = {
    "ski_resort_webcam_widgets": Station(
        name="Generic ski-resort webcam/weather widgets (Corvatsch-Diavolezza-Lagalb "
             "company site, engadin.ch reports page, etc.)",
        provider="unknown", latitude=None, longitude=None, elevation_m=None,
        variables=(), roles=(),
        verification="needs_discovery", confidence="n/a",
        historical_archive=False, suitable_for_live_retrieval=False, suitable_for_backtesting=False,
        licence="unknown",
        verification_note=(
            "Rejected as a data source (not a station) per the explicit instruction "
            "not to assume a named station exists merely because a webcam or resort "
            "page displays weather. These are presentation layers, typically over "
            "either a MeteoSwiss feed or a private sensor with no documented API, no "
            "historical archive, and no stable machine-readable access - see "
            "docs/STATION_RESEARCH.md."
        ),
        rejected=True,
        rejection_reason="no_machine_readable_access",
    ),
}


def confirmed_station_ids() -> tuple:
    """Station ids with verification == 'confirmed' - the only ones any
    analysis may treat as ground truth rather than exploratory research."""
    return tuple(sid for sid, s in STATIONS.items() if s.verification == "confirmed")


def stations_by_role(role: str) -> dict:
    return {sid: s for sid, s in STATIONS.items() if role in s.roles}


def stations_by_provider(provider: str) -> dict:
    return {sid: s for sid, s in STATIONS.items() if s.provider == provider}

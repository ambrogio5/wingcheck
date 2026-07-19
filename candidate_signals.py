"""
candidate_signals.py - LOG-ONLY probation for three new candidate signals.

These are recorded-but-NOT-yet-scored: nothing here touches features.py,
weights.json, model scoring, or the dashboard tiers. They are on probation
under the same maturity discipline as the SIA/lake ratio in
config/ground_truth_policy.json - we accumulate real history now so they
can be evaluated properly against lake wind in ~2-3 weeks, once there are
enough INDEPENDENT days to tell a real signal from a between-day artifact.

Why probation (do not trust the 2-day sample):
  - The 2-day correlations (COV daytime wind r~=0.80, VIO-SAM gradient
    r~=0.66) are almost entirely a BETWEEN-DAY artifact - Fri was windy,
    Sat was not, so any signal that also differs Fri/Sat correlates by
    coincidence, not mechanism.
  - COV free-air wind actually FLIPS sign within a single afternoon, so
    even its within-day relationship to lake wind is unstable.
Nothing here is trustworthy until ~2 weeks of independent days exist.

The three signals (per 10-minute UTC observation timestamp):
  1. corvatsch_wind : COV (Piz Corvatsch, 3294m) fu3010z0 speed / fu3010z1
     gust / dkl010z0 direction - the free-air flow aloft that may gate
     whether the valley thermal establishes.
  2. bregaglia_engadin_gradient : sea-level-reduced pressure difference
     VIO(Vicosoprano, 1089m, warm south side of Maloja) minus SAM(Samedan,
     1709m) and minus SIA(Segl-Maria, 1804m). MUST use a sea-level-reduced
     field (QFF preferred, QNH fallback) - the RAW station pressure
     (prestas0) difference is ~62 hPa of pure altitude offset (VIO is 620m
     lower) and is meteorologically meaningless. The reduction field
     actually used is recorded per pair in provenance.
  3. valley_summit_temp_spread : tre200s0(SIA) minus tre200s0(COV) - a
     thermal-buildup proxy that showed a faint ~1h lead on lake wind in
     the 2-day sample.

Every raw component is stored alongside every derived value, so the
signals can be re-derived differently later (different reduction, different
station pairing) without re-fetching.

Station codes and 10-minute column codes below are the user-verified
official MeteoSwiss values (from the official metadata CSV) - NOT guessed.
`vio` in particular is fetched by its official abbreviation and kept
strictly separate from the existing `vicosoprano` Open-Meteo forecast-grid
entry in config/stations.json (a different thing - see that entry's note).
A station whose 10-minute file cannot be fetched contributes honest None
components and an informational `station_unavailable:<id>` flag; it is
never invented.
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import meteoswiss

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "logs", "candidate_signals.jsonl")

# Official MeteoSwiss abbreviations (user-verified against the metadata CSV).
STATIONS = ("cov", "vio", "sam", "sia")

# Verified 10-minute raw column codes (already used elsewhere in the repo's
# 10-minute parser). QNH (pp0qnhs0) has no NORMALIZED_FIELDS slot, which is
# exactly why this module reads raw columns directly.
COL_WIND_KMH = "fu3010z0"
COL_GUST_KMH = "fu3010z1"
COL_DIR_DEG = "dkl010z0"
COL_TEMP_C = "tre200s0"
COL_QFF = "pp0qffs0"   # sea-level reduced (preferred for the gradient)
COL_QNH = "pp0qnhs0"   # sea-level reduced (fallback for the gradient)
COL_QFE = "prestas0"   # raw station pressure - stored for context, NEVER used in the gradient

REQUESTED_COLUMNS = (COL_WIND_KMH, COL_GUST_KMH, COL_DIR_DEG, COL_TEMP_C, COL_QFF, COL_QNH, COL_QFE)

KMH_TO_MS = 1000.0 / 3600.0

# On an empty log, seed only the most recent SEED_DAYS of 10-minute ticks
# (keeps the first commit small and the log forward-looking); afterwards
# only ticks strictly newer than the newest already logged are appended.
SEED_DAYS = 2


def _fetch_all(stations=STATIONS, fetch_fn=None):
    """Best-effort per station: {station_id: ({dt_utc: {col: val}}, flags)}.
    A station whose fetch raises contributes ({}, ['station_unavailable:id',
    'fetch_failed:...'])."""
    fetch_fn = fetch_fn or meteoswiss.fetch_station_raw_10min
    out = {}
    for sid in stations:
        try:
            obs = fetch_fn(sid, REQUESTED_COLUMNS)
            out[sid] = (obs, [] if obs else [f"station_no_data:{sid}"])
        except Exception as e:  # network/HTTP/parse - never abort the others
            out[sid] = ({}, [f"station_unavailable:{sid}", f"fetch_failed:{sid}:{e}"])
    return out


def _reduced_pressure(components: dict):
    """(value_hPa, field_used) preferring QFF over QNH; (None, None) if
    neither is populated. Never uses raw QFE - that difference is pure
    altitude offset."""
    if components.get(COL_QFF) is not None:
        return components[COL_QFF], "pp0qffs0"
    if components.get(COL_QNH) is not None:
        return components[COL_QNH], "pp0qnhs0"
    return None, None


def _round(x, n=3):
    return round(x, n) if x is not None else None


def build_record(dt_utc, per_station, station_flags):
    """One probation record for a single 10-minute UTC timestamp. Raw
    components + derived signals + per-pair provenance. Missing pieces are
    honest None, never invented."""
    cov = per_station.get("cov", {})
    vio = per_station.get("vio", {})
    sam = per_station.get("sam", {})
    sia = per_station.get("sia", {})

    cov_wind_ms = _round(cov[COL_WIND_KMH] * KMH_TO_MS) if cov.get(COL_WIND_KMH) is not None else None
    cov_gust_ms = _round(cov[COL_GUST_KMH] * KMH_TO_MS) if cov.get(COL_GUST_KMH) is not None else None

    vio_p, vio_field = _reduced_pressure(vio)
    sam_p, sam_field = _reduced_pressure(sam)
    sia_p, sia_field = _reduced_pressure(sia)

    grad_vio_sam = _round(vio_p - sam_p, 2) if (vio_p is not None and sam_p is not None) else None
    grad_vio_sia = _round(vio_p - sia_p, 2) if (vio_p is not None and sia_p is not None) else None

    temp_spread = (_round(sia[COL_TEMP_C] - cov[COL_TEMP_C], 2)
                   if (sia.get(COL_TEMP_C) is not None and cov.get(COL_TEMP_C) is not None) else None)

    flags = sorted({f for fl in station_flags.values() for f in fl})

    return {
        "observed_at": dt_utc.isoformat(),
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "status": "logged_only_unscored",
        "policy": "on_probation_2-3_weeks",
        # --- signal 1: Corvatsch free-air wind ---
        "corvatsch_wind": {
            "speed_ms": cov_wind_ms, "gust_ms": cov_gust_ms,
            "direction_deg": _round(cov.get(COL_DIR_DEG), 1),
            "raw": {COL_WIND_KMH: cov.get(COL_WIND_KMH), COL_GUST_KMH: cov.get(COL_GUST_KMH),
                    COL_DIR_DEG: cov.get(COL_DIR_DEG)},
        },
        # --- signal 2: altitude-corrected Bregaglia->Engadin pressure gradient ---
        "bregaglia_engadin_gradient": {
            "vio_minus_sam_hpa": grad_vio_sam,
            "vio_minus_sia_hpa": grad_vio_sia,
            "reduction_field_used": {"vio": vio_field, "sam": sam_field, "sia": sia_field},
            "reduced_pressure_hpa": {"vio": _round(vio_p, 2), "sam": _round(sam_p, 2), "sia": _round(sia_p, 2)},
            "raw": {
                "vio": {COL_QFF: vio.get(COL_QFF), COL_QNH: vio.get(COL_QNH), COL_QFE: vio.get(COL_QFE)},
                "sam": {COL_QFF: sam.get(COL_QFF), COL_QNH: sam.get(COL_QNH), COL_QFE: sam.get(COL_QFE)},
                "sia": {COL_QFF: sia.get(COL_QFF), COL_QNH: sia.get(COL_QNH), COL_QFE: sia.get(COL_QFE)},
            },
        },
        # --- signal 3: valley-summit temperature spread ---
        "valley_summit_temp_spread": {
            "sia_minus_cov_c": temp_spread,
            "raw": {"sia_" + COL_TEMP_C: sia.get(COL_TEMP_C), "cov_" + COL_TEMP_C: cov.get(COL_TEMP_C)},
        },
        "quality_flags": flags,
        "provenance": {
            "source": "meteoswiss:10min:t_recent",
            "stations": list(STATIONS),
            "column_codes": {"wind": COL_WIND_KMH, "gust": COL_GUST_KMH, "dir": COL_DIR_DEG,
                             "temp": COL_TEMP_C, "qff": COL_QFF, "qnh": COL_QNH, "qfe": COL_QFE},
        },
    }


def _load_existing_timestamps():
    if not os.path.exists(LOG_PATH):
        return set()
    seen = set()
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    seen.add(json.loads(line)["observed_at"])
                except (ValueError, KeyError):
                    continue
    return seen


def sample(fetch_fn=None, now=None):
    """Fetch, derive, and APPEND new 10-minute ticks (deduped by
    observed_at) to logs/candidate_signals.jsonl. On an empty log, seeds
    only the last SEED_DAYS; afterwards appends only ticks newer than the
    newest already logged. Returns a summary dict."""
    now = now or datetime.now(timezone.utc)
    fetched = _fetch_all(fetch_fn=fetch_fn)
    per_station = {sid: obs for sid, (obs, _flags) in fetched.items()}
    station_flags = {sid: flags for sid, (_obs, flags) in fetched.items()}

    all_ts = sorted({dt for obs in per_station.values() for dt in obs})
    existing = _load_existing_timestamps()
    if existing:
        newest = max(existing)
        candidates = [dt for dt in all_ts if dt.isoformat() > newest and dt.isoformat() not in existing]
    else:
        cutoff = now - timedelta(days=SEED_DAYS)
        candidates = [dt for dt in all_ts if dt >= cutoff]

    new_records = []
    for dt in candidates:
        per = {sid: per_station.get(sid, {}).get(dt, {}) for sid in STATIONS}
        new_records.append(build_record(dt, per, station_flags))

    if new_records:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            for rec in new_records:
                f.write(json.dumps(rec) + "\n")

    return {
        "appended": len(new_records),
        "ticks_fetched": len(all_ts),
        "stations_available": [sid for sid in STATIONS if per_station.get(sid)],
        "stations_unavailable": [sid for sid in STATIONS if not per_station.get(sid)],
        "latest_record": new_records[-1] if new_records else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Log-only candidate-signal sampler (probation, unscored)")
    parser.parse_args(argv)
    summary = sample()
    printable = dict(summary)
    printable["latest_record"] = summary["latest_record"]  # full record - useful to eyeball live values
    print(json.dumps(printable, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Auditable ground-truth registry for training and evaluation.

The registry preserves every observation from every source.  Label selection
is a separate, deterministic operation with the provisional SIA-first policy
(config/ground_truth_policy.json, policy_version 2):

    1. direct lake measurement (kitesailing / windsurfcenter / silvaplana_lake)
    2. official MeteoSwiss Segl-Maria (sia) - the principal near-lake
       reference while direct lake coverage is still sparse
    3. missing label - never fabricated

Samedan (sam) is deliberately NOT a default label source under this policy -
it stays in the registry as a preserved contextual observation and remains
available to an explicitly-named research experiment (allow_samedan_fallback,
off by default), but a missing SIA hour must NOT silently become a
Samedan-labeled hour.  SIA's measurement quality (official MeteoSwiss
station) is deliberately kept separate from its UNMEASURED equivalence to
the Windsurfcenter/lake target - see sia_calibration_status in the policy
file and station_calibration.py.  No source record is ever overwritten.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REGISTRY_PATH = os.path.join(BASE_DIR, "logs", "historical", "ground_truth", "observations.jsonl")
DEFAULT_POLICY_PATH = os.path.join(BASE_DIR, "config", "ground_truth_policy.json")

SOURCE_PRIORITY = {"kitesailing": 0, "windsurfcenter": 0, "silvaplana_lake": 0, "sia": 1, "sam": 2}

# The rideability threshold applied to a SIA-sourced label, in knots.
# PROVISIONAL (owner-approved 2026-07-19, "assume it's right and correct
# later"): 8.0kt at SIA is treated as equivalent to the 10kt lake criterion
# (SILVAPLANA_MARGINAL_KT), i.e. an assumed SIA/lake speed ratio of ~0.8.
# Evidence, both thin but consistent:
#   1. 100 aligned 20-min points, kitesailing.ch chart vs SIA 10-min data,
#      2026-07-17 15:30 -> 2026-07-19 00:50 CEST: mean SIA/lake ratio 0.80
#      (n=60 points with lake wind >= 10 km/h), r=0.51 overall, near-lockstep
#      (both 100% SW) through the Jul 17 thermal afternoon.
#   2. logs/historical/reports/station_calibration_kitesailing_sia_*.json:
#      bias_sia_minus_lake ~= -1.16 m/s (~-2.2kt), pearson 0.88, n=5 - same
#      direction and similar magnitude at ~10kt lake wind.
# Neither dataset passes station_calibration.py's 14-independent-day
# maturity gate - this is explicitly an assumption to be re-derived (and
# this constant re-tuned) once the kitesailing sampler has ~2 weeks of
# overlap. If the model starts missing days the owner actually rides,
# lower this; if it flags dead days, raise it.
# Both verify_and_learn.py (live) and backtest.py (historical retrain)
# import THIS constant so the live and historical labeling criteria can
# never silently drift apart again - that split (SAM_PROXY_KT vs
# SILVAPLANA_MARGINAL_KT) was exactly the documented labeling-criterion
# mismatch the SIA-first policy closed.
SIA_REFERENCE_KT = 8.0

# Provenance/derivation markers that describe HOW a record was produced, not
# a problem with it - they must never disqualify an observation from
# labeling the way a real data-quality flag (implausible value, gust <
# speed, ...) does. "derived_from_10min_mean" + "n_10min_samples:N" mark
# sia hourly records honestly aggregated from real 10-minute readings (see
# sia_import.py) - real measurements, transparently derived.
INFORMATIONAL_FLAG_PREFIXES = ("derived_from_10min", "n_10min_samples:")


def blocking_flags(row: dict) -> list:
    return [f for f in row.get("quality_flags", [])
            if not any(f.startswith(p) for p in INFORMATIONAL_FLAG_PREFIXES)]


@dataclass(frozen=True)
class LabelPolicy:
    policy_version: int = 2
    allow_sia_substitution: bool = True
    sia_confidence: Optional[float] = None
    sia_calibration_status: str = "insufficient_evidence"
    allow_samedan_fallback: bool = False
    samedan_confidence: float = 0.40
    maximum_quality_flags: int = 0


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def canonical_observation(*, timestamp_utc: str, source: str, station_id: str,
                          wind_speed_ms=None, wind_gust_ms=None,
                          wind_direction_deg=None, temperature_c=None,
                          quality_flags=None, provenance=None,
                          validation_status="unreviewed", confidence=None) -> dict:
    dt = datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("ground-truth timestamps must include a UTC offset")
    dt = dt.astimezone(timezone.utc)
    direction = _finite(wind_direction_deg)
    if direction is not None and not 0 <= direction <= 360:
        raise ValueError("wind direction must be within 0..360 degrees")
    conf = _finite(confidence)
    if conf is not None and not 0 <= conf <= 1:
        raise ValueError("confidence must be within 0..1")
    return {
        "timestamp_utc": dt.isoformat(),
        "source": source,
        "station_id": station_id,
        "wind_speed_ms": _finite(wind_speed_ms),
        "wind_gust_ms": _finite(wind_gust_ms),
        "wind_direction_deg": direction,
        "temperature_c": _finite(temperature_c),
        "quality_flags": sorted(set(quality_flags or [])),
        "validation_status": validation_status,
        "confidence": conf,
        "provenance": provenance or {},
    }


def records_from_normalized_station(records: Iterable[dict], source: str = None) -> list[dict]:
    converted = []
    for row in records:
        obs = canonical_observation(
            timestamp_utc=row["timestamp_utc"],
            source=source or row.get("station_id"),
            station_id=row["station_id"],
            wind_speed_ms=row.get("wind_speed_ms"),
            wind_gust_ms=row.get("wind_gust_ms"),
            wind_direction_deg=row.get("wind_direction_deg"),
            temperature_c=row.get("temperature_c"),
            quality_flags=row.get("quality_flags"),
            provenance={"source_asset": row.get("source_asset"), "retrieved_at": row.get("retrieved_at")},
            validation_status="source_validated" if not blocking_flags(row) else "flagged",
            confidence=1.0 if source in ("kitesailing", "windsurfcenter", "silvaplana_lake") else None,
        )
        converted.append(obs)
    return converted


KMH_TO_MS = 1000.0 / 3600.0


def records_from_kitesailing(observations: Iterable[dict]) -> list[dict]:
    """Converts logs/kitesailing_observations.jsonl rows (the real scraped
    Silvaplana lake readings - see kitesailing_weather.py) into canonical
    observations under source 'kitesailing'. Deliberately NOT labeled
    'windsurfcenter': the widget is embedded on kitesailing.ch and its
    upstream sensor identity/ownership has not been conclusively
    demonstrated - see docs/DATA_ARCHITECTURE.md's station-identity
    section. Wind arrives in km/h and converts to m/s here."""
    converted = []
    for row in observations:
        ts = row.get("observed_at")
        if not ts or row.get("avg_wind_kmh") is None:
            continue
        converted.append(canonical_observation(
            timestamp_utc=ts,
            source="kitesailing",
            station_id="silvaplana_kitesailing",
            wind_speed_ms=row["avg_wind_kmh"] * KMH_TO_MS,
            wind_gust_ms=(row.get("gust_kmh") or 0) * KMH_TO_MS if row.get("gust_kmh") is not None else None,
            wind_direction_deg=row.get("wind_dir_deg"),
            temperature_c=row.get("temp_c"),
            provenance={"source_asset": "logs/kitesailing_observations.jsonl",
                        "retrieved_at": row.get("retrieved_at") or ts},
            validation_status="source_validated",
            confidence=1.0,
        ))
    return converted


def merge_registry(existing: Iterable[dict], incoming: Iterable[dict]) -> list[dict]:
    """Preserve multiple sources per timestamp; dedupe only exact source/station rows."""
    merged = {(r["timestamp_utc"], r["source"], r["station_id"]): r for r in existing}
    for row in incoming:
        key = (row["timestamp_utc"], row["source"], row["station_id"])
        if key not in merged:
            merged[key] = row
        elif len(row.get("provenance", {})) > len(merged[key].get("provenance", {})):
            merged[key] = row
    return sorted(merged.values(), key=lambda r: (r["timestamp_utc"], r["source"], r["station_id"]))


def load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: str, records: Iterable[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    os.replace(tmp, path)


def load_policy(path: str = DEFAULT_POLICY_PATH) -> LabelPolicy:
    if not os.path.exists(path):
        return LabelPolicy()
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return LabelPolicy(**{key: raw[key] for key in LabelPolicy.__dataclass_fields__ if key in raw})


def select_label(observations: Iterable[dict], policy: LabelPolicy) -> Optional[dict]:
    """Deterministic SIA-first selection: direct lake sources always win;
    sia is the principal reference when no lake reading exists; sam is
    excluded unless the policy explicitly enables the legacy research
    fallback. Informational provenance flags (blocking_flags) never
    disqualify a record; real quality flags do. Returns None (missing
    label) rather than ever fabricating one."""
    eligible = []
    for row in observations:
        if row.get("wind_speed_ms") is None or len(blocking_flags(row)) > policy.maximum_quality_flags:
            continue
        source = row.get("source")
        if source == "sia" and not policy.allow_sia_substitution:
            continue
        if source == "sam" and not policy.allow_samedan_fallback:
            continue
        candidate = dict(row)
        if source == "sia":
            candidate["confidence"] = policy.sia_confidence
        elif source == "sam":
            candidate["confidence"] = policy.samedan_confidence
        candidate["policy_version"] = policy.policy_version
        eligible.append(candidate)
    if not eligible:
        return None
    return min(eligible, key=lambda r: (SOURCE_PRIORITY.get(r.get("source"), 99), r["station_id"]))


def coverage_summary(records: Iterable[dict]) -> dict:
    summary = {}
    for row in records:
        source = row["source"]
        item = summary.setdefault(source, {"n": 0, "start": None, "end": None,
                                            "quality_flagged": 0, "informational_flagged": 0})
        item["n"] += 1
        if blocking_flags(row):
            item["quality_flagged"] += 1
        elif row.get("quality_flags"):
            item["informational_flagged"] += 1
        ts = row["timestamp_utc"]
        item["start"] = ts if item["start"] is None or ts < item["start"] else item["start"]
        item["end"] = ts if item["end"] is None or ts > item["end"] else item["end"]
    return summary


def build_registry(station_files: dict[str, str], output_path=DEFAULT_REGISTRY_PATH) -> dict:
    records = load_jsonl(output_path)
    added_by_source = {}
    for source, path in station_files.items():
        before = len(records)
        if source == "kitesailing":
            incoming = records_from_kitesailing(load_jsonl(path))
        else:
            incoming = records_from_normalized_station(load_jsonl(path), source=source)
        records = merge_registry(records, incoming)
        added_by_source[source] = len(records) - before
    write_jsonl(output_path, records)
    return {"path": output_path, "total": len(records), "added_by_source": added_by_source,
            "coverage": coverage_summary(records)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build and inspect the canonical ground-truth registry")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--source", action="append", default=[], metavar="NAME=JSONL")
    build.add_argument("--output", default=DEFAULT_REGISTRY_PATH)
    cov = sub.add_parser("coverage")
    cov.add_argument("--registry", default=DEFAULT_REGISTRY_PATH)
    args = parser.parse_args(argv)
    if args.command == "build":
        sources = dict(item.split("=", 1) for item in args.source)
        print(json.dumps(build_registry(sources, args.output), indent=2))
    else:
        print(json.dumps(coverage_summary(load_jsonl(args.registry)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

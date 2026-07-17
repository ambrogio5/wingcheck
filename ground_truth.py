"""Auditable ground-truth registry for training and evaluation.

The registry preserves every observation from every source.  Label selection is
a separate, deterministic operation: direct lake measurements win; SIA may be
used only when a reviewed calibration policy explicitly permits substitution;
Samedan remains a lower-confidence fallback.  No source record is overwritten.
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

SOURCE_PRIORITY = {"windsurfcenter": 0, "silvaplana_lake": 0, "sia": 1, "sam": 2}


@dataclass(frozen=True)
class LabelPolicy:
    allow_sia_substitution: bool = False
    sia_confidence: Optional[float] = None
    allow_samedan_fallback: bool = True
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
        converted.append(canonical_observation(
            timestamp_utc=row["timestamp_utc"],
            source=source or row.get("station_id"),
            station_id=row["station_id"],
            wind_speed_ms=row.get("wind_speed_ms"),
            wind_gust_ms=row.get("wind_gust_ms"),
            wind_direction_deg=row.get("wind_direction_deg"),
            temperature_c=row.get("temperature_c"),
            quality_flags=row.get("quality_flags"),
            provenance={"source_asset": row.get("source_asset"), "retrieved_at": row.get("retrieved_at")},
            validation_status="source_validated" if not row.get("quality_flags") else "flagged",
            confidence=1.0 if source in ("windsurfcenter", "silvaplana_lake") else None,
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
    eligible = []
    for row in observations:
        if row.get("wind_speed_ms") is None or len(row.get("quality_flags", [])) > policy.maximum_quality_flags:
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
        eligible.append(candidate)
    if not eligible:
        return None
    return min(eligible, key=lambda r: (SOURCE_PRIORITY.get(r.get("source"), 99), r["station_id"]))


def coverage_summary(records: Iterable[dict]) -> dict:
    summary = {}
    for row in records:
        source = row["source"]
        item = summary.setdefault(source, {"n": 0, "start": None, "end": None, "flagged": 0})
        item["n"] += 1
        item["flagged"] += bool(row.get("quality_flags"))
        ts = row["timestamp_utc"]
        item["start"] = ts if item["start"] is None or ts < item["start"] else item["start"]
        item["end"] = ts if item["end"] is None or ts > item["end"] else item["end"]
    return summary


def build_registry(station_files: dict[str, str], output_path=DEFAULT_REGISTRY_PATH) -> dict:
    records = load_jsonl(output_path)
    added_by_source = {}
    for source, path in station_files.items():
        before = len(records)
        records = merge_registry(records, records_from_normalized_station(load_jsonl(path), source=source))
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

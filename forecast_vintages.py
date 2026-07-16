"""
forecast_vintages.py - archives the actual forecast payload exactly as it
was available at issue time, so a future retrain can eventually train on
genuine multi-day-lead forecasts instead of Open-Meteo's 0-hour historical
archive (backtest.py's long-standing, explicitly documented limitation -
see CLAUDE.md and backtest.py's own docstring).

Called from forecast_and_log.py, once per forecast run (07:00 & 10:00
CEST), right after features.fetch_raw() returns. Best-effort: archiving a
vintage must never break the actual forecast/Telegram pipeline, the same
philosophy as features.py's nowcast fetches.

What gets archived: the forecast-MODEL parts of `raw` - "silvaplana",
"bregaglia", "upper", "lugano", "zurich" (the deterministic best_match
forecast at each queried point) and "ensemble" (the ICON/GFS/ECMWF
individual series, already bundled into one response by Open-Meteo's
`models=` parameter - see features.py's _fetch_ensemble_wind). This
deliberately EXCLUDES "samedan_obs"/"lugano_obs"/"zurich_obs" - those are
real station NOWCAST observations, not forecast-model output, and
historical_data.py's own archive (station_hourly/) is where they belong;
duplicating them here would be redundant.

Storage: gzip-compressed JSON, one file per forecast run, deduplicated by
checksum (re-running forecast_and_log.py against an unchanged payload -
e.g. a manual re-run within the same cycle - does not create a second
copy). At roughly 2 runs/day, this is a small, bounded amount of data
(see docs/DATA_ARCHITECTURE.md's retention-and-size estimate) - unlike
the station archive, these payloads are NOT re-derivable once the
forecast window has passed, so they ARE committed to git going forward.
"""

import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VINTAGES_DIR = os.path.join(BASE_DIR, "logs", "historical", "forecast_vintages")
INDEX_PATH = os.path.join(VINTAGES_DIR, "index.jsonl")
ZURICH_TZ = ZoneInfo("Europe/Zurich")

SCHEMA_VERSION = 1

# Forecast-model keys in features.fetch_raw()'s return dict - see this
# module's docstring for why samedan_obs/lugano_obs/zurich_obs are excluded.
FORECAST_PAYLOAD_KEYS = ("silvaplana", "bregaglia", "upper", "lugano", "zurich", "ensemble")


def _checksum(payload) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def extract_forecast_payload(raw: dict) -> dict:
    """The subset of features.fetch_raw()'s output that constitutes a
    genuine forecast-model vintage (see module docstring)."""
    return {k: raw[k] for k in FORECAST_PAYLOAD_KEYS if k in raw and raw[k] is not None}


def _target_times_and_lead_hours(payload: dict, issue_time_utc: datetime):
    times = payload.get("silvaplana", {}).get("time", [])
    lead_hours = []
    for t in times:
        target_local = datetime.fromisoformat(t).replace(tzinfo=ZURICH_TZ)
        target_utc = target_local.astimezone(timezone.utc)
        lead_hours.append(round((target_utc - issue_time_utc).total_seconds() / 3600, 2))
    return times, lead_hours


def read_index() -> list:
    if not os.path.exists(INDEX_PATH):
        return []
    with open(INDEX_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def _append_index(entry: dict):
    os.makedirs(VINTAGES_DIR, exist_ok=True)
    with open(INDEX_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def archive_forecast_payload(raw: dict, provider: str = "open-meteo", model: str = "best_match+ensemble",
                              issue_time_utc: datetime = None, source_url: str = None) -> dict:
    """Archives `raw` (from features.fetch_raw()) exactly as fetched,
    deduplicated by checksum. Returns the index entry (freshly written, or
    the pre-existing one if this exact payload was already archived - the
    caller can tell the two apart via the returned entry's "retrieved_at"
    if needed, but in normal operation this distinction doesn't matter:
    either way the payload is safely in the archive)."""
    issue_time_utc = issue_time_utc or datetime.now(timezone.utc)
    payload = extract_forecast_payload(raw)
    checksum = _checksum(payload)

    existing = read_index()
    for e in existing:
        if e["checksum"] == checksum:
            return e  # already archived - do not write a duplicate file

    issue_local = issue_time_utc.astimezone(ZURICH_TZ)
    target_times, lead_hours = _target_times_and_lead_hours(payload, issue_time_utc)

    day_dir = os.path.join(
        VINTAGES_DIR, issue_time_utc.strftime("%Y"), issue_time_utc.strftime("%m"), issue_time_utc.strftime("%d"))
    os.makedirs(day_dir, exist_ok=True)
    filename = f"{issue_time_utc.strftime('%Y%m%dT%H%M%SZ')}_{provider}_{model}.json.gz"
    file_path = os.path.join(day_dir, filename)

    with gzip.open(file_path, "wt", encoding="utf-8") as f:
        json.dump(payload, f)

    entry = {
        "issue_timestamp_utc": issue_time_utc.isoformat(),
        "issue_timestamp_local": issue_local.isoformat(),
        "provider": provider,
        "model": model,
        "model_run_time": None,  # not exposed by Open-Meteo's response as of this schema version
        "target_timestamps": target_times,
        "lead_time_hours": lead_hours,
        "source_url": source_url,
        "retrieved_at": issue_time_utc.isoformat(),
        "checksum": checksum,
        "schema_version": SCHEMA_VERSION,
        "file_path": os.path.relpath(file_path, BASE_DIR),
        "n_target_hours": len(target_times),
    }
    _append_index(entry)
    return entry


def archive_forecast_payload_safe(raw: dict, **kwargs):
    """Best-effort wrapper for forecast_and_log.py - archiving a vintage
    must never break the actual forecast/Telegram pipeline."""
    try:
        return archive_forecast_payload(raw, **kwargs)
    except Exception as e:
        print(f"[warn] forecast vintage archiving failed (continuing without it): {e}")
        return None


def load_vintage(file_path: str) -> dict:
    """Reads back one archived payload, given the relative file_path from
    an index entry."""
    full_path = os.path.join(BASE_DIR, file_path)
    with gzip.open(full_path, "rt", encoding="utf-8") as f:
        return json.load(f)


def coverage_summary() -> dict:
    entries = read_index()
    if not entries:
        return {"n_vintages": 0, "earliest_issue": None, "latest_issue": None}
    issues = sorted(e["issue_timestamp_utc"] for e in entries)
    return {
        "n_vintages": len(entries),
        "earliest_issue": issues[0],
        "latest_issue": issues[-1],
        "providers": sorted({e["provider"] for e in entries}),
    }

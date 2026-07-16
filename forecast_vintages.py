"""
forecast_vintages.py - archives the genuine forecast-model payload actually
available at issuance time, before forecast_and_log.py scores it.

Open-Meteo's live API only retains ~3 months of past data, and
backtest.py's historical-archive fetch pulls 0-hour (same-day) data, not a
genuine multi-day-lead forecast (see features.py's docstring) - so this is
the ONLY record of what a real forecast actually said at real lead times,
and it can never be reconstructed after the fact. Every operational
forecast run calls archive_forecast_payload_safe() first.

FORECAST_PAYLOAD_KEYS deliberately excludes the station-nowcast keys
("samedan_obs"/"lugano_obs"/"zurich_obs") - those are refetched fresh
every call and would defeat checksum-based dedup for what is really the
same underlying forecast-model response.

Storage: logs/historical/forecast_vintages/YYYY/MM/DD/<issued_at>_<provider>_<model>.json.gz
(gzip-compressed, one file per genuinely new payload) plus an append-only
logs/historical/forecast_vintages/index.jsonl (one row per archived file).
Deduplicated by content checksum - an unchanged payload (e.g. two forecast
runs the same hour returning identical data) is never stored twice.

Archival failure must never break the actual forecast/Telegram send -
always call archive_forecast_payload_safe(), which catches everything but
logs the failure visibly (stderr), never archive_forecast_payload()
directly from forecast_and_log.py.
"""

import gzip
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VINTAGE_DIR = os.path.join(BASE_DIR, "logs", "historical", "forecast_vintages")
INDEX_PATH = os.path.join(VINTAGE_DIR, "index.jsonl")

FORECAST_PAYLOAD_KEYS = ("silvaplana", "bregaglia", "upper", "lugano", "zurich", "ensemble")
SCHEMA_VERSION = 1
ZURICH_TZ = ZoneInfo("Europe/Zurich")


def extract_forecast_payload(raw: dict) -> dict:
    """Keeps only genuine forecast-model keys - excludes station nowcasts
    (samedan_obs/lugano_obs/zurich_obs), which are re-fetched fresh each
    call and would defeat checksum dedup for the same underlying forecast."""
    return {k: raw[k] for k in FORECAST_PAYLOAD_KEYS if k in raw}


def _checksum(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _target_times_and_lead_hours(payload: dict, issued_at_utc: datetime):
    """Target local timestamps + lead-hours-from-issue for every hour in
    the 'silvaplana' series (the target-spot forecast - present in every
    real call)."""
    series = payload.get("silvaplana", {})
    times = series.get("time", [])
    target_local = []
    lead_hours = []
    for t in times:
        local_dt = datetime.fromisoformat(t).replace(tzinfo=ZURICH_TZ)
        target_local.append(local_dt.isoformat())
        lead_hours.append(round((local_dt.astimezone(timezone.utc) - issued_at_utc).total_seconds() / 3600.0, 2))
    return target_local, lead_hours


def read_index() -> list:
    if not os.path.exists(INDEX_PATH):
        return []
    with open(INDEX_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def _append_index(entry: dict):
    os.makedirs(VINTAGE_DIR, exist_ok=True)
    with open(INDEX_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def archive_forecast_payload(raw: dict, issued_at_utc: datetime, provider: str = "open-meteo",
                              model: str = "multi-model-ensemble", model_run_time: str = None,
                              source_url: str = None) -> dict:
    """Archives the genuine forecast payload. Returns the index entry
    (either freshly written, or the pre-existing one if this exact payload
    was already archived - checksum dedup)."""
    payload = extract_forecast_payload(raw)
    checksum = _checksum(payload)

    for entry in read_index():
        if entry.get("raw_payload_checksum") == checksum:
            return entry  # identical payload already archived - no duplicate storage

    retrieved_at = datetime.now(timezone.utc).isoformat()
    issued_at_local = issued_at_utc.astimezone(ZURICH_TZ)
    target_time, lead_hours = _target_times_and_lead_hours(payload, issued_at_utc)
    normalized_fields = sorted({var for series in payload.values() for var in series.keys() if var != "time"})

    day_dir = os.path.join(VINTAGE_DIR, issued_at_utc.strftime("%Y"), issued_at_utc.strftime("%m"), issued_at_utc.strftime("%d"))
    os.makedirs(day_dir, exist_ok=True)
    filename = f"{issued_at_utc.strftime('%Y%m%dT%H%M%SZ')}_{provider}_{model}.json.gz"
    filepath = os.path.join(day_dir, filename)
    with gzip.open(filepath, "wt", encoding="utf-8") as f:
        json.dump(payload, f)

    entry = {
        "issued_at_utc": issued_at_utc.isoformat(),
        "issued_at_local": issued_at_local.isoformat(),
        "provider": provider,
        "model": model,
        "model_run_time": model_run_time,
        "target_time": target_time,
        "lead_hours": lead_hours,
        "raw_payload_checksum": checksum,
        "normalized_fields": normalized_fields,
        "retrieved_at": retrieved_at,
        "schema_version": SCHEMA_VERSION,
        "source_url": source_url,
        "file": os.path.relpath(filepath, BASE_DIR),
    }
    _append_index(entry)
    return entry


def archive_forecast_payload_safe(raw: dict, issued_at_utc: datetime, **kwargs) -> dict:
    """Best-effort wrapper - NEVER raises. Archival failures are logged
    visibly to stderr but must never block the actual forecast/Telegram
    send that called this."""
    try:
        return archive_forecast_payload(raw, issued_at_utc, **kwargs)
    except Exception as e:
        print(f"[forecast-vintage] WARNING: archival failed, continuing without it: {e}", file=sys.stderr)
        return {}


def load_vintage(entry: dict) -> dict:
    """Loads the actual archived payload back from disk given an index entry."""
    path = os.path.join(BASE_DIR, entry["file"])
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def coverage_summary() -> dict:
    entries = read_index()
    if not entries:
        return {"n_vintages": 0, "earliest_issue": None, "latest_issue": None}
    issues = sorted(e["issued_at_utc"] for e in entries)
    return {"n_vintages": len(entries), "earliest_issue": issues[0], "latest_issue": issues[-1]}

"""
refresh_research_dashboard.py - assembles docs/research/research_data.json
from the latest report of each research script (station_analysis.py,
calibration_analysis.py, regime_analysis.py, continuous_target_analysis.py)
plus the station archive manifests, for docs/research.html to render.

No network calls. Never touches weights.json or the main
docs/dashboard_data.json - this is a completely separate, secondary data
file for the secondary research page (Phase 12's explicit "do not
overload the main operational dashboard").
"""

import glob
import json
import os
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "logs", "historical", "reports")
STATIONS_MANIFEST_PATH = os.path.join(BASE_DIR, "logs", "historical", "manifests", "stations.json")
ASSETS_MANIFEST_PATH = os.path.join(BASE_DIR, "logs", "historical", "manifests", "assets.jsonl")
FORECAST_VINTAGE_INDEX_PATH = os.path.join(BASE_DIR, "logs", "historical", "forecast_vintages", "index.jsonl")
OUT_PATH = os.path.join(BASE_DIR, "docs", "research", "research_data.json")


def _latest_report(script_name: str):
    pattern = os.path.join(REPORTS_DIR, f"{script_name}_*.json")
    matches = sorted(glob.glob(pattern))
    if not matches:
        return None
    with open(matches[-1]) as f:
        return json.load(f)


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _station_coverage():
    if not os.path.exists(STATIONS_MANIFEST_PATH):
        return {}
    with open(STATIONS_MANIFEST_PATH) as f:
        manifest = json.load(f)
    return manifest.get("stations", {})


def _forecast_vintage_summary():
    entries = _read_jsonl(FORECAST_VINTAGE_INDEX_PATH)
    if not entries:
        return {"n_vintages": 0, "earliest_issue": None, "latest_issue": None}
    issues = sorted(e["issue_timestamp_utc"] for e in entries)
    return {"n_vintages": len(entries), "earliest_issue": issues[0], "latest_issue": issues[-1]}


def _best_station_family(station_report):
    """Picks the family with the highest full-window 2026-reference ROC
    AUC from the rolling-origin comparison - purely descriptive, labeled
    as such; NOT a claim that this is a statistically validated winner
    (see station_analysis.py's own diagnostic-only framing)."""
    if not station_report:
        return None
    comparison = station_report.get("rolling_origin_family_comparison", {})
    best_name, best_auc = None, -1
    for name, folds in comparison.items():
        if name == "_note":
            continue
        for fold in folds:
            if fold.get("kind") == "reference":
                auc = fold.get("full_window", {}).get("roc_auc")
                if auc is not None and auc > best_auc:
                    best_auc, best_name = auc, name
    return {"name": best_name, "reference_roc_auc": best_auc} if best_name else None


def build_research_data():
    station_report = _latest_report("station_analysis")
    calibration_report = _latest_report("calibration_analysis")
    regime_report = _latest_report("regime_analysis")
    continuous_report = _latest_report("continuous_target_analysis")

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "is_sample_data": station_report is None,
        "station_coverage": _station_coverage(),
        "forecast_vintages": _forecast_vintage_summary(),
        "last_asset_sync": (_read_jsonl(ASSETS_MANIFEST_PATH) or [{}])[-1].get("retrieved_at"),
    }

    if station_report:
        data["station_analysis"] = {
            "generated_at": station_report["generated_at"],
            "correlation": station_report["correlation"],
            "rolling_origin_family_comparison": station_report["rolling_origin_family_comparison"],
            "station_coverage_status": station_report["station_coverage"],
            "best_family": _best_station_family(station_report),
        }
    if calibration_report:
        data["calibration_analysis"] = {
            "generated_at": calibration_report["generated_at"],
            "folds": calibration_report["folds"],
        }
    if regime_report:
        data["regime_analysis"] = {
            "generated_at": regime_report["generated_at"],
            "false_positive_summary": regime_report["false_positive_summary"],
        }
    if continuous_report:
        data["continuous_target_analysis"] = {
            "generated_at": continuous_report["generated_at"],
            "continuous_wind_target": continuous_report["continuous_wind_target"],
            "daily_session_target": continuous_report["daily_session_target"],
        }
    return data


def main():
    data = build_research_data()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Research dashboard data written to {OUT_PATH} "
          f"({'no reports found yet' if data['is_sample_data'] else 'populated from real reports'}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

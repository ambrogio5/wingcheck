"""
refresh_research_dashboard.py - assembles docs/research/research_data.json
from the latest station_analysis.py report plus the historical-archive
manifests, for docs/research.html to render.

No network calls. Never touches weights.json or the main
docs/dashboard_data.json - a completely separate, secondary data file for
the secondary research page (section 10's explicit "don't overload the
main dashboard").
"""

import glob
import json
import os
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "logs", "historical", "reports")
COVERAGE_MANIFEST_PATH = os.path.join(BASE_DIR, "logs", "historical", "manifests", "stations.json")
ASSETS_MANIFEST_PATH = os.path.join(BASE_DIR, "logs", "historical", "manifests", "assets.jsonl")
FORECAST_VINTAGE_INDEX_PATH = os.path.join(BASE_DIR, "logs", "historical", "forecast_vintages", "index.jsonl")
OUT_PATH = os.path.join(BASE_DIR, "docs", "research", "research_data.json")


def _latest_report(script_name: str):
    matches = sorted(glob.glob(os.path.join(REPORTS_DIR, f"{script_name}_*.json")))
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
    if not os.path.exists(COVERAGE_MANIFEST_PATH):
        return {}
    with open(COVERAGE_MANIFEST_PATH) as f:
        manifest = json.load(f)
    return manifest.get("stations", {})


def _forecast_vintage_summary():
    entries = _read_jsonl(FORECAST_VINTAGE_INDEX_PATH)
    if not entries:
        return {"n_vintages": 0, "earliest_issue": None, "latest_issue": None}
    issues = sorted(e["issued_at_utc"] for e in entries)
    return {"n_vintages": len(entries), "earliest_issue": issues[0], "latest_issue": issues[-1]}


def _best_family(station_report):
    """Descriptive only - the family with the highest full-window 2026
    reference ROC AUC. NOT a claim of statistical significance or a
    promotion recommendation - see feature-promotion prohibition in
    docs/STATION_RESEARCH.md."""
    if not station_report:
        return None
    comparison = station_report.get("rolling_origin_family_comparison", {})
    best_name, best_auc = None, -1
    for name, folds in comparison.items():
        for fold in folds:
            if fold.get("kind") == "reference":
                auc = fold.get("full_window", {}).get("roc_auc")
                if auc is not None and auc > best_auc:
                    best_auc, best_name = auc, name
    return {"name": best_name, "reference_roc_auc": best_auc} if best_name else None


def _data_health_warnings(station_report):
    warnings = list(station_report.get("warnings", [])) if station_report else []
    warnings += list(station_report.get("limitations", [])) if station_report else []
    return warnings


def build_research_data() -> dict:
    station_report = _latest_report("station_analysis")

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
            "commit_sha": station_report.get("commit_sha"),
            "correlation": station_report["correlation"],
            "rolling_origin_family_comparison": station_report["rolling_origin_family_comparison"],
            "calibration": station_report.get("calibration", []),
            "family_score_coverage": station_report.get("family_score_coverage", {}),
            "best_family": _best_family(station_report),
            "data_health_warnings": _data_health_warnings(station_report),
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

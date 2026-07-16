"""
research_report.py - shared report scaffolding for every research script
(station_analysis.py, calibration_analysis.py, regime_analysis.py,
continuous_target_analysis.py). Every report gets the same provenance
envelope (Phase 17): commit SHA, data manifest checksum, configuration,
seed, and a warnings/limitations list - and is written to a NEW
timestamped file rather than overwriting the previous run's report.
"""

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "logs", "historical", "reports")


def git_commit_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              cwd=BASE_DIR, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def file_checksum(path: str) -> str:
    if not os.path.exists(path):
        return "missing"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def new_report(script_name: str, config: dict, data_sources: list, warnings: list = None,
               limitations: list = None) -> dict:
    """Builds the shared envelope every report starts from. `data_sources`
    is a list of file paths whose checksums get recorded, so a report is
    reproducible against a specific state of the data on disk."""
    return {
        "script": script_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": git_commit_sha(),
        "config": config,
        "data_manifest": {path: file_checksum(path) for path in data_sources},
        "warnings": warnings or [],
        "limitations": limitations or [],
    }


def save_report(report: dict, script_name: str) -> str:
    """Writes a NEW timestamped file - never overwrites a prior report."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(REPORTS_DIR, f"{script_name}_{ts}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    return path

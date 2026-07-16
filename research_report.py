"""
research_report.py - the provenance envelope every research report gets:
commit SHA, input-file checksums, configuration, warnings, limitations.
Every report is written to a NEW timestamped file, never overwritten -
see historical_data.py's docstring on why this project treats research
output as an append-only, reproducible record.
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
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=BASE_DIR, stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def file_checksum(path: str) -> str:
    if not os.path.exists(path):
        return "missing"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def new_report(script_name: str, config: dict, data_sources: dict,
               warnings: list = None, limitations: list = None) -> dict:
    return {
        "script": script_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": git_commit_sha(),
        "config": config,
        "data_sources": {name: {"path": path, "checksum": file_checksum(path)} for name, path in data_sources.items()},
        "warnings": warnings or [],
        "limitations": limitations or [],
    }


def save_report(report: dict, script_name: str) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(REPORTS_DIR, f"{script_name}_{timestamp}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    return path

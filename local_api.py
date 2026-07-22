"""Small, dependency-free local API for health and operational status."""
from __future__ import annotations

import json
import fcntl
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

RUNTIME = Path(os.environ.get("WINGCHECK_RUNTIME_DIR", ".runtime"))
ROOT = Path(__file__).resolve().parent
_collection_lock = threading.Lock()
_forecast_lock = threading.Lock()
FORECAST_STATUS = RUNTIME / "status" / "manual_forecast.json"
LOCKS = RUNTIME / "locks"


def write_forecast_status(state: str, **extra: object) -> None:
    FORECAST_STATUS.parent.mkdir(parents=True, exist_ok=True)
    payload = {"state": state, **extra}
    temporary = FORECAST_STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(FORECAST_STATUS)


def latest_observation() -> dict | None:
    path = RUNTIME / "logs" / "kitesailing_observations.jsonl"
    try:
        with path.open() as handle:
            rows = [line for line in handle if line.strip()]
        return json.loads(rows[-1]) if rows else None
    except (OSError, json.JSONDecodeError):
        return None


def collect_once() -> None:
    if not _collection_lock.acquire(blocking=False):
        return
    try:
        subprocess.run([sys.executable, "kitesailing_weather.py"], cwd=ROOT, check=False)
        subprocess.run([sys.executable, "water_temperature.py"], cwd=ROOT, check=False)
        subprocess.run([sys.executable, "refresh_dashboard.py"], cwd=ROOT, check=False)
    finally:
        _collection_lock.release()


def acquire_forecast_lock():
    """Reserve the manual/API thread and the scheduler's shared file lock."""
    if not _forecast_lock.acquire(blocking=False):
        return None
    LOCKS.mkdir(parents=True, exist_ok=True)
    handle = (LOCKS / "forecast.lock").open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        _forecast_lock.release()
        return None
    return handle


def release_forecast_lock(handle) -> None:
    fcntl.flock(handle, fcntl.LOCK_UN)
    handle.close()
    _forecast_lock.release()


def refresh_forecast_once(lock_handle=None) -> bool:
    """Refresh every live input and rebuild the forecast without Telegram."""
    lock_handle = lock_handle or acquire_forecast_lock()
    if lock_handle is None:
        return False
    try:
        write_forecast_status("running")
        env = os.environ.copy()
        env["WINGCHECK_SKIP_TELEGRAM"] = "1"
        scripts = [
            "station_nowcast.py",
            "candidate_signals.py",
            "meteoswiss_local_forecast.py",
            "forecast_and_log.py",
            "refresh_dashboard.py",
        ]
        for script in scripts:
            result = subprocess.run([sys.executable, script], cwd=ROOT, env=env, check=False)
            if result.returncode:
                write_forecast_status("failed", failed_script=script)
                return False
        write_forecast_status("ok")
        return True
    except Exception as exc:  # keep the API process alive and expose a useful state
        write_forecast_status("failed", error=str(exc))
        return False
    finally:
        release_forecast_lock(lock_handle)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_json({"status": "ok", "service": "wingcheck-api"})
        elif path == "/api/status":
            statuses = {}
            for path in sorted((RUNTIME / "status").glob("*.json")):
                try: statuses[path.stem] = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError): statuses[path.stem] = {"state": "unreadable"}
            self.send_json(statuses)
        elif path == "/api/dashboard":
            path = RUNTIME / "dashboard" / "dashboard_data.json"
            try: self.send_json(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError): self.send_json({"error": "dashboard data unavailable"}, 503)
        elif path == "/api/latest-wind":
            observation = latest_observation()
            self.send_json(observation or {"error": "no wind reading available"}, 200 if observation else 404)
        elif path == "/api/forecast-status":
            try:
                self.send_json(json.loads(FORECAST_STATUS.read_text()))
            except (OSError, json.JSONDecodeError):
                self.send_json({"state": "idle"})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/collect":
            if _collection_lock.locked():
                self.send_json({"status": "already_running"}, 409)
                return
            threading.Thread(target=collect_once, daemon=True).start()
            self.send_json({"status": "started"}, 202)
        elif path == "/api/refresh-forecast":
            lock_handle = acquire_forecast_lock()
            if lock_handle is None:
                self.send_json({"status": "already_running"}, 409)
                return
            write_forecast_status("running")
            threading.Thread(target=refresh_forecast_once, args=(lock_handle,), daemon=True).start()
            self.send_json({"status": "started"}, 202)
        else:
            self.send_json({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"api: {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

"""Long-running local collection, scheduling, health, and backup service."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RUNTIME = Path(os.environ.get("WINGCHECK_RUNTIME_DIR", ROOT / ".runtime"))
STATUS = RUNTIME / "status"
LOCKS = RUNTIME / "locks"
ZONE = ZoneInfo(os.environ.get("TZ", "Europe/Zurich"))


def write_status(service: str, state: str, **extra: object) -> None:
    STATUS.mkdir(parents=True, exist_ok=True)
    payload = {"service": service, "state": state, "updated_at": dt.datetime.now(dt.UTC).isoformat(), **extra}
    tmp = STATUS / f".{service}.tmp"
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(STATUS / f"{service}.json")


def run_job(name: str, commands: list[list[str]], retries: int = 2,
            lock_name: str | None = None) -> bool:
    LOCKS.mkdir(parents=True, exist_ok=True)
    with (LOCKS / f"{lock_name or name}.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        write_status(name, "running")
        for attempt in range(1, retries + 2):
            try:
                for command in commands:
                    subprocess.run(command, cwd=ROOT, check=True)
                write_status(name, "ok", attempt=attempt)
                return True
            except subprocess.CalledProcessError as exc:
                write_status(name, "retrying" if attempt <= retries else "failed", attempt=attempt, exit_code=exc.returncode)
                if attempt <= retries:
                    time.sleep(min(60 * attempt, 180))
        return False


def collector() -> None:
    """Sample the lake automatically every 15 minutes."""
    last_sample = 0.0
    last_local_forecast = 0.0
    while True:
        now = time.time()
        if now - last_sample >= 15 * 60:
            run_job("lake_collection", [[sys.executable, "kitesailing_weather.py"]])
            run_job("water_temperature", [[sys.executable, "water_temperature.py"]])
            if now - last_local_forecast >= 60 * 60:
                if run_job("meteoswiss_local_forecast", [[sys.executable, "meteoswiss_local_forecast.py"]]):
                    last_local_forecast = now
            run_job("dashboard_refresh", [[sys.executable, "refresh_dashboard.py"]], retries=0)
            last_sample = now
        write_status("collector", "ok", last_sample=last_sample,
                     last_local_forecast=last_local_forecast, interval_seconds=15 * 60)
        time.sleep(30)


def scheduler() -> None:
    """Run forecasts, learning, dashboard refreshes, and archive sync locally."""
    completed: set[str] = set()
    while True:
        now = dt.datetime.now(ZONE)
        day = now.date().isoformat()
        jobs = [
            ("historical", 3, 30, [[sys.executable, "historical_data.py", "sync"], [sys.executable, "candidate_signals.py"]]),
            ("forecast-07", 7, 0, [[sys.executable, "station_nowcast.py"], [sys.executable, "candidate_signals.py"], [sys.executable, "forecast_and_log.py"], [sys.executable, "refresh_dashboard.py"]]),
            ("forecast-10", 10, 0, [[sys.executable, "station_nowcast.py"], [sys.executable, "candidate_signals.py"], [sys.executable, "forecast_and_log.py"], [sys.executable, "refresh_dashboard.py"]]),
            ("learn", 20, 0, [[sys.executable, "verify_and_learn.py"],
                              [sys.executable, "station_calibration.py", "--source-a", "kitesailing", "--source-b", "sia"],
                              [sys.executable, "refresh_dashboard.py"]]),
        ]
        for name, hour, minute, commands in jobs:
            key = f"{day}:{name}"
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= now < due + dt.timedelta(minutes=20) and key not in completed:
                shared_lock = "forecast" if name.startswith("forecast-") else None
                if run_job(name, commands, lock_name=shared_lock):
                    completed.add(key)
        completed = {key for key in completed if key.startswith(day + ":")}
        write_status("scheduler", "ok", local_time=now.isoformat(), completed=sorted(completed))
        time.sleep(30)


def telegram() -> None:
    """Answer authorized Telegram report requests; never collects labels."""
    while True:
        run_job("telegram_report", [[sys.executable, "telegram_ingest.py"]], retries=1)
        write_status("telegram", "ok", poll_interval_seconds=30)
        time.sleep(30)


def backup_once() -> None:
    backups = RUNTIME / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    subprocess.run(["pg_dump", "--format=custom", "--file", str(backups / f"postgres-{stamp}.dump")], check=True)
    with tarfile.open(backups / f"runtime-{stamp}.tar.gz", "w:gz") as archive:
        for name in ("logs", "dashboard", "weights.json", "status"):
            path = RUNTIME / name
            if path.exists():
                archive.add(path, arcname=name, filter=_backup_filter)
    cutoff = time.time() - int(os.environ.get("BACKUP_RETENTION_DAYS", "30")) * 86400
    for path in backups.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
    write_status("backup", "ok", completed_at=stamp)


def _backup_filter(member: tarfile.TarInfo):
    """Skip the large normalized archive, which can be rebuilt from raw data."""
    generated = "logs/historical/station_hourly"
    if member.name == generated or member.name.startswith(generated + "/"):
        return None
    return member


def backup_loop() -> None:
    last_day = ""
    while True:
        now = dt.datetime.now(ZONE)
        if now.hour == 2 and now.date().isoformat() != last_day:
            try:
                backup_once()
                last_day = now.date().isoformat()
            except Exception as exc:
                write_status("backup", "failed", error=str(exc))
        time.sleep(60)


def validate_runtime_archive(archive_path: Path) -> dict:
    """Extract a backup safely into a temporary directory and parse key JSON."""
    with tempfile.TemporaryDirectory(prefix="wingcheck-restore-") as temporary:
        destination = Path(temporary).resolve()
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                target = (destination / member.name).resolve()
                if target != destination and destination not in target.parents:
                    raise ValueError(f"unsafe archive member: {member.name}")
            archive.extractall(destination, filter="data")
        parsed = 0
        for relative in ("dashboard/dashboard_data.json", "status/collector.json"):
            path = destination / relative
            if path.exists():
                json.loads(path.read_text())
                parsed += 1
        if not (destination / "logs").exists():
            raise ValueError("runtime backup does not contain logs/")
        return {"members": len(members), "validated_json_files": parsed}


def restore_check_once() -> dict:
    """Restore the newest runtime and PostgreSQL backups without touching production."""
    backups = RUNTIME / "backups"
    runtime_archives = sorted(backups.glob("runtime-*.tar.gz"))
    database_dumps = sorted(backups.glob("postgres-*.dump"))
    if not runtime_archives or not database_dumps:
        raise FileNotFoundError("both runtime and PostgreSQL backups are required")

    runtime_result = validate_runtime_archive(runtime_archives[-1])
    database = f"wingcheck_restore_check_{os.getpid()}_{int(time.time())}"
    try:
        subprocess.run(["createdb", database], check=True)
        subprocess.run(["pg_restore", "--exit-on-error", "--dbname", database,
                        str(database_dumps[-1])], check=True)
        subprocess.run(["psql", "--dbname", database, "--tuples-only", "--command", "SELECT 1"],
                       check=True, capture_output=True, text=True)
    finally:
        subprocess.run(["dropdb", "--if-exists", database], check=False)
    result = {
        "runtime_archive": runtime_archives[-1].name,
        "postgres_dump": database_dumps[-1].name,
        **runtime_result,
    }
    write_status("restore_check", "ok", **result)
    return result


def check(service: str, max_age: int) -> int:
    path = STATUS / f"{service}.json"
    if not path.exists() or time.time() - path.stat().st_mtime > max_age:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collector")
    sub.add_parser("scheduler")
    sub.add_parser("backup")
    sub.add_parser("telegram")
    sub.add_parser("restore-check")
    check_parser = sub.add_parser("check")
    check_parser.add_argument("service")
    check_parser.add_argument("--max-age", type=int, default=300)
    args = parser.parse_args()
    if args.command == "collector": collector()
    elif args.command == "scheduler": scheduler()
    elif args.command == "backup": backup_loop()
    elif args.command == "telegram": telegram()
    elif args.command == "restore-check":
        print(json.dumps(restore_check_once(), indent=2))
    else: return check(args.service, args.max_age)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

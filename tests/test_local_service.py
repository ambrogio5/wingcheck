import json
import os
import tempfile
import tarfile
import unittest
from pathlib import Path
from unittest import mock

import local_service as service
import local_api


class LocalServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.status_patch = mock.patch.object(service, "STATUS", self.runtime / "status")
        self.locks_patch = mock.patch.object(service, "LOCKS", self.runtime / "locks")
        self.status_patch.start()
        self.locks_patch.start()

    def tearDown(self):
        self.status_patch.stop()
        self.locks_patch.stop()
        self.temp.cleanup()

    def test_status_write_is_readable_and_health_check_passes(self):
        service.write_status("scheduler", "ok", completed=["forecast"])
        payload = json.loads((service.STATUS / "scheduler.json").read_text())
        self.assertEqual(payload["state"], "ok")
        self.assertEqual(service.check("scheduler", 60), 0)

    def test_missing_status_fails_health_check(self):
        self.assertEqual(service.check("collector", 60), 1)

    def test_job_records_success(self):
        self.assertTrue(service.run_job("example", [["true"]], retries=0))
        payload = json.loads((service.STATUS / "example.json").read_text())
        self.assertEqual(payload["state"], "ok")

    def test_job_records_failure(self):
        self.assertFalse(service.run_job("example", [["false"]], retries=0))
        payload = json.loads((service.STATUS / "example.json").read_text())
        self.assertEqual(payload["state"], "failed")

    def test_shared_lock_prevents_two_differently_named_forecasts(self):
        import fcntl
        service.LOCKS.mkdir(parents=True, exist_ok=True)
        with (service.LOCKS / "forecast.lock").open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(service.run_job("forecast-10", [["true"]], retries=0,
                                             lock_name="forecast"))

    def test_backup_filter_skips_reproducible_station_archive(self):
        generated = tarfile.TarInfo("logs/historical/station_hourly/sia.jsonl")
        source = tarfile.TarInfo("logs/raw_cache/sia.json")
        self.assertIsNone(service._backup_filter(generated))
        self.assertIs(service._backup_filter(source), source)

    def test_runtime_restore_validation_extracts_and_parses_backup(self):
        archive_path = self.runtime / "runtime-test.tar.gz"
        source = self.runtime / "source"
        (source / "logs").mkdir(parents=True)
        (source / "dashboard").mkdir()
        (source / "dashboard" / "dashboard_data.json").write_text('{"ok": true}')
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source / "logs", arcname="logs")
            archive.add(source / "dashboard", arcname="dashboard")
        result = service.validate_runtime_archive(archive_path)
        self.assertGreaterEqual(result["members"], 2)
        self.assertEqual(result["validated_json_files"], 1)

    def test_restore_check_uses_temporary_database_and_drops_it(self):
        backups = self.runtime / "backups"
        backups.mkdir()
        (backups / "postgres-20260722.dump").write_bytes(b"fixture")
        (backups / "runtime-20260722.tar.gz").write_bytes(b"fixture")
        with mock.patch.object(service, "RUNTIME", self.runtime), \
             mock.patch.object(service, "validate_runtime_archive", return_value={"members": 3, "validated_json_files": 1}), \
             mock.patch.object(service.subprocess, "run") as run:
            result = service.restore_check_once()
        commands = [call.args[0][0] for call in run.call_args_list]
        self.assertEqual(commands, ["createdb", "pg_restore", "psql", "dropdb"])
        self.assertEqual(result["postgres_dump"], "postgres-20260722.dump")


class LocalApiTests(unittest.TestCase):
    def test_latest_observation_returns_last_jsonl_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            logs = runtime / "logs"
            logs.mkdir()
            (logs / "kitesailing_observations.jsonl").write_text(
                '{"avg_wind_kmh": 3}\n{"avg_wind_kmh": 9}\n'
            )
            with mock.patch.object(local_api, "RUNTIME", runtime):
                self.assertEqual(local_api.latest_observation()["avg_wind_kmh"], 9)

    def test_manual_collection_updates_wind_water_and_dashboard(self):
        with mock.patch.object(local_api.subprocess, "run") as run:
            local_api.collect_once()
        scripts = [call.args[0][1] for call in run.call_args_list]
        self.assertEqual(scripts, [
            "kitesailing_weather.py", "water_temperature.py", "refresh_dashboard.py",
        ])

    def test_manual_forecast_refresh_runs_full_pipeline_without_telegram(self):
        completed = mock.Mock(returncode=0)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(local_api, "FORECAST_STATUS", Path(tmp) / "status.json"), \
             mock.patch.object(local_api, "LOCKS", Path(tmp) / "locks"), \
             mock.patch.object(local_api.subprocess, "run", return_value=completed) as run:
            local_api.refresh_forecast_once()
            status = json.loads(local_api.FORECAST_STATUS.read_text())
        scripts = [call.args[0][1] for call in run.call_args_list]
        self.assertEqual(scripts, [
            "station_nowcast.py", "candidate_signals.py", "meteoswiss_local_forecast.py",
            "forecast_and_log.py", "refresh_dashboard.py",
        ])
        self.assertEqual(status["state"], "ok")
        self.assertTrue(all(call.kwargs["env"]["WINGCHECK_SKIP_TELEGRAM"] == "1" for call in run.call_args_list))

    def test_manual_refresh_respects_scheduler_forecast_lock(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(local_api, "LOCKS", Path(tmp)):
            lock_path = Path(tmp) / "forecast.lock"
            with lock_path.open("w") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse(local_api.refresh_forecast_once())


if __name__ == "__main__":
    unittest.main()

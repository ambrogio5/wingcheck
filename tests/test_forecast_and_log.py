"""Offline tests for forecast_and_log.py's new provenance/diagnostics
helpers: the cutoff-selection rule, best-effort station-input loading
(must never raise even with no archive present), diagnostics construction,
and issuance-record logging (must never raise and must never touch
logs/predictions.jsonl's existing schema). No network calls."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import forecast_and_log as fal
from model import load_weights


class CutoffSelectionTests(unittest.TestCase):
    def test_before_09_local_is_07_00_cutoff(self):
        self.assertEqual(fal._current_station_cutoff(datetime(2026, 7, 16, 7, 30)), "07:00")
        self.assertEqual(fal._current_station_cutoff(datetime(2026, 7, 16, 8, 59)), "07:00")

    def test_09_or_later_local_is_10_00_cutoff(self):
        self.assertEqual(fal._current_station_cutoff(datetime(2026, 7, 16, 9, 0)), "10:00")
        self.assertEqual(fal._current_station_cutoff(datetime(2026, 7, 16, 15, 0)), "10:00")


class LoadStationInputsTests(unittest.TestCase):
    def test_returns_empty_dicts_when_archive_absent(self):
        import historical_data as hd
        orig = hd.STATION_HOURLY_DIR
        hd.STATION_HOURLY_DIR = "/nonexistent/station_hourly"
        try:
            feats, ages, _records = fal._load_station_inputs("2026-07-16", "07:00")
            # every enabled station is attempted, but with no data on disk
            # each should report missing_indicator=1.0, not crash.
            for f in feats.values():
                self.assertEqual(f["missing_indicator"], 1.0)
        finally:
            hd.STATION_HOURLY_DIR = orig

    def test_never_raises_even_on_registry_failure(self):
        import station_registry as sr
        orig = sr.load_registry
        sr.load_registry = lambda path=None: (_ for _ in ()).throw(RuntimeError("simulated failure"))
        try:
            feats, ages, _records = fal._load_station_inputs("2026-07-16", "07:00")
            self.assertEqual(feats, {})
            self.assertEqual(ages, {})
        finally:
            sr.load_registry = orig


class BuildDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        import station_registry as sr
        self.registry = sr.load_registry()

    def test_returns_all_seven_diagnostic_families(self):
        diagnostics = fal._build_diagnostics({}, self.registry, forecast_pressure_signal=0.1)
        expected = {"source_heating", "pass_activation", "summit_support", "radiation_support",
                    "pressure_support", "competing_flow", "data_health"}
        self.assertEqual(set(diagnostics.keys()), expected)

    def test_pressure_support_uses_real_station_features_when_present(self):
        station_feats = {
            "lug": {"missing_indicator": 0.0, "pressure_latest": 1018.0},
            "sma": {"missing_indicator": 0.0, "pressure_latest": 1013.0},
        }
        diagnostics = fal._build_diagnostics(station_feats, self.registry, forecast_pressure_signal=0.2)
        self.assertFalse(diagnostics["pressure_support"]["missing"])

    def test_summit_role_reaches_summit_support_when_cov_present(self):
        station_feats = {"cov": {"missing_indicator": 0.0, "latest_wind_speed": 6.0, "wind_u": 3.2, "wind_v": 3.8}}
        diagnostics = fal._build_diagnostics(station_feats, self.registry, forecast_pressure_signal=None)
        self.assertNotEqual(diagnostics["summit_support"]["status"], "missing")
        self.assertEqual(diagnostics["summit_support"]["source_station"], "cov")

    def test_missing_summit_role_reports_missing(self):
        diagnostics = fal._build_diagnostics({}, self.registry, forecast_pressure_signal=None)
        self.assertEqual(diagnostics["summit_support"]["status"], "missing")


class LogIssuanceTests(unittest.TestCase):
    """Uses an empty temp station_hourly dir (not the real, multi-hundred-MB
    archive) so _load_station_inputs stays fast and these tests exercise
    only the "no station data available" path, not real data volume."""

    def setUp(self):
        import historical_data as hd
        self.tmpdir = tempfile.mkdtemp()
        self._orig_path = fal.ISSUANCE_LOG_PATH
        self._orig_station_hourly_dir = hd.STATION_HOURLY_DIR
        fal.ISSUANCE_LOG_PATH = os.path.join(self.tmpdir, "forecast_issuances.jsonl")
        hd.STATION_HOURLY_DIR = os.path.join(self.tmpdir, "station_hourly_empty")

    def tearDown(self):
        import historical_data as hd
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        fal.ISSUANCE_LOG_PATH = self._orig_path
        hd.STATION_HOURLY_DIR = self._orig_station_hourly_dir

    def _results(self):
        return [
            {"target_time": "2026-07-17T12:00", "probability": 0.2, "tier": "UNLIKELY",
             "model_wind_kt": 5, "model_gust_kt": 8, "features": {"pressure_signal": 0.1, "ensemble_agreement_score": 0.8}},
            {"target_time": "2026-07-17T15:00", "probability": 0.7, "tier": "GOOD",
             "model_wind_kt": 14, "model_gust_kt": 18, "features": {"pressure_signal": 0.3, "ensemble_agreement_score": 0.9}},
        ]

    def test_writes_one_append_only_record(self):
        weights = load_weights()
        fal._log_issuance({"silvaplana": {}}, self._results(), weights, datetime.now(timezone.utc), {"raw_payload_checksum": "abc"})
        with open(fal.ISSUANCE_LOG_PATH) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 1)

    def test_record_has_all_required_fields(self):
        weights = load_weights()
        fal._log_issuance({"silvaplana": {}}, self._results(), weights, datetime.now(timezone.utc), {"raw_payload_checksum": "abc"})
        with open(fal.ISSUANCE_LOG_PATH) as f:
            record = json.loads(f.readline())
        for key in ("issued_at", "model_version", "feature_schema_version", "calibration_version",
                    "station_cutoff", "station_inputs", "station_input_age", "station_quality_flags",
                    "diagnostics", "session_forecast", "hourly_predictions", "raw_payload_checksums", "commit_sha"):
            self.assertIn(key, record, f"missing field {key!r}")

    def test_session_forecast_keyed_by_date(self):
        weights = load_weights()
        fal._log_issuance({"silvaplana": {}}, self._results(), weights, datetime.now(timezone.utc), {"raw_payload_checksum": "abc"})
        with open(fal.ISSUANCE_LOG_PATH) as f:
            record = json.loads(f.readline())
        self.assertIn("2026-07-17", record["session_forecast"])

    def test_appends_not_overwrites(self):
        weights = load_weights()
        fal._log_issuance({"silvaplana": {}}, self._results(), weights, datetime.now(timezone.utc), {})
        fal._log_issuance({"silvaplana": {}}, self._results(), weights, datetime.now(timezone.utc), {})
        with open(fal.ISSUANCE_LOG_PATH) as f:
            lines = [l for l in f if l.strip()]
        self.assertEqual(len(lines), 2)

    def test_never_raises_on_malformed_input(self):
        weights = load_weights()
        # results missing "features" entirely - must not raise.
        try:
            fal._log_issuance({}, [{"target_time": "2026-07-17T12:00", "probability": 0.1, "tier": "UNLIKELY",
                                     "model_wind_kt": 1, "model_gust_kt": 1}],
                               weights, datetime.now(timezone.utc), {})
        except Exception as e:
            self.fail(f"_log_issuance raised unexpectedly: {e}")


if __name__ == "__main__":
    unittest.main()

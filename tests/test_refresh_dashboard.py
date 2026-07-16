"""Offline tests for refresh_dashboard.py: upcoming-forecast shaping
(hourly probability/wind/gust/direction), missing-data handling, and the
critical invariant that a normal refresh never recomputes or overwrites
the frozen evaluation/deployment/reproducibility sections written by
backtest.py. No network calls - everything here works on in-memory data
or temporary files."""

import json
import os
import tempfile
import unittest

import refresh_dashboard as rd


class CompassDirectionTests(unittest.TestCase):
    def test_known_angles(self):
        self.assertEqual(rd.compass_direction(0), "N")
        self.assertEqual(rd.compass_direction(45), "NE")
        self.assertEqual(rd.compass_direction(90), "E")
        self.assertEqual(rd.compass_direction(135), "SE")
        self.assertEqual(rd.compass_direction(180), "S")
        self.assertEqual(rd.compass_direction(225), "SW")
        self.assertEqual(rd.compass_direction(270), "W")
        self.assertEqual(rd.compass_direction(315), "NW")

    def test_wraps_around_360(self):
        self.assertEqual(rd.compass_direction(360), "N")
        self.assertEqual(rd.compass_direction(359), "N")

    def test_none_is_none(self):
        self.assertIsNone(rd.compass_direction(None))


class UpcomingForecastTests(unittest.TestCase):
    def test_includes_probability_wind_gust_direction(self):
        future_time = "2099-07-01T14:00"
        predictions = [{
            "target_time": future_time, "logged_at": "2099-06-30T10:00:00+00:00",
            "probability": 0.68, "tier": "GOOD",
            "model_wind_kt": 15.0, "model_gust_kt": 20.0,
            "model_wind_dir_deg": 225,
        }]
        result = rd.upcoming_forecast(predictions)
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row["probability"], 0.68)
        self.assertEqual(row["tier"], "GOOD")
        self.assertEqual(row["model_wind_kt"], 15.0)
        self.assertEqual(row["model_gust_kt"], 20.0)
        self.assertEqual(row["model_wind_dir"], "SW")

    def test_missing_wind_direction_is_handled_gracefully(self):
        """Predictions logged before model_wind_dir_deg existed must not
        crash upcoming_forecast() - they just get a None direction."""
        future_time = "2099-07-01T14:00"
        predictions = [{
            "target_time": future_time, "logged_at": "2099-06-30T10:00:00+00:00",
            "probability": 0.5, "tier": "MARGINAL",
            "model_wind_kt": 10.0, "model_gust_kt": 14.0,
            # no model_wind_dir_deg key at all
        }]
        result = rd.upcoming_forecast(predictions)
        self.assertEqual(result[0]["model_wind_dir"], None)

    def test_no_predictions_returns_empty_list(self):
        self.assertEqual(rd.upcoming_forecast([]), [])

    def test_only_future_hours_included(self):
        past_time = "2000-01-01T14:00"
        predictions = [{
            "target_time": past_time, "logged_at": "2000-01-01T10:00:00+00:00",
            "probability": 0.9, "tier": "GOOD",
            "model_wind_kt": 20.0, "model_gust_kt": 25.0, "model_wind_dir_deg": 0,
        }]
        self.assertEqual(rd.upcoming_forecast(predictions), [])

    def test_probability_is_preserved_raw_not_a_tier_threshold(self):
        future_time = "2099-07-01T15:00"
        predictions = [{
            "target_time": future_time, "logged_at": "2099-06-30T10:00:00+00:00",
            "probability": 0.4321, "tier": "MARGINAL",
            "model_wind_kt": 12.0, "model_gust_kt": 16.0, "model_wind_dir_deg": 90,
        }]
        result = rd.upcoming_forecast(predictions)
        # Must be the exact logged probability, not weights.json's marginal
        # threshold (which could easily also be ~0.4-0.6 and mask a bug).
        self.assertEqual(result[0]["probability"], 0.4321)


class FrozenSectionPreservationTests(unittest.TestCase):
    """The single most important invariant refresh_dashboard.py must
    uphold: evaluation/deployment/reproducibility, once written by
    backtest.py, are carried forward byte-for-byte on every subsequent
    refresh - never recomputed against the live, continuously-learning
    weights.json."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.dashboard_path = os.path.join(self.tmpdir, "dashboard_data.json")
        self._orig_dashboard_path = rd.DASHBOARD_DATA_PATH
        self._orig_dataset_path = rd.DATASET_PATH
        self._orig_predictions_path = rd.PREDICTIONS_PATH
        rd.DASHBOARD_DATA_PATH = self.dashboard_path
        rd.DATASET_PATH = os.path.join(self.tmpdir, "backtest_dataset.jsonl")
        rd.PREDICTIONS_PATH = os.path.join(self.tmpdir, "predictions.jsonl")

    def tearDown(self):
        rd.DASHBOARD_DATA_PATH = self._orig_dashboard_path
        rd.DATASET_PATH = self._orig_dataset_path
        rd.PREDICTIONS_PATH = self._orig_predictions_path

    def _seed_frozen_dashboard(self):
        frozen = {
            "evaluation": {
                "generated_at": "2026-07-16T12:00:00+00:00",
                "n_holdout_samples": 535,
                "thresholds": {"good": 0.6, "marginal": 0.59},
            },
            "deployment": {"thresholds": {"good": 0.84, "marginal": 0.69}},
            "reproducibility": {"seed": 20260716, "epochs": 40},
        }
        with open(self.dashboard_path, "w") as f:
            json.dump(frozen, f)
        return frozen

    def test_refresh_preserves_evaluation_deployment_reproducibility(self):
        frozen = self._seed_frozen_dashboard()
        rd.main()
        with open(self.dashboard_path) as f:
            after = json.load(f)
        self.assertEqual(after["evaluation"], frozen["evaluation"])
        self.assertEqual(after["deployment"], frozen["deployment"])
        self.assertEqual(after["reproducibility"], frozen["reproducibility"])

    def test_repeated_refreshes_do_not_drift(self):
        frozen = self._seed_frozen_dashboard()
        rd.main()
        rd.main()
        rd.main()
        with open(self.dashboard_path) as f:
            after = json.load(f)
        self.assertEqual(after["evaluation"]["generated_at"], frozen["evaluation"]["generated_at"])
        self.assertEqual(after["evaluation"], frozen["evaluation"])

    def test_missing_previous_dashboard_data_does_not_crash(self):
        # No dashboard_data.json exists yet at all (fresh checkout before
        # the first backtest.py run) - main() must still succeed.
        rd.main()
        with open(self.dashboard_path) as f:
            after = json.load(f)
        self.assertIn("evaluation", after)
        self.assertEqual(after["evaluation"], {"n_holdout_samples": 0})


class OptionalIssuanceFieldsTests(unittest.TestCase):
    """Section 10's dashboard contract: daily_diagnostics/session_forecast/
    station_health/model_agreement/data_provenance must all degrade to {}
    gracefully with no issuance log, and populate correctly when one
    exists - the dashboard must keep working either way."""

    def test_no_issuance_returns_all_empty_dicts(self):
        fields = rd.optional_issuance_fields(None)
        self.assertEqual(fields, {
            "daily_diagnostics": {}, "session_forecast": {}, "station_health": {},
            "model_agreement": {}, "data_provenance": {},
        })

    def test_populated_issuance_shapes_every_field(self):
        issuance = {
            "issued_at": "2026-07-16T07:00:00+00:00",
            "commit_sha": "abc123",
            "model_version": 3,
            "feature_schema_version": 3,
            "calibration_version": "uncalibrated-v1",
            "diagnostics": {"pressure_support": {"status": "favourable", "missing": False}},
            "session_forecast": {"2026-07-16": {"model_agreement": 0.8, "peak_hour": "2026-07-16T15:00"}},
            "station_input_age": {"sam": 12.0},
            "station_quality_flags": ["summit_support_missing_station_data"],
            "raw_payload_checksums": {"open_meteo": "deadbeef"},
        }
        fields = rd.optional_issuance_fields(issuance)
        self.assertEqual(fields["session_forecast"], issuance["session_forecast"])
        self.assertEqual(fields["daily_diagnostics"], {"2026-07-16": issuance["diagnostics"]})
        self.assertEqual(fields["model_agreement"], {"2026-07-16": 0.8})
        self.assertEqual(fields["station_health"]["station_quality_flags"], issuance["station_quality_flags"])
        self.assertEqual(fields["data_provenance"]["commit_sha"], "abc123")
        self.assertEqual(fields["data_provenance"]["raw_payload_checksums"], {"open_meteo": "deadbeef"})

    def test_latest_issuance_returns_none_when_file_absent(self):
        orig = rd.ISSUANCE_LOG_PATH
        rd.ISSUANCE_LOG_PATH = "/nonexistent/path/forecast_issuances.jsonl"
        try:
            self.assertIsNone(rd._latest_issuance())
        finally:
            rd.ISSUANCE_LOG_PATH = orig

    def test_main_still_succeeds_without_issuance_log(self):
        # main() itself (exercised by the classes above) must not require
        # forecast_issuances.jsonl to exist - already implicitly covered by
        # every other test in this file never creating one, but assert the
        # resulting dashboard_data.json explicitly has the optional keys.
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_dataset, orig_predictions, orig_dashboard, orig_issuance = (
                rd.DATASET_PATH, rd.PREDICTIONS_PATH, rd.DASHBOARD_DATA_PATH, rd.ISSUANCE_LOG_PATH)
            rd.DATASET_PATH = os.path.join(tmpdir, "backtest_dataset.jsonl")
            rd.PREDICTIONS_PATH = os.path.join(tmpdir, "predictions.jsonl")
            rd.DASHBOARD_DATA_PATH = os.path.join(tmpdir, "dashboard_data.json")
            rd.ISSUANCE_LOG_PATH = os.path.join(tmpdir, "forecast_issuances.jsonl")
            open(rd.DATASET_PATH, "w").close()
            open(rd.PREDICTIONS_PATH, "w").close()
            try:
                rd.main()
                with open(rd.DASHBOARD_DATA_PATH) as f:
                    data = json.load(f)
                for key in ("daily_diagnostics", "session_forecast", "station_health", "model_agreement", "data_provenance"):
                    self.assertEqual(data[key], {})
            finally:
                rd.DATASET_PATH, rd.PREDICTIONS_PATH, rd.DASHBOARD_DATA_PATH, rd.ISSUANCE_LOG_PATH = (
                    orig_dataset, orig_predictions, orig_dashboard, orig_issuance)


if __name__ == "__main__":
    unittest.main()

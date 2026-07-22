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


class LakeStationHealthTests(unittest.TestCase):
    """Part 10: lake_station_health() built purely from the local
    kitesailing health/observation logs - degrades to 'no_data' with
    neither, and never combines lake vs. Samedan accuracy without counts
    (that's verification_sources()'s job, tested separately below)."""

    def setUp(self):
        self._orig_health = rd.KITESAILING_HEALTH_PATH
        self._orig_obs = rd.KITESAILING_OBSERVATIONS_PATH
        self._tmpdir = tempfile.mkdtemp()
        rd.KITESAILING_HEALTH_PATH = os.path.join(self._tmpdir, "health.jsonl")
        rd.KITESAILING_OBSERVATIONS_PATH = os.path.join(self._tmpdir, "obs.jsonl")

    def tearDown(self):
        rd.KITESAILING_HEALTH_PATH = self._orig_health
        rd.KITESAILING_OBSERVATIONS_PATH = self._orig_obs

    def _write(self, path, rows):
        with open(path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_no_data_when_neither_log_exists(self):
        health = rd.lake_station_health()
        self.assertEqual(health["status"], "no_data")
        self.assertEqual(health["observations_today"], 0)

    def test_healthy_with_recent_success_and_no_failures(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        recent = (now).isoformat()
        self._write(rd.KITESAILING_HEALTH_PATH, [
            {"attempted_at": recent, "success": True},
        ])
        self._write(rd.KITESAILING_OBSERVATIONS_PATH, [
            {"observed_at": recent},
        ])
        health = rd.lake_station_health()
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(health["last_observation_at"], recent)

    def test_critical_after_three_consecutive_failures(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        rows = [
            {"attempted_at": (now - timedelta(minutes=90)).isoformat(), "success": False},
            {"attempted_at": (now - timedelta(minutes=60)).isoformat(), "success": False},
            {"attempted_at": (now - timedelta(minutes=30)).isoformat(), "success": False},
        ]
        self._write(rd.KITESAILING_HEALTH_PATH, rows)
        health = rd.lake_station_health()
        self.assertEqual(health["consecutive_failures"], 3)
        self.assertEqual(health["status"], "critical")

    def test_degraded_when_last_observation_is_stale(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        stale = (now - timedelta(minutes=90)).isoformat()
        self._write(rd.KITESAILING_HEALTH_PATH, [{"attempted_at": stale, "success": True}])
        self._write(rd.KITESAILING_OBSERVATIONS_PATH, [{"observed_at": stale}])
        health = rd.lake_station_health()
        self.assertEqual(health["status"], "degraded")
        self.assertGreater(health["age_minutes"], 60)

    def test_no_data_case_includes_expected_and_failure_category_keys(self):
        health = rd.lake_station_health()
        self.assertEqual(health["expected_collection_count"], 13)
        self.assertEqual(health["actual_collection_count"], 0)
        self.assertEqual(health["failure_categories"], {})

    def test_failure_categories_tallied_per_type(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self._write(rd.KITESAILING_HEALTH_PATH, [
            {"attempted_at": now, "success": False, "failure_category": "navigation_error:timeout"},
            {"attempted_at": now, "success": False, "failure_category": "navigation_error:timeout"},
            {"attempted_at": now, "success": False, "failure_category": "anti_bot_challenge_detected:cloudflare"},
            {"attempted_at": now, "success": True, "failure_category": None},
        ])
        health = rd.lake_station_health()
        self.assertEqual(health["failure_categories"], {
            "navigation_error:timeout": 2,
            "anti_bot_challenge_detected:cloudflare": 1,
        })

    def test_expected_and_actual_collection_counts_match_coverage(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        today_local = now.astimezone(rd.ZURICH_TZ).date()
        slot_local = datetime(today_local.year, today_local.month, today_local.day, 12, 0, tzinfo=rd.ZURICH_TZ)
        obs_time = slot_local.astimezone(timezone.utc).isoformat()
        self._write(rd.KITESAILING_HEALTH_PATH, [{"attempted_at": obs_time, "success": True}])
        self._write(rd.KITESAILING_OBSERVATIONS_PATH, [{"observed_at": obs_time}])
        health = rd.lake_station_health()
        self.assertEqual(health["expected_collection_count"], 13)
        self.assertEqual(health["actual_collection_count"], round(health["coverage_12_18"] * 13))


class SummitStationHealthTests(unittest.TestCase):
    def test_empty_when_no_issuance(self):
        self.assertEqual(rd.summit_station_health(None), {})
        self.assertEqual(rd.summit_station_health({}), {})

    def test_empty_when_no_summit_station_in_inputs(self):
        issuance = {"station_inputs": {}, "diagnostics": {"summit_support": {"source_station": "cov"}}}
        self.assertEqual(rd.summit_station_health(issuance), {})

    def test_populates_from_issuance_when_summit_station_present(self):
        issuance = {
            "station_inputs": {"cov": {"coverage": 1.0, "latest_wind_speed": 8.0,
                                        "max_morning_gust": 10.0, "temperature_latest": -2.0}},
            "station_input_age": {"cov": 12.5},
            "diagnostics": {"summit_support": {
                "source_station": "cov", "observed_at": "2026-07-16T10:00:00+00:00",
                "status": "supportive", "explanation_key": "summit_wind_supportive",
                "raw_values": {"wind_direction_deg": 225.0},
            }},
        }
        health = rd.summit_station_health(issuance)
        self.assertIn("cov", health)
        self.assertEqual(health["cov"]["age_minutes"], 12.5)
        self.assertEqual(health["cov"]["wind_speed"], 8.0)
        self.assertEqual(health["cov"]["direction"], 225.0)
        self.assertEqual(health["cov"]["quality_flags"], [])

    def test_quality_flags_populated_when_missing_status(self):
        issuance = {
            "station_inputs": {"cov": {}},
            "station_input_age": {"cov": None},
            "diagnostics": {"summit_support": {
                "source_station": "cov", "status": "missing",
                "explanation_key": "summit_wind_missing_station_data", "raw_values": {},
            }},
        }
        health = rd.summit_station_health(issuance)
        self.assertEqual(health["cov"]["quality_flags"], ["summit_wind_missing_station_data"])

    def test_provenance_fields_populated_from_issuance(self):
        issuance = {
            "station_inputs": {"cov": {"coverage": 1.0, "latest_wind_speed": 8.0}},
            "station_input_age": {"cov": 12.5},
            "diagnostics": {"summit_support": {
                "source_station": "cov", "status": "supportive",
                "explanation_key": "summit_wind_supportive", "raw_values": {},
            }},
            "station_source_assets": {"cov": ["meteoswiss:cov:recent"]},
            "station_reporting_delay_minutes": {"cov": 15},
        }
        health = rd.summit_station_health(issuance)
        self.assertEqual(health["cov"]["source_assets"], ["meteoswiss:cov:recent"])
        self.assertEqual(health["cov"]["reporting_delay_minutes"], 15)

    def test_provenance_fields_default_empty_when_absent(self):
        issuance = {
            "station_inputs": {"cov": {}},
            "station_input_age": {"cov": None},
            "diagnostics": {"summit_support": {
                "source_station": "cov", "status": "missing",
                "explanation_key": "summit_wind_missing_station_data", "raw_values": {},
            }},
        }
        health = rd.summit_station_health(issuance)
        self.assertEqual(health["cov"]["source_assets"], [])
        self.assertIsNone(health["cov"]["reporting_delay_minutes"])


class StationNowcastStatusTests(unittest.TestCase):
    def test_none_when_no_issuance(self):
        result = rd.station_nowcast_status(None)
        self.assertIsNone(result["snapshot_used"])

    def test_none_when_field_absent_from_older_issuance(self):
        result = rd.station_nowcast_status({"issued_at": "2026-07-16T07:00:00+00:00"})
        self.assertIsNone(result["snapshot_used"])

    def test_true_when_snapshot_was_used(self):
        result = rd.station_nowcast_status({
            "station_nowcast_snapshot_used": True, "issued_at": "2026-07-16T07:00:00+00:00"})
        self.assertTrue(result["snapshot_used"])
        self.assertEqual(result["issued_at"], "2026-07-16T07:00:00+00:00")

    def test_false_when_fallback_to_historical_archive(self):
        result = rd.station_nowcast_status({"station_nowcast_snapshot_used": False})
        self.assertFalse(result["snapshot_used"])


class UnmatchedPredictionsCountTests(unittest.TestCase):
    def test_mature_unverified_prediction_counted(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        old_target_local = (now - timedelta(hours=30)).astimezone(rd.ZURICH_TZ).replace(tzinfo=None)
        predictions = [{"target_time": old_target_local.isoformat(), "verified": False}]
        self.assertEqual(rd.unmatched_predictions_count(predictions, now), 1)

    def test_verified_prediction_not_counted(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        old_target_local = (now - timedelta(hours=30)).astimezone(rd.ZURICH_TZ).replace(tzinfo=None)
        predictions = [{"target_time": old_target_local.isoformat(), "verified": True}]
        self.assertEqual(rd.unmatched_predictions_count(predictions, now), 0)

    def test_too_recent_prediction_not_counted(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        recent_target_local = (now - timedelta(hours=2)).astimezone(rd.ZURICH_TZ).replace(tzinfo=None)
        predictions = [{"target_time": recent_target_local.isoformat(), "verified": False}]
        self.assertEqual(rd.unmatched_predictions_count(predictions, now), 0)


class LakeWaterTemperatureTests(unittest.TestCase):
    def setUp(self):
        self._orig_path = rd.WATER_TEMPERATURE_PATH
        self._tmpdir = tempfile.mkdtemp()
        rd.WATER_TEMPERATURE_PATH = os.path.join(self._tmpdir, "water.json")

    def tearDown(self):
        rd.WATER_TEMPERATURE_PATH = self._orig_path

    def test_none_when_no_observations(self):
        result = rd.lake_water_temperature()
        self.assertIsNone(result["temp_c"])
        self.assertTrue(result["estimated"])

    def test_estimated_water_reading_is_kept_separate_from_air_data(self):
        with open(rd.WATER_TEMPERATURE_PATH, "w") as f:
            json.dump({
                "temp_c": 17.0,
                "retrieved_at": "2026-07-20T19:00:00+00:00",
                "source_url": "https://example.test/water",
                "estimated": True,
            }, f)
        result = rd.lake_water_temperature()
        self.assertEqual(result["temp_c"], 17.0)
        self.assertEqual(result["retrieved_at"], "2026-07-20T19:00:00+00:00")
        self.assertTrue(result["estimated"])
        self.assertEqual(result["source_url"], "https://example.test/water")


class LatestStationObservationTests(unittest.TestCase):
    def setUp(self):
        self._original = rd.CURRENT_STATION_OBSERVATIONS_PATH
        self._tmpdir = tempfile.mkdtemp()
        rd.CURRENT_STATION_OBSERVATIONS_PATH = os.path.join(self._tmpdir, "stations.json")

    def tearDown(self):
        rd.CURRENT_STATION_OBSERVATIONS_PATH = self._original

    def test_latest_samedan_reading_is_converted_to_knots(self):
        with open(rd.CURRENT_STATION_OBSERVATIONS_PATH, "w") as handle:
            json.dump({"stations": {"sam": {"observations": [
                {"timestamp_utc": "2026-07-22T08:00:00+00:00", "timestamp_local": "2026-07-22T10:00:00+02:00", "wind_speed_ms": 5.0, "wind_gust_ms": 8.0},
                {"timestamp_utc": "2026-07-22T09:00:00+00:00", "timestamp_local": "2026-07-22T11:00:00+02:00", "wind_speed_ms": 6.0, "wind_gust_ms": 9.0},
            ]}}}, handle)
        result = rd.latest_samedan_observation()
        self.assertEqual(result["observed_at"], "2026-07-22T09:00:00+00:00")
        self.assertEqual(result["observed_at_local"], "2026-07-22T11:00:00+02:00")
        self.assertEqual(result["wind_kt"], 11.7)
        self.assertEqual(result["gust_kt"], 17.5)

    def test_prefers_provisional_10min_display_observation(self):
        hourly = {"timestamp_utc": "2026-07-22T09:00:00+00:00", "wind_speed_ms": 3.0}
        display = {"timestamp_utc": "2026-07-22T09:50:00+00:00", "timestamp_local": "2026-07-22T11:50:00+02:00", "wind_speed_ms": 6.0}
        with open(rd.CURRENT_STATION_OBSERVATIONS_PATH, "w") as handle:
            json.dump({"stations": {"sam": {
                "observations": [hourly],
                "latest_display_observation": display,
                "display_observation_metadata": {"quality_status": "provisional_live", "resolution_minutes": 10},
            }}}, handle)
        result = rd.latest_samedan_observation()
        self.assertEqual(result["observed_at"], display["timestamp_utc"])
        self.assertEqual(result["quality_status"], "provisional_live")
        self.assertEqual(result["resolution_minutes"], 10)


class VerificationSourcesTests(unittest.TestCase):
    """Part 10's explicit rule: never combine lake-labelled and
    Samedan-proxy accuracy without their source counts alongside."""

    def test_counts_by_ground_truth_source(self):
        verified = [
            {"ground_truth_source": "kitesailing"},
            {"ground_truth_source": "kitesailing"},
            {"ground_truth_source": "samedan_fallback"},
        ]
        result = rd.verification_sources(verified)
        self.assertEqual(result["silvaplana_lake_count"], 2)
        self.assertEqual(result["samedan_fallback_count"], 1)
        self.assertAlmostEqual(result["lake_coverage_pct"], 2 / 3, places=3)

    def test_none_pct_when_no_verified_predictions(self):
        result = rd.verification_sources([])
        self.assertIsNone(result["lake_coverage_pct"])
        self.assertEqual(result["silvaplana_lake_count"], 0)

    def test_unmatched_count_defaults_to_zero_without_predictions_arg(self):
        result = rd.verification_sources([{"ground_truth_source": "kitesailing"}])
        self.assertEqual(result["unmatched_count"], 0)

    def test_unmatched_count_reflects_predictions_arg(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        old_target_local = (now - timedelta(hours=30)).astimezone(rd.ZURICH_TZ).replace(tzinfo=None)
        predictions = [{"target_time": old_target_local.isoformat(), "verified": False}]
        result = rd.verification_sources([], predictions)
        self.assertEqual(result["unmatched_count"], 1)


if __name__ == "__main__":
    unittest.main()

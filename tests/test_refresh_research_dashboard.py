"""Offline tests for refresh_research_dashboard.py: handles a completely
empty reports directory gracefully (fresh checkout, nothing run yet), and
correctly assembles data when reports exist. No network calls."""

import json
import os
import shutil
import tempfile
import unittest

import refresh_research_dashboard as rrd


class EmptyStateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_reports_dir = rrd.REPORTS_DIR
        self._orig_stations_path = rrd.STATIONS_MANIFEST_PATH
        self._orig_assets_path = rrd.ASSETS_MANIFEST_PATH
        self._orig_vintage_index = rrd.FORECAST_VINTAGE_INDEX_PATH
        rrd.REPORTS_DIR = os.path.join(self.tmpdir, "reports")
        rrd.STATIONS_MANIFEST_PATH = os.path.join(self.tmpdir, "stations.json")
        rrd.ASSETS_MANIFEST_PATH = os.path.join(self.tmpdir, "assets.jsonl")
        rrd.FORECAST_VINTAGE_INDEX_PATH = os.path.join(self.tmpdir, "index.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        rrd.REPORTS_DIR = self._orig_reports_dir
        rrd.STATIONS_MANIFEST_PATH = self._orig_stations_path
        rrd.ASSETS_MANIFEST_PATH = self._orig_assets_path
        rrd.FORECAST_VINTAGE_INDEX_PATH = self._orig_vintage_index

    def test_no_reports_yields_sample_data_flag(self):
        data = rrd.build_research_data()
        self.assertTrue(data["is_sample_data"])
        self.assertNotIn("station_analysis", data)

    def test_empty_forecast_vintage_index(self):
        data = rrd.build_research_data()
        self.assertEqual(data["forecast_vintages"], {"n_vintages": 0, "earliest_issue": None, "latest_issue": None})

    def test_missing_stations_manifest_returns_empty_coverage(self):
        data = rrd.build_research_data()
        self.assertEqual(data["station_coverage"], {})


class PopulatedStateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_reports_dir = rrd.REPORTS_DIR
        rrd.REPORTS_DIR = os.path.join(self.tmpdir, "reports")
        os.makedirs(rrd.REPORTS_DIR)
        with open(os.path.join(rrd.REPORTS_DIR, "station_analysis_20260101T000000Z.json"), "w") as f:
            json.dump({
                "generated_at": "2026-01-01T00:00:00+00:00",
                "correlation": {"model_wind": {"pearson": 0.4}},
                "rolling_origin_family_comparison": {
                    "full_current_model": [{"kind": "reference", "full_window": {"roc_auc": 0.75}}],
                    "full_minus_samedan_morning": [{"kind": "reference", "full_window": {"roc_auc": 0.76}}],
                },
                "station_coverage": {"sam": {"status": "available_for_analysis"}},
            }, f)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        rrd.REPORTS_DIR = self._orig_reports_dir

    def test_picks_up_latest_station_analysis_report(self):
        data = rrd.build_research_data()
        self.assertFalse(data["is_sample_data"])
        self.assertIn("station_analysis", data)

    def test_best_family_picks_highest_reference_auc(self):
        data = rrd.build_research_data()
        self.assertEqual(data["station_analysis"]["best_family"]["name"], "full_minus_samedan_morning")
        self.assertEqual(data["station_analysis"]["best_family"]["reference_roc_auc"], 0.76)

    def test_uses_latest_of_multiple_reports(self):
        with open(os.path.join(rrd.REPORTS_DIR, "station_analysis_20260201T000000Z.json"), "w") as f:
            json.dump({
                "generated_at": "2026-02-01T00:00:00+00:00",
                "correlation": {},
                "rolling_origin_family_comparison": {"full_current_model": []},
                "station_coverage": {},
            }, f)
        data = rrd.build_research_data()
        self.assertEqual(data["station_analysis"]["generated_at"], "2026-02-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()

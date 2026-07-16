"""Offline tests for forecast_vintages.py: station-observation exclusion,
issue-vs-target time separation, lead-time correctness, checksum-based
dedup, and that archival failures never raise (archive_forecast_payload_safe).
No network calls - all writes go to a temp directory."""

import gzip
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import forecast_vintages as fv


def _sample_raw():
    return {
        "silvaplana": {"time": ["2026-07-16T12:00", "2026-07-16T13:00"], "wind_speed_10m": [10.0, 12.0]},
        "bregaglia": {"time": ["2026-07-16T12:00", "2026-07-16T13:00"], "temperature_2m": [20.0, 21.0]},
        "upper": {"time": ["2026-07-16T12:00", "2026-07-16T13:00"], "wind_speed_700hPa": [15.0, 16.0]},
        "lugano": {"time": ["2026-07-16T12:00", "2026-07-16T13:00"], "pressure_msl": [1015.0, 1015.5]},
        "zurich": {"time": ["2026-07-16T12:00", "2026-07-16T13:00"], "pressure_msl": [1012.0, 1012.5]},
        "ensemble": {"time": ["2026-07-16T12:00", "2026-07-16T13:00"], "wind_speed_10m": [11.0, 13.0]},
        "samedan_obs": {"2026-07-16T07:00:00+00:00": {"speed_kmh": 5.0}},
        "lugano_obs": {"2026-07-16T07:00:00+00:00": {"pressure_hpa": 1015.0}},
        "zurich_obs": {"2026-07-16T07:00:00+00:00": {"pressure_hpa": 1012.0}},
    }


class ExtractPayloadTests(unittest.TestCase):
    def test_excludes_station_observation_keys(self):
        payload = fv.extract_forecast_payload(_sample_raw())
        for key in ("samedan_obs", "lugano_obs", "zurich_obs"):
            self.assertNotIn(key, payload)

    def test_keeps_all_forecast_model_keys(self):
        payload = fv.extract_forecast_payload(_sample_raw())
        for key in fv.FORECAST_PAYLOAD_KEYS:
            self.assertIn(key, payload)


class VintageArchiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_vintage_dir = fv.VINTAGE_DIR
        self._orig_index_path = fv.INDEX_PATH
        fv.VINTAGE_DIR = os.path.join(self.tmpdir, "forecast_vintages")
        fv.INDEX_PATH = os.path.join(fv.VINTAGE_DIR, "index.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        fv.VINTAGE_DIR = self._orig_vintage_dir
        fv.INDEX_PATH = self._orig_index_path

    def test_archive_writes_index_entry_and_gzip_file(self):
        issued_at = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        entry = fv.archive_forecast_payload(_sample_raw(), issued_at)
        self.assertTrue(os.path.exists(os.path.join(fv.BASE_DIR, entry["file"])))
        index = fv.read_index()
        self.assertEqual(len(index), 1)

    def test_issue_time_and_target_time_are_separated(self):
        issued_at = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        entry = fv.archive_forecast_payload(_sample_raw(), issued_at)
        self.assertEqual(entry["issued_at_utc"], issued_at.isoformat())
        self.assertNotEqual(entry["issued_at_utc"], entry["target_time"][0])

    def test_lead_hours_are_correct(self):
        issued_at = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)  # 07:00 CEST
        entry = fv.archive_forecast_payload(_sample_raw(), issued_at)
        # target 12:00 local (10:00 UTC) minus issue 05:00 UTC = 5 hours
        self.assertAlmostEqual(entry["lead_hours"][0], 5.0, delta=0.1)
        self.assertAlmostEqual(entry["lead_hours"][1], 6.0, delta=0.1)

    def test_identical_payload_not_archived_twice(self):
        issued_at_1 = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        issued_at_2 = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
        fv.archive_forecast_payload(_sample_raw(), issued_at_1)
        fv.archive_forecast_payload(_sample_raw(), issued_at_2)
        self.assertEqual(len(fv.read_index()), 1)

    def test_changed_payload_is_archived_as_new_vintage(self):
        raw1 = _sample_raw()
        raw2 = _sample_raw()
        raw2["silvaplana"]["wind_speed_10m"] = [99.0, 99.0]
        fv.archive_forecast_payload(raw1, datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc))
        fv.archive_forecast_payload(raw2, datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(len(fv.read_index()), 2)

    def test_dedup_is_independent_of_dict_key_order(self):
        raw1 = _sample_raw()
        raw2 = {k: raw1[k] for k in reversed(list(raw1.keys()))}
        entry1 = fv.archive_forecast_payload(raw1, datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc))
        entry2 = fv.archive_forecast_payload(raw2, datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(entry1["raw_payload_checksum"], entry2["raw_payload_checksum"])
        self.assertEqual(len(fv.read_index()), 1)

    def test_load_vintage_returns_the_archived_payload(self):
        issued_at = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        entry = fv.archive_forecast_payload(_sample_raw(), issued_at)
        payload = fv.load_vintage(entry)
        self.assertIn("silvaplana", payload)

    def test_directory_structure_is_year_month_day(self):
        issued_at = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        entry = fv.archive_forecast_payload(_sample_raw(), issued_at)
        self.assertIn(os.path.join("2026", "07", "16"), entry["file"])

    def test_coverage_summary_reflects_archived_vintages(self):
        fv.archive_forecast_payload(_sample_raw(), datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc))
        raw2 = _sample_raw()
        raw2["silvaplana"]["wind_speed_10m"] = [1.0, 2.0]
        fv.archive_forecast_payload(raw2, datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc))
        summary = fv.coverage_summary()
        self.assertEqual(summary["n_vintages"], 2)

    def test_archive_safe_never_raises_on_bad_input(self):
        result = fv.archive_forecast_payload_safe(None, datetime.now(timezone.utc))
        self.assertEqual(result, {})

    def test_empty_index_coverage_summary(self):
        summary = fv.coverage_summary()
        self.assertEqual(summary, {"n_vintages": 0, "earliest_issue": None, "latest_issue": None})


if __name__ == "__main__":
    unittest.main()

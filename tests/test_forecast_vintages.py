"""Offline tests for forecast_vintages.py: raw payload preservation, issue
time vs target time separation, lead-time computation, checksum
deduplication, and that station-observation data never leaks into a
forecast vintage. No network calls - uses a temporary directory."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import forecast_vintages as fv


def _sample_raw():
    return {
        "silvaplana": {
            "time": ["2026-07-16T14:00", "2026-07-16T15:00", "2026-07-16T16:00"],
            "wind_speed_10m": [10.0, 12.0, 14.0],
        },
        "bregaglia": {"temperature_2m": [20.0, 21.0, 22.0]},
        "upper": {"wind_speed_700hPa": [30.0, 32.0, 34.0]},
        "lugano": {"pressure_msl": [1015.0, 1015.2, 1015.4]},
        "zurich": {"pressure_msl": [1016.0, 1016.1, 1016.2]},
        "ensemble": {"wind_speed_10m_icon_seamless": [9.5, 11.5, 13.5]},
        # Real station observations - must NEVER appear in a vintage.
        "samedan_obs": {"2026-07-16T07:00:00+00:00": {"speed_kmh": 5.0}},
        "lugano_obs": {"2026-07-16T07:00:00+00:00": {"pressure_hpa": 1014.0}},
        "zurich_obs": {"2026-07-16T07:00:00+00:00": {"pressure_hpa": 1015.5}},
    }


class ForecastVintageTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_vintages_dir = fv.VINTAGES_DIR
        self._orig_index_path = fv.INDEX_PATH
        fv.VINTAGES_DIR = os.path.join(self.tmpdir, "forecast_vintages")
        fv.INDEX_PATH = os.path.join(fv.VINTAGES_DIR, "index.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        fv.VINTAGES_DIR = self._orig_vintages_dir
        fv.INDEX_PATH = self._orig_index_path

    def test_station_observations_are_excluded_from_the_vintage(self):
        entry = fv.archive_forecast_payload(_sample_raw(), issue_time_utc=datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc))
        payload = fv.load_vintage(entry["file_path"])
        for key in ("samedan_obs", "lugano_obs", "zurich_obs"):
            self.assertNotIn(key, payload)

    def test_forecast_model_keys_are_preserved(self):
        entry = fv.archive_forecast_payload(_sample_raw(), issue_time_utc=datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc))
        payload = fv.load_vintage(entry["file_path"])
        for key in ("silvaplana", "bregaglia", "upper", "lugano", "zurich", "ensemble"):
            self.assertIn(key, payload)
        self.assertEqual(payload["silvaplana"]["wind_speed_10m"], [10.0, 12.0, 14.0])

    def test_issue_time_and_target_time_are_kept_separate(self):
        issue = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        entry = fv.archive_forecast_payload(_sample_raw(), issue_time_utc=issue)
        self.assertEqual(entry["issue_timestamp_utc"], "2026-07-16T05:00:00+00:00")
        self.assertEqual(entry["target_timestamps"], ["2026-07-16T14:00", "2026-07-16T15:00", "2026-07-16T16:00"])
        self.assertNotEqual(entry["issue_timestamp_utc"], entry["target_timestamps"][0])

    def test_lead_time_hours_correct(self):
        # Issue at 05:00 UTC = 07:00 CEST. Targets 14:00/15:00/16:00 local
        # (CEST, UTC+2) = 12:00/13:00/14:00 UTC -> lead times 7h/8h/9h.
        issue = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        entry = fv.archive_forecast_payload(_sample_raw(), issue_time_utc=issue)
        self.assertEqual(entry["lead_time_hours"], [7.0, 8.0, 9.0])

    def test_identical_payload_is_not_archived_twice(self):
        raw = _sample_raw()
        issue = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        entry1 = fv.archive_forecast_payload(raw, issue_time_utc=issue)
        entry2 = fv.archive_forecast_payload(raw, issue_time_utc=issue)
        self.assertEqual(entry1["checksum"], entry2["checksum"])
        self.assertEqual(len(fv.read_index()), 1)
        # Only one compressed payload file exists on disk.
        n_files = sum(len(files) for _, _, files in os.walk(fv.VINTAGES_DIR))
        self.assertEqual(n_files, 2)  # the payload file + index.jsonl

    def test_changed_payload_is_archived_as_a_new_entry(self):
        issue = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        raw1 = _sample_raw()
        raw2 = _sample_raw()
        raw2["silvaplana"]["wind_speed_10m"] = [99.0, 99.0, 99.0]  # changed forecast
        fv.archive_forecast_payload(raw1, issue_time_utc=issue)
        fv.archive_forecast_payload(raw2, issue_time_utc=issue)
        self.assertEqual(len(fv.read_index()), 2)

    def test_checksum_is_deterministic_regardless_of_dict_key_order(self):
        raw = _sample_raw()
        payload_a = fv.extract_forecast_payload(raw)
        payload_b = {k: payload_a[k] for k in reversed(list(payload_a))}
        self.assertEqual(fv._checksum(payload_a), fv._checksum(payload_b))

    def test_archive_forecast_payload_safe_never_raises(self):
        # Pass a raw dict missing everything - extract_forecast_payload
        # returns {}, _target_times_and_lead_hours sees an empty "time"
        # list, and the whole thing must still complete without raising.
        result = fv.archive_forecast_payload_safe({})
        self.assertIsNotNone(result)  # succeeds, just archives an empty payload

    def test_coverage_summary_empty_archive(self):
        self.assertEqual(fv.coverage_summary(), {"n_vintages": 0, "earliest_issue": None, "latest_issue": None})

    def test_coverage_summary_reflects_archived_vintages(self):
        raw_morning = _sample_raw()
        raw_late_morning = _sample_raw()
        raw_late_morning["silvaplana"]["wind_speed_10m"] = [11.0, 13.0, 15.0]  # genuinely different vintage
        fv.archive_forecast_payload(raw_morning, issue_time_utc=datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc))
        fv.archive_forecast_payload(raw_late_morning, issue_time_utc=datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc))
        summary = fv.coverage_summary()
        self.assertEqual(summary["n_vintages"], 2)
        self.assertEqual(summary["earliest_issue"], "2026-07-16T05:00:00+00:00")
        self.assertEqual(summary["latest_issue"], "2026-07-16T08:00:00+00:00")

    def test_files_stored_under_year_month_day_directory(self):
        entry = fv.archive_forecast_payload(_sample_raw(), issue_time_utc=datetime(2026, 3, 5, 5, 0, tzinfo=timezone.utc))
        self.assertIn(os.path.join("2026", "03", "05"), entry["file_path"])


if __name__ == "__main__":
    unittest.main()

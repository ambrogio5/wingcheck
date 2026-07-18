"""Offline tests for sia_import.py: 10-minute CSV inspection, immutable
raw-file preservation, richer-record merging, circular-mean direction
aggregation, and honest hourly derivation (derived hours flagged, absent
hours never fabricated). Fixture data only - no network, temp dirs only."""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import sia_import

FIXTURE_10MIN_CSV = (
    "station_abbr;reference_timestamp;tre200s0;fkl010z1;fu3010z0;fu3010z1;dkl010z0;prestas0\r\n"
    "SIA;01.07.2026 12:00;15.0;5.0;18.0;27.0;350;820.0\r\n"
    "SIA;01.07.2026 12:10;15.5;5.0;21.6;30.0;10;820.2\r\n"
    "SIA;01.07.2026 12:30;16.0;5.0;25.2;33.0;30;820.4\r\n"
    "SIA;01.07.2026 14:00;17.0;5.0;36.0;40.0;220;819.0\r\n"
)


class InspectCsvTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "fixture.csv")
        with open(self.path, "w", encoding="latin-1", newline="") as f:
            f.write(FIXTURE_10MIN_CSV)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reports_real_measured_statistics(self):
        report = sia_import.inspect_csv(self.path)
        self.assertEqual(report["row_count"], 4)
        self.assertEqual(report["duplicate_timestamp_count"], 0)
        self.assertEqual(report["modal_gap_minutes"], 10.0)
        self.assertEqual(report["first_timestamp"], "2026-07-01T12:00:00")
        self.assertEqual(report["last_timestamp"], "2026-07-01T14:00:00")

    def test_counts_missing_intervals_at_modal_cadence(self):
        report = sia_import.inspect_csv(self.path)
        # 12:30 -> 14:00 is 90 minutes at a 10-minute cadence: 8 missing slots,
        # plus 12:10->12:30 has 1. Only real gaps counted, nothing invented.
        self.assertEqual(report["missing_timestamp_count_at_modal_cadence"], 9)

    def test_non_empty_columns_measured_not_assumed(self):
        report = sia_import.inspect_csv(self.path)
        self.assertIn("fu3010z0", report["non_empty_columns"])
        self.assertNotIn("station_abbr", report["non_empty_columns"])


class PreserveRawFileTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_raw_dir = sia_import.RAW_SIA_DIR
        sia_import.RAW_SIA_DIR = os.path.join(self.tmpdir, "raw", "sia")
        self.src = os.path.join(self.tmpdir, "source.csv")
        with open(self.src, "w") as f:
            f.write("original content\n")

    def tearDown(self):
        sia_import.RAW_SIA_DIR = self._orig_raw_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_preserves_with_real_checksum(self):
        meta = sia_import.preserve_raw_file(self.src, "dest.csv")
        self.assertTrue(os.path.exists(meta["dest_path"]))
        self.assertEqual(len(meta["checksum"]), 64)
        with open(meta["dest_path"]) as f:
            self.assertEqual(f.read(), "original content\n")

    def test_identical_re_preserve_is_idempotent(self):
        first = sia_import.preserve_raw_file(self.src, "dest.csv")
        second = sia_import.preserve_raw_file(self.src, "dest.csv")
        self.assertEqual(first["checksum"], second["checksum"])

    def test_refuses_to_overwrite_with_different_content(self):
        sia_import.preserve_raw_file(self.src, "dest.csv")
        with open(self.src, "w") as f:
            f.write("DIFFERENT content\n")
        with self.assertRaises(ValueError):
            sia_import.preserve_raw_file(self.src, "dest.csv")
        # original preserved copy untouched
        with open(os.path.join(sia_import.RAW_SIA_DIR, "dest.csv")) as f:
            self.assertEqual(f.read(), "original content\n")


class MergeObsTests(unittest.TestCase):
    def test_richer_record_wins(self):
        dt = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        sparse = {dt: {"temperature_c": 15.0, "wind_speed_ms": None}}
        rich = {dt: {"temperature_c": 15.0, "wind_speed_ms": 5.0}}
        merged = sia_import.merge_obs(sparse, rich)
        self.assertEqual(merged[dt]["wind_speed_ms"], 5.0)
        # and the reverse order must not let the sparser one win
        merged2 = sia_import.merge_obs(rich, sparse)
        self.assertEqual(merged2[dt]["wind_speed_ms"], 5.0)

    def test_distinct_timestamps_union(self):
        d1 = {datetime(2026, 7, 1, 12, tzinfo=timezone.utc): {"temperature_c": 1.0}}
        d2 = {datetime(2026, 7, 1, 13, tzinfo=timezone.utc): {"temperature_c": 2.0}}
        self.assertEqual(len(sia_import.merge_obs(d1, d2)), 2)


class CircularMeanTests(unittest.TestCase):
    def test_wraps_correctly_across_north(self):
        # naive mean of 350 and 10 is 180 (south - exactly wrong);
        # circular mean is 0 (north).
        self.assertAlmostEqual(sia_import._circular_mean_degrees([350, 10]), 0.0, places=6)

    def test_simple_case(self):
        self.assertAlmostEqual(sia_import._circular_mean_degrees([90, 90]), 90.0, places=6)

    def test_empty_returns_none(self):
        self.assertIsNone(sia_import._circular_mean_degrees([]))


class DeriveHourlyTests(unittest.TestCase):
    def _obs(self):
        base = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        return {
            base: {"wind_speed_ms": 5.0, "wind_gust_ms": 7.5, "wind_direction_deg": 350.0},
            base.replace(minute=10): {"wind_speed_ms": 6.0, "wind_gust_ms": 8.3, "wind_direction_deg": 10.0},
        }

    def test_scalar_fields_are_arithmetic_means(self):
        hourly = sia_import.derive_hourly_from_10min(self._obs())
        hour = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        self.assertAlmostEqual(hourly[hour]["wind_speed_ms"], 5.5)

    def test_gust_is_the_peak_not_the_mean(self):
        hourly = sia_import.derive_hourly_from_10min(self._obs())
        hour = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        self.assertAlmostEqual(hourly[hour]["wind_gust_ms"], 8.3)

    def test_direction_uses_circular_mean(self):
        hourly = sia_import.derive_hourly_from_10min(self._obs())
        hour = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        self.assertAlmostEqual(hourly[hour]["wind_direction_deg"], 0.0, places=6)

    def test_sample_count_recorded(self):
        hourly = sia_import.derive_hourly_from_10min(self._obs())
        hour = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        self.assertEqual(hourly[hour]["_n_10min_samples"], 2)

    def test_absent_hours_never_fabricated(self):
        hourly = sia_import.derive_hourly_from_10min(self._obs())
        self.assertEqual(len(hourly), 1)  # only the hour with real samples exists


if __name__ == "__main__":
    unittest.main()

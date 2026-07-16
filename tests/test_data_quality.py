"""Offline tests for data_quality.py: implausible-value flagging,
duplicate/gap detection (UTC-based, DST-safe), and sync-health checks.
Flags-never-discards is asserted throughout - validate_record/
validate_station_records never mutate or drop records."""

import unittest
from datetime import datetime, timedelta, timezone

import data_quality as dq


def _rec(ts, **overrides):
    base = {
        "timestamp_utc": ts, "wind_speed_ms": 5.0, "wind_gust_ms": 8.0,
        "temperature_c": 15.0, "pressure_sea_level_hpa": 1013.0,
    }
    base.update(overrides)
    return base


class ValidateRecordTests(unittest.TestCase):
    def test_clean_record_has_no_flags(self):
        self.assertEqual(dq.validate_record(_rec("2026-07-01T06:00:00+00:00")), [])

    def test_implausible_temperature_flagged(self):
        flags = dq.validate_record(_rec("2026-07-01T06:00:00+00:00", temperature_c=200.0))
        self.assertIn("implausible_temperature_c", flags)

    def test_implausible_pressure_flagged(self):
        flags = dq.validate_record(_rec("2026-07-01T06:00:00+00:00", pressure_sea_level_hpa=1500.0))
        self.assertIn("implausible_pressure_sea_level_hpa", flags)

    def test_negative_wind_speed_flagged(self):
        flags = dq.validate_record(_rec("2026-07-01T06:00:00+00:00", wind_speed_ms=-5.0))
        self.assertIn("negative_wind_speed", flags)

    def test_gust_less_than_speed_flagged(self):
        flags = dq.validate_record(_rec("2026-07-01T06:00:00+00:00", wind_speed_ms=10.0, wind_gust_ms=5.0))
        self.assertIn("gust_less_than_speed", flags)

    def test_future_timestamp_flagged(self):
        future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        flags = dq.validate_record(_rec(future))
        self.assertIn("future_timestamp", flags)

    def test_null_values_are_not_flagged(self):
        flags = dq.validate_record(_rec("2026-07-01T06:00:00+00:00", temperature_c=None, wind_speed_ms=None, wind_gust_ms=None))
        self.assertEqual(flags, [])

    def test_does_not_mutate_input(self):
        rec = _rec("2026-07-01T06:00:00+00:00", temperature_c=200.0)
        original = dict(rec)
        dq.validate_record(rec)
        self.assertEqual(rec, original)


class DuplicateAndGapTests(unittest.TestCase):
    def test_find_duplicates(self):
        records = [_rec("2026-07-01T06:00:00+00:00"), _rec("2026-07-01T06:00:00+00:00"), _rec("2026-07-01T07:00:00+00:00")]
        self.assertEqual(dq.find_duplicates(records), ["2026-07-01T06:00:00+00:00"])

    def test_no_duplicates_when_all_unique(self):
        records = [_rec("2026-07-01T06:00:00+00:00"), _rec("2026-07-01T07:00:00+00:00")]
        self.assertEqual(dq.find_duplicates(records), [])

    def test_finds_a_real_gap(self):
        records = [_rec("2026-07-01T06:00:00+00:00"), _rec("2026-07-01T10:00:00+00:00")]
        gaps = dq.find_timestamp_gaps(records)
        self.assertEqual(len(gaps), 1)

    def test_no_gap_for_consecutive_hours(self):
        records = [_rec(f"2026-07-01T{h:02d}:00:00+00:00") for h in range(6, 10)]
        self.assertEqual(dq.find_timestamp_gaps(records), [])

    def test_dst_transition_produces_no_spurious_gap(self):
        # UTC timestamps are evenly spaced across the real 2026 spring-
        # forward transition (01:00 UTC -> 03:00 CEST local) - since gap
        # detection works entirely in UTC, this must show zero gaps even
        # though the LOCAL clock skips an hour.
        records = [_rec(f"2026-03-29T{h:02d}:00:00+00:00") for h in range(0, 4)]
        self.assertEqual(dq.find_timestamp_gaps(records), [])


class StationRecordsValidationTests(unittest.TestCase):
    def test_clean_records_report_zero_issues(self):
        records = [_rec(f"2026-07-01T{h:02d}:00:00+00:00") for h in range(6, 10)]
        result = dq.validate_station_records(records)
        self.assertEqual(result["n_flagged"], 0)
        self.assertEqual(result["n_duplicates"], 0)
        self.assertEqual(result["n_gaps"], 0)

    def test_flagged_records_preserved_with_index(self):
        records = [_rec("2026-07-01T06:00:00+00:00", temperature_c=999.0)]
        result = dq.validate_station_records(records)
        self.assertEqual(result["n_flagged"], 1)
        self.assertEqual(result["flagged"][0]["index"], 0)
        self.assertIn("implausible_temperature_c", result["flagged"][0]["flags"])


class SyncHealthTests(unittest.TestCase):
    def test_empty_archive_for_enabled_station_flagged(self):
        findings = dq.validate_sync_health({"sam": {"enabled": True, "n_records": 0}})
        self.assertIn("sam", findings)
        self.assertEqual(findings["sam"], "empty_archive_for_enabled_station")

    def test_unconfirmed_station_is_skipped(self):
        findings = dq.validate_sync_health({"cor": {"enabled": False, "n_records": 0}})
        self.assertNotIn("cor", findings)

    def test_stale_archive_flagged(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        findings = dq.validate_sync_health({"sam": {"enabled": True, "n_records": 100, "data_end": old_date}})
        self.assertIn("sam", findings)
        self.assertIn("stale_archive", findings["sam"])

    def test_fresh_archive_not_flagged(self):
        recent_date = datetime.now(timezone.utc).isoformat()
        findings = dq.validate_sync_health({"sam": {"enabled": True, "n_records": 100, "data_end": recent_date}})
        self.assertNotIn("sam", findings)


if __name__ == "__main__":
    unittest.main()

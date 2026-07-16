"""Offline tests for data_quality.py: implausible-value flagging (never
discarding), duplicate/gap detection, DST-transition safety (UTC
timestamps never produce a spurious gap), and sync-health checks. No
network calls."""

import unittest
from datetime import datetime, timezone

import data_quality as dq


def _record(**overrides):
    base = {
        "timestamp_utc": "2026-07-16T10:00:00+00:00", "station_id": "sam",
        "air_temperature_c": None, "dew_point_c": None, "relative_humidity_pct": None,
        "pressure_station_hpa": None, "pressure_sea_level_hpa": None,
        "wind_speed_ms": 5.0, "wind_gust_ms": 8.0, "wind_direction_deg": None,
        "precipitation_mm": None, "snow_depth_cm": None,
    }
    base.update(overrides)
    return base


class ValidateRecordTests(unittest.TestCase):
    def test_plausible_record_has_no_flags(self):
        self.assertEqual(dq.validate_record(_record()), [])

    def test_implausible_temperature_is_flagged(self):
        flags = dq.validate_record(_record(air_temperature_c=90.0))
        self.assertIn("implausible_air_temperature_c", flags)

    def test_implausible_pressure_is_flagged(self):
        flags = dq.validate_record(_record(pressure_sea_level_hpa=1500.0))
        self.assertIn("implausible_pressure_sea_level_hpa", flags)

    def test_invalid_wind_direction_is_flagged(self):
        flags = dq.validate_record(_record(wind_direction_deg=400.0))
        self.assertIn("implausible_wind_direction_deg", flags)

    def test_negative_wind_speed_is_flagged(self):
        flags = dq.validate_record(_record(wind_speed_ms=-2.0))
        self.assertIn("negative_wind_speed", flags)

    def test_gust_less_than_speed_is_flagged(self):
        flags = dq.validate_record(_record(wind_speed_ms=10.0, wind_gust_ms=5.0))
        self.assertIn("gust_lt_speed", flags)

    def test_future_timestamp_is_flagged(self):
        now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        flags = dq.validate_record(_record(timestamp_utc="2026-07-16T13:00:00+00:00"), now=now)
        self.assertIn("future_timestamp", flags)

    def test_past_timestamp_not_flagged_as_future(self):
        now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        flags = dq.validate_record(_record(timestamp_utc="2026-07-16T10:00:00+00:00"), now=now)
        self.assertNotIn("future_timestamp", flags)

    def test_multiple_flags_can_coexist(self):
        flags = dq.validate_record(_record(wind_speed_ms=-5.0, air_temperature_c=100.0))
        self.assertIn("negative_wind_speed", flags)
        self.assertIn("implausible_air_temperature_c", flags)

    def test_does_not_mutate_input_record(self):
        record = _record(air_temperature_c=90.0)
        original = dict(record)
        dq.validate_record(record)
        self.assertEqual(record, original)


class TimestampGapTests(unittest.TestCase):
    def test_no_gap_in_regular_hourly_series(self):
        timestamps = [f"2026-07-16T{h:02d}:00:00+00:00" for h in range(10)]
        self.assertEqual(dq.find_timestamp_gaps(timestamps), [])

    def test_gap_detected(self):
        timestamps = ["2026-07-16T10:00:00+00:00", "2026-07-16T15:00:00+00:00"]
        gaps = dq.find_timestamp_gaps(timestamps)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["gap_hours"], 5.0)

    def test_dst_transition_does_not_produce_a_spurious_gap(self):
        # UTC timestamps 1 hour apart across a real Europe/Zurich DST
        # transition must show no gap at all - the whole point of storing
        # UTC first.
        timestamps = ["2026-03-29T00:00:00+00:00", "2026-03-29T01:00:00+00:00", "2026-03-29T02:00:00+00:00"]
        self.assertEqual(dq.find_timestamp_gaps(timestamps), [])

    def test_empty_input(self):
        self.assertEqual(dq.find_timestamp_gaps([]), [])


class ValidateStationRecordsTests(unittest.TestCase):
    def test_duplicate_timestamps_detected(self):
        records = [_record(timestamp_utc="2026-07-16T10:00:00+00:00"),
                   _record(timestamp_utc="2026-07-16T10:00:00+00:00")]
        report = dq.validate_station_records(records)
        self.assertEqual(report["n_duplicate_timestamps"], 1)

    def test_flagged_records_reported(self):
        records = [_record(air_temperature_c=90.0)]
        report = dq.validate_station_records(records)
        self.assertEqual(report["n_flagged_records"], 1)

    def test_clean_records_report_zero_issues(self):
        records = [_record(timestamp_utc=f"2026-07-16T{h:02d}:00:00+00:00") for h in range(5)]
        report = dq.validate_station_records(records)
        self.assertEqual(report["n_duplicate_timestamps"], 0)
        self.assertEqual(report["n_flagged_records"], 0)
        self.assertEqual(report["n_gaps"], 0)

    def test_does_not_mutate_input(self):
        records = [_record(air_temperature_c=90.0)]
        original = [dict(r) for r in records]
        dq.validate_station_records(records)
        self.assertEqual(records, original)


class SyncHealthTests(unittest.TestCase):
    def test_confirmed_station_with_zero_records_flagged_empty_download(self):
        manifest = {"stations": {"sam": {"verification": "confirmed", "coverage": {"n_records": 0}}}}
        report = dq.validate_sync_health(manifest)
        self.assertIn("empty_download", report["sam"])

    def test_unconfirmed_station_not_checked(self):
        manifest = {"stations": {"cor": {"verification": "candidate_unconfirmed", "coverage": {"n_records": 0}}}}
        report = dq.validate_sync_health(manifest)
        self.assertNotIn("cor", report)

    def test_stale_archive_flagged(self):
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        manifest = {"stations": {"sam": {
            "verification": "confirmed",
            "coverage": {"n_records": 100, "data_end": "2026-01-01T00:00:00+00:00"},
        }}}
        report = dq.validate_sync_health(manifest, now=now)
        self.assertIn("stale_archive", report["sam"])

    def test_fresh_archive_not_flagged(self):
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        manifest = {"stations": {"sam": {
            "verification": "confirmed",
            "coverage": {"n_records": 100, "data_end": "2026-07-16T10:00:00+00:00"},
        }}}
        report = dq.validate_sync_health(manifest, now=now)
        self.assertNotIn("sam", report)


if __name__ == "__main__":
    unittest.main()

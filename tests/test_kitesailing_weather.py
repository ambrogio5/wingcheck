"""Offline tests for kitesailing_weather.py (Part 8 hardening): the
Europe/Zurich collection/priority window helpers across the CET/CEST
transition, anti-bot challenge detection, pure reading parsing
(retrieved_at vs source_observed_at), and duplicate-reading detection /
health-log writing with LOG_PATH/HEALTH_LOG_PATH monkeypatched to a temp
directory. No network, no playwright/browser import anywhere in this file
- fetch_current_reading()/attempt_reading()'s browser-driving code paths
are exercised only indirectly, through the pure helpers they call."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import kitesailing_weather as kw


TODAY_TEXT = "Tuesday, 21.7.2026 (19:50:00) 20.5°C Windspitzen 25.0 km/h (13.5 kn)"
DETAILS_TEXT = (
    "Feuchtigkeit: 55% Luftdruck: 950.5 hPa "
    "Windrichtung: SW (225.0°) Mittelwind: 15.0 km/h (4 Bft)"
)


class CollectionWindowTests(unittest.TestCase):
    """Broad UTC cron + Python-side Europe/Zurich window filter - must
    handle the CET (UTC+1) / CEST (UTC+2) transition correctly, i.e. via
    zoneinfo rather than a fixed offset."""

    def test_summer_midday_is_within_window(self):
        # 2026-07-16 12:00 Europe/Zurich (CEST, UTC+2) -> 10:00 UTC
        now_utc = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)
        self.assertTrue(kw.is_within_collection_window(now_utc))

    def test_summer_before_window_excluded(self):
        # 03:00 UTC -> 05:00 CEST is exactly the start; 02:59 UTC is before it.
        now_utc = datetime(2026, 7, 16, 2, 59, tzinfo=timezone.utc)
        self.assertFalse(kw.is_within_collection_window(now_utc))

    def test_summer_after_window_excluded(self):
        # 21:45 CEST = 19:45 UTC is the end; one minute later is excluded.
        now_utc = datetime(2026, 7, 16, 19, 46, tzinfo=timezone.utc)
        self.assertFalse(kw.is_within_collection_window(now_utc))

    def test_winter_midday_is_within_window(self):
        # 2026-01-16 12:00 Europe/Zurich (CET, UTC+1) -> 11:00 UTC
        now_utc = datetime(2026, 1, 16, 11, 0, tzinfo=timezone.utc)
        self.assertTrue(kw.is_within_collection_window(now_utc))

    def test_same_utc_instant_straddles_dst_boundary_differently(self):
        # The same UTC hour (10:00) falls inside the window in summer
        # (12:00 CEST) but is checked independently in winter (11:00 CET) -
        # both must be computed via real zoneinfo conversion, not a fixed offset.
        summer = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)
        winter = datetime(2026, 1, 16, 10, 0, tzinfo=timezone.utc)
        self.assertTrue(kw.is_within_collection_window(summer))
        self.assertTrue(kw.is_within_collection_window(winter))


class PriorityWindowTests(unittest.TestCase):
    def test_within_priority_window(self):
        now_utc = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)  # 14:00 CEST
        self.assertTrue(kw.is_in_priority_window(now_utc))

    def test_outside_priority_window_but_inside_collection_window(self):
        now_utc = datetime(2026, 7, 16, 7, 30, tzinfo=timezone.utc)  # 09:30 CEST
        self.assertFalse(kw.is_in_priority_window(now_utc))
        self.assertTrue(kw.is_within_collection_window(now_utc))


class AntiBotDetectionTests(unittest.TestCase):
    def test_normal_page_not_flagged(self):
        self.assertIsNone(kw._detect_anti_bot_challenge("Spot Webcam - kitesailing.ch", TODAY_TEXT))

    def test_cloudflare_interstitial_flagged(self):
        cat = kw._detect_anti_bot_challenge("Just a moment...", "Checking your browser before accessing")
        self.assertIsNotNone(cat)
        self.assertIn("anti_bot_challenge_detected", cat)

    def test_captcha_flagged(self):
        cat = kw._detect_anti_bot_challenge("Please verify", "Complete the CAPTCHA to continue")
        self.assertIsNotNone(cat)

    def test_case_insensitive(self):
        cat = kw._detect_anti_bot_challenge("ATTENTION REQUIRED", "")
        self.assertIsNotNone(cat)


class ParseReadingTests(unittest.TestCase):
    def test_parses_all_fields(self):
        retrieved_at = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        reading = kw._parse_reading(TODAY_TEXT, DETAILS_TEXT, retrieved_at)
        self.assertEqual(reading["temp_c"], 20.5)
        self.assertEqual(reading["gust_kmh"], 25.0)
        self.assertEqual(reading["gust_kn"], 13.5)
        self.assertEqual(reading["avg_wind_kmh"], 15.0)
        self.assertEqual(reading["avg_wind_bft"], 4)
        self.assertEqual(reading["wind_dir_compass"], "SW")
        self.assertEqual(reading["wind_dir_deg"], 225.0)
        self.assertEqual(reading["humidity_pct"], 55.0)
        self.assertEqual(reading["pressure_hpa"], 950.5)

    def test_published_timestamp_is_canonical_observation_time(self):
        retrieved_at = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        reading = kw._parse_reading(TODAY_TEXT, DETAILS_TEXT, retrieved_at)
        self.assertEqual(reading["retrieved_at"], retrieved_at.isoformat())
        self.assertEqual(reading["observed_at"], "2026-07-21T17:50:00+00:00")
        self.assertEqual(reading["source_observed_at"], "2026-07-21T17:50:00+00:00")
        self.assertEqual(reading["source_url"], kw.URL)

    def test_german_northeast_compass_is_normalized(self):
        details = DETAILS_TEXT.replace("SW (225.0°)", "NO (45°)")
        reading = kw._parse_reading(TODAY_TEXT, details, datetime.now(timezone.utc))
        self.assertEqual(reading["wind_dir_compass"], "NE")
        self.assertEqual(reading["wind_dir_compass_raw"], "NO")

    def test_in_priority_window_flag_set_correctly(self):
        inside_text = TODAY_TEXT.replace("19:50:00", "14:00:00")
        outside_text = TODAY_TEXT.replace("19:50:00", "07:00:00")
        retrieved = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)
        self.assertTrue(kw._parse_reading(inside_text, DETAILS_TEXT, retrieved)["in_priority_window"])
        self.assertFalse(kw._parse_reading(outside_text, DETAILS_TEXT, retrieved)["in_priority_window"])

    def test_missing_field_raises_value_error(self):
        broken_details = "Feuchtigkeit: 55% Luftdruck: 950.5 hPa"  # no direction, no avg wind
        with self.assertRaises(ValueError):
            kw._parse_reading(TODAY_TEXT, broken_details, datetime.now(timezone.utc))


class DuplicateAndHealthLogTests(unittest.TestCase):
    """Exercises _is_duplicate_reading/_append_observation/_write_health_row
    against a temp log directory - never the real repo logs/ files."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_log_path = kw.LOG_PATH
        self._orig_health_path = kw.HEALTH_LOG_PATH
        kw.LOG_PATH = os.path.join(self._tmpdir, "kitesailing_observations.jsonl")
        kw.HEALTH_LOG_PATH = os.path.join(self._tmpdir, "kitesailing_ingestion_health.jsonl")

    def tearDown(self):
        kw.LOG_PATH = self._orig_log_path
        kw.HEALTH_LOG_PATH = self._orig_health_path
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _reading(self, **overrides):
        base = dict(kw._parse_reading(TODAY_TEXT, DETAILS_TEXT, datetime.now(timezone.utc)))
        base.update(overrides)
        return base

    def test_not_duplicate_when_log_empty(self):
        self.assertFalse(kw._is_duplicate_reading(self._reading()))

    def test_duplicate_when_dedup_fields_match_last_entry(self):
        first = self._reading()
        kw._append_observation(first)
        second = self._reading()  # same weather values, different retrieved_at/observed_at
        self.assertNotEqual(first["retrieved_at"], second["retrieved_at"])
        self.assertTrue(kw._is_duplicate_reading(second))

    def test_not_duplicate_when_a_dedup_field_changed(self):
        first = self._reading()
        kw._append_observation(first)
        second = self._reading(temp_c=21.9)
        self.assertFalse(kw._is_duplicate_reading(second))

    def test_timestamps_excluded_from_dedup_comparison(self):
        # Confirms timestamps are NOT in _DEDUP_FIELDS - two readings differing only in timestamps must
        # still be treated as duplicates.
        for f in ("retrieved_at", "observed_at", "source_observed_at"):
            self.assertNotIn(f, kw._DEDUP_FIELDS)

    def test_append_observation_writes_jsonl_line(self):
        reading = self._reading()
        kw._append_observation(reading)
        with open(kw.LOG_PATH) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["temp_c"], reading["temp_c"])

    def test_write_health_row_appends(self):
        kw._write_health_row({"attempted_at": "x", "success": True})
        kw._write_health_row({"attempted_at": "y", "success": False})
        with open(kw.HEALTH_LOG_PATH) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["attempted_at"], "x")
        self.assertEqual(rows[1]["success"], False)


class LoadAndClosestObservationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_log_path = kw.LOG_PATH
        kw.LOG_PATH = os.path.join(self._tmpdir, "kitesailing_observations.jsonl")

    def tearDown(self):
        kw.LOG_PATH = self._orig_log_path
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_load_observations_empty_when_no_file(self):
        self.assertEqual(kw.load_observations(), [])

    def test_closest_observation_within_tolerance(self):
        obs_time = datetime(2026, 7, 16, 12, 5, tzinfo=timezone.utc)
        kw._append_observation({"observed_at": obs_time.isoformat(), "temp_c": 20.0})
        target = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        found = kw.closest_observation(kw.load_observations(), target, tolerance_minutes=30)
        self.assertIsNotNone(found)
        self.assertEqual(found["temp_c"], 20.0)

    def test_closest_observation_outside_tolerance_returns_none(self):
        obs_time = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)
        kw._append_observation({"observed_at": obs_time.isoformat(), "temp_c": 20.0})
        target = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        found = kw.closest_observation(kw.load_observations(), target, tolerance_minutes=30)
        self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()

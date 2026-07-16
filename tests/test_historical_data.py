"""Offline tests for historical_data.py: normalization correctness,
idempotent/deduplicated sync, checksum-based asset tracking, DST-safe
timestamp handling, and full sync-integration behaviour against temp
directories (never the real logs/historical/). No network calls -
_attempt_live_fetch is monkeypatched to a no-op in every integration test."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import historical_data as hd
import station_registry as sr


def _fake_station(station_id="sam", enabled=True, verification="confirmed", delay=10):
    return sr.Station(
        station_id=station_id, name=station_id.upper(), provider="meteoswiss",
        latitude=46.5, longitude=9.8, elevation_m=1700, roles=("target_region",),
        available_variables=("wind_speed_ms",), historical_available=True, live_available=True,
        licence="test", reporting_delay_minutes=delay, enabled=enabled, verification=verification, notes="",
    )


class NormalizationTests(unittest.TestCase):
    def test_wind_kmh_to_ms_conversion(self):
        station = _fake_station()
        obs = {datetime(2026, 7, 1, 6, tzinfo=timezone.utc): {"speed_kmh": 36.0, "gust_kmh": 72.0}}
        recs = hd.normalize_wind_observations(station, obs, "src", "2026-07-01T00:00:00+00:00")
        self.assertAlmostEqual(recs[0]["wind_speed_ms"], 10.0, places=3)
        self.assertAlmostEqual(recs[0]["wind_gust_ms"], 20.0, places=3)

    def test_gust_less_than_speed_is_flagged(self):
        station = _fake_station()
        obs = {datetime(2026, 7, 1, 6, tzinfo=timezone.utc): {"speed_kmh": 50.0, "gust_kmh": 10.0}}
        recs = hd.normalize_wind_observations(station, obs, "src", "now")
        self.assertIn("gust_less_than_speed", recs[0]["quality_flags"])

    def test_missing_wind_fields_are_null_not_zero(self):
        station = _fake_station()
        obs = {datetime(2026, 7, 1, 6, tzinfo=timezone.utc): {}}
        recs = hd.normalize_wind_observations(station, obs, "src", "now")
        self.assertIsNone(recs[0]["wind_speed_ms"])
        self.assertIsNone(recs[0]["wind_gust_ms"])

    def test_pressure_normalization(self):
        station = _fake_station("lug")
        obs = {datetime(2026, 7, 1, 6, tzinfo=timezone.utc): {"pressure_hpa": 1013.2}}
        recs = hd.normalize_pressure_observations(station, obs, "src", "now")
        self.assertEqual(recs[0]["pressure_sea_level_hpa"], 1013.2)

    def test_timestamp_utc_and_local_both_recorded(self):
        station = _fake_station()
        # 2026-03-29 01:00 UTC -> 03:00 CEST (spring-forward, 02:00 skipped)
        obs = {datetime(2026, 3, 29, 1, tzinfo=timezone.utc): {"speed_kmh": 10.0}}
        recs = hd.normalize_wind_observations(station, obs, "src", "now")
        self.assertEqual(recs[0]["timestamp_utc"][:16], "2026-03-29T01:00")
        self.assertIn("2026-03-29T03:00", recs[0]["timestamp_local"])


class MergeDedupTests(unittest.TestCase):
    def _rec(self, ts, **overrides):
        base = hd._blank_record(_fake_station(), datetime.fromisoformat(ts), "src", "now")
        base.update(overrides)
        return base

    def test_new_timestamp_is_added(self):
        existing = [self._rec("2026-07-01T06:00:00+00:00", wind_speed_ms=1.0)]
        new = [self._rec("2026-07-01T07:00:00+00:00", wind_speed_ms=2.0)]
        merged, added = hd.merge_normalized_records(existing, new)
        self.assertEqual(added, 1)
        self.assertEqual(len(merged), 2)

    def test_duplicate_timestamp_not_duplicated(self):
        existing = [self._rec("2026-07-01T06:00:00+00:00", wind_speed_ms=1.0)]
        new = [self._rec("2026-07-01T06:00:00+00:00", wind_speed_ms=1.0)]
        merged, added = hd.merge_normalized_records(existing, new)
        self.assertEqual(added, 0)
        self.assertEqual(len(merged), 1)

    def test_richer_record_wins_over_sparser(self):
        sparse = self._rec("2026-07-01T06:00:00+00:00", wind_speed_ms=1.0, wind_gust_ms=None)
        rich = self._rec("2026-07-01T06:00:00+00:00", wind_speed_ms=1.0, wind_gust_ms=3.0)
        merged, _ = hd.merge_normalized_records([sparse], [rich])
        self.assertEqual(merged[0]["wind_gust_ms"], 3.0)
        # and the reverse direction must not let a sparser new record win
        merged2, _ = hd.merge_normalized_records([rich], [sparse])
        self.assertEqual(merged2[0]["wind_gust_ms"], 3.0)

    def test_merged_output_is_chronologically_sorted(self):
        a = self._rec("2026-07-02T06:00:00+00:00")
        b = self._rec("2026-07-01T06:00:00+00:00")
        merged, _ = hd.merge_normalized_records([a], [b])
        self.assertEqual([r["timestamp_utc"] for r in merged],
                          sorted(r["timestamp_utc"] for r in merged))


class ChecksumTests(unittest.TestCase):
    def test_checksum_is_deterministic(self):
        obj = {"b": 2, "a": 1}
        self.assertEqual(hd._checksum(obj), hd._checksum({"a": 1, "b": 2}))

    def test_checksum_changes_with_content(self):
        self.assertNotEqual(hd._checksum({"a": 1}), hd._checksum({"a": 2}))


class SyncIntegrationTests(unittest.TestCase):
    """Full sync() behaviour against temp directories - never touches the
    real logs/historical/. _attempt_live_fetch is monkeypatched to a no-op
    so this never makes a real network call regardless of environment."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_hist_dir = hd.HIST_DIR
        self._orig_manifest_dir = hd.MANIFEST_DIR
        self._orig_station_hourly_dir = hd.STATION_HOURLY_DIR
        self._orig_raw_cache_dir = hd.RAW_CACHE_DIR
        self._orig_coverage_path = hd.COVERAGE_MANIFEST_PATH
        self._orig_assets_path = hd.ASSETS_MANIFEST_PATH
        self._orig_live_fetch = hd._attempt_live_fetch

        hd.HIST_DIR = os.path.join(self.tmpdir, "historical")
        hd.MANIFEST_DIR = os.path.join(hd.HIST_DIR, "manifests")
        hd.STATION_HOURLY_DIR = os.path.join(hd.HIST_DIR, "station_hourly")
        hd.RAW_CACHE_DIR = os.path.join(self.tmpdir, "raw_cache")
        hd.COVERAGE_MANIFEST_PATH = os.path.join(hd.MANIFEST_DIR, "stations.json")
        hd.ASSETS_MANIFEST_PATH = os.path.join(hd.MANIFEST_DIR, "assets.jsonl")
        hd._attempt_live_fetch = lambda station_id: ({}, None)

        os.makedirs(hd.RAW_CACHE_DIR)
        with open(os.path.join(hd.RAW_CACHE_DIR, "samedan_archive.json"), "w") as f:
            json.dump({
                "2026-07-01T06:00:00+00:00": {"speed_kmh": 18.0, "gust_kmh": 30.0},
                "2026-07-01T07:00:00+00:00": {"speed_kmh": 20.0, "gust_kmh": 33.0},
            }, f)

        self._orig_registry = sr.load_registry
        sr.load_registry = lambda path=None: {"sam": _fake_station("sam")}

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        hd.HIST_DIR = self._orig_hist_dir
        hd.MANIFEST_DIR = self._orig_manifest_dir
        hd.STATION_HOURLY_DIR = self._orig_station_hourly_dir
        hd.RAW_CACHE_DIR = self._orig_raw_cache_dir
        hd.COVERAGE_MANIFEST_PATH = self._orig_coverage_path
        hd.ASSETS_MANIFEST_PATH = self._orig_assets_path
        hd._attempt_live_fetch = self._orig_live_fetch
        sr.load_registry = self._orig_registry

    def test_sync_ingests_raw_cache_and_reports_added(self):
        results = hd.sync(["sam"])
        self.assertEqual(results["sam"]["status"], "ok")
        self.assertEqual(results["sam"]["added"], 2)
        self.assertEqual(results["sam"]["total"], 2)

    def test_sync_is_idempotent(self):
        hd.sync(["sam"])
        results = hd.sync(["sam"])
        self.assertEqual(results["sam"]["added"], 0)
        self.assertEqual(results["sam"]["total"], 2)

    def test_unknown_station_reported_not_crashed(self):
        results = hd.sync(["not_a_real_station"])
        self.assertEqual(results["not_a_real_station"]["status"], "unknown_station")

    def test_not_enabled_station_is_skipped(self):
        sr.load_registry = lambda path=None: {"cor": _fake_station("cor", enabled=False, verification="unverified")}
        results = hd.sync(["cor"])
        self.assertEqual(results["cor"]["status"], "not_enabled")

    def test_no_data_available_reported_honestly(self):
        os.remove(os.path.join(hd.RAW_CACHE_DIR, "samedan_archive.json"))
        sr.load_registry = lambda path=None: {"sam": _fake_station("sam")}
        results = hd.sync(["sam"])
        self.assertEqual(results["sam"]["status"], "no_data_available")

    def test_asset_manifest_deduped_by_checksum(self):
        hd.sync(["sam"])
        hd.sync(["sam"])
        entries = hd._read_jsonl(hd.ASSETS_MANIFEST_PATH)
        # Two identical syncs of the same raw_cache content must not double
        # the asset manifest.
        checksums = [e["checksum"] for e in entries]
        self.assertEqual(len(checksums), len(set(checksums)))

    def test_coverage_report_reflects_synced_data(self):
        hd.sync(["sam"])
        cov = hd.coverage_report("sam")
        self.assertEqual(cov["sam"]["n_records"], 2)
        self.assertEqual(cov["sam"]["data_start"], "2026-07-01T06:00:00+00:00")

    def test_coverage_manifest_written_to_disk(self):
        hd.sync(["sam"])
        self.assertTrue(os.path.exists(hd.COVERAGE_MANIFEST_PATH))
        with open(hd.COVERAGE_MANIFEST_PATH) as f:
            manifest = json.load(f)
        self.assertIn("sam", manifest["stations"])

    def test_validate_archive_runs_over_synced_stations(self):
        hd.sync(["sam"])
        report = hd.validate_archive()
        self.assertIn("sam", report)
        self.assertEqual(report["sam"]["n_records"], 2)


class LiveFetchIsBestEffortTests(unittest.TestCase):
    """Exercises _attempt_live_fetch's real exception-handling path (as
    opposed to the monkeypatched no-op used above) via a FAKE meteoswiss
    module substituted into sys.modules - never a real HTTP call."""

    def test_fetch_failure_returns_empty_not_raises(self):
        import sys
        import types
        fake = types.ModuleType("meteoswiss")

        def _raise(*a, **k):
            raise RuntimeError("simulated network failure")

        fake.fetch_sam_hourly_observations = _raise
        fake.fetch_pressure_observations = _raise
        orig = sys.modules.get("meteoswiss")
        sys.modules["meteoswiss"] = fake
        try:
            obs, source = hd._attempt_live_fetch("sam")
            self.assertEqual(obs, {})
            self.assertIsNone(source)
        finally:
            if orig is not None:
                sys.modules["meteoswiss"] = orig
            else:
                del sys.modules["meteoswiss"]


if __name__ == "__main__":
    unittest.main()

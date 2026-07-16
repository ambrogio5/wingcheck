"""Offline tests for historical_data.py: normalization, deduplication,
idempotent sync, manifest updates, timezone handling (incl. DST
boundaries), and missing-field honesty. No network calls - uses small
synthetic fixtures written to a temporary directory, never the real
(large, git-ignored) archive.
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import historical_data as hd


class NormalizationTests(unittest.TestCase):
    def test_wind_observations_normalize_correctly(self):
        obs = {
            datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc): {"speed_kmh": 36.0, "gust_kmh": 54.0},
        }
        records = hd.normalize_wind_observations("sam", obs, source_file="test.json", retrieved_at="2026-07-16T00:00:00+00:00")
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(set(r.keys()), set(hd.NORMALIZED_FIELDS))
        self.assertAlmostEqual(r["wind_speed_ms"], 10.0, places=2)   # 36 km/h = 10 m/s
        self.assertAlmostEqual(r["wind_gust_ms"], 15.0, places=2)    # 54 km/h = 15 m/s
        self.assertEqual(r["station_id"], "sam")
        self.assertEqual(r["station_name"], "Samedan")
        self.assertIsNone(r["air_temperature_c"])   # not provided by this source - null, not fabricated
        self.assertIsNone(r["pressure_sea_level_hpa"])
        self.assertEqual(r["quality_flags"], [])

    def test_gust_less_than_speed_is_flagged_not_discarded(self):
        obs = {datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc): {"speed_kmh": 40.0, "gust_kmh": 20.0}}
        records = hd.normalize_wind_observations("sam", obs, source_file="t", retrieved_at="t")
        self.assertEqual(records[0]["quality_flags"], ["gust_lt_speed"])
        # Still preserved, not discarded:
        self.assertIsNotNone(records[0]["wind_speed_ms"])
        self.assertIsNotNone(records[0]["wind_gust_ms"])

    def test_pressure_observations_normalize_correctly(self):
        obs = {datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc): {"pressure_hpa": 1013.2}}
        records = hd.normalize_pressure_observations("lug", obs, source_file="t", retrieved_at="t")
        self.assertEqual(records[0]["pressure_sea_level_hpa"], 1013.2)
        self.assertIsNone(records[0]["wind_speed_ms"])

    def test_timestamp_local_reflects_cest_in_summer(self):
        obs = {datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc): {"speed_kmh": 10.0, "gust_kmh": 12.0}}
        r = hd.normalize_wind_observations("sam", obs, "t", "t")[0]
        self.assertEqual(r["timestamp_utc"], "2026-07-16T10:00:00+00:00")
        self.assertTrue(r["timestamp_local"].startswith("2026-07-16T12:00:00"))  # UTC+2 in summer

    def test_timestamp_local_reflects_cet_in_winter(self):
        obs = {datetime(2026, 1, 16, 10, 0, tzinfo=timezone.utc): {"speed_kmh": 10.0, "gust_kmh": 12.0}}
        r = hd.normalize_wind_observations("sam", obs, "t", "t")[0]
        self.assertTrue(r["timestamp_local"].startswith("2026-01-16T11:00:00"))  # UTC+1 in winter

    def test_dst_spring_forward_boundary(self):
        # Europe/Zurich's 2026 DST-start transition is at 01:00 UTC:
        # 00:00 UTC -> 01:00 CET (just before the jump), 01:00 UTC -> 03:00
        # CEST (immediately after - wall-clock 02:00-03:00 never occurs).
        obs = {datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc): {"speed_kmh": 5.0, "gust_kmh": 6.0}}
        r = hd.normalize_wind_observations("sam", obs, "t", "t")[0]
        self.assertTrue(r["timestamp_local"].startswith("2026-03-29T01:00:00"))
        obs2 = {datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc): {"speed_kmh": 5.0, "gust_kmh": 6.0}}
        r2 = hd.normalize_wind_observations("sam", obs2, "t", "t")[0]
        self.assertTrue(r2["timestamp_local"].startswith("2026-03-29T03:00:00"))


class MergeDeduplicationTests(unittest.TestCase):
    def test_new_timestamp_is_added(self):
        existing = hd.normalize_wind_observations(
            "sam", {datetime(2026, 7, 16, 10, tzinfo=timezone.utc): {"speed_kmh": 10.0, "gust_kmh": 12.0}}, "a", "a")
        new = hd.normalize_wind_observations(
            "sam", {datetime(2026, 7, 16, 11, tzinfo=timezone.utc): {"speed_kmh": 11.0, "gust_kmh": 13.0}}, "b", "b")
        merged = hd.merge_normalized_records(existing, new)
        self.assertEqual(len(merged), 2)
        self.assertEqual([r["timestamp_utc"] for r in merged],
                          sorted(r["timestamp_utc"] for r in merged))

    def test_duplicate_timestamp_does_not_duplicate_record(self):
        obs = {datetime(2026, 7, 16, 10, tzinfo=timezone.utc): {"speed_kmh": 10.0, "gust_kmh": 12.0}}
        existing = hd.normalize_wind_observations("sam", obs, "a", "a")
        new = hd.normalize_wind_observations("sam", obs, "b", "b")
        merged = hd.merge_normalized_records(existing, new)
        self.assertEqual(len(merged), 1)

    def test_richer_new_record_replaces_sparser_old_one(self):
        old = hd._blank_record("sam", datetime(2026, 7, 16, 10, tzinfo=timezone.utc), "old", "old")
        new = dict(old)
        new["air_temperature_c"] = 18.5  # more filled-in than `old`
        new["source_file"] = "new"
        merged = hd.merge_normalized_records([old], [new])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source_file"], "new")

    def test_sparser_new_record_does_not_overwrite_richer_old_one(self):
        """The core 'never replace valid old observations with missing
        values' guarantee (Phase 2.4)."""
        rich = hd._blank_record("sam", datetime(2026, 7, 16, 10, tzinfo=timezone.utc), "rich", "rich")
        rich["wind_speed_ms"] = 10.0
        rich["air_temperature_c"] = 18.5
        sparse = dict(rich)
        sparse["air_temperature_c"] = None
        sparse["source_file"] = "sparse"
        merged = hd.merge_normalized_records([rich], [sparse])
        self.assertEqual(merged[0]["air_temperature_c"], 18.5)
        self.assertEqual(merged[0]["source_file"], "rich")

    def test_merge_result_is_sorted_chronologically(self):
        obs_a = {datetime(2026, 7, 16, 12, tzinfo=timezone.utc): {"speed_kmh": 1.0, "gust_kmh": 1.0}}
        obs_b = {datetime(2026, 7, 16, 9, tzinfo=timezone.utc): {"speed_kmh": 1.0, "gust_kmh": 1.0}}
        merged = hd.merge_normalized_records(
            hd.normalize_wind_observations("sam", obs_a, "a", "a"),
            hd.normalize_wind_observations("sam", obs_b, "b", "b"))
        self.assertEqual([r["timestamp_utc"][:19] for r in merged],
                          ["2026-07-16T09:00:00", "2026-07-16T12:00:00"])


class ChecksumTests(unittest.TestCase):
    def test_checksum_is_deterministic(self):
        obj = {"b": 2, "a": 1}
        self.assertEqual(hd._checksum(obj), hd._checksum({"a": 1, "b": 2}))

    def test_checksum_changes_with_content(self):
        self.assertNotEqual(hd._checksum({"a": 1}), hd._checksum({"a": 2}))


class SyncIntegrationTests(unittest.TestCase):
    """Exercises sync()'s file-system side effects against a temporary
    directory standing in for logs/historical/ and logs/raw_cache/ - no
    network, no touching the real (git-ignored) archive."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_hist_dir = hd.HIST_DIR
        self._orig_manifest_dir = hd.MANIFEST_DIR
        self._orig_station_hourly_dir = hd.STATION_HOURLY_DIR
        self._orig_raw_cache_dir = hd.RAW_CACHE_DIR
        self._orig_stations_manifest_path = hd.STATIONS_MANIFEST_PATH
        self._orig_assets_manifest_path = hd.ASSETS_MANIFEST_PATH
        self._orig_attempt_live_fetch = hd._attempt_live_fetch
        # Tests must never make a real network call, even a failing one -
        # _attempt_live_fetch normally calls requests.get() via meteoswiss.py.
        # Stub it out entirely; live-fetch behavior itself is exercised by
        # LiveFetchIsBestEffortTests below with a monkeypatched meteoswiss
        # module instead of a real HTTP attempt.
        hd._attempt_live_fetch = lambda station_id: []

        hd.HIST_DIR = os.path.join(self.tmpdir, "historical")
        hd.MANIFEST_DIR = os.path.join(hd.HIST_DIR, "manifests")
        hd.STATION_HOURLY_DIR = os.path.join(hd.HIST_DIR, "station_hourly")
        hd.RAW_CACHE_DIR = os.path.join(self.tmpdir, "raw_cache")
        hd.STATIONS_MANIFEST_PATH = os.path.join(hd.MANIFEST_DIR, "stations.json")
        hd.ASSETS_MANIFEST_PATH = os.path.join(hd.MANIFEST_DIR, "assets.jsonl")

        os.makedirs(hd.RAW_CACHE_DIR)
        # A tiny fixture standing in for the real (much larger) samedan_archive.json.
        with open(os.path.join(hd.RAW_CACHE_DIR, "samedan_archive.json"), "w") as f:
            json.dump({
                "2026-07-16T10:00:00+00:00": {"speed_kmh": 10.0, "gust_kmh": 14.0},
                "2026-07-16T11:00:00+00:00": {"speed_kmh": 12.0, "gust_kmh": 16.0},
            }, f)
        with open(os.path.join(hd.RAW_CACHE_DIR, "pressure_lug.json"), "w") as f:
            json.dump({"2026-07-16T10:00:00+00:00": {"pressure_hpa": 1015.0}}, f)
        with open(os.path.join(hd.RAW_CACHE_DIR, "pressure_sma.json"), "w") as f:
            json.dump({"2026-07-16T10:00:00+00:00": {"pressure_hpa": 1016.5}}, f)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        hd.HIST_DIR = self._orig_hist_dir
        hd.MANIFEST_DIR = self._orig_manifest_dir
        hd.STATION_HOURLY_DIR = self._orig_station_hourly_dir
        hd.RAW_CACHE_DIR = self._orig_raw_cache_dir
        hd.STATIONS_MANIFEST_PATH = self._orig_stations_manifest_path
        hd.ASSETS_MANIFEST_PATH = self._orig_assets_manifest_path
        hd._attempt_live_fetch = self._orig_attempt_live_fetch

    def test_sync_ingests_from_raw_cache_fixture(self):
        results = hd.sync(["sam", "lug", "sma"])
        self.assertEqual(results["sam"]["total"], 2)
        self.assertEqual(results["lug"]["total"], 1)
        self.assertEqual(results["sma"]["total"], 1)
        self.assertTrue(os.path.exists(hd.station_hourly_path("sam")))

    def test_sync_is_idempotent(self):
        hd.sync(["sam"])
        first = hd._read_jsonl(hd.station_hourly_path("sam"))
        results = hd.sync(["sam"])
        second = hd._read_jsonl(hd.station_hourly_path("sam"))
        self.assertEqual(results["sam"]["added"], 0)
        self.assertEqual(first, second)

    def test_sync_with_no_data_reports_no_data_available(self):
        results = hd.sync(["cor"])  # candidate station, no fixture, network blocked
        self.assertEqual(results["cor"]["status"], "no_data_available")
        self.assertEqual(results["cor"]["total"], 0)
        self.assertFalse(os.path.exists(hd.station_hourly_path("cor")))

    def test_sync_rejects_unknown_station_id(self):
        with self.assertRaises(SystemExit):
            hd.sync(["not_a_real_station"])

    def test_sync_updates_stations_manifest(self):
        hd.sync(["sam"])
        self.assertTrue(os.path.exists(hd.STATIONS_MANIFEST_PATH))
        with open(hd.STATIONS_MANIFEST_PATH) as f:
            manifest = json.load(f)
        self.assertEqual(manifest["stations"]["sam"]["coverage"]["n_records"], 2)
        # Every registered station appears, even ones with zero data.
        self.assertIn("cor", manifest["stations"])
        self.assertEqual(manifest["stations"]["cor"]["coverage"]["n_records"], 0)

    def test_asset_manifest_is_deduplicated_by_checksum(self):
        hd.sync(["sam"])
        hd.sync(["sam"])  # identical fixture data both times
        entries = hd._read_jsonl(hd.ASSETS_MANIFEST_PATH)
        sam_entries = [e for e in entries if e["station_id"] == "sam"]
        self.assertEqual(len(sam_entries), 1, "re-syncing identical data must not duplicate the asset manifest entry")

    def test_coverage_report_reflects_synced_data(self):
        hd.sync(["sam"])
        report = hd.coverage_report("sam")
        self.assertEqual(report["sam"]["n_records"], 2)
        self.assertEqual(report["sam"]["verification"], "confirmed")

    def test_validate_passes_on_clean_archive(self):
        hd.sync(["sam", "lug", "sma"])
        problems = hd.validate_archive()
        self.assertEqual(problems, [])

    def test_export_training_defaults_to_confirmed_stations_only(self):
        hd.sync(["sam", "lug", "sma"])
        out_path, n = hd.export_training(None, os.path.join(self.tmpdir, "export.jsonl"))
        self.assertEqual(n, 4)  # 2 sam + 1 lug + 1 sma
        records = hd._read_jsonl(out_path)
        self.assertEqual({r["station_id"] for r in records}, {"sam", "lug", "sma"})


class LiveFetchIsBestEffortTests(unittest.TestCase):
    """Exercises _attempt_live_fetch's error handling with a fake
    meteoswiss module substituted in sys.modules - no real network call
    is ever attempted, unlike calling the real function against a live
    (or blocked) host."""

    def test_wind_fetch_failure_does_not_raise(self):
        import sys
        import types
        fake_meteoswiss = types.ModuleType("meteoswiss")

        def _raise_wind(station, include_historical=True):
            raise RuntimeError("simulated network failure")

        def _raise_pressure(station, include_historical=True):
            raise RuntimeError("simulated network failure")

        fake_meteoswiss.fetch_wind_observations = _raise_wind
        fake_meteoswiss.fetch_pressure_observations = _raise_pressure

        orig = sys.modules.get("meteoswiss")
        sys.modules["meteoswiss"] = fake_meteoswiss
        try:
            result = hd._attempt_live_fetch("sam")
        finally:
            if orig is not None:
                sys.modules["meteoswiss"] = orig
            else:
                del sys.modules["meteoswiss"]
        self.assertEqual(result, [])

    def test_non_meteoswiss_provider_returns_empty_without_importing_meteoswiss(self):
        # "piz_nair"'s provider is "unknown" - _attempt_live_fetch must
        # short-circuit before touching meteoswiss.py at all.
        result = hd._attempt_live_fetch("piz_nair")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()

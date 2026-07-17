"""Offline tests for historical_data.py: normalization correctness,
idempotent/deduplicated sync, checksum-based asset tracking, DST-safe
timestamp handling, and full sync-integration behaviour against temp
directories (never the real logs/historical/). No network calls -
_attempt_live_fetch is monkeypatched to a no-op in every integration test."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
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
        hd._attempt_live_fetch = lambda station_id, full_history=False: ({}, None)

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

    def test_default_sync_skips_not_enabled_stations(self):
        # station_ids=None (the default, used by the scheduled job) must
        # never attempt a live fetch for a disabled station - it isn't
        # even included in the default id list, so it never appears in
        # the results at all. Only an operator explicitly naming a
        # candidate opts into probing it (see the next test).
        sr.load_registry = lambda path=None: {
            "sam": _fake_station("sam"),
            "cor": _fake_station("cor", enabled=False, verification="unverified"),
        }
        results = hd.sync(None)
        self.assertIn("sam", results)
        self.assertNotIn("cor", results)

    def test_explicitly_named_not_enabled_station_is_probed(self):
        # `sync --station <id>` for a not-yet-enabled candidate must
        # actually attempt a fetch, so a human can inspect real data
        # before deciding whether to confirm/enable it - this is what
        # docs/STATION_RESEARCH.md's documented bootstrap process
        # ("run sync --station <id>, inspect the data, then enable it by
        # hand") depends on; without this, that process was impossible.
        sr.load_registry = lambda path=None: {"cor": _fake_station("cor", enabled=False, verification="unverified")}
        dt = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)
        hd._attempt_live_fetch = lambda station_id, full_history=False: (
            {dt: {"wind_speed_ms": 5.0}}, "meteoswiss:cor:recent")
        results = hd.sync(["cor"])
        self.assertEqual(results["cor"]["status"], "ok")
        self.assertEqual(results["cor"]["added"], 1)

    def test_explicitly_named_not_enabled_station_with_no_data_reports_honestly(self):
        # If the explicit probe finds nothing (e.g. network still blocked),
        # it must report that honestly rather than falling back to the
        # generic "not_enabled" status, which would look like the probe
        # never even tried.
        sr.load_registry = lambda path=None: {"cor": _fake_station("cor", enabled=False, verification="unverified")}
        hd._attempt_live_fetch = lambda station_id, full_history=False: ({}, None)
        results = hd.sync(["cor"])
        self.assertEqual(results["cor"]["status"], "no_data_available")

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


class NoLiveSourceStationTests(unittest.TestCase):
    """NO_LIVE_SOURCE_STATIONS (e.g. sils) must short-circuit
    _attempt_live_fetch before it ever reaches _parser_kind_for's generic
    MeteoSwiss fallback - no meteoswiss import, no network attempt."""

    def test_attempt_live_fetch_returns_empty_without_importing_meteoswiss(self):
        self.assertIn("sils", hd.NO_LIVE_SOURCE_STATIONS)
        obs, source = hd._attempt_live_fetch("sils")
        self.assertEqual(obs, {})
        self.assertIsNone(source)

    def test_normal_station_not_in_no_live_source_set(self):
        self.assertNotIn("sam", hd.NO_LIVE_SOURCE_STATIONS)
        self.assertNotIn("cov", hd.NO_LIVE_SOURCE_STATIONS)


class ImportManualCsvTests(unittest.TestCase):
    """import_manual_csv(): parses a manually-provided file, merges it into
    logs/raw_cache/generic_<station_id>.json, and syncs it into the
    normalized archive - against temp directories only."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_hist_dir = hd.HIST_DIR
        self._orig_manifest_dir = hd.MANIFEST_DIR
        self._orig_station_hourly_dir = hd.STATION_HOURLY_DIR
        self._orig_raw_cache_dir = hd.RAW_CACHE_DIR
        self._orig_coverage_path = hd.COVERAGE_MANIFEST_PATH
        self._orig_assets_path = hd.ASSETS_MANIFEST_PATH
        self._orig_registry = sr.load_registry

        hd.HIST_DIR = os.path.join(self.tmpdir, "historical")
        hd.MANIFEST_DIR = os.path.join(hd.HIST_DIR, "manifests")
        hd.STATION_HOURLY_DIR = os.path.join(hd.HIST_DIR, "station_hourly")
        hd.RAW_CACHE_DIR = os.path.join(self.tmpdir, "raw_cache")
        hd.COVERAGE_MANIFEST_PATH = os.path.join(hd.MANIFEST_DIR, "stations.json")
        hd.ASSETS_MANIFEST_PATH = os.path.join(hd.MANIFEST_DIR, "assets.jsonl")
        sr.load_registry = lambda path=None: {
            "sils": _fake_station("sils", enabled=True, verification="confirmed")
        }

        self.csv_path = os.path.join(self.tmpdir, "weatherdata_silser_see.csv")
        with open(self.csv_path, "w", encoding="utf-8") as f:
            f.write(
                '"date/time (local)";"wind direction [degrees]";"wind speed [kts]";'
                '"air temperature [°C]";"air pressure [hPa]";"clouds"\n'
                '"2014-04-02 00:00:00";110;2.00;0.00;847.00;""\n'
                '"2014-04-02 08:00:00";10;1.00;-5.00;847.00;"SKC"\n'
            )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        hd.HIST_DIR = self._orig_hist_dir
        hd.MANIFEST_DIR = self._orig_manifest_dir
        hd.STATION_HOURLY_DIR = self._orig_station_hourly_dir
        hd.RAW_CACHE_DIR = self._orig_raw_cache_dir
        hd.COVERAGE_MANIFEST_PATH = self._orig_coverage_path
        hd.ASSETS_MANIFEST_PATH = self._orig_assets_path
        sr.load_registry = self._orig_registry

    def test_rejects_a_station_not_in_no_live_source_stations(self):
        with self.assertRaises(ValueError):
            hd.import_manual_csv("sam", self.csv_path, "semicolon_weather")

    def test_rejects_unknown_format(self):
        with self.assertRaises(ValueError):
            hd.import_manual_csv("sils", self.csv_path, "not_a_real_format")

    def test_import_writes_raw_cache_and_syncs(self):
        result = hd.import_manual_csv("sils", self.csv_path, "semicolon_weather")
        self.assertEqual(result["n_parsed"], 2)
        self.assertEqual(result["sync_result"]["status"], "ok")
        self.assertEqual(result["sync_result"]["added"], 2)
        cache_path = hd._generic_raw_cache_path("sils")
        self.assertTrue(os.path.exists(cache_path))
        with open(cache_path) as f:
            cached = json.load(f)
        self.assertEqual(len(cached), 2)

    def test_second_import_merges_rather_than_overwrites(self):
        hd.import_manual_csv("sils", self.csv_path, "semicolon_weather")
        second_path = os.path.join(self.tmpdir, "more_data.csv")
        with open(second_path, "w", encoding="utf-8") as f:
            f.write(
                '"date/time (local)";"wind direction [degrees]";"wind speed [kts]";'
                '"air temperature [°C]";"air pressure [hPa]";"clouds"\n'
                '"2014-04-03 00:00:00";200;3.00;1.00;845.00;""\n'
            )
        result = hd.import_manual_csv("sils", second_path, "semicolon_weather")
        self.assertEqual(result["n_cached_total"], 3)
        self.assertEqual(result["sync_result"]["total"], 3)


class ImportCsvCliTests(unittest.TestCase):
    """historical_data.py import-csv --station/--file/--format"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_hist_dir = hd.HIST_DIR
        self._orig_manifest_dir = hd.MANIFEST_DIR
        self._orig_station_hourly_dir = hd.STATION_HOURLY_DIR
        self._orig_raw_cache_dir = hd.RAW_CACHE_DIR
        self._orig_coverage_path = hd.COVERAGE_MANIFEST_PATH
        self._orig_assets_path = hd.ASSETS_MANIFEST_PATH
        self._orig_registry = sr.load_registry

        hd.HIST_DIR = os.path.join(self.tmpdir, "historical")
        hd.MANIFEST_DIR = os.path.join(hd.HIST_DIR, "manifests")
        hd.STATION_HOURLY_DIR = os.path.join(hd.HIST_DIR, "station_hourly")
        hd.RAW_CACHE_DIR = os.path.join(self.tmpdir, "raw_cache")
        hd.COVERAGE_MANIFEST_PATH = os.path.join(hd.MANIFEST_DIR, "stations.json")
        hd.ASSETS_MANIFEST_PATH = os.path.join(hd.MANIFEST_DIR, "assets.jsonl")
        sr.load_registry = lambda path=None: {
            "sils": _fake_station("sils", enabled=True, verification="confirmed")
        }

        self.csv_path = os.path.join(self.tmpdir, "weatherdata_silser_see.csv")
        with open(self.csv_path, "w", encoding="utf-8") as f:
            f.write(
                '"date/time (local)";"wind direction [degrees]";"wind speed [kts]";'
                '"air temperature [°C]";"air pressure [hPa]";"clouds"\n'
                '"2014-04-02 00:00:00";110;2.00;0.00;847.00;""\n'
            )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        hd.HIST_DIR = self._orig_hist_dir
        hd.MANIFEST_DIR = self._orig_manifest_dir
        hd.STATION_HOURLY_DIR = self._orig_station_hourly_dir
        hd.RAW_CACHE_DIR = self._orig_raw_cache_dir
        hd.COVERAGE_MANIFEST_PATH = self._orig_coverage_path
        hd.ASSETS_MANIFEST_PATH = self._orig_assets_path
        sr.load_registry = self._orig_registry

    def test_cli_import_csv_prints_result(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hd.main(["import-csv", "--station", "sils", "--file", self.csv_path,
                     "--format", "semicolon_weather"])
        self.assertIn('"status": "ok"', buf.getvalue())
        self.assertIn('"n_parsed": 1', buf.getvalue())


class MetadataCliTests(unittest.TestCase):
    """historical_data.py metadata --station/--search - a real network
    fetch in production, mocked here via a fake meteoswiss module (same
    sys.modules-patching technique as LiveFetchIsBestEffortTests above)."""

    def setUp(self):
        self._orig = sys.modules.get("meteoswiss")
        self._fake = types.ModuleType("meteoswiss")
        sys.modules["meteoswiss"] = self._fake

    def tearDown(self):
        if self._orig is not None:
            sys.modules["meteoswiss"] = self._orig
        else:
            del sys.modules["meteoswiss"]

    def test_station_lookup_prints_metadata(self):
        self._fake.fetch_station_metadata = lambda station_id: {"station_abbr": "COV", "elevation_m": 3295}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hd.main(["metadata", "--station", "cov"])
        self.assertIn("3295", buf.getvalue())

    def test_search_prints_matches(self):
        self._fake.search_stations_by_name = lambda q: {"cov": {"name": "Piz Corvatsch"}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hd.main(["metadata", "--search", "corvatsch"])
        self.assertIn("Piz Corvatsch", buf.getvalue())

    def test_search_no_match_reports_honestly(self):
        self._fake.search_stations_by_name = lambda q: {}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hd.main(["metadata", "--search", "bernina"])
        self.assertIn("No official MeteoSwiss station found", buf.getvalue())

    def test_lookup_failure_reported_not_raised(self):
        def _raise(station_id):
            raise ValueError("station 'zzz' not found in official MeteoSwiss metadata CSV")
        self._fake.fetch_station_metadata = _raise
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hd.main(["metadata", "--station", "zzz"])
        self.assertIn("[error]", buf.getvalue())

    def test_neither_flag_given_reports_usage(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hd.main(["metadata"])
        self.assertIn("--station", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

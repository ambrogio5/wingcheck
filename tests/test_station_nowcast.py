"""Offline tests for station_nowcast.py: bounded output (LOOKBACK_HOURS),
age-minutes computation, quality-flag propagation on fetch failure, and
that build_snapshot() only ever touches enabled stations. The one
network-shaped seam (_fetch_normalized_recent) is monkeypatched in every
test - no real fetch is ever attempted."""

import unittest
from datetime import datetime, timedelta, timezone

import historical_data as hd
import station_nowcast as sn
import station_registry as sr


def _fake_station(sid="cov", enabled=True):
    return sr.Station(
        station_id=sid, name=sid.upper(), provider="meteoswiss", latitude=46.4, longitude=9.8,
        elevation_m=3295, roles=("summit",), available_variables=("wind_speed_ms",),
        historical_available=True, live_available=True, licence="test",
        reporting_delay_minutes=15, enabled=enabled, verification="unverified", notes="",
    )


def _rec(ts_utc, **overrides):
    base = {"timestamp_utc": ts_utc, "timestamp_local": ts_utc, "station_id": "cov", "wind_speed_ms": 5.0}
    base.update(overrides)
    return base


class BoundToLookbackTests(unittest.TestCase):
    def test_excludes_records_older_than_lookback(self):
        now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        old = (now - timedelta(hours=sn.LOOKBACK_HOURS + 5)).isoformat()
        recent = (now - timedelta(hours=1)).isoformat()
        records = [_rec(old), _rec(recent)]
        bounded = sn._bound_to_lookback(records, now)
        self.assertEqual(len(bounded), 1)
        self.assertEqual(bounded[0]["timestamp_utc"], recent)

    def test_sorts_chronologically(self):
        now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        r1 = _rec((now - timedelta(hours=1)).isoformat())
        r2 = _rec((now - timedelta(hours=3)).isoformat())
        bounded = sn._bound_to_lookback([r1, r2], now)
        self.assertEqual([r["timestamp_utc"] for r in bounded],
                          sorted(r["timestamp_utc"] for r in bounded))


class BuildSnapshotTests(unittest.TestCase):
    def test_only_enabled_stations_included(self):
        registry = {"cov": _fake_station("cov", enabled=True), "piz_nair": _fake_station("piz_nair", enabled=False)}
        orig = sn._fetch_normalized_recent
        sn._fetch_normalized_recent = lambda station: ([], [])
        try:
            snapshot = sn.build_snapshot(registry=registry, station_ids=["cov", "piz_nair"])
        finally:
            sn._fetch_normalized_recent = orig
        self.assertIn("cov", snapshot["stations"])
        self.assertNotIn("piz_nair", snapshot["stations"])

    def test_fetch_failure_produces_empty_but_present_entry(self):
        registry = {"cov": _fake_station("cov")}
        orig = sn._fetch_normalized_recent
        sn._fetch_normalized_recent = lambda station: ([], ["fetch_failed:simulated"])
        try:
            snapshot = sn.build_snapshot(registry=registry, station_ids=["cov"])
        finally:
            sn._fetch_normalized_recent = orig
        entry = snapshot["stations"]["cov"]
        self.assertEqual(entry["observations"], [])
        self.assertIn("fetch_failed:simulated", entry["quality_flags"])
        self.assertIsNone(entry["latest_available_at"])
        self.assertIsNone(entry["age_minutes"])

    def test_age_minutes_computed_from_latest_observation(self):
        now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        registry = {"cov": _fake_station("cov")}
        obs_time = (now - timedelta(minutes=45)).isoformat()
        orig = sn._fetch_normalized_recent
        sn._fetch_normalized_recent = lambda station: ([_rec(obs_time)], [])
        try:
            snapshot = sn.build_snapshot(registry=registry, station_ids=["cov"], now=now)
        finally:
            sn._fetch_normalized_recent = orig
        entry = snapshot["stations"]["cov"]
        self.assertAlmostEqual(entry["age_minutes"], 45.0, delta=0.1)

    def test_metadata_includes_reporting_delay_and_roles(self):
        registry = {"cov": _fake_station("cov")}
        orig = sn._fetch_normalized_recent
        sn._fetch_normalized_recent = lambda station: ([], [])
        try:
            snapshot = sn.build_snapshot(registry=registry, station_ids=["cov"])
        finally:
            sn._fetch_normalized_recent = orig
        meta = snapshot["stations"]["cov"]["metadata"]
        self.assertEqual(meta["reporting_delay_minutes"], 15)
        self.assertEqual(meta["roles"], ["summit"])

    def test_unknown_station_id_skipped_not_crashed(self):
        registry = {"cov": _fake_station("cov")}
        snapshot = sn.build_snapshot(registry=registry, station_ids=["not_a_real_station"])
        self.assertEqual(snapshot["stations"], {})


class NoLiveSourceStationGuardTests(unittest.TestCase):
    """A station in historical_data.NO_LIVE_SOURCE_STATIONS (e.g. sils, a
    manually-imported-only station with no MeteoSwiss API) must never
    attempt a real fetch here - _parser_kind_for's generic fallback would
    otherwise wrongly treat it as a MeteoSwiss station."""

    def test_fetch_normalized_recent_short_circuits(self):
        self.assertIn("sils", hd.NO_LIVE_SOURCE_STATIONS)
        station = _fake_station("sils")
        # No meteoswiss import/network call happens - if it did, this would
        # raise (or hang) since no fake meteoswiss module is installed.
        records, flags = sn._fetch_normalized_recent(station)
        self.assertEqual(records, [])
        self.assertIn("no_live_source:manual_import_only", flags)

    def test_build_snapshot_reports_flag_for_no_live_source_station(self):
        registry = {"sils": _fake_station("sils")}
        snapshot = sn.build_snapshot(registry=registry, station_ids=["sils"])
        entry = snapshot["stations"]["sils"]
        self.assertEqual(entry["observations"], [])
        self.assertIn("no_live_source:manual_import_only", entry["quality_flags"])


if __name__ == "__main__":
    unittest.main()

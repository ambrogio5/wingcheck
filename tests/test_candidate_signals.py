"""Offline tests for candidate_signals.py: the log-only probation sampler.
The single network seam (meteoswiss.fetch_station_raw_10min) is injected as
a fake in every test - no real fetch, and file I/O is redirected to a temp
path. Asserts the derivation math, QFF>QNH>none pressure preference, that
raw station pressure (QFE) is NEVER used in the gradient, honest None +
flags for a missing station, dedup/append semantics, and that nothing here
touches features/weights."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import candidate_signals as cs


def _fake_fetch(data):
    """data: {station_id: {dt_utc: {col: val}}} -> a fetch_fn; a station_id
    absent from `data` raises (simulating an unfetchable station)."""
    def fetch_fn(station_id, column_codes):
        if station_id not in data:
            raise RuntimeError(f"simulated 404 for {station_id}")
        return data[station_id]
    return fetch_fn


T = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


class DerivationTests(unittest.TestCase):
    def _one_record(self, data, now=None):
        with tempfile.TemporaryDirectory() as tmp:
            cs.LOG_PATH = os.path.join(tmp, "candidate_signals.jsonl")
            cs.sample(fetch_fn=_fake_fetch(data), now=now or (T + timedelta(minutes=5)))
            with open(cs.LOG_PATH) as f:
                return [json.loads(l) for l in f if l.strip()]

    def test_corvatsch_wind_converts_kmh_to_ms(self):
        data = {"cov": {T: {cs.COL_WIND_KMH: 36.0, cs.COL_GUST_KMH: 72.0, cs.COL_DIR_DEG: 270.0}}}
        rec = self._one_record(data)[0]
        self.assertAlmostEqual(rec["corvatsch_wind"]["speed_ms"], 10.0, places=2)
        self.assertAlmostEqual(rec["corvatsch_wind"]["gust_ms"], 20.0, places=2)
        self.assertEqual(rec["corvatsch_wind"]["direction_deg"], 270.0)
        # raw components preserved for re-derivation
        self.assertEqual(rec["corvatsch_wind"]["raw"][cs.COL_WIND_KMH], 36.0)

    def test_gradient_prefers_qff_and_records_field(self):
        data = {
            "vio": {T: {cs.COL_QFF: 1015.0, cs.COL_QNH: 1014.0, cs.COL_QFE: 895.0}},
            "sam": {T: {cs.COL_QFF: 1013.0, cs.COL_QNH: 1012.0, cs.COL_QFE: 833.0}},
        }
        g = self._one_record(data)[0]["bregaglia_engadin_gradient"]
        self.assertAlmostEqual(g["vio_minus_sam_hpa"], 2.0, places=2)
        self.assertEqual(g["reduction_field_used"]["vio"], "pp0qffs0")
        self.assertEqual(g["reduction_field_used"]["sam"], "pp0qffs0")

    def test_gradient_falls_back_to_qnh_when_qff_missing(self):
        data = {
            "vio": {T: {cs.COL_QFF: None, cs.COL_QNH: 1014.0, cs.COL_QFE: 895.0}},
            "sam": {T: {cs.COL_QFF: None, cs.COL_QNH: 1012.0, cs.COL_QFE: 833.0}},
        }
        g = self._one_record(data)[0]["bregaglia_engadin_gradient"]
        self.assertAlmostEqual(g["vio_minus_sam_hpa"], 2.0, places=2)
        self.assertEqual(g["reduction_field_used"]["vio"], "pp0qnhs0")

    def test_gradient_never_uses_raw_station_pressure(self):
        # Only raw QFE present -> reduction is impossible -> gradient None,
        # NOT the ~62 hPa altitude-offset nonsense.
        data = {
            "vio": {T: {cs.COL_QFE: 895.0}},
            "sam": {T: {cs.COL_QFE: 833.0}},
        }
        g = self._one_record(data)[0]["bregaglia_engadin_gradient"]
        self.assertIsNone(g["vio_minus_sam_hpa"])
        self.assertIsNone(g["reduction_field_used"]["vio"])
        # but the raw QFE is still stored as context
        self.assertEqual(g["raw"]["vio"][cs.COL_QFE], 895.0)

    def test_temp_spread_sia_minus_cov(self):
        data = {
            "sia": {T: {cs.COL_TEMP_C: 18.0}},
            "cov": {T: {cs.COL_TEMP_C: 3.0}},
        }
        rec = self._one_record(data)[0]
        self.assertAlmostEqual(rec["valley_summit_temp_spread"]["sia_minus_cov_c"], 15.0, places=2)

    def test_missing_station_is_honest_none_with_flag(self):
        # vio unfetchable: gradient components involving vio are None and a
        # station_unavailable flag is recorded - never invented.
        data = {"sam": {T: {cs.COL_QFF: 1013.0}}, "sia": {T: {cs.COL_QFF: 1012.0}}}
        rec = self._one_record(data)[0]
        g = rec["bregaglia_engadin_gradient"]
        self.assertIsNone(g["vio_minus_sam_hpa"])
        self.assertIsNone(g["vio_minus_sia_hpa"])
        self.assertTrue(any(f.startswith("station_unavailable:vio") for f in rec["quality_flags"]))

    def test_record_is_marked_logged_only_unscored(self):
        rec = self._one_record({"cov": {T: {cs.COL_WIND_KMH: 10.0}}})[0]
        self.assertEqual(rec["status"], "logged_only_unscored")


class AppendDedupTests(unittest.TestCase):
    def test_seed_then_append_only_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            cs.LOG_PATH = os.path.join(tmp, "candidate_signals.jsonl")
            now = T + timedelta(minutes=5)
            t_old = T - timedelta(days=5)          # outside SEED_DAYS window
            t_a = T                                 # inside window
            t_b = T + timedelta(minutes=10)         # newer than t_a
            data1 = {"cov": {t_old: {cs.COL_WIND_KMH: 1.0}, t_a: {cs.COL_WIND_KMH: 2.0}}}
            s1 = cs.sample(fetch_fn=_fake_fetch(data1), now=now)
            self.assertEqual(s1["appended"], 1)     # t_old excluded by SEED_DAYS

            # second run: t_a already logged, t_b is new
            data2 = {"cov": {t_a: {cs.COL_WIND_KMH: 2.0}, t_b: {cs.COL_WIND_KMH: 3.0}}}
            s2 = cs.sample(fetch_fn=_fake_fetch(data2), now=now + timedelta(minutes=10))
            self.assertEqual(s2["appended"], 1)     # only t_b

            with open(cs.LOG_PATH) as f:
                ts = [json.loads(l)["observed_at"] for l in f if l.strip()]
            self.assertEqual(ts, sorted(ts))        # chronological
            self.assertEqual(len(ts), len(set(ts)))  # no dupes


class NoProductionMutationTests(unittest.TestCase):
    def test_sampling_does_not_touch_weights_or_features(self):
        import hashlib
        wpath = os.path.join(os.path.dirname(cs.__file__), "weights.json")
        def digest():
            with open(wpath, "rb") as fh:
                return hashlib.md5(fh.read()).hexdigest()
        before = digest()
        with tempfile.TemporaryDirectory() as tmp:
            cs.LOG_PATH = os.path.join(tmp, "candidate_signals.jsonl")
            cs.sample(fetch_fn=_fake_fetch({"cov": {T: {cs.COL_WIND_KMH: 10.0}}}),
                      now=T + timedelta(minutes=5))
        self.assertEqual(before, digest())


if __name__ == "__main__":
    unittest.main()

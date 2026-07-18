"""Offline tests for verify_and_learn.py's SIA-first ground-truth priority
(policy v2): lake preferred, SIA reference otherwise, Samedan context-only
(never a label), missing lake+SIA leaves the row unverified, and legacy
samedan_fallback rows are never rewritten. All network-shaped seams
(kitesailing loader, SIA fetch, Samedan fetch, model update, log I/O) are
monkeypatched - no real network calls, no real file writes."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import verify_and_learn as val


def _prediction(target_time="2026-07-15T15:00", logged_at="2026-07-15T08:00:00", **overrides):
    base = {
        "target_time": target_time, "logged_at": logged_at,
        "verified": False, "tier": "GOOD", "features": {},
    }
    base.update(overrides)
    return base


def _run(records, kitesailing=None, sia=None, samedan=None):
    """Runs main() with every external seam patched; returns the records
    list as saved (save_predictions is captured, not written to disk)."""
    saved = {}

    def fake_save(recs):
        saved["records"] = recs

    with mock.patch.object(val, "load_predictions", return_value=records), \
         mock.patch.object(val, "save_predictions", side_effect=fake_save), \
         mock.patch.object(val, "load_observations", return_value=kitesailing or []), \
         mock.patch.object(val, "closest_observation",
                           side_effect=lambda obs, target, tol: obs[0] if obs else None), \
         mock.patch.object(val, "fetch_sia_hourly_observations", return_value=sia or {}), \
         mock.patch.object(val, "fetch_sam_hourly_observations", return_value=samedan or {}), \
         mock.patch.object(val, "model_update") as update_mock:
        val.main()
    return saved.get("records", records), update_mock


def _target_hour_utc(target_time="2026-07-15T15:00"):
    local = datetime.fromisoformat(target_time).replace(tzinfo=val.ZURICH_TZ)
    return local.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


class SiaFirstPriorityTests(unittest.TestCase):
    def test_lake_reading_preferred_over_sia(self):
        r = _prediction()
        hour = _target_hour_utc()
        records, _ = _run([r],
                          kitesailing=[{"avg_wind_kmh": 26.0, "gust_kmh": 37.0}],
                          sia={hour: {"wind_speed_ms": 2.0, "wind_gust_ms": 3.0}})
        self.assertTrue(records[0]["verified"])
        self.assertEqual(records[0]["ground_truth_source"], "kitesailing")
        # SIA context still logged alongside the lake label
        self.assertIn("sia_wind_kt", records[0])

    def test_sia_labels_when_no_lake_reading(self):
        r = _prediction()
        hour = _target_hour_utc()
        records, _ = _run([r], sia={hour: {"wind_speed_ms": 6.0, "wind_gust_ms": 9.0}})
        self.assertTrue(records[0]["verified"])
        self.assertEqual(records[0]["ground_truth_source"], "sia_reference")
        self.assertEqual(records[0]["ground_truth_station_id"], "sia")
        # 6 m/s = 11.7kt >= SIA_REFERENCE_KT (8kt, policy v3)
        self.assertEqual(records[0]["outcome"], 1.0)

    def test_samedan_alone_never_labels(self):
        r = _prediction()
        hour = _target_hour_utc()
        records, update_mock = _run([r], samedan={hour: {"speed_kmh": 30.0, "gust_kmh": 45.0}})
        self.assertFalse(records[0].get("verified"))
        self.assertNotIn("ground_truth_source", records[0])
        update_mock.assert_not_called()

    def test_samedan_context_logged_on_sia_labeled_row(self):
        r = _prediction()
        hour = _target_hour_utc()
        records, _ = _run([r],
                          sia={hour: {"wind_speed_ms": 6.0, "wind_gust_ms": 9.0}},
                          samedan={hour: {"speed_kmh": 20.0, "gust_kmh": 30.0}})
        self.assertEqual(records[0]["ground_truth_source"], "sia_reference")
        self.assertIn("samedan_wind_kt", records[0])
        self.assertIn("samedan_gust_kt", records[0])

    def test_policy_metadata_written_on_verified_row(self):
        r = _prediction()
        hour = _target_hour_utc()
        records, _ = _run([r], sia={hour: {"wind_speed_ms": 6.0}})
        policy = records[0]["ground_truth_policy"]
        self.assertEqual(policy["priority"], ["direct_lake", "sia_reference"])
        self.assertFalse(policy["samedan_reference_allowed"])
        self.assertIn("sia_calibration_status", policy)

    def test_legacy_samedan_fallback_rows_never_rewritten(self):
        legacy = _prediction(verified=True, ground_truth_source="samedan_fallback",
                             actual_wind_kt=12.0, outcome=1.0)
        hour = _target_hour_utc()
        records, update_mock = _run([legacy], sia={hour: {"wind_speed_ms": 1.0}})
        self.assertEqual(records[0]["ground_truth_source"], "samedan_fallback")
        self.assertEqual(records[0]["actual_wind_kt"], 12.0)
        update_mock.assert_not_called()

    def test_only_latest_prediction_per_hour_trains(self):
        early = _prediction(logged_at="2026-07-15T05:00:00")
        late = _prediction(logged_at="2026-07-15T08:00:00")
        hour = _target_hour_utc()
        records, update_mock = _run([early, late], sia={hour: {"wind_speed_ms": 6.0}})
        self.assertTrue(all(r["verified"] for r in records))
        self.assertEqual(update_mock.call_count, 1)

    def test_sia_below_threshold_labels_negative(self):
        r = _prediction()
        hour = _target_hour_utc()
        records, _ = _run([r], sia={hour: {"wind_speed_ms": 2.0}})  # 3.9kt < SIA_REFERENCE_KT (8kt)
        self.assertEqual(records[0]["outcome"], 0.0)


class SiaFetchIsBestEffortTests(unittest.TestCase):
    def test_fetch_failure_returns_empty_not_raises(self):
        with mock.patch.object(val, "fetch_station_observations",
                               side_effect=RuntimeError("simulated network failure")):
            self.assertEqual(val.fetch_sia_hourly_observations(), {})


if __name__ == "__main__":
    unittest.main()

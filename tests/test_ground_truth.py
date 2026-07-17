import json
import os
import tempfile
import unittest

import ground_truth


def obs(ts="2024-07-01T12:00:00+00:00", source="sia", station="sia", speed=5.0, flags=None):
    return ground_truth.canonical_observation(
        timestamp_utc=ts, source=source, station_id=station,
        wind_speed_ms=speed, quality_flags=flags or [], provenance={"asset": "fixture"})


class GroundTruthTests(unittest.TestCase):
    def test_registry_preserves_multiple_sources_same_timestamp(self):
        merged = ground_truth.merge_registry([], [obs(source="sia"), obs(source="windsurfcenter", station="lake")])
        self.assertEqual(len(merged), 2)

    def test_exact_source_row_is_idempotent(self):
        row = obs()
        self.assertEqual(len(ground_truth.merge_registry([row], [row])), 1)

    def test_direct_lake_wins(self):
        policy = ground_truth.LabelPolicy(allow_sia_substitution=True, sia_confidence=.9)
        selected = ground_truth.select_label([obs(source="sia"), obs(source="windsurfcenter", station="lake")], policy)
        self.assertEqual(selected["source"], "windsurfcenter")

    def test_sia_disabled_until_calibration_review(self):
        self.assertIsNone(ground_truth.select_label([obs(source="sia")], ground_truth.LabelPolicy()))

    def test_sia_confidence_is_policy_derived(self):
        policy = ground_truth.LabelPolicy(allow_sia_substitution=True, sia_confidence=.91)
        selected = ground_truth.select_label([obs(source="sia")], policy)
        self.assertEqual(selected["confidence"], .91)

    def test_flagged_record_excluded(self):
        policy = ground_truth.LabelPolicy(allow_sia_substitution=True, sia_confidence=.9)
        self.assertIsNone(ground_truth.select_label([obs(flags=["suspect"])], policy))

    def test_atomic_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "registry.jsonl")
            ground_truth.write_jsonl(path, [obs()])
            self.assertEqual(ground_truth.load_jsonl(path)[0]["source"], "sia")
            self.assertFalse(os.path.exists(path + ".tmp"))


if __name__ == "__main__":
    unittest.main()

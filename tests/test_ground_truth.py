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
        policy = ground_truth.LabelPolicy()
        selected = ground_truth.select_label([obs(source="sia"), obs(source="windsurfcenter", station="lake")], policy)
        self.assertEqual(selected["source"], "windsurfcenter")

    def test_kitesailing_outranks_sia(self):
        policy = ground_truth.LabelPolicy()
        selected = ground_truth.select_label(
            [obs(source="sia"), obs(source="kitesailing", station="silvaplana_kitesailing")], policy)
        self.assertEqual(selected["source"], "kitesailing")

    def test_sia_is_principal_reference_when_no_lake_reading(self):
        selected = ground_truth.select_label([obs(source="sia")], ground_truth.LabelPolicy())
        self.assertEqual(selected["source"], "sia")
        self.assertEqual(selected["policy_version"], 2)

    def test_samedan_alone_produces_no_default_label(self):
        # Policy v2: a missing lake+SIA hour stays UNLABELED - Samedan must
        # never silently become the label.
        self.assertIsNone(ground_truth.select_label(
            [obs(source="sam", station="sam")], ground_truth.LabelPolicy()))

    def test_sia_outranks_samedan_even_in_legacy_experiment(self):
        legacy = ground_truth.LabelPolicy(allow_samedan_fallback=True)
        selected = ground_truth.select_label(
            [obs(source="sam", station="sam"), obs(source="sia")], legacy)
        self.assertEqual(selected["source"], "sia")

    def test_sia_confidence_is_policy_derived(self):
        policy = ground_truth.LabelPolicy(sia_confidence=.91)
        selected = ground_truth.select_label([obs(source="sia")], policy)
        self.assertEqual(selected["confidence"], .91)

    def test_sia_confidence_null_by_default_not_guessed(self):
        # Equivalence to the lake target is unmeasured - the default policy
        # must not invent a numeric SIA confidence.
        selected = ground_truth.select_label([obs(source="sia")], ground_truth.LabelPolicy())
        self.assertIsNone(selected["confidence"])

    def test_flagged_record_excluded(self):
        self.assertIsNone(ground_truth.select_label([obs(flags=["suspect"])], ground_truth.LabelPolicy()))

    def test_informational_derivation_flags_do_not_exclude(self):
        # sia hourly records derived from real 10-minute readings carry
        # provenance flags that must not disqualify them from labeling.
        selected = ground_truth.select_label(
            [obs(flags=["derived_from_10min_mean", "n_10min_samples:6"])], ground_truth.LabelPolicy())
        self.assertIsNotNone(selected)
        self.assertEqual(selected["source"], "sia")

    def test_real_quality_flag_still_excludes_alongside_informational(self):
        self.assertIsNone(ground_truth.select_label(
            [obs(flags=["derived_from_10min_mean", "gust_less_than_speed"])], ground_truth.LabelPolicy()))

    def test_atomic_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "registry.jsonl")
            ground_truth.write_jsonl(path, [obs()])
            self.assertEqual(ground_truth.load_jsonl(path)[0]["source"], "sia")
            self.assertFalse(os.path.exists(path + ".tmp"))


if __name__ == "__main__":
    unittest.main()

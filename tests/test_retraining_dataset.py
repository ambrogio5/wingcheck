import unittest

import ground_truth
import retraining_dataset


class RetrainingDatasetTests(unittest.TestCase):
    def test_provenance_survives_label_preparation(self):
        features = [{"date": "2024-07-01T14:00:00+02:00", "features": {}}]
        observations = [ground_truth.canonical_observation(
            timestamp_utc="2024-07-01T12:00:00+00:00", source="windsurfcenter",
            station_id="lake", wind_speed_ms=6, confidence=1, provenance={"file": "raw.csv"})]
        rows, excluded = retraining_dataset.prepare(features, observations, ground_truth.LabelPolicy())
        self.assertEqual(excluded, {})
        self.assertEqual(rows[0]["label_provenance"]["source"], "windsurfcenter")
        self.assertEqual(rows[0]["label_provenance"]["source_provenance"]["file"], "raw.csv")

    def test_missing_label_is_not_fabricated(self):
        features = [{"date": "2024-07-01T14:00:00+02:00", "features": {}}]
        rows, excluded = retraining_dataset.prepare(features, [], ground_truth.LabelPolicy())
        self.assertEqual(rows, [])
        self.assertEqual(excluded["no_acceptable_ground_truth"], 1)


if __name__ == "__main__":
    unittest.main()

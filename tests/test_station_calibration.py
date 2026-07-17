import unittest

import station_calibration as calibration


def row(ts, source, speed, direction=180):
    return {"timestamp_utc": ts, "source": source, "station_id": source,
            "wind_speed_ms": speed, "wind_direction_deg": direction}


class StationCalibrationTests(unittest.TestCase):
    def test_perfect_agreement(self):
        records = []
        for hour, speed in enumerate((1, 2, 3, 4)):
            ts = f"2024-07-01T{hour:02d}:00:00+00:00"
            records.extend((row(ts, "windsurfcenter", speed), row(ts, "sia", speed)))
        metrics = calibration.metrics_for_pairs(calibration.pair_records(records))
        self.assertAlmostEqual(metrics["pearson"], 1.0)
        self.assertEqual(metrics["mae_ms"], 0)

    def test_lag_detection(self):
        records = []
        for hour, speed in enumerate((1, 2, 4, 8)):
            lake_ts = f"2024-07-01T{hour:02d}:00:00+00:00"
            sia_ts = f"2024-07-01T{hour + 1:02d}:00:00+00:00"
            records.extend((row(lake_ts, "windsurfcenter", speed), row(sia_ts, "sia", speed)))
        report = calibration.analyze(records, maximum_lag_hours=2)
        self.assertEqual(report["best_lag_hours"], 1)

    def test_circular_direction_error_wraps(self):
        self.assertEqual(calibration.circular_error(359, 1), 2)

    def test_small_sample_never_auto_qualifies(self):
        self.assertEqual(calibration.classify_relationship({"n": 10, "pearson": 1, "mae_ms": 0,
                                                           "bias_sia_minus_lake_ms": 0}),
                         "insufficient_overlap")


if __name__ == "__main__":
    unittest.main()

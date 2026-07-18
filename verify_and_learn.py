"""
verify_and_learn.py - run once daily, a few hours after a prediction window
has passed (e.g. 20:00 local, checking the 12-18h window from that day).

1. Reads unverified predictions from logs/predictions.jsonl.
2. For any whose target_time is safely in the past, labels the hour against
   real observed wind under the SIA-first ground-truth policy
   (config/ground_truth_policy.json, see ground_truth.py):
     a. the kitesailing.ch Silvaplana lake reading (the actual spot) when a
        scrape exists within tolerance of the target hour;
     b. otherwise the official MeteoSwiss Segl-Maria (SIA) station - the
        principal near-lake reference, ~4km up-corridor from the lake at
        the same elevation band, far closer physically than Samedan;
     c. otherwise the hour STAYS UNVERIFIED. Samedan is deliberately no
        longer a label fallback - a real 10km-distant proxy label
        (correlation ~0.5) is worse than an honest missing label now that
        SIA exists. Samedan and SIA readings are still logged as context
        on every verified row regardless of which source labeled it.
3. Labels the hour: did the wind actually reach a rideable threshold?
   SILVAPLANA_MARGINAL_KT for the real lake reading; SIA_REFERENCE_KT (same
   10kt value, explicitly provisional - SIA/lake equivalence is UNMEASURED,
   see station_calibration.py) for the SIA path.
4. Feeds (features, actual_outcome) into model.update() - one gradient
   step per sample, nudging weights.json toward what actually happened.
5. Rewrites the log with verified=True, the observed value(s), the label
   source, and the ground_truth_policy metadata attached. Rows verified
   under the old Samedan-fallback policy are NEVER rewritten - their
   ground_truth_source stays "samedan_fallback" and new-model evaluation
   classifies them separately.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import ground_truth
from kitesailing_weather import load_observations, closest_observation
from meteoswiss import fetch_sam_hourly_observations, fetch_station_observations
from model import update as model_update

LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "predictions.jsonl")

SILVAPLANA_MARGINAL_KT = 10  # real lake threshold - no proxy correction needed
SIA_REFERENCE_KT = 10  # provisional: same threshold as the lake, calibration pending (see docstring)
OBSERVATION_TOLERANCE_MINUTES = 30  # max drift between a target hour and the nearest scrape
ZURICH_TZ = ZoneInfo("Europe/Zurich")
MIN_AGE_HOURS = 20  # only verify predictions at least this old (data lag safety margin)

MS_TO_KT = 1.943844


def _ground_truth_policy_metadata(policy) -> dict:
    return {
        "priority": ["direct_lake", "sia_reference"],
        "policy_version": policy.policy_version,
        "samedan_reference_allowed": policy.allow_samedan_fallback,
        "sia_calibration_status": policy.sia_calibration_status,
    }


def fetch_sia_hourly_observations():
    """Best-effort live fetch of SIA's recent hourly observations, keyed by
    top-of-hour UTC datetime with wind in m/s. Returns {} on any failure -
    a missing SIA feed must not block verification of lake-labeled hours
    or anything else operational."""
    try:
        result = fetch_station_observations("sia", include_historical=False)
        return result["observations"]
    except Exception as e:
        print(f"  [sia] best-effort live fetch failed ({e}); continuing without it")
        return {}


def kt(kmh: float) -> float:
    return kmh / 1.852


def load_predictions():
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def save_predictions(records):
    with open(LOG_PATH, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def main():
    records = load_predictions()
    if not records:
        print("No predictions logged yet.")
        return

    now = datetime.now(timezone.utc)
    to_verify = [
        r for r in records
        if not r.get("verified")
        and (now - datetime.fromisoformat(r["target_time"]).replace(tzinfo=ZURICH_TZ).astimezone(timezone.utc))
            >= timedelta(hours=MIN_AGE_HOURS)
    ]

    if not to_verify:
        print("Nothing ready to verify yet.")
        return

    kitesailing_obs = load_observations()
    sia_obs = fetch_sia_hourly_observations()
    sam_obs = fetch_sam_hourly_observations(include_historical=False)
    policy = ground_truth.load_policy()
    policy_metadata = _ground_truth_policy_metadata(policy)

    # The same target hour gets predicted multiple times (07:00 and 10:00
    # runs; 3-day horizons re-forecast the same hours daily). Verify and
    # label ALL of them (useful history), but only take a training step on
    # the most recent prediction per target hour - otherwise repeated
    # near-identical samples get silently overweighted.
    latest_per_hour = {}
    for r in to_verify:
        key = r["target_time"]
        if key not in latest_per_hour or r["logged_at"] > latest_per_hour[key]["logged_at"]:
            latest_per_hour[key] = r

    verified_count, correct_count, trained_count = 0, 0, 0
    by_source = {"kitesailing": 0, "sia_reference": 0}
    for r in to_verify:
        target_local = datetime.fromisoformat(r["target_time"]).replace(tzinfo=ZURICH_TZ)
        target_utc = target_local.astimezone(timezone.utc)
        target_hour_utc = target_utc.replace(minute=0, second=0, microsecond=0)

        primary = closest_observation(kitesailing_obs, target_utc, OBSERVATION_TOLERANCE_MINUTES)
        sia = sia_obs.get(target_hour_utc)
        samedan = sam_obs.get(target_hour_utc)

        if primary is not None:
            actual_kt = kt(primary["avg_wind_kmh"])
            actual_gust_kt = kt(primary["gust_kmh"])
            outcome = 1.0 if actual_kt >= SILVAPLANA_MARGINAL_KT else 0.0
            ground_truth_source = "kitesailing"
            ground_truth_station_id = "silvaplana_kitesailing"
        elif sia is not None and sia.get("wind_speed_ms") is not None:
            actual_kt = sia["wind_speed_ms"] * MS_TO_KT
            actual_gust_kt = (sia.get("wind_gust_ms") or 0.0) * MS_TO_KT
            outcome = 1.0 if actual_kt >= SIA_REFERENCE_KT else 0.0
            ground_truth_source = "sia_reference"
            ground_truth_station_id = "sia"
        else:
            # No lake reading and no SIA reading: the hour stays unverified.
            # Samedan is deliberately NOT a fallback label under policy v2 -
            # an honest missing label beats a 10km-distant proxy label.
            continue

        if latest_per_hour[r["target_time"]] is r:
            model_update(r["features"], outcome)
            trained_count += 1

        r["verified"] = True
        r["ground_truth_source"] = ground_truth_source
        r["ground_truth_station_id"] = ground_truth_station_id
        r["ground_truth_policy"] = policy_metadata
        r["actual_wind_kt"] = round(actual_kt, 1)
        r["actual_gust_kt"] = round(actual_gust_kt, 1)
        # Contextual readings, logged whenever available regardless of which
        # source produced the label - the basis for the SIA/lake calibration
        # study and the long-promised Samedan correlation study, once enough
        # overlapping data has accumulated.
        if sia is not None and sia.get("wind_speed_ms") is not None:
            r["sia_wind_kt"] = round(sia["wind_speed_ms"] * MS_TO_KT, 1)
            if sia.get("wind_gust_ms") is not None:
                r["sia_gust_kt"] = round(sia["wind_gust_ms"] * MS_TO_KT, 1)
        if samedan is not None:
            r["samedan_wind_kt"] = round(kt(samedan["speed_kmh"]), 1)
            r["samedan_gust_kt"] = round(kt(samedan["gust_kmh"]), 1)
        r["outcome"] = outcome
        predicted_good = r["tier"] in ("GOOD", "MARGINAL")
        r["prediction_correct"] = (predicted_good == (outcome == 1.0))

        verified_count += 1
        correct_count += int(r["prediction_correct"])
        by_source[ground_truth_source] += 1

    save_predictions(records)

    if verified_count:
        print(f"Verified {verified_count} predictions, {correct_count} correct "
              f"({correct_count/verified_count:.0%}). Trained on {trained_count} "
              f"deduplicated hours. Ground truth source: "
              f"{by_source['kitesailing']} kitesailing, {by_source['sia_reference']} sia_reference.")
    else:
        print("Found predictions old enough to verify, but no matching lake or SIA "
              "ground truth yet - left unverified (Samedan is context only, not a label).")


if __name__ == "__main__":
    sys.exit(main())

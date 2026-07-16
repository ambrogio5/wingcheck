"""
verify_and_learn.py - run once daily, a few hours after a prediction window
has passed (e.g. 20:00 local, checking the 15-18h window from that day).

1. Reads unverified predictions from logs/predictions.jsonl.
2. For any whose target_time is safely in the past, fetches the REAL
   observed wind from MeteoSwiss's official station at Samedan (SAM) -
   not another model run, actual measured data.
3. Labels the hour: did the wind actually reach MARGINAL_KT? (1.0/0.0)
4. Feeds (features, actual_outcome) into model.update() - one gradient
   step per sample, nudging weights.json toward what actually happened.
5. Rewrites the log with verified=True and the observed value attached.

Why Samedan and not Silvaplana itself: Samedan is a real, freely-licensed
MeteoSwiss station (~10km away) and MeteoSwiss's own documentation notes
the Malojawind reaches that far, so it's a legitimate ground truth. A
live private station right on the Silvaplana lake exists too (used for
your original alerting) but scraping it isn't an official/licensed API -
Samedan keeps this on solid ground for the automated, unattended part.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from meteoswiss import fetch_sam_hourly_observations, SAM_PROXY_KT
from model import update as model_update

LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "predictions.jsonl")

MARGINAL_KT = 10
ZURICH_TZ = ZoneInfo("Europe/Zurich")
MIN_AGE_HOURS = 20  # only verify predictions at least this old (data lag safety margin)


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

    sam_obs = fetch_sam_hourly_observations(include_historical=False)

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
    for r in to_verify:
        target_local = datetime.fromisoformat(r["target_time"]).replace(tzinfo=ZURICH_TZ)
        target_utc = target_local.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)

        obs = sam_obs.get(target_utc)
        if obs is None:
            continue  # SAM data not available for this hour yet - leave unverified, try next run

        actual_kt = kt(obs["speed_kmh"])
        actual_gust_kt = kt(obs["gust_kmh"])
        outcome = 1.0 if actual_kt >= SAM_PROXY_KT else 0.0

        if latest_per_hour[r["target_time"]] is r:
            model_update(r["features"], outcome)
            trained_count += 1

        r["verified"] = True
        r["actual_wind_kt"] = round(actual_kt, 1)
        r["actual_gust_kt"] = round(actual_gust_kt, 1)
        r["outcome"] = outcome
        predicted_good = r["tier"] in ("GOOD", "MARGINAL")
        r["prediction_correct"] = (predicted_good == (outcome == 1.0))

        verified_count += 1
        correct_count += int(r["prediction_correct"])

    save_predictions(records)

    if verified_count:
        print(f"Verified {verified_count} predictions, {correct_count} correct "
              f"({correct_count/verified_count:.0%}). Trained on {trained_count} "
              f"deduplicated hours.")
    else:
        print("Found predictions old enough to verify, but no matching SAM data yet.")


if __name__ == "__main__":
    sys.exit(main())

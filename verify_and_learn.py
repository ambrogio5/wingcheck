"""
verify_and_learn.py - run once daily, a few hours after a prediction window
has passed (e.g. 20:00 local, checking the 12-18h window from that day).

1. Reads unverified predictions from logs/predictions.jsonl.
2. For any whose target_time is safely in the past, labels the hour against
   real observed wind - PRIMARILY the kitesailing.ch Silvaplana lake station
   (kitesailing_weather.py), since that's the actual spot, not a 10km-away
   proxy. Falls back to MeteoSwiss's Samedan (SAM) station only for hours
   the scraper missed. Samedan is still fetched and logged for every hour
   regardless of which source labeled it, as a secondary signal - see
   features.py's samedan_morning_score for how it also feeds the model
   directly, as a real-time upstream nowcast feature.
3. Labels the hour: did the wind actually reach a rideable threshold?
   (1.0/0.0) - SILVAPLANA_MARGINAL_KT against the real lake reading, or the
   older SAM_PROXY_KT correction against Samedan when that's the fallback.
4. Feeds (features, actual_outcome) into model.update() - one gradient
   step per sample, nudging weights.json toward what actually happened.
5. Rewrites the log with verified=True and the observed value(s) attached.

Why the switch from Samedan to kitesailing: Samedan is real, licensed,
official data, but it's ~10km from the lake and only correlates ~0.5 with
what actually happens at Silvaplana - a proxy, not the real thing, and a
likely accuracy ceiling (see CLAUDE.md). kitesailing.ch's widget is the
actual target location. The tradeoff: it's scraped (no official API, see
kitesailing_weather.py's docstring) and has no historical archive, so
backtest.py's historical retrain still has to train on Samedan-labeled
data - there's a real labeling-criterion mismatch between the historically
trained weights and the online updates this script makes. See CLAUDE.md.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kitesailing_weather import load_observations, closest_observation
from meteoswiss import fetch_sam_hourly_observations, SAM_PROXY_KT
from model import update as model_update

LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "predictions.jsonl")

SILVAPLANA_MARGINAL_KT = 10  # real lake threshold - no proxy correction needed
OBSERVATION_TOLERANCE_MINUTES = 30  # max drift between a target hour and the nearest scrape
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

    kitesailing_obs = load_observations()
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
    by_source = {"kitesailing": 0, "samedan_fallback": 0}
    for r in to_verify:
        target_local = datetime.fromisoformat(r["target_time"]).replace(tzinfo=ZURICH_TZ)
        target_utc = target_local.astimezone(timezone.utc)

        primary = closest_observation(kitesailing_obs, target_utc, OBSERVATION_TOLERANCE_MINUTES)
        samedan = sam_obs.get(target_utc.replace(minute=0, second=0, microsecond=0))

        if primary is not None:
            actual_kt = kt(primary["avg_wind_kmh"])
            actual_gust_kt = kt(primary["gust_kmh"])
            outcome = 1.0 if actual_kt >= SILVAPLANA_MARGINAL_KT else 0.0
            ground_truth_source = "kitesailing"
        elif samedan is not None:
            actual_kt = kt(samedan["speed_kmh"])
            actual_gust_kt = kt(samedan["gust_kmh"])
            outcome = 1.0 if actual_kt >= SAM_PROXY_KT else 0.0
            ground_truth_source = "samedan_fallback"
        else:
            continue  # no ground truth from either source yet - leave unverified, try next run

        if latest_per_hour[r["target_time"]] is r:
            model_update(r["features"], outcome)
            trained_count += 1

        r["verified"] = True
        r["ground_truth_source"] = ground_truth_source
        r["actual_wind_kt"] = round(actual_kt, 1)
        r["actual_gust_kt"] = round(actual_gust_kt, 1)
        # Secondary reading, logged whenever available regardless of which
        # source produced the label - the basis for a future correlation
        # study once enough overlapping data has accumulated.
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
              f"{by_source['kitesailing']} kitesailing, {by_source['samedan_fallback']} samedan_fallback.")
    else:
        print("Found predictions old enough to verify, but no matching ground truth from either source yet.")


if __name__ == "__main__":
    sys.exit(main())

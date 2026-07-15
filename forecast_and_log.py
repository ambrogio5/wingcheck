"""
forecast_and_log.py - run this on a schedule (e.g. twice daily).

1. Pulls the 20 raw data points (features.py).
2. Scores each hour in the afternoon window with the current model (model.py).
3. Sends a Telegram summary.
4. Logs every scored hour to logs/predictions.jsonl - this is the record
   verify_and_learn.py will later check against reality.
"""

import os
import sys
import json
from datetime import datetime, timezone

from features import fetch_raw, engineer_features
from model import score, load_weights

WINDOW_START_HOUR = 12
WINDOW_END_HOUR = 18
MARGINAL_KT = 10
GOOD_KT = 13

LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "predictions.jsonl")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def kt(kmh: float) -> float:
    return kmh / 1.852


def tier_from_prob(p, weights):
    # Thresholds are calibrated by backtest.py on real historical outcomes
    # and stored in weights.json; the values below are only fallbacks.
    th = weights.get("tier_thresholds", {})
    good = th.get("good", 0.65)
    marginal = th.get("marginal", 0.40)
    if p >= good:
        return "GOOD"
    if p >= marginal:
        return "MARGINAL"
    return "UNLIKELY"


def build_and_log():
    raw = fetch_raw(forecast_days=3)
    weights = load_weights()
    times = raw["silvaplana"]["time"]

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    results = []

    with open(LOG_PATH, "a") as logf:
        for idx, t in enumerate(times):
            dt = datetime.fromisoformat(t)
            if not (WINDOW_START_HOUR <= dt.hour <= WINDOW_END_HOUR):
                continue

            feats = engineer_features(raw, idx)
            p = score(feats, weights)
            tier = tier_from_prob(p, weights)
            model_kt = kt(raw["silvaplana"]["wind_speed_10m"][idx])
            gust_kt = kt(raw["silvaplana"]["wind_gusts_10m"][idx])

            record = {
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "target_time": t,
                "probability": round(p, 3),
                "tier": tier,
                "model_wind_kt": round(model_kt, 1),
                "model_gust_kt": round(gust_kt, 1),
                "features": feats,
                "weights_version": weights.get("version", 1),
                "verified": False,
            }
            logf.write(json.dumps(record) + "\n")
            results.append(record)

    return results


def format_message(results):
    good = [r for r in results if r["tier"] == "GOOD"]
    marginal = [r for r in results if r["tier"] == "MARGINAL"]

    if not good and not marginal:
        return "🌬️ *Silvaplana*: nessun segnale di Maloja wind nei prossimi giorni (finestra 12-18h)."

    lines = ["🪁 *Silvaplana - previsione Maloja wind*"]
    if good:
        lines.append(f"\n✅ *BUONO ({GOOD_KT}kt+, prob. {max(r['probability'] for r in good):.0%})*")
        for r in good:
            dt = datetime.fromisoformat(r["target_time"])
            lines.append(f"  {dt.strftime('%a %d/%m %H:%M')} — p={r['probability']:.0%}, "
                          f"modello {r['model_wind_kt']:.0f}kt (raffica {r['model_gust_kt']:.0f}kt)")
    if marginal:
        lines.append(f"\n🟡 *MARGINALE ({MARGINAL_KT}-{GOOD_KT}kt)*")
        for r in marginal:
            dt = datetime.fromisoformat(r["target_time"])
            lines.append(f"  {dt.strftime('%a %d/%m %H:%M')} — p={r['probability']:.0%}, "
                          f"modello {r['model_wind_kt']:.0f}kt (raffica {r['model_gust_kt']:.0f}kt)")

    return "\n".join(lines)


def send_telegram(message: str):
    import requests
    if not BOT_TOKEN or not CHAT_ID:
        print("[warn] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing instead:\n")
        print(message)
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=20)
    resp.raise_for_status()


def main():
    results = build_and_log()
    send_telegram(format_message(results))
    print(f"[{datetime.now(timezone.utc).isoformat()}] logged {len(results)} predictions.")


if __name__ == "__main__":
    sys.exit(main())

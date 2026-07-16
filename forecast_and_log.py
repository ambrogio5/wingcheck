"""
forecast_and_log.py - run this on a schedule (e.g. twice daily).

1. Pulls the raw data points (features.py).
2. Scores each hour in the afternoon window with the current model (model.py).
3. Sends a Telegram summary.
4. Logs every scored hour to logs/predictions.jsonl - engineered features
   (for verify_and_learn.py to later check against reality) AND the full
   unnormalized raw snapshot (features.raw_snapshot) - Open-Meteo's live API
   only serves ~3 months of history, so this is the only lasting record of
   what a given live forecast actually said, useful for building new
   features later even on hours that are long since verified.
"""

import os
import sys
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from features import fetch_raw, engineer_features, raw_snapshot
from model import score, load_weights, SCHEMA_VERSION

WINDOW_START_HOUR = 12  # reverted from a brief narrowing to 15 - backtest.py's
WINDOW_END_HOUR = 18    # docstring has the evidence: narrowing dropped
                        # holdout AUC from 0.750 to 0.683, it didn't help
MARGINAL_KT = 10
GOOD_KT = 13

LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "predictions.jsonl")
ISSUANCE_LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "forecast_issuances.jsonl")

# There is no probability-calibration step (Platt/isotonic/etc.) in this
# project yet - model.score() outputs the raw sigmoid probability. This
# constant exists purely so forecast_issuances.jsonl records an explicit,
# stable value now rather than leaving the field silently absent - bump it
# only if a real calibration transform is ever added ahead of model.score().
CALIBRATION_VERSION = "uncalibrated-v1"

ZURICH_TZ = ZoneInfo("Europe/Zurich")

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


def _current_station_cutoff(issued_at_local: datetime) -> str:
    """Which of the two scheduled issuance cutoffs (see station_features.py)
    this run corresponds to - a simple, deterministic rule matching the
    07:00/10:00 CEST schedule in .github/workflows/wingcheck.yml, not a
    guess: anything before 09:00 local counts as the 07:00 run."""
    return "07:00" if issued_at_local.hour < 9 else "10:00"


def _load_station_inputs(today_local_date: str, cutoff: str):
    """Best-effort: loads each enabled station's already-synced archive
    (historical_data.py sync populates it - this function does NOT fetch
    over the network itself, keeping forecast_and_log.py's own network
    surface unchanged) and generates today's pre-forecast station
    features. Returns ({}, {}) on any failure - diagnostics/session
    summaries degrade to "missing" gracefully rather than blocking the
    actual forecast/Telegram send."""
    try:
        import historical_data as hd
        import station_features as sf
        import station_registry
        registry = station_registry.load_registry()
        station_feats = {}
        station_input_age = {}
        for sid, s in registry.items():
            if not s.enabled:
                continue
            records = hd._read_jsonl(hd.station_hourly_path(sid))
            feats = sf.generate_station_features(records, today_local_date, cutoff, s.reporting_delay_minutes)
            station_feats[sid] = feats
            todays = [r for r in records if r["timestamp_local"].startswith(today_local_date)]
            if todays:
                latest = max(todays, key=lambda r: r["timestamp_local"])
                latest_dt = datetime.fromisoformat(latest["timestamp_local"])
                station_input_age[sid] = round((datetime.now(ZURICH_TZ) - latest_dt).total_seconds() / 60.0, 1)
            else:
                station_input_age[sid] = None
        return station_feats, station_input_age
    except Exception as e:
        print(f"[warn] could not load station inputs for diagnostics ({e}); continuing without them")
        return {}, {}


def _build_diagnostics(station_feats: dict, forecast_pressure_signal):
    import maloja_diagnostics as md
    return {
        "source_heating": md.source_heating(station_feats.get("_source_region", {}), station_feats.get("sam", {})),
        "pass_activation": md.pass_activation(station_feats.get("_pass", {})),
        "summit_support": md.summit_support(station_feats.get("_summit", {})),
        "radiation_support": md.radiation_support(station_feats.get("_source_region", {})),
        "pressure_support": md.pressure_support(station_feats.get("lug", {}), station_feats.get("sma", {}),
                                                  forecast_pressure_signal=forecast_pressure_signal),
        "competing_flow": md.competing_flow(None),  # no confirmed direction-reporting station yet
        "data_health": md.data_health({k: v for k, v in station_feats.items() if not k.startswith("_")}),
    }


def _log_issuance(raw, results, weights, issued_at_utc, vintage_entry):
    """Best-effort append-only issuance record (section 11) - failures here
    must never break the forecast/Telegram send that already happened."""
    try:
        import session_forecast as sfc
        issued_at_local = issued_at_utc.astimezone(ZURICH_TZ)
        cutoff = _current_station_cutoff(issued_at_local)
        today_local_date = issued_at_local.strftime("%Y-%m-%d")

        station_feats, station_input_age = _load_station_inputs(today_local_date, cutoff)
        pressure_signal_values = [r["features"].get("pressure_signal") for r in results if r["features"].get("pressure_signal") is not None]
        forecast_pressure_signal = sum(pressure_signal_values) / len(pressure_signal_values) if pressure_signal_values else None
        diagnostics = _build_diagnostics(station_feats, forecast_pressure_signal)
        station_quality_flags = [d["explanation_key"] for d in diagnostics.values() if d.get("missing")]

        by_date = {}
        for r in results:
            by_date.setdefault(r["target_time"][:10], []).append(r)
        agreement_values = [r["features"].get("ensemble_agreement_score") for r in results if r["features"].get("ensemble_agreement_score") is not None]
        model_agreement = sum(agreement_values) / len(agreement_values) if agreement_values else None
        any_station_missing = any(f.get("missing_indicator") == 1.0 for f in station_feats.values() if isinstance(f, dict))
        max_station_age = max([a for a in station_input_age.values() if a is not None], default=0.0)

        session_forecasts = {
            date: sfc.build_session_forecast(
                day_results, diagnostics=diagnostics, model_agreement=model_agreement,
                station_data_missing=any_station_missing, data_age_minutes=max_station_age,
            )
            for date, day_results in by_date.items()
        }

        record = {
            "issued_at": issued_at_utc.isoformat(),
            "model_version": weights.get("version"),
            "feature_schema_version": SCHEMA_VERSION,
            "calibration_version": CALIBRATION_VERSION,
            "station_cutoff": cutoff,
            "station_inputs": station_feats,
            "station_input_age": station_input_age,
            "station_quality_flags": station_quality_flags,
            "diagnostics": diagnostics,
            "session_forecast": session_forecasts,
            "hourly_predictions": [
                {"target_time": r["target_time"], "probability": r["probability"], "tier": r["tier"],
                 "model_wind_kt": r["model_wind_kt"], "model_gust_kt": r["model_gust_kt"]}
                for r in results
            ],
            "raw_payload_checksums": {"open_meteo": vintage_entry.get("raw_payload_checksum")} if vintage_entry else {},
            "commit_sha": _git_commit_sha(),
        }
        os.makedirs(os.path.dirname(ISSUANCE_LOG_PATH), exist_ok=True)
        with open(ISSUANCE_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"[warn] issuance record logging failed, continuing without it: {e}")


def _git_commit_sha():
    try:
        import subprocess
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=os.path.dirname(__file__), stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def build_and_log():
    from forecast_vintages import archive_forecast_payload_safe

    raw = fetch_raw(forecast_days=3)
    issued_at_utc = datetime.now(timezone.utc)
    vintage_entry = archive_forecast_payload_safe(
        raw, issued_at_utc,
        source_url="https://api.open-meteo.com/v1/forecast (Silvaplana/Bregaglia/Maloja + Lugano/Zurich pressure + ICON/GFS/ECMWF ensemble)",
    )

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
            wind_dir_deg = raw["silvaplana"]["wind_direction_10m"][idx]

            record = {
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "target_time": t,
                "probability": round(p, 3),
                "tier": tier,
                "model_wind_kt": round(model_kt, 1),
                "model_gust_kt": round(gust_kt, 1),
                # Raw compass degrees (0-360, meteorological convention - the
                # direction the wind is blowing FROM), kept unconverted here;
                # refresh_dashboard.py turns this into a human-readable
                # compass label (e.g. "SW") for display.
                "model_wind_dir_deg": round(wind_dir_deg, 0),
                "features": feats,
                # Full unnormalized snapshot of what the live forecast said,
                # kept even though only `feats` is used for scoring - once
                # this ages past Open-Meteo's ~3 month live-data window it
                # can never be pulled again (see raw_snapshot's docstring),
                # so future feature engineering needs it logged now.
                "raw": raw_snapshot(raw, idx),
                "weights_version": weights.get("version", 1),
                "verified": False,
            }
            logf.write(json.dumps(record) + "\n")
            results.append(record)

    _log_issuance(raw, results, weights, issued_at_utc, vintage_entry)
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

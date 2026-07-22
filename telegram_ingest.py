"""Telegram backup ingest for manual Silvaplana lake readings.

A phone-side fallback for the kitesailing.ch scraper (kitesailing_weather.py):
when you're at the lake and the scraper is throttled or the site is
unreachable, text the bot what the LiveMeteo widget shows and it is logged
as a real lake observation - into the SAME logs/kitesailing_observations.jsonl
that verify_and_learn.py already reads, so it feeds verification exactly like
a scraped reading (the real spot, no proxy).

Two properties make this robust despite GitHub throttling scheduled jobs:

  * Each reading is stamped with YOUR Telegram message's own send time
    (message.date), NOT the moment CI happens to process it. A reading typed
    at 16:30 stays 16:30 even if the poller only wakes at 19:00.
  * It runs from the EXISTING kitesailing-sampler workflow (no new cron):
    each sampler run drains the Telegram inbox via getUpdates, using a
    committed offset (logs/telegram_offset.json) so nothing is processed
    twice.

Safety:
  * Only messages from the configured TELEGRAM_CHAT_ID are accepted - nobody
    else can inject ground truth into the training labels.
  * Every reading is plausibility-checked before logging; an implausible
    value is rejected with an explanatory reply instead of poisoning the
    label set.
  * Each logged reading is tagged `source: "telegram_manual"` so it is
    auditable and removable, and the bot echoes the full parsed reading back
    so a typo is caught immediately.

Command: `/lake` followed by the values, e.g.
    /lake 5                         (bare number = mean wind km/h)
    /lake mean=5 gust=16 dir=90 temp=18.1 hum=43 pres=1016.2
Keys are forgiving (mean/avg/wind, gust/spitze/boe, dir/direction,
temp, hum/humidity, pres/pressure), order-independent, German or English.
Mean wind is required; gust defaults to the mean if omitted. `/help` prints
usage. Non-command chatter is ignored.

Pure-stdlib (urllib, no requests) so the sampler workflow needs no extra
dependency. The two network calls (getUpdates / sendMessage) are injectable
for offline tests.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from kitesailing_weather import LOG_PATH, is_in_priority_window

OFFSET_PATH = os.path.join(os.path.dirname(__file__), "logs", "telegram_offset.json")
DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "docs", "dashboard_data.json")
API_BASE = "https://api.telegram.org/bot{token}/{method}"

KT_PER_KMH = 1 / 1.852

# forgiving key aliases (English + German) -> canonical observation field
_LAKE_KEYS = {
    "mean": "avg_wind_kmh", "avg": "avg_wind_kmh", "wind": "avg_wind_kmh",
    "mittelwind": "avg_wind_kmh",
    "gust": "gust_kmh", "spitze": "gust_kmh", "windspitzen": "gust_kmh",
    "boe": "gust_kmh", "gusts": "gust_kmh",
    "dir": "wind_dir_deg", "direction": "wind_dir_deg", "windrichtung": "wind_dir_deg",
    "temp": "temp_c", "temperature": "temp_c",
    "hum": "humidity_pct", "humidity": "humidity_pct", "feuchtigkeit": "humidity_pct",
    "pres": "pressure_hpa", "pressure": "pressure_hpa", "luftdruck": "pressure_hpa",
}

# plausibility bounds; None means "no bound on that side"
_BOUNDS = {
    "avg_wind_kmh": (0.0, 150.0),
    "gust_kmh": (0.0, 250.0),
    "wind_dir_deg": (0.0, 360.0),
    "temp_c": (-40.0, 45.0),
    "humidity_pct": (0.0, 100.0),
    "pressure_hpa": (950.0, 1080.0),   # widget reports reduced (sea-level-ish) pressure
}

_COMPASS_DE = ["N", "NO", "O", "SO", "S", "SW", "W", "NW"]


def _beaufort_kmh(kmh):
    for upper, b in [(1, 0), (6, 1), (12, 2), (20, 3), (29, 4), (39, 5),
                     (50, 6), (62, 7), (75, 8), (89, 9), (103, 10), (118, 11)]:
        if kmh < upper:
            return b
    return 12


def _compass_de(deg):
    return _COMPASS_DE[round(deg / 45.0) % 8]


def parse_lake_command(text):
    """Parse a `/lake ...` body into {canonical_field: float}. Raises
    ValueError with a human-usage message on anything unparseable."""
    body = text.strip()
    # drop the leading /lake (and any @botname suffix)
    body = body.split(None, 1)
    body = body[1] if len(body) > 1 else ""
    fields = {}
    bare_used = False
    for tok in body.replace(",", " ").split():
        if "=" in tok or ":" in tok:
            sep = "=" if "=" in tok else ":"
            k, v = tok.split(sep, 1)
            key = _LAKE_KEYS.get(k.strip().lower())
            if key is None:
                raise ValueError(f"Unknown field '{k}'. Use mean/gust/dir/temp/hum/pres.")
            fields[key] = _to_float(v, k)
        else:
            # a bare number is the mean wind (only the first one)
            if bare_used:
                raise ValueError("Only one bare number allowed (mean wind). "
                                 "Use key=value for the rest, e.g. gust=16.")
            fields["avg_wind_kmh"] = _to_float(tok, "mean")
            bare_used = True
    if "avg_wind_kmh" not in fields:
        raise ValueError("Need at least the mean wind, e.g. `/lake 5` or "
                         "`/lake mean=5 gust=16 dir=90`.")
    return fields


def _to_float(v, label):
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"'{label}' must be a number, got '{v}'.")


def validate(fields):
    """Return a list of human-readable plausibility problems (empty if OK)."""
    problems = []
    for key, val in fields.items():
        lo, hi = _BOUNDS.get(key, (None, None))
        if lo is not None and val < lo:
            problems.append(f"{key}={val} below plausible minimum {lo}")
        if hi is not None and val > hi:
            problems.append(f"{key}={val} above plausible maximum {hi}")
    g, m = fields.get("gust_kmh"), fields.get("avg_wind_kmh")
    if g is not None and m is not None and g < m:
        problems.append(f"gust ({g}) is below mean wind ({m})")
    return problems


def build_observation(fields, msg_dt):
    """Assemble a reading in kitesailing_weather.py's schema, stamped with
    the Telegram message time (msg_dt, tz-aware UTC)."""
    mean = fields["avg_wind_kmh"]
    gust = fields.get("gust_kmh")
    gust_defaulted = gust is None
    if gust_defaulted:
        gust = mean
    deg = fields.get("wind_dir_deg")
    iso = msg_dt.isoformat()
    return {
        "observed_at": iso,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source_observed_at": iso,
        "temp_c": fields.get("temp_c"),
        "gust_kmh": gust,
        "gust_kn": round(gust * KT_PER_KMH, 1),
        "avg_wind_kmh": mean,
        "avg_wind_bft": _beaufort_kmh(mean),
        "wind_dir_compass": _compass_de(deg) if deg is not None else None,
        "wind_dir_deg": deg,
        "humidity_pct": fields.get("humidity_pct"),
        "pressure_hpa": fields.get("pressure_hpa"),
        "in_priority_window": is_in_priority_window(msg_dt),
        "source": "telegram_manual",
        "_gust_defaulted": gust_defaulted,
    }


def _echo(obs):
    parts = [f"mean {obs['avg_wind_kmh']:g} km/h",
             f"gust {obs['gust_kmh']:g}" + (" (=mean, none given)" if obs["_gust_defaulted"] else "")]
    if obs["wind_dir_deg"] is not None:
        parts.append(f"dir {obs['wind_dir_deg']:g}° {obs['wind_dir_compass']}")
    if obs["temp_c"] is not None:
        parts.append(f"{obs['temp_c']:g}°C")
    if obs["humidity_pct"] is not None:
        parts.append(f"hum {obs['humidity_pct']:g}%")
    if obs["pressure_hpa"] is not None:
        parts.append(f"{obs['pressure_hpa']:g} hPa")
    local = datetime.fromisoformat(obs["observed_at"]).astimezone().strftime("%H:%M")
    return ("✅ Logged Silvaplana lake reading (" + " · ".join(parts) +
            f") stamped {obs['observed_at'][:16]}Z. Tagged telegram_manual; feeds verification.")


_USAGE = (
    "\U0001f3c4 Wingcheck\n"
    "Send /report for the latest lake wind and forecast summary.\n\n"
    "Optional manual lake backup:\n"
    "Send what the kitesailing.ch widget shows:\n"
    "  /lake 5                (bare number = mean wind km/h)\n"
    "  /lake mean=5 gust=16 dir=90 temp=18.1 hum=43 pres=1016.2\n"
    "Keys: mean gust dir temp hum pres (order-free, German ok).\n"
    "Mean wind is required; it's stamped with your message time."
)


def build_report(observations_path=LOG_PATH, dashboard_path=DASHBOARD_PATH):
    """Build a compact report from the latest real reading and forecast."""
    latest = None
    try:
        with open(observations_path) as handle:
            for line in handle:
                if line.strip():
                    latest = json.loads(line)
    except (OSError, json.JSONDecodeError):
        pass
    dashboard = {}
    try:
        with open(dashboard_path) as handle:
            dashboard = json.load(handle)
    except (OSError, json.JSONDecodeError):
        pass

    lines = ["🏄 Wingcheck report"]
    if latest:
        observed = latest.get("observed_at", "unknown time")
        wind = latest.get("avg_wind_kmh")
        gust = latest.get("gust_kmh")
        direction = latest.get("wind_dir_compass") or "—"
        temp = latest.get("temp_c")
        wind_text = f"{wind * KT_PER_KMH:.1f} kt" if isinstance(wind, (int, float)) else "—"
        gust_text = f"{gust * KT_PER_KMH:.1f} kt" if isinstance(gust, (int, float)) else "—"
        lines.append(f"Latest lake: {wind_text}, gust {gust_text}, {direction}")
        if isinstance(temp, (int, float)):
            lines[-1] += f", {temp:g}°C"
        lines.append(f"Observed: {observed[:16].replace('T', ' ')} UTC")
    else:
        lines.append("Latest lake: no reading available")

    forecasts = dashboard.get("upcoming_forecast") or []
    if forecasts:
        best = max(forecasts, key=lambda row: row.get("probability", 0) or 0)
        probability = best.get("probability")
        probability_text = f"{probability * 100:.0f}%" if isinstance(probability, (int, float)) else "—"
        when = best.get("target_time") or best.get("datetime") or best.get("timestamp") or "upcoming"
        lines.append(f"Best forecast: {probability_text} at {str(when)[11:16] or when} ({best.get('tier', '—')})")
    else:
        lines.append("Forecast: no upcoming forecast available")
    return "\n".join(lines)


def handle_command(text, msg_dt):
    """Pure command handler. Returns (reply_text, observation_or_None)."""
    t = (text or "").strip()
    low = t.lower()
    if low.startswith("/report"):
        return (build_report(), None)
    if low.startswith("/lake"):
        try:
            fields = parse_lake_command(t)
        except ValueError as e:
            return (f"⚠️ {e}\n\n{_USAGE}", None)
        problems = validate(fields)
        if problems:
            return ("⚠️ Rejected (implausible): " + "; ".join(problems) +
                    "\nNothing logged - re-check and resend.", None)
        obs = build_observation(fields, msg_dt)
        return (_echo(obs), obs)
    if low.startswith("/help") or low.startswith("/start"):
        return (_USAGE, None)
    if low.startswith("/"):
        return (f"Unknown command. \n\n{_USAGE}", None)
    return (None, None)   # ignore ordinary chatter


# ---- persistence + network seams -------------------------------------------

def _load_offset(path=OFFSET_PATH):
    try:
        with open(path) as f:
            return int(json.load(f).get("offset", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def _save_offset(offset, path=OFFSET_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"offset": offset,
                   "updated_at": datetime.now(timezone.utc).isoformat()}, f)


def _append_observation(obs, path=LOG_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obs) + "\n")


def _http_get_updates(token, offset):
    url = API_BASE.format(token=token, method="getUpdates")
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if offset:
        params["offset"] = offset
    url = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read().decode())
    return data.get("result", []) if data.get("ok") else []


def _http_send_message(token, chat_id, text):
    url = API_BASE.format(token=token, method="sendMessage")
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        urllib.request.urlopen(url, data=body, timeout=30).read()
    except Exception as e:   # a reply failure must never lose the logged reading
        print(f"[warn] telegram sendMessage failed: {e}", file=sys.stderr)


def poll(token, chat_id, *, get_updates=_http_get_updates,
         send_message=_http_send_message, append=_append_observation,
         load_offset=_load_offset, save_offset=_save_offset):
    """Drain the Telegram inbox once. Only messages from chat_id are acted
    on. Returns a small summary dict. Best-effort - swallows per-update
    errors so one bad message can't strand the rest."""
    offset = load_offset()
    updates = get_updates(token, offset)
    logged = ignored = rejected = 0
    highest = None
    for u in updates:
        uid = u.get("update_id")
        if uid is not None:
            highest = uid if highest is None else max(highest, uid)
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        if str((msg.get("chat") or {}).get("id")) != str(chat_id):
            ignored += 1
            continue   # unauthorized sender - never trust their data
        try:
            ts = int(msg.get("date"))
            msg_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (TypeError, ValueError):
            msg_dt = datetime.now(timezone.utc)
        reply, obs = handle_command(msg.get("text", ""), msg_dt)
        if obs is not None:
            clean = {k: v for k, v in obs.items() if k != "_gust_defaulted"}
            append(clean)
            logged += 1
        elif reply is not None and (msg.get("text", "").strip().lower().startswith("/lake")):
            rejected += 1
        if reply:
            send_message(token, chat_id, reply)
    if highest is not None:
        save_offset(highest + 1)
    return {"updates": len(updates), "logged": logged,
            "rejected": rejected, "ignored": ignored}


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        # No secrets configured - this is a best-effort backup, so exit
        # cleanly rather than failing the sampler job.
        print("[skip] telegram_ingest: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
        return
    try:
        summary = poll(token, chat_id)
        print(f"[telegram_ingest] {summary}")
    except Exception as e:   # a backup path must never break the sampler
        print(f"[warn] telegram_ingest failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

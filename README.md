# Malojawind — Silvaplana wingfoil forecast

Self-improving forecast for the Maloja wind at Lake Silvaplana.
Scores 20 engineered features (Bregaglia thermal contrast, Lugano–Zürich
pressure gradient, 700hPa wind, CAPE, a multi-model wind ensemble, and more)
from 20+ raw data points, sends Telegram alerts, verifies itself against
MeteoSwiss's real Samedan station, and retrains its weights nightly.

## Setup (once, ~15 minutes)

### 1. Create the repo
Create a **private** GitHub repository and upload this entire folder,
preserving the structure (especially `.github/workflows/wingcheck.yml`).

### 2. Telegram bot
1. In Telegram, message **@BotFather** → `/newbot` → follow prompts → copy the token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and
   copy the number in `"chat":{"id": ...}`.

### 3. Repo configuration
- **Settings → Secrets and variables → Actions → New repository secret**:
  add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- **Settings → Actions → General → Workflow permissions**:
  select **"Read and write permissions"** (jobs commit logs/weights back).
- **Settings → Pages**: Source "Deploy from a branch", branch `main`,
  folder `/docs`. Dashboard will be at `https://<user>.github.io/<repo>/`.

### 4. Pre-train on history (run once)
**Actions → wingfoil-check → Run workflow → tick "run_backtest" → Run.**
This pulls May–Oct 2024/2025/2026 weather + real Samedan observations,
trains the model, evaluates on a 2026 holdout, and generates the real
dashboard data (replacing the sample data shipped in `docs/`).
Takes a few minutes. Check the job log for the holdout accuracy —
compare it against the printed "trivial baseline" to judge if the model
is genuinely adding signal.

### 5. Done
From here it runs itself:
- **07:00 & 10:00 CEST** — forecast + Telegram alert, predictions logged
- **20:00 CEST** — verifies past predictions against real Samedan wind,
  updates the model weights, refreshes the dashboard

## Tuning

- **`meteoswiss.py → SAM_PROXY_KT`** (default 8.0): the main calibration
  knob. Samedan wind understates lake wind. After a few real sessions:
  model misses days you rode → lower it; flags dead days → raise it.
  After changing it, re-run the backtest to retrain with the new labels.
- **`forecast_and_log.py → tier_from_prob`**: probability cutoffs for
  GOOD (0.65) / MARGINAL (0.40) alerts. Raise for fewer, surer alerts.
- **`weights.json`**: the model itself. Never edit while jobs are running;
  re-running the backtest resets and retrains it from scratch.

## Files

| File | Role |
|---|---|
| `features.py` | Fetches 20 raw data points, engineers 9 signals |
| `model.py` | Logistic scorer + online learning step |
| `meteoswiss.py` | Real Samedan station data (ground truth) |
| `forecast_and_log.py` | Daily forecast + Telegram + prediction log |
| `verify_and_learn.py` | Checks predictions vs reality, updates weights |
| `backtest.py` | One-shot historical training (2024–2026) |
| `refresh_dashboard.py` | Nightly dashboard data rebuild |
| `weights.json` | Current model weights (auto-updated) |
| `docs/` | Dashboard (GitHub Pages) |
| `logs/` | Prediction log + backtest dataset (auto-committed) |

## Known limitations

- Ground truth is Samedan, ~10 km down-valley — a proxy for the lake, not
  the lake itself.
- Backtest features come from 0-hour archive data; live forecasts carry
  1–3 day lead-time error. Expect live accuracy below backtest accuracy —
  that's why the dashboard reports them separately.
- The 2026 holdout is thin (partial season); trust the live accuracy
  number as it accumulates over the backtest one.

Data: Open-Meteo (CC BY 4.0) · MeteoSwiss Open Data (Source: MeteoSwiss)

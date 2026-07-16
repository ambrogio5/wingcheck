# Malojawind — Silvaplana wingfoil forecast

Self-improving forecast for the Maloja wind at Lake Silvaplana.
Scores 22 engineered features (Bregaglia thermal contrast, Lugano–Zürich
pressure gradient, 700hPa wind, CAPE, a multi-model wind ensemble, and more)
from 20+ raw data points, sends Telegram alerts, verifies itself against the
real kitesailing.ch Silvaplana lake reading (MeteoSwiss's Samedan station as
fallback + secondary signal), and retrains its weights nightly.

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
trains two SEPARATE models (see "Evaluation vs. deployment" below), and
generates the real dashboard data (replacing the sample data shipped in
`docs/`). Takes a few minutes. Check the job log for the 2026 holdout
accuracy — compare it against the printed majority-class baseline to
judge if the model is genuinely adding signal.

### 5. Done
From here it runs itself:
- **07:00 & 10:00 CEST** — forecast + Telegram alert, predictions logged
- **every 15 min, 05:00–21:45 CEST (sunrise to sundown)** — scrapes the
  kitesailing.ch Silvaplana reading into `logs/kitesailing_observations.jsonl`
  - well beyond the scored 12:00-18:00 window, so there's a full-day
  record available for future analysis
- **20:00 CEST** — verifies past predictions against the real Silvaplana
  reading (Samedan as fallback), updates the model weights, refreshes the
  dashboard

## Dashboard

`docs/index.html` (GitHub Pages) shows, top to bottom:
- **Today's forecast** — one card per hour from 14:00 to 18:00, each with
  the model's estimated likelihood as a prominent percentage (labeled
  "est. likelihood", never implied to be a guaranteed frequency), the
  alert tier, and forecast wind/gust/direction. The best hour is visually
  highlighted.
- **Daily summary** — best hour, max estimated likelihood, expected peak
  wind, and a one-line recommendation.
- **Live performance** — rolling accuracy/balanced-accuracy/precision/
  recall on verified real forecasts, with a provisional-sample warning
  below n=30.
- **Frozen holdout evaluation** — the honest 2024+2025-trained,
  2026-holdout numbers (see "Evaluation vs. deployment" below), full
  window and the 14:00–18:00 diagnostic prime window side by side, with
  the evaluation's own generation timestamp and a staleness flag once it's
  more than 30 days old. A **"Run fresh evaluation"** button links out to
  this repo's GitHub Actions page (`workflow_dispatch`) where you tick
  `run_backtest` and run it manually — a static GitHub Pages site can't
  trigger a workflow itself without embedding a credential in frontend
  code, which this deliberately never does.
- **Feature ablation** and a collapsed **Technical details** section
  (operational threshold confusion matrices, reproducibility seed, the
  weights list, and the historical charts).

The page is a single dependency-free `index.html` (inline CSS/JS, Chart.js
from a CDN for the two historical charts only) — no build step, no
framework, works fully offline/degraded if the CDN is blocked.

## Continuous integration

Every pull request runs a `validate` job (`.github/workflows/wingcheck.yml`)
that syntax-checks every module and runs `python -m unittest discover -s tests`
— nothing else. It never runs forecasts, scraping, learning, backtests,
Telegram calls, or commits, and never sees the Telegram secrets, so it's
safe to run on any PR. The scheduled/manual jobs (`forecast`, `learn`,
`backtest`) also run the same test suite before doing anything real, so a
regression fails the job instead of silently committing bad output.

## Tuning

- **`verify_and_learn.py → SILVAPLANA_MARGINAL_KT`** (default 10): the real
  lake threshold, applied directly to the kitesailing.ch reading (primary
  ground truth) - no proxy correction needed since it's the actual spot.
- **`meteoswiss.py → SAM_PROXY_KT`** (default 8.0): only used as the
  Samedan-fallback threshold, for hours the kitesailing scrape missed.
  Samedan wind understates lake wind, hence the lower cutoff.
- **`forecast_and_log.py → tier_from_prob`**: probability cutoffs for
  GOOD (0.65) / MARGINAL (0.40) alerts. Raise for fewer, surer alerts.
- **`weights.json`**: the model itself. Never edit while jobs are running;
  re-running the backtest resets and retrains it from scratch.

## How the model is evaluated

`backtest.py` trains **two separate models from scratch**, both built via
`model.new_weights()` (never by loading and partially resetting the
existing `weights.json` — that was a real leakage bug in earlier versions,
see below):

- **Evaluation model** — trained only on 2024+2025, with its alert
  thresholds (MARGINAL/GOOD) calibrated only on 2024+2025. It is scored
  exactly once against the untouched 2026 season and then discarded — it
  is never saved to `weights.json` and never trained further. Its 2026
  numbers are the only honest answer to "how would this do on data it's
  never seen."
- **Deployment model** — trained on all of 2024+2025+2026 and saved to
  `weights.json`. It has seen 2026, so its own accuracy on 2026 is not a
  valid holdout number and isn't reported.

Both `docs/dashboard_data.json`'s `evaluation` and `deployment` sections
reflect this split, and `refresh_dashboard.py` carries them forward
unchanged on every nightly run — only `backtest.py` may (re)write them,
since recomputing `evaluation` with the live, continuously-learning
`weights.json` would quietly turn the holdout into training data.

**Why this matters**: an earlier version of `backtest.py` loaded the
already-trained `weights.json`, reset only the bias, then trained on
2024+2025 and "evaluated" on 2026 — but since `weights.json` was itself
the output of a *previous* retrain that had already folded 2026 into
training, the per-feature weights already carried information about the
holdout. The fix is to build both models from a genuinely blank slate.

**Metrics reported** (`metrics.py`, pure stdlib, no scikit-learn):
accuracy, balanced accuracy, precision, recall, specificity, F1,
majority-class baseline, Brier score, ROC AUC, and PR AUC (average
precision), each for:
- **Hourly, full window (12:00–18:00)** — every scored hour, matching the
  live forecast window exactly.
- **Hourly, prime window (14:00–18:00)** — a same-model diagnostic slice
  (NOT a different training window — `WINDOW_START_HOUR`/`WINDOW_END_HOUR`
  never change) kept because hours 12–13 are easier, more separable
  negatives that measurably help discrimination; see `backtest.py`'s
  docstring for the concrete AUC evidence.
- **Session-level, both windows** — one row per calendar day: the day
  counts as rideable if ANY hour in the window was, and the day's predicted
  probability is the MAX hourly probability in the window (documented in
  `metrics.build_session_samples`) — answers "will there be a session
  today," not "is every hour classified correctly."
- **Operational MARGINAL/GOOD thresholds** — the same metrics at the
  actual calibrated alert cutoffs, not just the raw 0.5 cutoff, so the
  dashboard reflects what a live alert would really perform like.

MARGINAL is calibrated to maximize balanced accuracy; GOOD is the lowest
threshold meeting 0.75 precision with at least 20 predicted-positive
samples (falls back to `marginal + 0.15`, capped at 0.9, if nothing on the
grid clears both bars) — see `metrics.calibrate_marginal_threshold` /
`calibrate_good_threshold`.

A **feature ablation** (`ablation.py`) trains one fresh model per feature
group (majority-class baseline, forecast wind alone, wind+gust+direction,
the pre-2026 "core physical" 8-feature set, the full current model, and
the full model minus each new nowcast feature) on 2024+2025 and scores
each once against the same 2026 holdout. This is a **diagnostic**
comparison, not model selection — picking the best-looking row and
reporting its holdout number as unbiased would itself be a form of
leakage, since the choice among 7 candidates would have used 2026.

**Reproducibility**: `model.train_epochs()` shuffles with a
locally-scoped `random.Random(DEFAULT_TRAIN_SEED)` instance (currently
`20260716`), not the global `random` module — re-running `backtest.py`
against identical cached raw data (`logs/raw_cache/`) reproduces
byte-identical weights and metrics. The seed and epoch count are recorded
in `docs/dashboard_data.json`'s `reproducibility` section.

**Known limitation, unchanged by this fix**: `features.fetch_raw_historical`
pulls Open-Meteo's *archive* API, which is 0-hour data, not a genuine
1–3-day-ahead forecast with real lead-time error. Backtest accuracy is
therefore an upper bound on live accuracy — expect live numbers to run
below it, which is why the dashboard reports them separately (see
`live_metrics` vs. `evaluation`).

Run `python -m unittest discover -s tests` to exercise the model schema,
metric, calibration, and ablation logic offline (stdlib-only, no network
calls) — see `tests/`.

## Files

| File | Role |
|---|---|
| `features.py` | Fetches 20+ raw data points, engineers 22 signals (`FEATURE_NAMES` is the single source of truth for the schema) |
| `model.py` | Logistic scorer, online learning step, `new_weights()`/`validate_schema()`/`train_epochs()` |
| `metrics.py` | Reusable, dependency-free metric helpers (accuracy, AUC, PR-AUC, session aggregation, threshold calibration) |
| `ablation.py` | Feature-group ablation comparison (diagnostic, not model selection) |
| `meteoswiss.py` | Real Samedan wind + Lugano/Zürich pressure (fallback ground truth + nowcast features) |
| `kitesailing_weather.py` | Scrapes the real Silvaplana lake reading (primary ground truth) |
| `forecast_and_log.py` | Daily forecast + Telegram + prediction log |
| `verify_and_learn.py` | Checks predictions vs reality, updates weights |
| `backtest.py` | One-shot historical retrain (2024–2026, Samedan-labeled) — leak-free evaluation/deployment split, see above |
| `historical_cache.py` | Caches backtest.py's raw fetches so retrains don't re-pull the same history |
| `refresh_dashboard.py` | Nightly dashboard data rebuild (never recomputes the frozen `evaluation`/`deployment` sections) |
| `tests/` | Offline `unittest` suite (stdlib-only, no network) |
| `weights.json` | Current DEPLOYMENT model weights (auto-updated live, replaced wholesale by each `backtest.py` run) |
| `docs/` | Dashboard (GitHub Pages) |
| `logs/` | Prediction log, backtest dataset, kitesailing observations, raw data cache (auto-committed) |

## Known limitations

- `kitesailing_weather.py` has no historical archive - only live data going
  forward from whenever scraping started. `backtest.py`'s historical retrain
  can therefore only train on Samedan-labeled data, while the live loop's
  online updates are labeled against the real lake reading - a real
  labeling-criterion mismatch between the two until enough kitesailing
  history accumulates to backtest against directly.
- Backtest features come from 0-hour archive data; live forecasts carry
  1–3 day lead-time error. Expect live accuracy below backtest accuracy —
  that's why the dashboard reports them separately.
- The 2026 holdout is thin (partial season); trust the live accuracy
  number as it accumulates over the backtest one.
- The evaluation model's 2026 holdout accuracy (~69% at the 0.5 cutoff,
  see the dashboard for the current run's exact figure) is close to but
  not dramatically above the majority-class baseline — most of the 22
  features add little over the raw forecast wind alone (see the feature
  ablation table). ~31% of hours land within ±2kt of the rideable
  threshold, an inherent noise floor no amount of feature/model tuning
  removes on its own; see CLAUDE.md's "Accuracy ceiling" note.

Data: Open-Meteo (CC BY 4.0) · MeteoSwiss Open Data (Source: MeteoSwiss)

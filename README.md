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
- **07:00 & 10:00 CEST** — forecast + Telegram alert, predictions logged,
  **dashboard refreshed immediately** so today's/tomorrow's forecast is
  visible on the published page right after each run (fixed 2026-07-16 -
  previously the dashboard only refreshed at 20:00, so a forecast logged
  at 07:00 or 10:00 could sit invisible on the page for hours)
- **every 15 min, 05:00–21:45 CEST (sunrise to sundown)** — scrapes the
  kitesailing.ch Silvaplana reading into `logs/kitesailing_observations.jsonl`
  - well beyond the scored 12:00-18:00 window, so there's a full-day
  record available for future analysis
- **20:00 CEST** — verifies past predictions against the real Silvaplana
  reading (Samedan as fallback), updates the model weights, refreshes the
  dashboard again (this is when `live_metrics` picks up newly-verified
  outcomes)

## Dashboard

`docs/index.html` (GitHub Pages) shows, top to bottom:
- **Today &amp; tomorrow** — two independent day-blocks, each with one card
  per hour from 14:00 to 18:00 (Europe/Zurich), the model's estimated
  likelihood as a prominent percentage (labeled "est. likelihood", never
  implied to be a guaranteed frequency), the alert tier, and forecast
  wind/gust/direction. Each day's best hour is highlighted independently.
  "Today" only shows hours that haven't happened yet; once today's window
  is over, it shows a clear "window is complete" message while "Tomorrow"
  keeps showing its full set of hours - the two are computed and rendered
  completely independently (`docs/dashboard-logic.js`'s `getForecastByDay`),
  so tomorrow is never hidden just because today still has data logged.
  "Today"/"tomorrow" are determined from Europe/Zurich calendar dates, not
  the viewer's own device timezone - this replaced an earlier version that
  picked "the earliest date with any forecast data" as a stand-in for
  today, which silently hid tomorrow's forecast for as long as even one
  hour of today was still logged (fixed 2026-07-16).
- **Daily summary** — best hour, max estimated likelihood, expected peak
  wind, and a one-line recommendation, shown separately for today and
  tomorrow.
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
from a CDN for the two historical charts only) plus one small plain-JS
helper file, `docs/dashboard-logic.js` (Europe/Zurich date math and the
today/tomorrow grouping logic - split out so it's directly testable with
Node, see "Tests" below) — no build step, no framework, works fully
offline/degraded if the CDN is blocked. The dashboard data fetch bypasses
HTTP caching (`cache: 'no-store'` plus a `?v=<timestamp>` query string),
since the forecast job now updates `dashboard_data.json` multiple times a
day and a cached copy would otherwise show stale forecasts.

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
calls) — see `tests/`. A handful of tests (`tests/test_dashboard_logic.py`)
exercise `docs/dashboard-logic.js` directly via Node (present by default
on GitHub Actions `ubuntu-latest` runners) rather than reimplementing its
date-handling logic in Python; they skip cleanly if `node` isn't on PATH,
so the core Python suite never depends on it.

## Historical data archive and station research

Beyond the operational pipeline above, `logs/historical/` holds a durable,
provenance-tracked archive built for long-term retraining and for
evaluating whether additional local weather stations would improve the
forecast. Full detail lives in two dedicated documents:

- **`docs/DATA_ARCHITECTURE.md`** — the archive's directory layout, the
  canonical normalized hourly schema, what's committed vs. regenerable and
  why, the manifest format, data-quality validation, forecast-vintage
  archiving, and disaster-recovery/rebuild instructions.
- **`docs/STATION_RESEARCH.md`** — every candidate weather station around
  Silvaplana/Maloja/Upper Engadin/Bregaglia that's been investigated (not
  just the ones that panned out), each with an honest verification status
  and rejection reasons where applicable.

In short:
```bash
python historical_data.py sync              # incrementally refresh the station archive (idempotent)
python historical_data.py list-stations     # see every registered station and its status
python historical_data.py coverage          # per-station record counts / date ranges
python historical_data.py validate          # data-quality + gap/staleness checks
python historical_data.py export-training   # export a combined dataset for research scripts
python station_analysis.py                  # correlation + rolling-origin station-family comparison
python calibration_analysis.py              # Platt/isotonic calibration comparison
python regime_analysis.py                   # weather-regime false-positive breakdown
python continuous_target_analysis.py        # continuous wind-target + daily-session-target research
```

All of the above are **research-only**: none of them ever writes
`weights.json` or the operational `docs/dashboard_data.json` — see
`docs/research.html` (a separate, explicitly-labeled-exploratory dashboard,
fed by `refresh_research_dashboard.py`) for their output. Promoting a
research finding into the production model is a deliberate, human-driven
process — see `feature_candidates.py`'s `PROMOTION_PROCESS` and the
"Feature promotion" note under "Known limitations" below.

**Evaluation-integrity note**: station research inevitably involves
inspecting the 2026 season repeatedly while comparing candidate features —
this makes 2026 no longer a pristine, single-use holdout for that
purpose (though it remains valid for `backtest.py`'s own evaluation/
deployment split, which only ever looks at it once per run). To keep
comparisons honest anyway, `station_analysis.py` and friends use
**rolling-origin (expanding-window) evaluation** — training on 2024,
validating each subsequent month/season in turn — with 2026 reported
separately as a labeled **"reference"** result, not a "holdout": treat it
as one more data point, not the final word, and expect it to keep being
inspected as research continues. See `research_metrics.rolling_origin_splits()`.

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
| `docs/` | Dashboard (GitHub Pages): `index.html` + `dashboard-logic.js` (date-handling helpers) + `dashboard_data.json` |
| `logs/` | Prediction log, backtest dataset, kitesailing observations, raw data cache (auto-committed) |
| `stations.py` | Station metadata registry (id, coordinates, provider, roles, honest verification status) — see `docs/STATION_RESEARCH.md` |
| `historical_data.py` | Durable historical-archive CLI (`sync`/`list-stations`/`coverage`/`validate`/`export-training`) — see `docs/DATA_ARCHITECTURE.md` |
| `forecast_vintages.py` | Archives genuine forecast-model payloads (issue time, target time, lead time) before scoring, gzip-compressed and deduped by checksum |
| `data_quality.py` | Implausible-value/gap/staleness validation for the historical archive — flags, never silently discards |
| `research_metrics.py` | Rolling-origin (expanding-window) splits, day-level bootstrap CIs, Benjamini-Hochberg FDR correction |
| `research_report.py` | Provenance envelope (commit SHA, data checksums, config, warnings) for every research script's saved report |
| `station_analysis.py` | Correlation screen + rolling-origin station-family incremental-value comparison (research-only, never touches `weights.json`) |
| `model_regularized.py` | Research-only L2-regularized logistic regression, isolated from the production model |
| `calibration.py` / `calibration_analysis.py` | Reliability tables, ECE/MCE, Platt scaling, isotonic regression (research-only) |
| `regimes.py` / `regime_analysis.py` | Rule-based weather-regime classification and per-regime false-positive analysis |
| `continuous_target_analysis.py` | Research-only continuous wind-target and daily-session-target modeling |
| `feature_candidates.py` | Explicit candidate-feature promotion registry and the 9-step `PROMOTION_PROCESS` checklist |
| `refresh_research_dashboard.py` | Rebuilds `docs/research/research_data.json` from the latest research reports (never touches the main dashboard) |
| `docs/research.html` | Secondary, explicitly-labeled-exploratory research dashboard — separate from the main operational dashboard |

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
- **Feature promotion is deliberately manual.** No research script — not
  `station_analysis.py`, not any future station-derived feature — can
  auto-add itself to `features.FEATURE_NAMES` or retrain `weights.json`.
  A candidate feature must clear all of `feature_candidates.PROMOTION_PROCESS`
  (coverage validation, rolling-origin validation across multiple folds,
  a calibration check, an operational-reliability check, and manual human
  approval) before a source-code change adds it and a fresh `backtest.py`
  run deploys it. This is intentional friction: the accuracy-ceiling note
  above already shows how easy it is for a feature to look useful on one
  inspection of 2026 and add nothing real (`samedan_morning_score` and
  `pressure_nowcast_score` are themselves now flagged, post-hoc, as
  `validated_unstable` — see `feature_candidates.py`).
- **Most candidate weather stations remain unverified.** The station
  registry (`stations.py`) proposes over 20 candidates around Silvaplana/
  Maloja/Upper Engadin/Bregaglia, but only 3 (Samedan, Lugano, Zürich) have
  been confirmed against a live source and have real historical data in
  this repo. See `docs/STATION_RESEARCH.md` for every candidate's status
  and why — most were blocked by a sandboxed research environment with no
  outbound network access to `data.geo.admin.ch`, not by a negative
  finding about the station itself.

Data: Open-Meteo (CC BY 4.0) · MeteoSwiss Open Data (Source: MeteoSwiss)

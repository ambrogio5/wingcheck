# Malojawind — Silvaplana wingfoil forecast

> **Main live dashboard:** <http://127.0.0.1:8081>. This is the canonical
> Docker-served Wingcheck interface on this Mac, backed by the live local
> collector and scheduler. Do not start a separate static preview server for
> normal use.
>
> **Local continuous service:** see [docs/LOCAL_SERVICE.md](docs/LOCAL_SERVICE.md)
> for the Docker Compose deployment used for frequent collection on a Mac.
> GitHub remains the code/PR/CI workflow; operational data stays outside Git.

Self-improving forecast for the Maloja wind at Lake Silvaplana.
Scores 22 engineered features (Bregaglia thermal contrast, Lugano–Zürich
pressure gradient, 700hPa wind, CAPE, a multi-model wind ensemble, and more)
from 20+ raw data points, sends Telegram alerts, verifies itself against the
real kitesailing.ch Silvaplana lake reading (MeteoSwiss's Samedan station as
fallback + secondary signal), and retrains its weights nightly.

## SIA and auditable ground truth

Segl-Maria (`SIA`, WIGOS 0-20000-0-06779) is a confirmed official MeteoSwiss
station ~4km up-corridor from the Silvaplana lake at the same elevation band -
the principal near-lake historical reference while direct lake coverage is
still sparse. Real verified coverage (from user-supplied official OGD files,
preserved + checksummed under `logs/historical/raw/meteoswiss/sia/`):
2004-2009 synoptic-hours temperature/humidity only (no wind), plus
full-variable 10-minute data (wind, gust, direction, QFE pressure,
precipitation, radiation, sunshine) for 2026-01-01 onward. **There is a real,
open coverage gap from 2010 through 2025** - see
`docs/DATA_ARCHITECTURE.md`'s SIA section. Hourly records are honest
mean-of-10-minute aggregates, flagged as derived.

The live ground-truth priority (`config/ground_truth_policy.json`, v2) is:

1. direct lake measurement (kitesailing.ch scrape, or a `telegram_manual`
   backup reading - see below) - always wins
2. SIA - principal reference when no lake reading exists
3. missing label - never fabricated; **Samedan is context only, no longer a
   default label** (re-enable only inside an explicitly named research
   experiment)

**Getting more lake readings.** The kitesailing.ch scraper is throttled by
GitHub's scheduled-workflow limits, so two manual boosters exist for the
days you're at the spot: a top-of-page **“Sample the lake now”** button on
the dashboard (one-tap deep-link to the sampler workflow's *Run* page), and
a **Telegram backup** — text the bot `/lake mean=5 gust=16 dir=90` (or just
`/lake 5`) and `telegram_ingest.py` logs it as a real lake reading stamped
with your message's own send-time, feeding verification exactly like a
scrape. Only your `TELEGRAM_CHAT_ID` is accepted, readings are
plausibility-checked and echoed back, and each is tagged
`source: telegram_manual` for auditability. It runs from its own
`telegram-ingest.yml` workflow — kept separate from the scraper so that
low-privilege job never holds the Telegram secrets.

Training-data preparation now has explicit, inspectable steps:

```bash
python3 sia_import.py --historical <ogd-smn_sia_t_historical_*.csv> --recent <ogd-smn_sia_t_recent.csv>
python3 ground_truth.py build \
  --source kitesailing=logs/kitesailing_observations.jsonl \
  --source sia=logs/historical/station_hourly/sia.jsonl \
  --source sam=logs/historical/station_hourly/sam.jsonl
python3 station_calibration.py --source-a kitesailing --source-b sia
python3 retraining_dataset.py
python3 model_comparison_sia.py
```

`ground_truth.py` preserves multiple observations per timestamp with full
provenance. SIA's equivalence to the lake target is UNMEASURED (calibration
maturity: insufficient - fewer than 14 independent overlapping days) - its
label confidence is deliberately null, not guessed. None of these scripts
ever writes `weights.json`; production retraining stays a separate,
human-reviewed step gated on real calibration and label coverage.

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
- **Settings → Pages** (optional): Source "Deploy from a branch", branch
  `main`, folder `/docs`. This creates a static remote mirror at
  `https://<user>.github.io/<repo>/`; it is not the primary live dashboard.

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
  reading (SIA as the reference when the lake reading is absent; Samedan
  remains context only), updates the model weights, refreshes the
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

The page has no build step or application framework: `index.html` retains the
data renderers, `docs/dashboard-logic.js` contains Europe/Zurich date math and
today/tomorrow grouping, and `docs/retro-dashboard.css` contains the
late-1970s/1980s instrument-cluster visual system. Three skins are selectable
from the **CLASSIC / TECH / RETRO** toggle (persisted in `localStorage`),
with **CLASSIC** — the original light dashboard — as the default; **TECH** is
the cyan CRT terminal skin and **RETRO** is the amber instrument console. The
retro skin uses centralized smoked-black, amber, bronze and phosphor tokens; a
responsive 12-column console grid; explicit focus/reduced-motion behavior; and
only real values from `dashboard_data.json`. The supplied design-reference
image and prototype metro telemetry are not shipped. Chart.js is loaded
from a CDN for historical charts, while the small plain-JS
helper file `docs/dashboard-logic.js` (Europe/Zurich date math and the
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

## Historical data, station research, and diagnostics

Alongside the operational pipeline, this repo also has a durable
historical station archive and a diagnostic/research layer that evaluates
whether real local weather stations would improve the forecast - kept
completely separate from the production model until a finding clears an
explicit promotion process. Full detail lives in three dedicated
documents:

- **`docs/DATA_ARCHITECTURE.md`** — the archive's directory layout,
  canonical normalized schema, what's committed vs. regenerable, the
  station registry, sync/validate/coverage commands, and disaster
  recovery.
- **`docs/STATION_RESEARCH.md`** — every registered station (confirmed and
  unverified) and its status, plus the explicit feature-promotion
  prohibition for this PR.
- **`docs/MALOJA_DIAGNOSTICS.md`** — the pre-forecast station feature
  layer, the seven fixed diagnostic families (source heating, pass
  activation, summit support, radiation support, pressure support,
  competing flow, data health), and the session-level summary shown on
  the dashboard's "Session outlook" section.

In short:
```bash
python3 historical_data.py sync       # incrementally refresh the station archive (idempotent)
python3 historical_data.py validate   # data-quality + continuity checks
python3 historical_data.py coverage   # per-station record counts / date ranges
python3 station_analysis.py           # ten fixed station-family comparisons + correlation screen + calibration
python3 refresh_research_dashboard.py # rebuild docs/research/research_data.json (no network)
```

**None of the above ever writes `weights.json` or the main
`docs/dashboard_data.json`** - `station_analysis.py` asserts this itself
and `tests/test_station_analysis.py` checks it offline. Only three
stations (Samedan, Lugano, Zürich) are confirmed with real historical
data; every other candidate is honestly marked unverified rather than
assumed to exist - see `docs/STATION_RESEARCH.md`. **2026 is a
repeatedly-inspected reference for this research, not a pristine
holdout** - `station_analysis.py` uses chronological, day-grouped
rolling-origin (expanding-window) evaluation, with 2026 reported
separately and labeled accordingly, so a real trend across multiple years
of folds - not one inspection of 2026 - is what would actually justify a
future promotion.

## Files

| File | Role |
|---|---|
| `features.py` | Fetches 20+ raw data points, engineers 22 signals (`FEATURE_NAMES` is the single source of truth for the schema) |
| `model.py` | Logistic scorer, online learning step, `new_weights()`/`validate_schema()`/`train_epochs()` |
| `metrics.py` | Reusable, dependency-free metric helpers (accuracy, AUC, PR-AUC, session aggregation, threshold calibration) |
| `ablation.py` | Feature-group ablation comparison (diagnostic, not model selection) |
| `meteoswiss.py` | Real Samedan wind + Lugano/Zürich pressure (fallback ground truth + nowcast features) |
| `kitesailing_weather.py` | Scrapes the real Silvaplana lake reading (primary ground truth) |
| `forecast_and_log.py` | Daily forecast + Telegram + prediction log + forecast-vintage archival + issuance provenance record |
| `verify_and_learn.py` | Checks predictions vs reality, updates weights |
| `backtest.py` | One-shot historical retrain (2024–2026, Samedan-labeled) — leak-free evaluation/deployment split, see above |
| `historical_cache.py` | Caches backtest.py's raw fetches so retrains don't re-pull the same history |
| `refresh_dashboard.py` | Nightly dashboard data rebuild (never recomputes the frozen `evaluation`/`deployment` sections) |
| `station_registry.py` | Loads/validates `config/stations.json` (the station registry) — see `docs/STATION_RESEARCH.md` |
| `historical_data.py` | Durable historical-archive CLI (`sync`/`validate`/`coverage`) — see `docs/DATA_ARCHITECTURE.md` |
| `data_quality.py` | Implausible-value/gap/staleness validation for the historical archive — flags, never discards |
| `forecast_vintages.py` | Archives genuine forecast-model payloads before scoring, gzip-compressed, deduped by checksum |
| `station_features.py` | Pre-forecast station feature generation (07:00/10:00 cutoffs, reporting delay) — see `docs/MALOJA_DIAGNOSTICS.md` |
| `maloja_diagnostics.py` | Seven fixed diagnostic families with a small, fixed explanation-key vocabulary |
| `session_forecast.py` | Deterministic session-level summary (onset/peak/decline, confidence rules) |
| `research_metrics.py` | Rolling-origin (expanding-window) splits, day-level bootstrap CIs, Benjamini-Hochberg FDR correction |
| `research_report.py` | Provenance envelope (commit SHA, data checksums, config) for every research report |
| `station_analysis.py` | Ten fixed station-family comparisons + correlation screen + calibration (research-only, never touches `weights.json`) |
| `refresh_research_dashboard.py` | Rebuilds `docs/research/research_data.json` from the latest report (never touches the main dashboard) |
| `docs/research.html` | Secondary, explicitly-labeled-exploratory research dashboard |
| `tests/` | Offline `unittest` suite (stdlib-only, no network) |
| `weights.json` | Current DEPLOYMENT model weights (auto-updated live, replaced wholesale by each `backtest.py` run) |
| `docs/` | Dashboard (GitHub Pages): `index.html` + `dashboard-logic.js` (date-handling helpers) + `dashboard_data.json` |
| `logs/` | Prediction log, backtest dataset, kitesailing observations, raw data cache, forecast issuances (auto-committed) |

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
- **No station-derived feature is promoted to production in this PR, and
  none should be without a real trend across multiple rolling-origin
  folds.** Four of the five new candidate diagnostic families (source
  heating, pass activation, summit support, radiation support) have zero
  real station coverage today - only three stations (Samedan, Lugano,
  Zürich) are confirmed. See `docs/STATION_RESEARCH.md` for every
  candidate's status and `docs/MALOJA_DIAGNOSTICS.md` for how they're
  honestly reported as "missing" rather than fabricated.

Data: Open-Meteo (CC BY 4.0) · MeteoSwiss Open Data (Source: MeteoSwiss)

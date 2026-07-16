# Station research: what's been investigated, and its status

This document lists every weather station candidate investigated for the
Silvaplana wingfoil forecast, with an honest status for each. It is the
human-readable companion to `stations.py` (the machine-readable registry) -
if the two ever disagree, `stations.py` is the source of truth.

**Why this document exists**: Phase 3 of the 2026-07-16 local-station
research project required documenting every candidate station "even
rejected ones, with explicit rejection reasons" - not just the stations
that turned out useful.

## Sandbox network constraint (read this first)

The research session that produced this document ran in an environment
where outbound network access to `data.geo.admin.ch` (MeteoSwiss's open-data
catalog host) was blocked at the gateway/proxy level, confirmed two
independent ways:

- Direct `requests.get()` calls timed out / were rejected by the proxy.
- The `WebFetch` tool returned connection-rejected errors for this domain
  and, as a sanity check, for unrelated domains too (`example.com`,
  Wikipedia, GitHub file pages) - i.e. this wasn't a target-specific block,
  the sandbox simply had no general-purpose fetch capability that session.

Only `WebSearch` (snippet-only, no raw file download) worked. This means:
**no station beyond the 3 this project already had real cached data for
could be verified against a live source during that research pass.**

Every other candidate below is marked `candidate_unconfirmed` (a specific
station code is proposed, from general knowledge of the Swiss/Engadin
station network, but not verified live) or `needs_discovery` (no station
code could be proposed with any confidence at all). **Do not treat either
status as ground truth.** A wrong guessed station code fails loudly (the
MeteoSwiss STAC API 404s / returns no assets) rather than silently
returning wrong data, so attempting a real `historical_data.py sync
--station <id>` against these is safe - but no result should be reported
as a validated finding until that sync has actually returned real data and
the entry's `verification` field has been updated to `confirmed`.

## Confirmed stations (real data already in this repo)

| id | Name | Provider | Role | Evidence |
|---|---|---|---|---|
| `sam` | Samedan | MeteoSwiss | Historical label proxy, morning wind nowcast | 399,181 real hourly records in `logs/raw_cache/samedan_archive.json`; `fu3010h0`/`fu3010h1` columns confirmed live 2026-07-16 |
| `lug` | Lugano | MeteoSwiss | Southern pressure-gradient term | 196,835 real hourly records in `logs/raw_cache/pressure_lug.json`; `pp0qffh0` confirmed live |
| `sma` | Zürich / Fluntern | MeteoSwiss | Northern pressure-gradient term | 196,770 real hourly records in `logs/raw_cache/pressure_sma.json`; `pp0qffh0` confirmed live |

These three are the *only* entries in `stations.py` with
`verification="confirmed"` - enforced by `tests/test_stations.py`.

## Candidate stations (proposed, not verified this session)

### High-altitude / ridge stations (Phase 3 priority list)

| id | Name | Elevation | Verification | Confidence | Note |
|---|---|---|---|---|---|
| `cor` | Corvatsch (summit) | 3315m | candidate_unconfirmed | low | Cable-car-served summit; unclear if a full SwissMetNet member vs. SLF/IMIS-only or ski-operator sensor - exactly what a real sync must answer. |
| `piz_nair` | Piz Nair (St. Moritz) | 3057m | needs_discovery | n/a | No station code could be proposed even with low confidence. May only be SLF/IMIS or ski-operator instrumented. |
| `diavolezza` | Diavolezza | 2973m | needs_discovery | n/a | Likely SLF/IMIS avalanche-network rather than MeteoSwiss - unconfirmed. |
| `bernina_hospiz` | Bernina Hospiz / Pass | 2253m | candidate_unconfirmed | low | Eastern-flow-suppression signal candidate. |
| `julier` | Julier Pass | 2284m | candidate_unconfirmed | low | Pass-gradient candidate. |
| `albula` | Albula Pass | 2312m | needs_discovery | n/a | Pass-gradient candidate. |
| `buffalora` | Buffalora / Ofen Pass | 1970m | candidate_unconfirmed | low | Far from Silvaplana (near the Italian border past Zernez) - doubtful physical relevance, included only because the original task's candidate list named it. Lowest priority of the ridge group to actually pursue. |

### Direct/valley-level Engadin stations

| id | Name | Verification | Note |
|---|---|---|---|
| `st_moritz` | St. Moritz | needs_discovery | May be served by the Samedan station rather than having its own distinct automatic station. |
| `sils` | Sils / Segl | needs_discovery | **Highest-priority target for the next real sync attempt** - the lake immediately upstream of Silvaplana; if it exists with real data it would likely be the single most valuable new station. |
| `bever` | Bever | needs_discovery | Valley context only, no specific rationale beyond general coverage. |
| `zuoz` | Zuoz | needs_discovery | Valley context only. |
| `pontresina` | Pontresina | needs_discovery | Valley context only. |

### Upper Bregaglia / southern source region

| id | Name | Elevation | Verification | Note |
|---|---|---|---|---|
| `vicosoprano` | Vicosoprano | 1067m | needs_discovery | **Important distinction**: these coordinates are already used in `features.py` as an Open-Meteo *forecast-model grid point*, not a real station. No evidence was found that Vicosoprano hosts an actual MeteoSwiss ground station - do not conflate the two. A real Bregaglia ground station, if one exists, would be a materially different and potentially valuable addition (see `bregaglia_real_station_heating` in `feature_candidates.py`). |
| `bondo` | Bondo | 823m | needs_discovery | Bregaglia heating-family candidate. |
| `soglio` | Soglio | 1097m | needs_discovery | Bregaglia heating-family candidate. |
| `castasegna` | Castasegna | 697m | needs_discovery | Lower valley entrance; Bregaglia heating-family candidate. |
| `maloja` | Maloja | 1815m | candidate_unconfirmed | **moderate** | One of Switzerland's oldest continuously operated climate stations (long precipitation/temperature record) - higher confidence than most other candidates that a MeteoSwiss station exists here, but the exact current API station code was not confirmed. |

### Pressure-gradient context (beyond the 2 already in production)

| id | Name | Verification | Confidence | Note |
|---|---|---|---|---|
| `locarno_monti` | Locarno / Monti | candidate_unconfirmed | moderate | Western pressure-gradient / Ticino context. |
| `poschiavo` | Poschiavo | candidate_unconfirmed | low | Eastern pressure-gradient / eastern-flow-suppression context. |
| `davos` | Davos | candidate_unconfirmed | moderate | Well-known long-record Swiss climate station. |
| `chur` | Chur | candidate_unconfirmed | moderate | Northern valley / pressure-gradient context. |

### Italian side (different provider entirely)

| id | Name | Provider | Verification | Note |
|---|---|---|---|---|
| `chiavenna` | Chiavenna (Italy) | ARPA Lombardia | needs_discovery | A completely different provider/API/licensing regime from MeteoSwiss; no adapter exists in this codebase yet. **Lowest-priority candidate to actually implement** given the extra integration cost for a station this far down-valley from the thermal source region, and its licence was not reviewed this session. |

## Rejected candidates

| id | What | Rejection reason |
|---|---|---|
| `ski_resort_webcam_widgets` | Generic ski-resort webcam/weather widgets (Corvatsch-Diavolezza-Lagalb company site, engadin.ch reports page, etc.) | `no_machine_readable_access` - these are presentation layers, typically over either a MeteoSwiss feed or a private sensor with no documented API, no historical archive, and no stable machine-readable access. Per explicit instruction: do not assume a named station exists merely because a webcam or resort page displays weather. |

(`kitesailing_weather.py`'s LiveMeteo widget scrape is a related but
distinct case - see its own docstring; it was investigated and *is* used,
specifically because its DOM was directly inspected and confirmed
server-side-rendered with no stable API alternative, unlike the rejected
entries above which were never individually inspected this deeply.)

## Providers considered but not yet integrated

- **SLF/IMIS** (avalanche-warning network): likely operator for several of
  the `needs_discovery` summit stations above (Piz Nair, Diavolezza). No
  adapter exists yet; would need its own module, comparable to
  `meteoswiss.py`, if pursued.
- **ARPA Lombardia / ARPA Piemonte / Sondrio open data**: relevant for the
  Italian side of Bregaglia (Chiavenna and points south). Different API,
  different licence regime, not reviewed this session.
- **MeteoGroup / cantonal feeds**: no specific candidate identified; kept
  as an open avenue for a future research pass.

## What would change a candidate's status

1. Run `python historical_data.py sync --station <id>` (or `sync` with no
   argument to attempt all registered stations). A successful fetch with
   real records is the only thing that can move a station from
   `candidate_unconfirmed`/`needs_discovery` to `confirmed` - this must be
   done by hand-updating `stations.py`'s entry after inspecting the actual
   returned data, not automatically.
2. Run `python historical_data.py coverage` to see how much history came
   back, and `python historical_data.py validate` for data-quality checks.
3. Only once a station is `confirmed` with meaningful coverage should
   `station_analysis.py` be extended to test features derived from it -
   see `feature_candidates.py`'s `PROMOTION_PROCESS`.

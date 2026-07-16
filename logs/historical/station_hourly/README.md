# `station_hourly/`

Normalized canonical hourly records, one JSONL file per station
(`<station_id>.jsonl`), produced by `historical_data.py sync`.

**These `.jsonl` files are intentionally git-ignored** (see the repo's
`.gitignore`), not committed - see `docs/DATA_ARCHITECTURE.md`'s
"Repository size" section for the full reasoning. In short: the canonical
schema repeats several descriptive fields (station name, provider,
coordinates, elevation) on every single hourly row for self-describing
portability, which is roughly 10x more verbose per record than the
compact `{timestamp: {value, value}}` shape `logs/raw_cache/*.json`
already uses (which IS committed). Duplicating 40+ years of Samedan
history in both shapes would add well over 1GB to the repository for data
that's fully regenerable in seconds, offline, from what's already
committed.

**To regenerate this directory locally:**

```bash
python3 historical_data.py sync
```

This ingests `logs/raw_cache/*.json` (already committed, already fetched)
for the three confirmed stations (`sam`, `lug`, `sma`) with zero network
calls required, and attempts a live fetch for every other registered
station (best-effort - most will report "no data available" in any
environment without real network access to `data.geo.admin.ch`).

Manifests (`../manifests/stations.json`, `../manifests/assets.jsonl`) ARE
committed - they're small (tens of KB) and are what `coverage`/`validate`/
research tooling actually need to reason about the archive without
requiring the full regenerated files to be present.

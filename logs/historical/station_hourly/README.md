Gitignored (`*.jsonl`, see ../../../.gitignore) - normalized per-station
hourly observations, one row per NORMALIZED_FIELDS record
(historical_data.py). Regenerates in seconds from the committed
logs/raw_cache/ + manifests via `python3 historical_data.py sync`; never
committed since it's ~10x more verbose per row than raw_cache's compact
format and offers nothing raw_cache doesn't already have.

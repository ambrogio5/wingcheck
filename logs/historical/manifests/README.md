Committed. `stations.json` is an auto-regenerated coverage snapshot
(per-station record count/date range, rebuilt on every `historical_data.py
sync`/`coverage` call) - distinct from the hand-maintained station
*registry* at `config/stations.json`. `assets.jsonl` is an append-only,
checksum-deduped log of every raw source asset ever ingested.

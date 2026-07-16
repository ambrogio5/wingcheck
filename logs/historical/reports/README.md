# `reports/`

Timestamped, versioned output from the research scripts: `station_analysis.py`,
`calibration_analysis.py`, `regime_analysis.py`, `continuous_target_analysis.py`.

Each report is a JSON file (plus an optional human-readable Markdown
summary) named `<script>_<UTC-timestamp>.json`, and includes, per
`docs/DATA_ARCHITECTURE.md`'s reproducibility requirements: the code
commit SHA (when available), a checksum of the data manifests it read,
its exact configuration, training/validation periods, station coverage,
metrics, warnings, and limitations. Reports are never overwritten -
running a script again writes a new timestamped file, so past research
results stay auditable.

These reports ARE committed to git (unlike `station_hourly/`/`datasets/`)
since they're small, human-authored-adjacent research artifacts, not bulk
derived data.

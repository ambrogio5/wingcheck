#!/bin/sh
set -eu

runtime="${WINGCHECK_RUNTIME_DIR:-/runtime}"
mkdir -p "$runtime/logs" "$runtime/dashboard" "$runtime/status" "$runtime/backups" "$runtime/locks"

# Only one container seeds mutable state. Compose starts several containers in
# parallel; without this guard two first-start copies can nest directories.
if mkdir "$runtime/.seed-lock" 2>/dev/null; then
  if [ ! -e "$runtime/weights.json" ]; then cp /app/weights.json "$runtime/weights.json"; fi
  if [ ! -e "$runtime/dashboard/dashboard_data.json" ]; then cp /app/docs/dashboard_data.json "$runtime/dashboard/dashboard_data.json"; fi
  for item in /app/logs/*; do
    name="$(basename "$item")"
    if [ ! -e "$runtime/logs/$name" ]; then cp -R "$item" "$runtime/logs/$name"; fi
  done
  touch "$runtime/.seeded"
  rmdir "$runtime/.seed-lock"
else
  while [ ! -e "$runtime/.seeded" ]; do sleep 1; done
fi

rm -rf /app/logs
ln -s "$runtime/logs" /app/logs
ln -sf "$runtime/weights.json" /app/weights.json
ln -sf "$runtime/dashboard/dashboard_data.json" /app/docs/dashboard_data.json

exec "$@"

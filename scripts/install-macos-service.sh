#!/bin/sh
set -eu

repo="$(cd "$(dirname "$0")/.." && pwd)"
label="com.wingcheck.local"
plist="$HOME/Library/LaunchAgents/$label.plist"
chmod +x "$repo/scripts/start-wingcheck.sh"
mkdir -p "$HOME/Library/LaunchAgents"

sed "s|__REPO__|$repo|g" "$repo/scripts/$label.plist.template" > "$plist"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl enable "gui/$(id -u)/$label"
echo "Wingcheck automatic startup installed: $plist"

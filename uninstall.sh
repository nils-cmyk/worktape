#!/bin/bash
# Removes WorkTape.app and its scheduled jobs. Recordings and reports in ~/WorkTape are kept
# unless you pass --delete-recordings.
set -uo pipefail
cd "$(dirname "$0")"
[ -f local.env ] && source local.env
BUNDLE_ID="${BUNDLE_ID:-com.worktape.app}"

for label in "$BUNDLE_ID" "$BUNDLE_ID.daily" "$BUNDLE_ID.live" "$BUNDLE_ID.weekly" "$BUNDLE_ID.monthly"; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
pkill -f "WorkTape.app/Contents/MacOS/WorkTape" 2>/dev/null
rm -rf "$HOME/Applications/WorkTape.app"
tccutil reset ScreenCapture "$BUNDLE_ID" >/dev/null 2>&1
tccutil reset Accessibility "$BUNDLE_ID" >/dev/null 2>&1

if [ "${1:-}" = "--delete-recordings" ]; then
  mv "$HOME/WorkTape" "$HOME/.Trash/WorkTape-$(date +%s)" && echo "Recordings moved to the Trash."
else
  echo "WorkTape removed. Your recordings and reports are still in ~/WorkTape."
fi

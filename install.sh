#!/bin/bash
# WorkTape installer. Builds WorkTape.app on this Mac, installs it to ~/Applications, and schedules
# the daily classifier (07:00) and the weekly (Mon 08:00) and monthly (1st, 08:00) reviews.
# Safe to re-run: it rebuilds and reinstalls, keeping your recordings and settings in ~/WorkTape.
#
# Optional overrides in ./local.env (not shared):
#   BUNDLE_ID=com.you.worktape     app identifier (default com.worktape.app)
#   SIGN_ID="Apple Development: …" signing identity (default: your Apple Development cert if any, else local)
set -euo pipefail
cd "$(dirname "$0")"
[ -f local.env ] && source local.env

BUNDLE_ID="${BUNDLE_ID:-com.worktape.app}"
APP="$HOME/Applications/WorkTape.app"
DATA="$HOME/WorkTape"
UID_="$(id -u)"

say()  { printf "\033[1m%s\033[0m\n" "$*"; }
fail() { printf "\033[31m%s\033[0m\n" "$*"; exit 1; }

# ---- requirements ----
sw_vers -productVersion | awk -F. '$1 < 14 { exit 1 }' || fail "WorkTape needs macOS 14 (Sonoma) or newer."
command -v swiftc >/dev/null || fail "Xcode Command Line Tools are missing. Run: xcode-select --install   then re-run this installer."
FFMPEG="$(command -v ffmpeg || true)"
[ -z "$FFMPEG" ] && for c in /opt/homebrew/bin/ffmpeg /usr/local/bin/ffmpeg; do [ -x "$c" ] && FFMPEG="$c"; done
[ -n "$FFMPEG" ] || fail "ffmpeg is missing. Install Homebrew (https://brew.sh), then run: brew install ffmpeg"
PY=/usr/bin/python3
[ -x "$PY" ] || PY="$(command -v python3)" || fail "python3 is missing (it comes with the Command Line Tools)."
CLAUDE="$(command -v claude || true)"
[ -z "$CLAUDE" ] && [ -x "$HOME/.local/bin/claude" ] && CLAUDE="$HOME/.local/bin/claude"
if [ -z "$CLAUDE" ]; then
  say "Note: Claude Code isn't installed. Recording works, but the daily reports need it:"
  echo "      curl -fsSL https://claude.ai/install.sh | bash   then run 'claude' once to sign in."
fi

# ---- signing: your own Apple Development cert keeps macOS permissions across rebuilds ----
if [ -z "${SIGN_ID:-}" ]; then
  SIGN_ID="$(security find-identity -v -p codesigning 2>/dev/null | sed -n 's/.*"\(Apple Development: [^"]*\)".*/\1/p' | head -1)"
  SIGN_ID="${SIGN_ID:--}"
fi

# ---- build ----
say "Building WorkTape.app…"
rm -rf build && mkdir -p build/WorkTape.app/Contents/MacOS build/WorkTape.app/Contents/Resources build/WorkTape.iconset
swiftc -O -swift-version 5 -o build/WorkTape.app/Contents/MacOS/WorkTape WorkTape.swift \
  -framework Cocoa -framework ScreenCaptureKit -framework WebKit 2> build/swiftc.log \
  || { cat build/swiftc.log; fail "Build failed."; }
swift make-icon.swift build/icon.png >/dev/null
for sz in 16 32 128 256 512; do
  sips -z $sz $sz build/icon.png --out build/WorkTape.iconset/icon_${sz}x${sz}.png >/dev/null
  sips -z $((sz*2)) $((sz*2)) build/icon.png --out build/WorkTape.iconset/icon_${sz}x${sz}@2x.png >/dev/null
done
iconutil -c icns build/WorkTape.iconset -o build/WorkTape.app/Contents/Resources/WorkTape.icns
cat > build/WorkTape.app/Contents/Info.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleName</key><string>WorkTape</string>
  <key>CFBundleExecutable</key><string>WorkTape</string>
  <key>CFBundleIconFile</key><string>WorkTape</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
</dict></plist>
EOF
codesign --force --sign "$SIGN_ID" build/WorkTape.app 2>/dev/null

# ---- install app + scripts ----
mkdir -p "$HOME/Applications" "$HOME/Library/LaunchAgents" "$DATA/bin" "$DATA/logs"
launchctl bootout "gui/$UID_/$BUNDLE_ID" 2>/dev/null || true
pkill -f "WorkTape.app/Contents/MacOS/WorkTape" 2>/dev/null || true
rm -rf "$APP" && cp -R build/WorkTape.app "$APP"
cp classify.py watcher.py "$DATA/bin/"

# ---- first run: who the reports are about ----
if [ ! -f "$DATA/classify.json" ] && [ -t 0 ]; then
  say "Two quick questions for the reports (Enter to skip):"
  read -r -p "  Your first name: " NAME || NAME=""
  read -r -p "  Your work in one line (e.g. 'runs a design studio'): " ABOUT || ABOUT=""
  read -r -p "  Clients or projects, comma-separated (optional): " CLIENTS || CLIENTS=""
  NAME="$NAME" ABOUT="$ABOUT" CLIENTS="$CLIENTS" "$PY" - <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.path.expanduser("~/WorkTape/bin"))
import classify as C
cfg = dict(C.DEFAULTS)
cfg.update(name=os.environ["NAME"].strip(), about=os.environ["ABOUT"].strip(),
           clients=[c.strip() for c in os.environ["CLIENTS"].split(",") if c.strip()])
C.ROOT.mkdir(parents=True, exist_ok=True)
C.CONFIG.write_text(json.dumps(cfg, indent=2))
PYEOF
fi

# ---- launchd: recorder at login, classifier daily, watchers weekly/monthly ----
PATHS="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
agent() {  # label, schedule-dict (empty = run at login and keep alive), program args...
  local label="$1" when="$2"; shift 2
  local plist="$HOME/Library/LaunchAgents/$label.plist" args=""
  for a in "$@"; do args="$args<string>$a</string>"; done
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo "<plist version=\"1.0\"><dict><key>Label</key><string>$label</string>"
    echo "<key>ProgramArguments</key><array>$args</array>"
    if [ -z "$when" ]; then
      echo "<key>RunAtLoad</key><true/><key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict><key>ProcessType</key><string>Interactive</string>"
    else
      echo "<key>StartCalendarInterval</key><dict>$when</dict>"
      echo "<key>StandardOutPath</key><string>$DATA/logs/${label##*.}.log</string><key>StandardErrorPath</key><string>$DATA/logs/${label##*.}.log</string>"
    fi
    echo "<key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATHS</string></dict></dict></plist>"
  } > "$plist"
  launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_" "$plist"
}
agent "$BUNDLE_ID"         ""  "$APP/Contents/MacOS/WorkTape"
agent "$BUNDLE_ID.daily"   "<key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer>" "$PY" "$DATA/bin/classify.py" --catch-up
agent "$BUNDLE_ID.weekly"  "<key>Weekday</key><integer>1</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer>" "$PY" "$DATA/bin/watcher.py" weekly
agent "$BUNDLE_ID.monthly" "<key>Day</key><integer>1</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer>" "$PY" "$DATA/bin/watcher.py" monthly

say "WorkTape is installed and running (menu bar: ● recording, ○ idle)."
if [ "$SIGN_ID" = "-" ]; then
  echo "Signed locally. After a future reinstall macOS may ask for Screen Recording again."
fi
cat <<EOF

Next:
  1. macOS asks for Screen Recording: allow WorkTape in System Settings → Privacy & Security → Screen Recording,
     then restart it:  launchctl kickstart -k gui/$UID_/$BUNDLE_ID
  2. Choose what gets recorded in ~/WorkTape/config.json (default: Claude, Claude Code terminals, and all
     windows of Chrome, Brave, Arc, Edge and Safari).
  3. The first report appears in the WorkTape window after 07:00 tomorrow.
EOF

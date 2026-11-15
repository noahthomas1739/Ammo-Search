#!/bin/bash
# Installs the ammo monitor on this Mac. It runs:
#   - when you log in,
#   - every 6 hours while the Mac is awake,
#   - right after the Mac wakes up if a check was missed while it slept or was off.
# Safe to run again (e.g. to change your email settings, delete alerts.env first).
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/AmmoFinder"
LABEL="com.ammofinder.monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ENV_FILE="$SUPPORT/alerts.env"

echo "==> Installing from $DIR"
mkdir -p "$SUPPORT" "$HOME/Library/LaunchAgents"

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  echo "Python 3.9+ is needed. Run:  xcode-select --install   then run this script again."
  exit 1
fi

echo "==> Setting up Python packages (one minute)"
python3 -m venv "$SUPPORT/venv"
"$SUPPORT/venv/bin/python" -m pip install -q --upgrade pip
"$SUPPORT/venv/bin/python" -m pip install -q -r "$DIR/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
  echo
  echo "==> Email alerts (Gmail). Use a Google App Password, not your normal password."
  read -r -p "Gmail address: " GMAIL
  read -r -s -p "App Password (typing is hidden): " APPPW; echo
  APPPW="${APPPW// /}"
  ( umask 077; printf 'SMTP_USER=%q\nSMTP_PASSWORD=%q\n' "$GMAIL" "$APPPW" > "$ENV_FILE" )
fi

cat > "$SUPPORT/run.sh" <<EOF
#!/bin/bash
# Started by launchd. Pulls config/code updates from GitHub, then runs the monitor.
LOG="$SUPPORT/monitor.log"
[ -f "\$LOG" ] && [ \$(wc -c < "\$LOG") -gt 5000000 ] && tail -c 1000000 "\$LOG" > "\$LOG.tmp" && mv "\$LOG.tmp" "\$LOG"
echo "----- \$(date) -----"
cd "$DIR" || exit 1
git pull --ff-only -q 2>/dev/null || echo "(git pull skipped)"
"$SUPPORT/venv/bin/python" -m pip install -q -r requirements.txt >/dev/null 2>&1 || true
set -a; . "$ENV_FILE"; set +a
export AMMO_STATE="$SUPPORT/state.json"
exec "$SUPPORT/venv/bin/python" -m ammo_finder "\${1:-auto}"
EOF
chmod 700 "$SUPPORT/run.sh"

echo
echo "==> Sending a test email"
if ! /bin/bash "$SUPPORT/run.sh" test-alert; then
  echo "Test email failed. Check the Gmail address / App Password, then:"
  echo "  rm \"$ENV_FILE\" && bash \"$0\""
  exit 1
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$SUPPORT/run.sh</string><string>auto</string></array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>21600</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$SUPPORT/monitor.log</string>
  <key>StandardErrorPath</key><string>$SUPPORT/monitor.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "Done. The first check is running now in the background."
echo "See what it's doing:   tail -f \"$SUPPORT/monitor.log\""
echo "Run a check right now: launchctl kickstart gui/$(id -u)/$LABEL"
echo "Uninstall:             bash \"$DIR/mac/uninstall.sh\""

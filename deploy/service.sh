#!/usr/bin/env bash
# Run erranderpy in the background, start it at boot, restart it if it crashes.
#   macOS: a launchd agent (~/Library/LaunchAgents), runs while you are logged in
#   Linux: a systemd service (/etc/systemd/system, needs sudo), runs from boot
# Usage: deploy/service.sh install | uninstall | status | logs | print-unit     (make service, ...)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LABEL="com.erranderpy.bot"            # launchd
UNIT="erranderpy"                     # systemd
LOG_DIR="$ROOT/data/logs"

find_uv() {
  command -v uv 2>/dev/null || { [ -x "$HOME/.local/bin/uv" ] && echo "$HOME/.local/bin/uv"; } || {
    echo "✗ uv not found. Run ./start.sh once first." >&2; exit 1; }
}

preflight() {
  if [ ! -f .env ]; then
    echo "✗ No .env yet. Run ./start.sh once in the foreground first (setup and pairing), then Ctrl+C." >&2
    exit 1
  fi
  # Two copies polling the same bot fight over updates (Telegram answers 409 Conflict).
  if pgrep -f "python -m erranderpy$" >/dev/null 2>&1 && ! service_running; then
    echo "✗ The bot is already running in a terminal. Stop it (Ctrl+C) before installing the service." >&2
    exit 1
  fi
}

service_running() {
  case "$(uname -s)" in
    Darwin) launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 ;;
    Linux) systemctl is-active --quiet "$UNIT" 2>/dev/null ;;
    *) return 1 ;;
  esac
}

# --- macOS ---------------------------------------------------------------------------
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mac_install() {
  local uv; uv="$(find_uv)"
  mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$uv</string><string>run</string><string>--quiet</string><string>python</string><string>-m</string><string>erranderpy</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(dirname "$uv"):/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <!-- Start at login; restart if it exits with an error (not after a clean stop). -->
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/bot.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/bot.log</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  # bootout returns before the old job is gone; bootstrapping too early fails
  # (the reinstall left the bot stopped once). Wait up to ~10 s for it to go.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break
    sleep 1
  done
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "✓ Installed: $PLIST"
  echo "  It starts when you log in and restarts after a crash. While the Mac sleeps, the bot sleeps too;"
  echo "  messages sent meanwhile are answered when it wakes up (Telegram keeps them for 24 hours)."
  echo "  Logs: make logs"
}

mac_uninstall() {
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✓ Removed the launchd agent. Start the bot by hand again with ./start.sh"
}

mac_status() {
  if launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "^\s+(state|pid|last exit code) =" ; then :; else
    echo "Not installed (make service)"; fi
}

# --- Linux ---------------------------------------------------------------------------
UNIT_FILE="/etc/systemd/system/$UNIT.service"

linux_unit() {
  local uv; uv="$(find_uv)"
  cat <<UNITFILE
[Unit]
Description=erranderpy Telegram assistant
After=network-online.target
Wants=network-online.target
# Stop restarting if it keeps crashing: 5 starts within 5 minutes.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
User=$(id -un)
WorkingDirectory=$ROOT
ExecStart=$uv run --quiet python -m erranderpy
Environment=PYTHONUNBUFFERED=1
# Restart after a crash, not after a clean stop.
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNITFILE
}

linux_install() {
  local tmp; tmp="$(mktemp)"
  linux_unit > "$tmp"
  echo "→ Writing $UNIT_FILE (needs sudo)"
  sudo install -m 644 "$tmp" "$UNIT_FILE"
  rm -f "$tmp"
  sudo systemctl daemon-reload
  sudo systemctl enable --now "$UNIT"
  echo "✓ Installed and started: systemctl status $UNIT"
  echo "  It starts at boot and restarts after a crash. Logs: make logs"
}

linux_uninstall() {
  sudo systemctl disable --now "$UNIT" 2>/dev/null || true
  sudo rm -f "$UNIT_FILE"
  sudo systemctl daemon-reload
  echo "✓ Removed the systemd service. Start the bot by hand again with ./start.sh"
}

# --- dispatch ------------------------------------------------------------------------
os="$(uname -s)"
case "${1:-}" in
  install)
    preflight
    case "$os" in Darwin) mac_install ;; Linux) linux_install ;; *) echo "✗ $os: use Docker (docker compose up -d)"; exit 1 ;; esac ;;
  uninstall)
    case "$os" in Darwin) mac_uninstall ;; Linux) linux_uninstall ;; esac ;;
  status)
    case "$os" in Darwin) mac_status ;; Linux) systemctl status "$UNIT" --no-pager || true ;; esac ;;
  print-unit)  # the systemd unit install would write (CI checks it with systemd-analyze verify)
    linux_unit ;;
  logs)
    case "$os" in
      Darwin) touch "$LOG_DIR/bot.log" 2>/dev/null || { mkdir -p "$LOG_DIR"; touch "$LOG_DIR/bot.log"; }; tail -n 50 -f "$LOG_DIR/bot.log" ;;
      Linux) journalctl -u "$UNIT" -n 50 -f ;;
    esac ;;
  *) echo "Usage: $0 install | uninstall | status | logs | print-unit"; exit 2 ;;
esac

#!/usr/bin/env bash
# Keep CLIProxyAPI running at login as a launchd user agent.
#
# docs/gateway-setup.md step 5 says "use a launchd user agent" but shipped no
# implementation; this is it. Nothing here touches the synced kit: the plist,
# the binary, the config, and the logs are all local to the device.
set -euo pipefail

LABEL="com.router-for-me.cli-proxy-api"
PROXY_DIR="${1:-$HOME/.local/share/cli-proxy-api}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"

usage() {
  cat <<EOF
Usage: install-launchd-macos.sh [PROXY_DIR] [--uninstall]

PROXY_DIR   directory holding cli-proxy-api and config.yaml
            (default: ~/.local/share/cli-proxy-api)
--uninstall  stop the agent and remove the plist
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi

if [[ "${1:-}" == "--uninstall" || "${2:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL"
  exit 0
fi

BIN="$PROXY_DIR/cli-proxy-api"
CONFIG="$PROXY_DIR/config.yaml"
[[ -x "$BIN" ]] || { echo "Not executable: $BIN" >&2; exit 1; }
[[ -f "$CONFIG" ]] || { echo "Missing config: $CONFIG" >&2; exit 1; }
case "$PROXY_DIR" in
  # A synced binary is a cloud placeholder on some boots; launchd would fail
  # silently and the gateway would look "installed" while being unreachable.
  *CloudStorage*|*OneDrive*|*Dropbox*|*"Google Drive"*)
    echo "Refusing to supervise a binary inside a synced folder: $PROXY_DIR" >&2
    exit 1 ;;
esac

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>
    <string>-config</string>
    <string>$CONFIG</string>
  </array>
  <key>WorkingDirectory</key><string>$PROXY_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG_DIR/cli-proxy-api.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/cli-proxy-api.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null

# bootout first so a rerun replaces the running agent instead of colliding on
# the listening port.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true

echo "Installed launch agent: $PLIST"
echo "Log: $LOG_DIR/cli-proxy-api.log"
echo "Status: launchctl print gui/$(id -u)/$LABEL | head -20"

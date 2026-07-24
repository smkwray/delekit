#!/usr/bin/env bash
set -euo pipefail

# Build and install a CLIProxyAPI binary patched to serve Claude models that
# Anthropic already serves but the upstream CLIProxyAPI catalog has not yet
# published — currently claude-opus-5 (Opus 5). Without the patch, ccg dies with
# "502 unknown provider for model claude-opus-5" while ccc (direct) works, because
# the proxy's Claude catalog (embedded models.json + the remote router-for-me feed)
# lags new Anthropic models and the OAuth channel has no config-level model add.
# See docs/known-issues.md. The patch is a no-op once the upstream catalog adds
# the model — at that point delete this script and patches/, and use a stock
# release. To bridge a further new model, add one line to ensureBridgeModels in
# patches/cliproxy-claude-opus-5.patch.
#
# Usage:  bin/build-cliproxy-opus5.sh [PROXY_DIR]
#   PROXY_DIR defaults to ~/.local/share/cli-proxy-api (the delekit macOS path).
# Requires: go, git. Restarts a launchd-managed proxy if one is installed.

KIT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${CLIPROXY_TAG:-v7.2.98}"
PATCH="$KIT_ROOT/patches/cliproxy-claude-opus-5.patch"
PROXY_DIR="${1:-$HOME/.local/share/cli-proxy-api}"
LABEL="com.router-for-me.cli-proxy-api"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

command -v go  >/dev/null 2>&1 || { echo "go toolchain required: https://go.dev/dl/" >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "git required" >&2; exit 1; }
[[ -f "$PATCH" ]] || { echo "patch not found: $PATCH" >&2; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
echo "Cloning CLIProxyAPI $TAG ..."
git clone -q --depth 1 --branch "$TAG" https://github.com/router-for-me/CLIProxyAPI.git "$WORK/src"
echo "Applying $(basename "$PATCH") ..."
git -C "$WORK/src" apply "$PATCH"
echo "Building (go build) ..."
( cd "$WORK/src" && go build -ldflags "-s -w -X main.Version=${TAG}-opus5-bridge" -o cli-proxy-api-patched ./cmd/server )

mkdir -p "$PROXY_DIR"
if [[ -x "$PROXY_DIR/cli-proxy-api" ]]; then
  BAK="$PROXY_DIR/cli-proxy-api.bak-$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "$PROXY_DIR/cli-proxy-api" "$BAK"; echo "Backed up existing binary -> $BAK"
fi

# Stop a launchd-managed instance so the running executable can be replaced,
# then restart it after the swap. On non-launchd hosts, restart the proxy yourself.
running=0
if [[ -f "$PLIST" ]] && launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; then running=1; sleep 2; fi
cp "$WORK/src/cli-proxy-api-patched" "$PROXY_DIR/cli-proxy-api"
chmod +x "$PROXY_DIR/cli-proxy-api"
[[ "$running" -eq 1 ]] && { launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true; }

echo "Installed: $("$PROXY_DIR/cli-proxy-api" -version 2>/dev/null | head -1)"
if [[ "$running" -eq 1 ]]; then
  echo "Proxy restarted via launchd. Verify (needs your CLIENT_KEY):"
  echo "  curl -s http://127.0.0.1:8317/v1/models -H 'Authorization: Bearer <CLIENT_KEY>' | grep claude-opus-5"
else
  echo "No running launchd proxy detected; restart the proxy to load the new binary."
fi

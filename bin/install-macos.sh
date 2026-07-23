#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
BIN_DIR="${HOME}/.local/bin"
DEVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/delekit"
GATEWAY_CLAUDE_HOME="$DEVICE_DIR/claude-profile"
COPY_MODE=0

usage() {
  cat <<EOF
Usage: install-macos.sh [--copy] [--bin-dir PATH]

Default: create symlinks from Claude Code's user directories to this synced kit.
--copy       copy generated files instead (updates then require reinstalling)
--bin-dir    command directory (default: ~/.local/bin)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --copy) COPY_MODE=1; shift ;;
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "python3 is required to render the templates." >&2; exit 1; }
python3 "$KIT_ROOT/tools/render_config.py"
python3 "$KIT_ROOT/tools/verify_kit.py"

mkdir -p "$CLAUDE_HOME/agents" "$CLAUDE_HOME/skills" \
  "$GATEWAY_CLAUDE_HOME/agents" "$GATEWAY_CLAUDE_HOME/skills" \
  "$BIN_DIR" "$DEVICE_DIR"

wire_dir() {
  local source="$1" target="$2"
  if [[ "$COPY_MODE" -eq 1 ]]; then
    local marker="$target/.delekit-copy-source"
    if [[ -e "$target" || -L "$target" ]]; then
      if [[ -d "$target" && -f "$marker" && "$(cat "$marker")" == "$source" ]]; then
        rm -rf "$target"
        cp -R "$source" "$target"
        printf '%s\n' "$source" > "$marker"
        echo "Refreshed copy: $target"
        return
      fi
      echo "Refusing to overwrite unmanaged path: $target" >&2
      echo "Move it aside or remove it deliberately, then rerun." >&2
      exit 1
    fi
    cp -R "$source" "$target"
    printf '%s\n' "$source" > "$marker"
    echo "Installed copy: $target"
    return
  fi
  if [[ -e "$target" || -L "$target" ]]; then
    if [[ -L "$target" && "$(readlink "$target")" == "$source" ]]; then
      echo "Already linked: $target"
      return
    fi
    echo "Refusing to overwrite existing path: $target" >&2
    echo "Move it aside or remove it deliberately, then rerun." >&2
    exit 1
  fi
  ln -s "$source" "$target"
  echo "Installed: $target"
}

wire_file() {
  local source="$1" target="$2"
  if [[ "$COPY_MODE" -eq 1 ]]; then
    # A literal copy would lose the script's relative path to config/models.env.
    # Install a tiny wrapper that keeps the synced kit as the source of truth.
    local temp
    temp="$(mktemp "${TMPDIR:-/tmp}/delekit-wrapper.XXXXXX")"
    printf '#!/usr/bin/env bash\nexec %q "$@"\n' "$source" > "$temp"
    if [[ -e "$target" || -L "$target" ]]; then
      if [[ -f "$target" ]] && cmp -s "$temp" "$target"; then
        rm -f "$temp"
        echo "Already installed: $target"
        return
      fi
      rm -f "$temp"
      echo "Refusing to overwrite existing command: $target" >&2
      exit 1
    fi
    mv "$temp" "$target"
  else
    if [[ -e "$target" || -L "$target" ]]; then
      if [[ -L "$target" && "$(readlink "$target")" == "$source" ]]; then
        echo "Already linked: $target"
        return
      fi
      echo "Refusing to overwrite existing command: $target" >&2
      exit 1
    fi
    ln -s "$source" "$target"
  fi
  chmod +x "$target" 2>/dev/null || true
  echo "Installed: $target"
}

wire_dir "$KIT_ROOT/generated/claude/agents" "$CLAUDE_HOME/agents/delekit"
wire_dir "$KIT_ROOT/generated/claude/skills/orchestrate-delegates" "$CLAUDE_HOME/skills/orchestrate-delegates"
if [[ "$GATEWAY_CLAUDE_HOME" != "$CLAUDE_HOME" ]]; then
  wire_dir "$KIT_ROOT/generated/claude/agents" "$GATEWAY_CLAUDE_HOME/agents/delekit"
  wire_dir "$KIT_ROOT/generated/claude/skills/orchestrate-delegates" "$GATEWAY_CLAUDE_HOME/skills/orchestrate-delegates"
fi
wire_file "$KIT_ROOT/bin/claudex.sh" "$BIN_DIR/claudex"
wire_file "$KIT_ROOT/bin/dairy.sh" "$BIN_DIR/dairy"
wire_file "$KIT_ROOT/bin/herd.sh" "$BIN_DIR/herd"
wire_file "$KIT_ROOT/bin/prune-worktrees.sh" "$BIN_DIR/prune-worktrees"

DEVICE_ENV="$DEVICE_DIR/device.env"
# Carry over a device.env from the pre-rename location so a switchover keeps the
# gateway token without re-entry. No-op on a fresh install (old path absent).
LEGACY_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/delegate-kit/device.env"
if [[ ! -f "$DEVICE_ENV" && -f "$LEGACY_ENV" ]]; then
  cp "$LEGACY_ENV" "$DEVICE_ENV"
  chmod 600 "$DEVICE_ENV" 2>/dev/null || true
  echo "Migrated existing configuration: $LEGACY_ENV -> $DEVICE_ENV"
fi
if [[ ! -f "$DEVICE_ENV" ]]; then
  cp "$KIT_ROOT/config/device.env.example" "$DEVICE_ENV"
  chmod 600 "$DEVICE_ENV" 2>/dev/null || true
  echo "Created local credential template: $DEVICE_ENV"
else
  echo "Kept existing local configuration: $DEVICE_ENV"
fi

cat <<EOF

Installation complete.

Next:
1. Edit $DEVICE_ENV (never put the real key in the synced kit).
2. Merge the appropriate generated/cliproxy YAML fragment into CLIProxyAPI.
3. Restart CLIProxyAPI.
4. Ensure $BIN_DIR is on PATH, then run: claudex
5. Start a new Claude Code session if ~/.claude/agents did not exist before this run.

Optional project setting: merge config/claude-settings.fragment.json to base
native worktrees on your current committed HEAD.
EOF

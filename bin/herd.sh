#!/usr/bin/env bash
# herd: detached, resumable delegate workers (codex/claude). Thin shim onto the
# cross-platform supervisor in tools/delegate_supervisor.py. Runtime state is
# device-local and never synced; see docs/detached-runner.md.
set -euo pipefail

# Resolve through symlinks: this script is normally invoked via a symlink in
# ~/.local/bin, so a bare dirname would resolve KIT_ROOT to that bin's parent
# and look for the supervisor in the wrong tree.
resolve_self() {
  local source="${BASH_SOURCE[0]}"
  while [[ -L "$source" ]]; do
    local dir
    dir="$(cd -P "$(dirname "$source")" && pwd)"
    source="$(readlink "$source")"
    [[ "$source" != /* ]] && source="$dir/$source"
  done
  cd -P "$(dirname "$source")" && pwd
}
HERE="$(resolve_self)"
KIT_ROOT="$(cd "$HERE/.." && pwd)"
PY="${DELEKIT_PYTHON:-python3}"

# Load model IDs and runner defaults from the synced source of truth, unless the
# caller already picked a models file.
if [[ -z "${DELEGATE_MODELS_FILE:-}" && -f "$KIT_ROOT/config/models.env" ]]; then
  export DELEGATE_MODELS_FILE="$KIT_ROOT/config/models.env"
fi

exec "$PY" "$KIT_ROOT/tools/delegate_supervisor.py" "$@"

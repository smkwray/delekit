#!/usr/bin/env bash
# herd: detached, resumable delegate workers (codex/claude). Thin shim onto the
# cross-platform supervisor in tools/delegate_supervisor.py. Runtime state is
# device-local and never synced; see docs/detached-runner.md.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(cd "$HERE/.." && pwd)"
PY="${DELEKIT_PYTHON:-python3}"

# Load model IDs and runner defaults from the synced source of truth, unless the
# caller already picked a models file.
if [[ -z "${DELEGATE_MODELS_FILE:-}" && -f "$KIT_ROOT/config/models.env" ]]; then
  export DELEGATE_MODELS_FILE="$KIT_ROOT/config/models.env"
fi

exec "$PY" "$KIT_ROOT/tools/delegate_supervisor.py" "$@"

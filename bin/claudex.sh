#!/usr/bin/env bash
set -euo pipefail

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

SCRIPT_DIR="$(resolve_self)"
KIT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEVICE_ENV="${DELEGATE_DEVICE_ENV:-${XDG_CONFIG_HOME:-$HOME/.config}/delekit/device.env}"

load_env_file() {
  local file="$1" line key value
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *=* ]] || { echo "Invalid line in $file: $line" >&2; exit 2; }
    key="${line%%=*}"
    value="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "Invalid key in $file: $key" >&2; exit 2; }
    value="${value#\"}"; value="${value%\"}"
    value="${value#\'}"; value="${value%\'}"
    export "$key=$value"
  done < "$file"
}

load_env_file "$DEVICE_ENV"

: "${ANTHROPIC_BASE_URL:?Set ANTHROPIC_BASE_URL in $DEVICE_ENV or the environment}"
if [[ -z "${ANTHROPIC_AUTH_TOKEN:-}" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "Set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY in $DEVICE_ENV or the environment." >&2
  exit 2
fi

# A global override would defeat the per-agent and per-invocation aliases.
unset CLAUDE_CODE_SUBAGENT_MODEL
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY="${CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY:-1}"
export CLAUDE_CODE_ALWAYS_ENABLE_EFFORT="${CLAUDE_CODE_ALWAYS_ENABLE_EFFORT:-1}"
export CLAUDE_CODE_ATTRIBUTION_HEADER="${CLAUDE_CODE_ATTRIBUTION_HEADER:-0}"
export ENABLE_TOOL_SEARCH="${ENABLE_TOOL_SEARCH:-false}"
export DELEKIT_ROOT="$KIT_ROOT"

# Claude Code persists a /model choice into the *global* user settings file, so
# picking a gateway-only alias inside a gateway session leaks it to every later
# launch, including direct-to-Anthropic ones, which then fail with "model may
# not exist". Pinning the parent per launcher makes each one self-consistent
# regardless of what settings.json currently holds. An explicit --model on the
# command line still wins, so `claudex --model fable` keeps working.
parent_args=()
if [[ -n "${DELEGATE_PARENT_MODEL:-}" ]]; then
  user_set_model=0
  for arg in "$@"; do
    case "$arg" in
      --model|-m|--model=*) user_set_model=1; break ;;
    esac
  done
  [[ "$user_set_model" -eq 0 ]] && parent_args=(--model "$DELEGATE_PARENT_MODEL")
fi

exec claude ${parent_args[@]+"${parent_args[@]}"} "$@"

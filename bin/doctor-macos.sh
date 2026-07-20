#!/usr/bin/env bash
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
DEVICE_ENV="${DELEGATE_DEVICE_ENV:-${XDG_CONFIG_HOME:-$HOME/.config}/delegate-kit/device.env}"
FAIL=0

check_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    printf 'OK   %-18s %s\n' "$1" "$(command -v "$1")"
    return 0
  fi
  printf 'MISS %-18s\n' "$1"
  FAIL=1
  return 1
}

load_env_file() {
  local file="$1" line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" != *=* ]]; then echo "BAD  device env line    $line"; FAIL=1; continue; fi
    key="${line%%=*}"; value="${line#*=}"
    key="${key//[[:space:]]/}"
    if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then echo "BAD  device env key     $key"; FAIL=1; continue; fi
    value="${value#\"}"; value="${value%\"}"; value="${value#\'}"; value="${value%\'}"
    export "$key=$value"
  done < "$file"
}

config_value() {
  local key="$1"
  awk -F= -v k="$key" '$1 == k {sub(/^[^=]*=/, ""); print; exit}' "$KIT_ROOT/config/models.env"
}

have_python=0; check_cmd python3 && have_python=1
have_claude=0; check_cmd claude && have_claude=1
if [[ "$have_claude" -eq 1 ]]; then
  claude_version_text="$(claude --version 2>/dev/null || true)"
  claude_version="$(printf '%s' "$claude_version_text" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
  if [[ -z "$claude_version" ]]; then
    echo "WARN claude version     could not parse: $claude_version_text"
  elif awk -v have="$claude_version" -v need="2.1.211" 'BEGIN { split(have,h,"."); split(need,n,"."); for (i=1;i<=3;i++) { if ((h[i]+0)>(n[i]+0)) exit 0; if ((h[i]+0)<(n[i]+0)) exit 1 } exit 0 }'; then
    echo "OK   claude version     $claude_version"
  else
    echo "OLD  claude version     $claude_version (need >= 2.1.211)"
    FAIL=1
  fi
fi
check_cmd git || true
have_curl=0; check_cmd curl && have_curl=1

if [[ "$have_python" -eq 1 ]]; then
  python3 "$KIT_ROOT/tools/verify_kit.py" || FAIL=1
fi

for path in "$CLAUDE_HOME/agents/delegate-kit" "$CLAUDE_HOME/skills/orchestrate-delegates"; do
  if [[ -e "$path" ]]; then echo "OK   wired             $path"; else echo "MISS wired             $path"; FAIL=1; fi
done

if [[ -f "$DEVICE_ENV" ]]; then
  echo "OK   device env        $DEVICE_ENV"
  load_env_file "$DEVICE_ENV"
  if [[ -z "${ANTHROPIC_BASE_URL:-}" ]]; then
    echo "MISS gateway URL       set ANTHROPIC_BASE_URL"
    FAIL=1
  elif [[ "$have_curl" -eq 1 ]]; then
    auth_args=()
    [[ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]] && auth_args=(-H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN")
    [[ -n "${ANTHROPIC_API_KEY:-}" ]] && auth_args=(-H "x-api-key: $ANTHROPIC_API_KEY")
    if [[ "${#auth_args[@]}" -eq 0 ]]; then
      echo "MISS gateway auth      set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY"
      FAIL=1
    else
      models_json="$(mktemp -t delegate-kit-models.XXXXXX)"
      trap 'rm -f "$models_json"' EXIT
      base_url="${ANTHROPIC_BASE_URL%/}"
      if curl -fsS --max-time 5 "${auth_args[@]}" "$base_url/v1/models" > "$models_json" 2>/dev/null; then
        echo "OK   proxy models      $base_url/v1/models"
        for role in DEFAULT FAST DEEP; do
          alias_name="$(config_value "DELEGATE_ALIAS_$role")"
          role_lower="$(printf '%s' "$role" | tr '[:upper:]' '[:lower:]')"
          if [[ -n "$alias_name" && $(grep -Fc "$alias_name" "$models_json") -gt 0 ]]; then
            printf 'OK   %-18s %s\n' "$role_lower alias" "$alias_name"
          else
            printf 'WARN %-18s %s\n' "$role_lower alias" "not found: $alias_name"
          fi
        done
      else
        echo "WARN proxy models      endpoint unreachable with current local credential"
      fi
    fi
  fi
else
  echo "MISS device env        $DEVICE_ENV"
  FAIL=1
fi

exit "$FAIL"

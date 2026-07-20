#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# macOS ships bash 3.2, where `set -e` does NOT abort on a bare failing
# `[[ ... ]]`. Assertions must therefore fail explicitly or they are no-ops.
assert_file() { [[ -f "$1" ]] || { echo "FAIL: expected file $1" >&2; exit 1; }; }
assert_grep() { grep -qF "$1" "$2" || { echo "FAIL: $2 missing: $1" >&2; exit 1; }; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/xdg" CLAUDE_CONFIG_DIR="$TMP/claude" \
  "$ROOT/bin/install-macos.sh" --copy --bin-dir "$TMP/bin" >/dev/null
HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/xdg" CLAUDE_CONFIG_DIR="$TMP/claude" \
  "$ROOT/bin/install-macos.sh" --copy --bin-dir "$TMP/bin" >/dev/null

# Agent filenames come from config/models.env, so assert the rendered set
# rather than one hardcoded name that a rename would silently invalidate.
for agent in "$ROOT"/generated/claude/agents/*.md; do
  assert_file "$TMP/claude/agents/delekit/$(basename "$agent")"
done
assert_file "$TMP/claude/skills/orchestrate-delegates/SKILL.md"
assert_grep "exec $ROOT/bin/claudex.sh" "$TMP/bin/claudex"

mkdir -p "$TMP/fake"
cat > "$TMP/fake/claude" <<'FAKE'
#!/usr/bin/env bash
python3 - "$@" <<'PY'
import json, os, sys
print(json.dumps({
    'args': sys.argv[1:],
    'subagent': os.environ.get('CLAUDE_CODE_SUBAGENT_MODEL'),
    'effort': os.environ.get('CLAUDE_CODE_ALWAYS_ENABLE_EFFORT'),
    'attrib': os.environ.get('CLAUDE_CODE_ATTRIBUTION_HEADER'),
    'tools': os.environ.get('ENABLE_TOOL_SEARCH'),
}))
PY
FAKE
chmod +x "$TMP/fake/claude"
sed -i.bak 's#replace-with-local-proxy-client-key#test-key#' "$TMP/xdg/delekit/device.env"
rm -f "$TMP/xdg/delekit/device.env.bak"
PATH="$TMP/fake:/usr/bin:/bin" HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/xdg" \
  CLAUDE_CODE_SUBAGENT_MODEL=bad "$TMP/bin/claudex" --model opus > "$TMP/launch.json"
python3 - "$TMP/launch.json" <<'PY'
import json, sys
obj=json.load(open(sys.argv[1], encoding='utf-8'))
assert obj['args'] == ['--model', 'opus']
assert obj['subagent'] is None
assert obj['effort'] == '1'
assert obj['attrib'] == '0'
assert obj['tools'] == 'false'
PY
printf 'mac installer and launcher smoke tests passed\n'

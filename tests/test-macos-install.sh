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
for agent in "$ROOT"/generated/claude/agents/*.md; do
  assert_file "$TMP/xdg/delekit/claude-profile/agents/delekit/$(basename "$agent")"
done
assert_file "$TMP/xdg/delekit/claude-profile/skills/orchestrate-delegates/SKILL.md"
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
    'config_dir': os.environ.get('CLAUDE_CONFIG_DIR'),
    'auto_window': os.environ.get('CLAUDE_CODE_AUTO_COMPACT_WINDOW'),
    'max_context': os.environ.get('CLAUDE_CODE_MAX_CONTEXT_TOKENS'),
    'disable_compact': os.environ.get('DISABLE_COMPACT'),
    'api_key': os.environ.get('ANTHROPIC_API_KEY'),
}))
PY
FAKE
chmod +x "$TMP/fake/claude"
sed -i.bak 's#replace-with-local-proxy-client-key#test-key#' "$TMP/xdg/delekit/device.env"
rm -f "$TMP/xdg/delekit/device.env.bak"
PATH="$TMP/fake:/usr/bin:/bin" HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/xdg" \
  CLAUDE_CODE_SUBAGENT_MODEL=bad CLAUDE_CODE_AUTO_COMPACT_WINDOW=123000 \
  CLAUDE_CODE_MAX_CONTEXT_TOKENS=123000 DISABLE_COMPACT=1 \
  "$TMP/bin/claudex" --model opus > "$TMP/launch.json"
python3 - "$TMP/launch.json" <<'PY'
import json, sys
obj=json.load(open(sys.argv[1], encoding='utf-8'))
assert obj['args'] == ['--model', 'opus']
assert obj['subagent'] is None
assert obj['effort'] == '1'
assert obj['attrib'] == '0'
assert obj['tools'] == 'false'
assert obj['config_dir'] is None
assert obj['auto_window'] is None
assert obj['max_context'] is None
assert obj['disable_compact'] is None
PY

PATH="$TMP/fake:/usr/bin:/bin" HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/xdg" \
  DELEKIT_TANDY_CONTEXT_MODE=clientdata-272k ANTHROPIC_API_KEY=must-be-removed \
  "$TMP/bin/claudex" > "$TMP/launch-272k.json"
python3 - "$TMP/launch-272k.json" "$TMP/xdg/delekit/claude-profile/.claude.json" <<'PY'
import json, sys
launch=json.load(open(sys.argv[1], encoding='utf-8'))
state=json.load(open(sys.argv[2], encoding='utf-8'))
assert launch['config_dir'].endswith('/xdg/delekit/claude-profile')
assert launch['api_key'] is None
assert state['clientDataCache']['kelp_forest_sonnet'] == '272000'
assert state['clientDataCache']['rowan_thicket']['claude-sonnet-4-6'] == 272000
assert state['autoCompactWindowsCache']['claude-sonnet-4-6'] == 272000
PY
printf 'mac installer and launcher smoke tests passed\n'

#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/repo"
git -C "$TMP/repo" init -q
git -C "$TMP/repo" config user.name test
git -C "$TMP/repo" config user.email test@example.invalid
# Test repositories must not inherit the owner's global commit-identity hook;
# this suite deliberately uses a synthetic identity and only tests runner
# behavior, not the owner's publication guard.
git -C "$TMP/repo" config core.hooksPath /dev/null
printf 'base\n' > "$TMP/repo/base.txt"
printf '.worktrees/\n' > "$TMP/repo/.gitignore"
git -C "$TMP/repo" add base.txt .gitignore
git -C "$TMP/repo" commit -qm base

# Dry-run must not require Codex or create the state/log directory.
XDG_STATE_HOME="$TMP/dry-state" PATH="/usr/bin:/bin" \
  "$ROOT/bin/dairy.sh" write --backend codex --project-root "$TMP/repo" \
  --prompt 'dry-run smoke' --worktree --dry-run --json > "$TMP/dry.json"
python3 - "$TMP/dry.json" <<'PY'
import json, sys
obj=json.load(open(sys.argv[1], encoding='utf-8'))
assert obj['dry_run'] is True
assert obj['worktree'] is True
assert obj['access'] == 'workspace-write'
assert obj['execution_root'].startswith(obj['project_root'] + '/.worktrees/'), obj
PY
[[ ! -e "$TMP/dry-state" ]]
[[ "$(git -C "$TMP/repo" worktree list --porcelain | grep -c '^worktree ' || true)" -eq 1 ]]

# Fake Codex exercises prompt piping, worktree creation, report capture, status,
# auto-commit, and handoff without using a real model.
mkdir -p "$TMP/fake-bin"
cat > "$TMP/fake-bin/codex" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
cwd=""; report=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cd) cwd="$2"; shift 2 ;;
    -o) report="$2"; shift 2 ;;
    -) cat >/dev/null; shift ;;
    *) shift ;;
  esac
done
[[ -n "$cwd" && -n "$report" ]]
printf 'delegate output\n' > "$cwd/delegate-output.txt"
printf 'fake codex completed\n' > "$report"
FAKE
chmod +x "$TMP/fake-bin/codex"
cat > "$TMP/fake-bin/osascript" <<'FAKE_NOTIFY'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" > "${DELEGATE_NOTIFY_MARKER:?}"
FAKE_NOTIFY
chmod +x "$TMP/fake-bin/osascript"

XDG_STATE_HOME="$TMP/run-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  DELEGATE_NOTIFY_MARKER="$TMP/default-notify" \
  "$ROOT/bin/dairy.sh" write --backend codex --project-root "$TMP/repo" \
  --prompt 'write smoke' --worktree --json > "$TMP/result.json"
[[ ! -e "$TMP/default-notify" ]]

python3 - "$TMP/result.json" <<'PY'
import json, os, sys
obj=json.load(open(sys.argv[1], encoding='utf-8'))
assert obj['status'] == 'completed', obj
assert obj['access'] == 'workspace-write', obj
assert obj['worktree'] and os.path.isdir(obj['worktree']), obj
assert obj['worktree'].startswith(obj['project_root'] + '/.worktrees/'), obj
assert os.path.isfile(os.path.join(obj['worktree'], 'delegate-output.txt')), obj
assert os.path.isfile(obj['report']), obj
assert os.path.isfile(obj['stdout']), obj
assert os.path.isfile(obj['stderr']), obj
text=open(obj['report'], encoding='utf-8').read()
assert 'fake codex completed' in text
assert '## Worktree handoff' in text
print(obj['worktree'])
print(obj['branch'])
PY

WORKTREE="$(python3 -c 'import json; print(json.load(open("'"$TMP/result.json"'"))["worktree"])')"
BRANCH="$(python3 -c 'import json; print(json.load(open("'"$TMP/result.json"'"))["branch"])')"
git -C "$TMP/repo" worktree remove --force "$WORKTREE"
git -C "$TMP/repo" branch -D "$BRANCH" >/dev/null

# Desktop notifications require explicit opt-in.
XDG_STATE_HOME="$TMP/notify-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  DELEGATE_DESKTOP_NOTIFY=1 DELEGATE_NOTIFY_MARKER="$TMP/opt-in-notify" \
  "$ROOT/bin/dairy.sh" read --backend codex --project-root "$TMP/repo" \
  --prompt 'notification smoke' --json > "$TMP/notify.json"
grep -q 'delegate readonly finished' "$TMP/opt-in-notify"

# A backend that exits zero without a usable final report must still fail.
cat > "$TMP/fake-bin/codex" <<'FAKE_EMPTY'
#!/usr/bin/env bash
set -euo pipefail
while [[ $# -gt 0 ]]; do
  case "$1" in
    -) cat >/dev/null; shift ;;
    *) shift ;;
  esac
done
FAKE_EMPTY
chmod +x "$TMP/fake-bin/codex"
set +e
XDG_STATE_HOME="$TMP/empty-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  "$ROOT/bin/dairy.sh" read --backend codex --project-root "$TMP/repo" \
  --prompt 'empty report smoke' --json > "$TMP/empty.json" 2> "$TMP/empty.stderr"
empty_rc=$?
set -e
[[ "$empty_rc" -eq 1 ]]
python3 - "$TMP/empty.json" <<'PY_EMPTY'
import json, os, sys
obj=json.load(open(sys.argv[1], encoding='utf-8'))
assert obj['status'] == 'failed', obj
assert obj['exit_code'] == 1, obj
assert os.path.isfile(obj['report']), obj
assert 'no valid final report' in open(obj['report'], encoding='utf-8').read().lower()
PY_EMPTY

# Pi uses the shared Codex profiles through the ChatGPT-backed provider. Its
# honest read-only mode removes shell/write tools and disables ambient add-ons.
cat > "$TMP/fake-bin/pi" <<'FAKE_PI'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\0' "$@" > "$PI_ARGS"
cat > "$PI_STDIN"
printf 'fake pi completed\n'
FAKE_PI
chmod +x "$TMP/fake-bin/pi"

XDG_STATE_HOME="$TMP/pi-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  PI_ARGS="$TMP/pi.args" PI_STDIN="$TMP/pi.stdin" \
  "$ROOT/bin/dairy.sh" read --backend pi --profile luna --project-root "$TMP/repo" \
  --prompt 'pi smoke' --json > "$TMP/pi.json"

python3 - "$TMP/pi.json" "$TMP/pi.args" "$TMP/pi.stdin" <<'PY_PI'
import json, sys
obj=json.load(open(sys.argv[1], encoding='utf-8'))
assert obj['status'] == 'completed' and obj['backend'] == 'pi', obj
assert obj['profile'] == 'luna' and obj['model'] and obj['access'] == 'read-only', obj
assert 'fake pi completed' in open(obj['report'], encoding='utf-8').read(), obj
args=open(sys.argv[2], 'rb').read().decode('utf-8').split('\x00')[:-1]
for expected in ('--provider', 'openai-codex', '--no-approve', '--no-extensions',
                 '--no-skills', '--no-prompt-templates', '--no-session', '--tools',
                 'read,grep,find,ls', '-p'):
    assert expected in args, (expected, args)
prompt=open(sys.argv[3], encoding='utf-8').read()
assert 'no shell or test execution' in prompt
assert prompt.rstrip().endswith('pi smoke')
PY_PI

# Pi has no write sandbox, so workspace-write fails before logs/worktrees.
set +e
XDG_STATE_HOME="$TMP/pi-ws-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  "$ROOT/bin/dairy.sh" workspace --backend pi --project-root "$TMP/repo" \
  --prompt 'nope' --worktree 2> "$TMP/pi-ws.err"
pi_ws_rc=$?
set -e
[[ "$pi_ws_rc" -eq 2 ]]
grep -q 'no confined workspace-write' "$TMP/pi-ws.err"
[[ ! -e "$TMP/pi-ws-state" ]]
[[ "$(git -C "$TMP/repo" worktree list --porcelain | grep -c '^worktree ' || true)" -eq 1 ]]

# Antigravity (agy) backend: access maps to --mode, the prompt is the value of
# -p (not stdin), and stdout is captured as the report. A fake agy records its
# argv so the mapping is asserted without a real model.
cat > "$TMP/fake-bin/agy" <<'FAKE_AGY'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\0' "$@" > "$AGY_ARGS"   # NUL-delimited: the prompt arg contains newlines
printf 'fake agy completed\n'
FAKE_AGY
chmod +x "$TMP/fake-bin/agy"

XDG_STATE_HOME="$TMP/agy-state" PATH="$TMP/fake-bin:/usr/bin:/bin" AGY_ARGS="$TMP/agy.args" \
  "$ROOT/bin/dairy.sh" read --backend agy --model gemini-3.7-flash-high \
  --project-root "$TMP/repo" --prompt 'agy smoke' --json > "$TMP/agy.json"

python3 - "$TMP/agy.json" "$TMP/agy.args" <<'PY_AGY'
import json, sys
obj=json.load(open(sys.argv[1], encoding='utf-8'))
assert obj['status'] == 'completed', obj
assert obj['backend'] == 'agy', obj
assert obj['access'] == 'read-only', obj
assert 'fake agy completed' in open(obj['report'], encoding='utf-8').read(), obj
args=open(sys.argv[2], 'rb').read().decode('utf-8').split('\x00')[:-1]  # drop trailing empty
i=args.index('--mode'); assert args[i+1] == 'plan', args      # read-only -> --mode plan
assert 'gemini-3.7-flash-high' in args, args                  # explicit --model passed through
assert args[-2] == '-p', args                                 # prompt is the single value of -p
assert args[-1].endswith('agy smoke'), args                   # ...and carries the task text
PY_AGY

# agy profiles use backend-specific names: flash-high is the default and pro-high
# selects the strongest configured model. Legacy Codex profile names canonicalize
# to their agy replacements during migration.
# Dry-run needs no agy on PATH.
XDG_STATE_HOME="$TMP/agy-dry" PATH="/usr/bin:/bin" \
  "$ROOT/bin/dairy.sh" read --backend agy --project-root "$TMP/repo" \
  --prompt 'x' --dry-run --json > "$TMP/agy-flash.json"
XDG_STATE_HOME="$TMP/agy-dry" PATH="/usr/bin:/bin" \
  "$ROOT/bin/dairy.sh" read --backend agy --profile pro-high --project-root "$TMP/repo" \
  --prompt 'x' --dry-run --json > "$TMP/agy-pro.json"
XDG_STATE_HOME="$TMP/agy-dry" PATH="/usr/bin:/bin" \
  "$ROOT/bin/dairy.sh" read --backend agy --profile terra --project-root "$TMP/repo" \
  --prompt 'x' --dry-run --json > "$TMP/agy-legacy.json" 2> "$TMP/agy-legacy.err"
python3 - "$TMP/agy-flash.json" "$TMP/agy-pro.json" "$TMP/agy-legacy.json" <<'PY_AGY_PROF'
import json, sys
flash=json.load(open(sys.argv[1], encoding='utf-8'))
pro=json.load(open(sys.argv[2], encoding='utf-8'))
legacy=json.load(open(sys.argv[3], encoding='utf-8'))
assert flash['profile'] == 'flash-high' and flash['model'] == 'gemini-3.7-flash-high', flash
assert pro['profile'] == 'pro-high' and pro['model'] == 'gemini-3.1-pro-high', pro
assert legacy['profile'] == 'flash-high' and legacy['model'] == 'gemini-3.7-flash-high', legacy
PY_AGY_PROF
grep -q 'Deprecated agy profile terra; use flash-high' "$TMP/agy-legacy.err"

# agy cannot confine writes, so workspace-write must be refused up front (exit 2)
# before any log dir or worktree is created — never silently granted host-wide.
set +e
XDG_STATE_HOME="$TMP/agy-ws-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  "$ROOT/bin/dairy.sh" workspace --backend agy --model x --project-root "$TMP/repo" \
  --prompt 'nope' --worktree 2> "$TMP/agy-ws.err"
ws_rc=$?
set -e
[[ "$ws_rc" -eq 2 ]]
grep -q 'no confined workspace-write' "$TMP/agy-ws.err"
[[ ! -e "$TMP/agy-ws-state" ]]                                                    # failed before log dir
[[ "$(git -C "$TMP/repo" worktree list --porcelain | grep -c '^worktree ' || true)" -eq 1 ]]  # no worktree

# A failed run must NOT commit worktree changes. This used to run regardless of
# exit code, so a backend that modified the worktree and then failed still had
# its partial work committed under a failed status.
cat > "$TMP/fake-bin/failwrite" <<'FAKE_FW'
#!/usr/bin/env bash
set -euo pipefail
# Write into the execution root codex was pointed at, the way a real backend
# would, so the partial work lands in the worktree under test.
# No default: guessing the cwd is exactly how an earlier version of this fixture
# wrote partial.txt into the repository root and got it committed.
target=""
prev=""
for a in "$@"; do
  [[ "$prev" == "--cd" ]] && target="$a"
  prev="$a"
done
[[ -n "$target" ]] || { echo "failwrite: no --cd given; refusing to guess a write target" >&2; exit 9; }
printf 'partial\n' > "$target/partial.txt"
printf 'Execution error\n'
exit 3
FAKE_FW
chmod +x "$TMP/fake-bin/failwrite"
cp "$TMP/fake-bin/failwrite" "$TMP/fake-bin/codex"
set +e
XDG_STATE_HOME="$TMP/fw-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  "$ROOT/bin/dairy.sh" write --backend codex --model m --project-root "$TMP/repo" \
  --prompt 'x' --worktree --dirty-policy ignore --json > "$TMP/fw.json" 2>"$TMP/fw.err"
fw_rc=$?
set -e
[[ "$fw_rc" -ne 0 ]]
python3 - "$TMP/fw.json" <<'PY_FW'
import json, subprocess, sys
obj = json.load(open(sys.argv[1], encoding='utf-8'))
assert obj['status'] == 'failed', obj
wt = obj['worktree']
n = subprocess.run(['git', '-C', wt, 'rev-list', '--count', 'HEAD'],
                   capture_output=True, text=True).stdout.strip()
base = subprocess.run(['git', '-C', wt, 'status', '--porcelain'],
                      capture_output=True, text=True).stdout
assert 'partial.txt' in base, f"failed run must leave work UNCOMMITTED, got: {base!r}"
PY_FW
rm -f "$TMP/fake-bin/codex"

# DELEGATE_<BACKEND>_BIN must be honoured, and must work when NO command of that
# name is on PATH at all -- that is the whole point of an override. It also has
# to be the binary actually executed, not merely validated.
mkdir -p "$TMP/override"
cat > "$TMP/override/not-on-path" <<'FAKE_OV'
#!/usr/bin/env bash
printf 'OVERRIDE-RAN\n'
FAKE_OV
chmod +x "$TMP/override/not-on-path"
XDG_STATE_HOME="$TMP/ov-state" PATH="/usr/bin:/bin" \
  DELEGATE_CODEX_BIN="$TMP/override/not-on-path" \
  "$ROOT/bin/dairy.sh" read --backend codex --model m --project-root "$TMP/repo" \
  --prompt 'x' --json > "$TMP/ov.json"
python3 - "$TMP/ov.json" <<'PY_OV'
import json, sys
obj = json.load(open(sys.argv[1], encoding='utf-8'))
assert obj['status'] == 'completed', obj
assert 'OVERRIDE-RAN' in open(obj['report'], encoding='utf-8').read(), obj
PY_OV

# Opencode. Three properties, each a silent failure mode: the prompt must arrive
# on STDIN (an stdin that never reaches EOF hangs `opencode run` forever),
# read-only must export a DENY-BY-DEFAULT object policy (opencode leaves unlisted
# keys at allow, and the rule-array form is ignored outright, so either mistake
# grants writes to a run labelled read-only), and --auto plus --pure must be sent.
cat > "$TMP/fake-bin/opencode" <<'FAKE_OC'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\0' "$@" > "$OC_ARGS"
printf '%s' "${OPENCODE_PERMISSION-<unset>}" > "$OC_PERM"
printf '%s' "${OPENCODE_DISABLE_PROJECT_CONFIG-<unset>}" > "$OC_FLAGS"
cat > "$OC_STDIN"
printf 'fake opencode completed\n'
FAKE_OC
chmod +x "$TMP/fake-bin/opencode"

XDG_STATE_HOME="$TMP/oc-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  OC_ARGS="$TMP/oc.args" OC_PERM="$TMP/oc.perm" OC_STDIN="$TMP/oc.stdin" OC_FLAGS="$TMP/oc.flags" \
  "$ROOT/bin/dairy.sh" read --backend opencode --project-root "$TMP/repo" \
  --prompt 'opencode smoke' --json > "$TMP/oc.json"

python3 - "$TMP/oc.json" "$TMP/oc.args" "$TMP/oc.perm" "$TMP/oc.stdin" "$TMP/oc.flags" <<'PY_OC'
import json, sys
obj=json.load(open(sys.argv[1], encoding='utf-8'))
assert obj['status'] == 'completed', obj
assert obj['backend'] == 'opencode' and obj['access'] == 'read-only', obj
assert 'fake opencode completed' in open(obj['report'], encoding='utf-8').read(), obj
args=open(sys.argv[2], 'rb').read().decode('utf-8').split('\x00')[:-1]
assert args[0] == '--pure' and args[1] == 'run', args      # --pure is a GLOBAL flag
assert '--format' not in args, args                        # dairy takes plain text; herd takes json
assert '--auto' in args, args                              # clears residual ask states
i=args.index('--dir'); assert args[i+1] == obj['execution_root'], args
perm=json.loads(open(sys.argv[3], encoding='utf-8').read())
assert isinstance(perm, dict), perm                        # array form is ignored: fails OPEN
assert perm.get('*') == 'deny', perm                       # unlisted tools must not default to allow
assert list(perm)[0] == '*', perm                          # catch-all first; last match wins
assert perm.get('glob') == 'allow' and perm.get('grep') == 'allow', perm
read = perm.get('read') or {}
assert read.get('*') == 'allow', perm                      # repo files stay readable
assert read.get('mcp:*') == 'deny', perm                   # MCP resources do not
keys = list(read)
assert keys.index('mcp:*') > keys.index('*'), keys         # last match wins: deny must follow
stdin=open(sys.argv[4], encoding='utf-8').read()
assert 'opencode smoke' in stdin, stdin                    # prompt on stdin, not argv
assert not any('opencode smoke' in a for a in args), args
assert open(sys.argv[5], encoding='utf-8').read() == '1'   # project config disabled
PY_OC

# Full access is the only mode that opens the policy up.
XDG_STATE_HOME="$TMP/oc-full" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  OC_ARGS="$TMP/ocf.args" OC_PERM="$TMP/ocf.perm" OC_STDIN="$TMP/ocf.stdin" OC_FLAGS="$TMP/ocf.flags" \
  "$ROOT/bin/dairy.sh" full --backend opencode --model vendor/anything \
  --project-root "$TMP/repo" --prompt 'x' --json > "$TMP/ocf.json"
python3 - "$TMP/ocf.perm" <<'PY_OCF'
import json, sys
assert json.loads(open(sys.argv[1], encoding='utf-8').read()) == {'*': 'allow'}
PY_OCF

# Opencode's shell writes outside --dir, so workspace-write is refused up front,
# before any log dir or worktree exists.
set +e
XDG_STATE_HOME="$TMP/oc-ws-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  "$ROOT/bin/dairy.sh" workspace --backend opencode --project-root "$TMP/repo" \
  --prompt 'nope' --worktree 2> "$TMP/oc-ws.err"
oc_ws_rc=$?
set -e
[[ "$oc_ws_rc" -eq 2 ]]
grep -q 'no confined workspace-write' "$TMP/oc-ws.err"
[[ ! -e "$TMP/oc-ws-state" ]]
[[ "$(git -C "$TMP/repo" worktree list --porcelain | grep -c '^worktree ' || true)" -eq 1 ]]

# Grok Build: dairy uses the prompt file and plain output, and maps read-only
# to Grok's native sandbox plus a read-only built-in tool allowlist.
cat > "$TMP/fake-bin/grok" <<'FAKE_GROK'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\0' "$@" > "$GROK_ARGS"
prompt_file=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prompt-file) prompt_file="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[[ -n "$prompt_file" && -f "$prompt_file" ]]
cp "$prompt_file" "$GROK_PROMPT"
printf 'fake grok completed\n'
FAKE_GROK
chmod +x "$TMP/fake-bin/grok"

XDG_STATE_HOME="$TMP/grok-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  GROK_ARGS="$TMP/grok.args" GROK_PROMPT="$TMP/grok.prompt" \
  "$ROOT/bin/dairy.sh" read --backend grok --model grok-4.6 --project-root "$TMP/repo" \
  --prompt 'grok smoke' --json > "$TMP/grok.json"

python3 - "$TMP/grok.json" "$TMP/grok.args" "$TMP/grok.prompt" <<'PY_GROK'
import json, sys
obj=json.load(open(sys.argv[1], encoding='utf-8'))
assert obj['status'] == 'completed' and obj['backend'] == 'grok', obj
assert obj['model'] == 'grok-4.6' and obj['access'] == 'read-only', obj
assert 'fake grok completed' in open(obj['report'], encoding='utf-8').read(), obj
args=open(sys.argv[2], 'rb').read().decode('utf-8').split('\x00')[:-1]
for expected in ('--no-auto-update', '--prompt-file', '--cwd', '--output-format', 'plain',
                 '--permission-mode', 'dontAsk', '--sandbox', 'read-only',
                 '--tools', 'read_file,grep,list_dir', '--deny', 'MCPTool(*)',
                 '--no-subagents', '--disable-web-search'):
    assert expected in args, (expected, args)
prompt=open(sys.argv[3], encoding='utf-8').read()
assert 'no shell, write tools, subagents, web search, or test execution' in prompt
assert prompt.rstrip().endswith('grok smoke')
assert not any('grok smoke' in a for a in args), args
PY_GROK

# Bare Sol resolves the existing Codex profile, and an explicit effort changes
# only effort. Dry-run keeps this check independent of a real backend binary.
PATH="/usr/bin:/bin" "$ROOT/bin/dairy.sh" read --backend codex --profile sol \
  --project-root "$TMP/repo" --prompt 'sol default' --dry-run --json > "$TMP/sol.json"
PATH="/usr/bin:/bin" "$ROOT/bin/dairy.sh" read --backend codex --profile sol --effort low \
  --project-root "$TMP/repo" --prompt 'sol override' --dry-run --json > "$TMP/sol-low.json"
python3 - "$TMP/sol.json" "$TMP/sol-low.json" "$ROOT/config/models.env" <<'PY_SOL'
import json, sys
default = json.load(open(sys.argv[1], encoding='utf-8'))
override = json.load(open(sys.argv[2], encoding='utf-8'))
config = {}
for line in open(sys.argv[3], encoding='utf-8'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        config[key] = value
expected_model = config['DELEGATE_MODEL_SOL']
expected_effort = config['DELEGATE_EFFORT_SOL']
assert (default['model'], default['effort']) == (expected_model, expected_effort), default
assert (override['model'], override['effort']) == (expected_model, 'low'), override
PY_SOL

# Grok workspace-write is refused before state or worktree creation because
# the native workspace sandbox has no Windows enforcement and fails open when
# application fails. The PowerShell runner must carry the same refusal.
set +e
XDG_STATE_HOME="$TMP/grok-ws-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  "$ROOT/bin/dairy.sh" workspace --backend grok --project-root "$TMP/repo" \
  --prompt 'nope' --worktree 2> "$TMP/grok-ws.err"
grok_ws_rc=$?
set -e
[[ "$grok_ws_rc" -eq 2 ]]
grep -q 'no cross-platform fail-closed workspace-write' "$TMP/grok-ws.err"
[[ ! -e "$TMP/grok-ws-state" ]]
[[ "$(git -C "$TMP/repo" worktree list --porcelain | grep -c '^worktree ' || true)" -eq 1 ]]
grep -Fq "if (\$Backend -eq 'grok' -and \$Access -eq 'workspace-write')" "$ROOT/bin/dairy.ps1"
! grep -Fq "'workspace-write' { \$arguments += @('--permission-mode', 'bypassPermissions', '--sandbox', 'workspace') }" "$ROOT/bin/dairy.ps1"

# Cursor Agent: dairy uses stdin + print/text, looks up cursor-agent not PATH
# `agent`, maps read-only to --force --mode plan, and refuses workspace-write
# because --sandbox enabled still wrote outside --workspace.
cat > "$TMP/fake-bin/cursor-agent" <<'FAKE_CURSOR'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\0' "$@" > "$CURSOR_ARGS"
cat > "$CURSOR_PROMPT"
printf 'fake cursor completed\n'
FAKE_CURSOR
chmod +x "$TMP/fake-bin/cursor-agent"

XDG_STATE_HOME="$TMP/cursor-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  CURSOR_ARGS="$TMP/cursor.args" CURSOR_PROMPT="$TMP/cursor.prompt" \
  "$ROOT/bin/dairy.sh" read --backend cursor --project-root "$TMP/repo" \
  --prompt 'cursor smoke' --json > "$TMP/cursor.json"

python3 - "$TMP/cursor.json" "$TMP/cursor.args" "$TMP/cursor.prompt" "$ROOT/config/models.env" <<'PY_CURSOR'
import json, sys
obj=json.load(open(sys.argv[1], encoding='utf-8'))
config = {}
for line in open(sys.argv[4], encoding='utf-8'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        config[key] = value
assert obj['status'] == 'completed' and obj['backend'] == 'cursor', obj
assert obj['profile'] == 'grok', obj
assert obj['model'] == config['DELEGATE_CURSOR_MODEL_GROK'], obj
assert obj['access'] == 'read-only', obj
assert obj.get('effort') in ('', None), obj
assert 'fake cursor completed' in open(obj['report'], encoding='utf-8').read(), obj
args=open(sys.argv[2], 'rb').read().decode('utf-8').split('\x00')[:-1]
for expected in ('-p', '--output-format', 'text', '--trust', '--workspace',
                 '--force', '--mode', 'plan', '--sandbox', 'enabled',
                 '--model', config['DELEGATE_CURSOR_MODEL_GROK']):
    assert expected in args, (expected, args)
assert '--effort' not in args, args
prompt=open(sys.argv[3], encoding='utf-8').read()
assert prompt.rstrip().endswith('cursor smoke')
assert not any('cursor smoke' in a for a in args), args
PY_CURSOR

PATH="/usr/bin:/bin" "$ROOT/bin/dairy.sh" read --backend cursor --profile grok-fast \
  --project-root "$TMP/repo" --prompt 'cursor fast' --dry-run --json > "$TMP/cursor-fast.json"
python3 - "$TMP/cursor-fast.json" "$ROOT/config/models.env" <<'PY_CURSOR_FAST'
import json, sys
obj=json.load(open(sys.argv[1], encoding='utf-8'))
config = {}
for line in open(sys.argv[2], encoding='utf-8'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        config[key] = value
assert obj['profile'] == 'grok-fast', obj
assert obj['model'] == config['DELEGATE_CURSOR_MODEL_GROK_FAST'], obj
PY_CURSOR_FAST

PATH="/usr/bin:/bin" "$ROOT/bin/dairy.sh" read --backend cursor --profile auto \
  --project-root "$TMP/repo" --prompt 'cursor auto' --dry-run --json > "$TMP/cursor-auto.json"
python3 - "$TMP/cursor-auto.json" <<'PY_CURSOR_AUTO'
import json, sys
obj=json.load(open(sys.argv[1], encoding='utf-8'))
assert obj['profile'] == 'auto' and obj['model'] == 'auto', obj
PY_CURSOR_AUTO

set +e
XDG_STATE_HOME="$TMP/cursor-ws-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  "$ROOT/bin/dairy.sh" workspace --backend cursor --project-root "$TMP/repo" \
  --prompt 'nope' --worktree 2> "$TMP/cursor-ws.err"
cursor_ws_rc=$?
PATH="/usr/bin:/bin" "$ROOT/bin/dairy.sh" read --backend cursor --effort high \
  --project-root "$TMP/repo" --prompt 'nope' --dry-run 2> "$TMP/cursor-effort.err"
cursor_effort_rc=$?
set -e
[[ "$cursor_ws_rc" -eq 2 ]]
grep -q 'no fail-closed workspace-write' "$TMP/cursor-ws.err"
[[ ! -e "$TMP/cursor-ws-state" ]]
[[ "$cursor_effort_rc" -eq 2 ]]
grep -q 'not supported for cursor' "$TMP/cursor-effort.err"
grep -Fq "if (\$Backend -eq 'cursor' -and \$Access -eq 'workspace-write')" "$ROOT/bin/dairy.ps1"

printf 'dairy runner smoke tests passed\n'

#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/repo"
git -C "$TMP/repo" init -q
git -C "$TMP/repo" config user.name test
git -C "$TMP/repo" config user.email test@example.invalid
printf 'base\n' > "$TMP/repo/base.txt"
git -C "$TMP/repo" add base.txt
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
  "$ROOT/bin/dairy.sh" read --backend agy --model gemini-3.6-flash-high \
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
assert 'gemini-3.6-flash-high' in args, args                  # explicit --model passed through
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
assert flash['profile'] == 'flash-high' and flash['model'] == 'gemini-3.6-flash-high', flash
assert pro['profile'] == 'pro-high' and pro['model'] == 'gemini-3.1-pro-high', pro
assert legacy['profile'] == 'flash-high' and legacy['model'] == 'gemini-3.6-flash-high', legacy
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

printf 'dairy runner smoke tests passed\n'

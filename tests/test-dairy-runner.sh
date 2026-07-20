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

XDG_STATE_HOME="$TMP/run-state" PATH="$TMP/fake-bin:/usr/bin:/bin" \
  "$ROOT/bin/dairy.sh" write --backend codex --project-root "$TMP/repo" \
  --prompt 'write smoke' --worktree --json > "$TMP/result.json"

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

printf 'fallback runner smoke tests passed\n'

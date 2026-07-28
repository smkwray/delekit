#!/usr/bin/env bash
# Each worktree here is a distinct reason the prune helper must KEEP or REMOVE.
# If any guard stops mattering, exactly one of these assertions fails.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRUNE="$ROOT/bin/prune-worktrees.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

repo="$TMP/repo"
mkdir -p "$repo"; cd "$repo"
git init -q; git commit -q --allow-empty -m base

printf '.worktrees/\n' > .gitignore
git add .gitignore; git commit -q -m ignore-worktrees
mk() { git worktree add -q -b "$1" ".worktrees/agent-$1" HEAD; }

# (a) finished: branched, no unique commits, only cruft untracked -> REMOVE
mk done1; ( cd ".worktrees/agent-done1" && mkdir -p .venv && echo x > .venv/pyvenv.cfg )

# (b) unmerged work: a real commit not in main HEAD -> KEEP
mk unmerged; ( cd ".worktrees/agent-unmerged" && echo feature > f.txt && git add f.txt && git commit -q -m work )

# (c) dirty tracked file -> KEEP
echo tracked > shared.txt; git add shared.txt; git commit -q -m add-shared
mk dirty; ( cd ".worktrees/agent-dirty" && echo changed >> shared.txt )

# (d) untracked non-cruft file (real work someone forgot to commit) -> KEEP
mk untracked; ( cd ".worktrees/agent-untracked" && echo important > NOTES.md )

fail() { echo "FAIL: $1" >&2; exit 1; }

# idle-min 0 so the idle guard doesn't mask the other reasons.
out="$("$PRUNE" --project-root "$repo" --idle-min 0 --apply 2>&1)"
echo "$out"

echo "$out" | grep -q "removed worktree.*agent-done1" || fail "did not remove the finished worktree"
[[ -d "$repo/.worktrees/agent-done1" ]] && fail "finished worktree still on disk"
[[ -d "$repo/.worktrees/agent-unmerged" ]]  || fail "removed a worktree with unmerged commits"
[[ -d "$repo/.worktrees/agent-dirty" ]]     || fail "removed a worktree with tracked changes"
[[ -d "$repo/.worktrees/agent-untracked" ]] || fail "removed a worktree with untracked work"

# The kept branch's commit must still exist.
git -C "$repo" rev-parse --verify unmerged >/dev/null 2>&1 || fail "deleted an unmerged branch"

echo "prune-worktrees smoke tests passed"

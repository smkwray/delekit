#!/usr/bin/env bash
# Remove FINISHED delegate worktrees, and only those. A worktree is finished
# when its work is already in the main branch and nothing would be lost by
# deleting it. Anything else is kept and reported, never silently removed.
#
# Covers every worktree registered with the repo (`git worktree list`), wherever
# it lives — the standard project-local <project>/.worktrees/<name> (owner
# convention), native Claude Code isolation (.claude/worktrees/agent-*), and the
# dairy --worktree runner's legacy <parent>/.delegate-worktrees/<project>/*.
#
# A worktree is REMOVED only when ALL hold:
#   - not the main checkout, not locked, not the current directory
#   - idle longer than --idle-min (guards against an in-flight agent)
#   - no modified or staged tracked files
#   - every commit is already reachable from the main worktree's HEAD (merged)
#   - any untracked files are recognised build cruft (.venv, __pycache__, …)
# Otherwise it is KEPT with the reason printed. Default is a dry run.
# No pipefail: several pipelines end in `head`, whose early close would otherwise
# surface as SIGPIPE and abort the run.
set -u

PROJECT_ROOT=""
IDLE_MIN=30
APPLY=0
KEEP_UNTRACKED=0

usage() {
  cat <<'EOF'
Usage: prune-worktrees.sh [--project-root PATH] [--apply] [--idle-min N] [--keep-untracked]

  --project-root PATH  repo to clean (default: git root of the current directory)
  --apply              actually remove; without it this is a dry run
  --idle-min N         never touch a worktree modified in the last N minutes (default 30)
  --keep-untracked     treat ANY untracked file as work; never prune a worktree that has one
                       (default: prune when untracked files are only known build cruft)

Removes only worktrees whose work is already merged and which are otherwise
clean and idle. Everything else is reported, not removed.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-root) PROJECT_ROOT="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --idle-min) IDLE_MIN="$2"; shift 2 ;;
    --keep-untracked) KEEP_UNTRACKED=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ "$IDLE_MIN" =~ ^[0-9]+$ ]] || { echo "--idle-min must be an integer" >&2; exit 2; }

command -v git >/dev/null || { echo "git is required" >&2; exit 127; }
PROJECT_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
[[ -n "$PROJECT_ROOT" && -d "$PROJECT_ROOT" ]] || { echo "Not in a git repo; pass --project-root." >&2; exit 2; }
PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd -P)"
cd "$PROJECT_ROOT"

MAIN_HEAD="$(git rev-parse HEAD)"
HERE="$(pwd -P)"
now="$(date +%s)"

# Untracked paths that are safe to discard when the worktree is otherwise done.
is_cruft() {
  case "$1" in
    .venv|.venv/*|venv|venv/*|__pycache__|*/__pycache__|__pycache__/*|\
    .pytest_cache|.pytest_cache/*|.mypy_cache|.mypy_cache/*|.ruff_cache|.ruff_cache/*|\
    node_modules|node_modules/*|dist|dist/*|build|build/*|*.pyc|.DS_Store|*/.DS_Store) return 0 ;;
    *) return 1 ;;
  esac
}

removed=0 kept=0
to_remove=()

# Iterate every registered worktree except the main one. (bash 3.2-safe.)
while IFS= read -r wt; do
  [[ -z "$wt" ]] && continue
  wtabs="$(cd "$wt" 2>/dev/null && pwd -P || echo "$wt")"

  # The main checkout is the one equal to PROJECT_ROOT.
  if [[ "$wtabs" == "$PROJECT_ROOT" ]]; then continue; fi

  reason=""
  # locked?
  if git worktree list --porcelain | awk -v w="$wt" '
      $1=="worktree"{cur=$2} $1=="locked" && cur==w {found=1} END{exit !found}'; then
    reason="locked"
  fi
  # currently sitting inside it?
  [[ -z "$reason" && "$HERE" == "$wtabs"* ]] && reason="current directory"
  # idle guard: newest mtime under the worktree
  if [[ -z "$reason" ]]; then
    newest="$(find "$wtabs" -type f -not -path '*/.git/*' -print0 2>/dev/null \
              | xargs -0 stat -f '%m' 2>/dev/null | sort -rn | head -1)"
    [[ -n "$newest" ]] && (( (now - newest) < IDLE_MIN * 60 )) && reason="active in last ${IDLE_MIN}m"
  fi
  # tracked modifications?
  if [[ -z "$reason" ]]; then
    if [[ -n "$(git -C "$wtabs" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
      reason="uncommitted tracked changes"
    fi
  fi
  # unmerged commits?
  if [[ -z "$reason" ]]; then
    wt_head="$(git -C "$wtabs" rev-parse HEAD 2>/dev/null || echo)"
    ahead="$(git rev-list --count "${MAIN_HEAD}..${wt_head}" 2>/dev/null || echo 1)"
    [[ "$ahead" != "0" ]] && reason="$ahead unmerged commit(s)"
  fi
  # untracked files that aren't cruft?
  if [[ -z "$reason" ]]; then
    while IFS= read -r u; do
      [[ -z "$u" ]] && continue
      path="${u#?? }"
      if [[ "$KEEP_UNTRACKED" -eq 1 ]] || ! is_cruft "$path"; then
        reason="untracked file: $path"; break
      fi
    done < <(git -C "$wtabs" status --porcelain 2>/dev/null | grep '^??' || true)
  fi

  if [[ -n "$reason" ]]; then
    printf 'KEEP    %s\n          (%s)\n' "$wtabs" "$reason"
    kept=$((kept+1))
  else
    printf 'REMOVE  %s\n' "$wtabs"
    to_remove+=("$wtabs")
    removed=$((removed+1))
  fi
done < <(git worktree list --porcelain | awk '$1=="worktree"{print $2}')

echo
if [[ "$removed" -eq 0 ]]; then
  echo "Nothing to prune. ($kept kept)"
  exit 0
fi

if [[ "$APPLY" -ne 1 ]]; then
  echo "Dry run: $removed would be removed, $kept kept. Re-run with --apply."
  exit 0
fi

for wtabs in "${to_remove[@]}"; do
  branch="$(git -C "$wtabs" rev-parse --abbrev-ref HEAD 2>/dev/null || echo)"
  git worktree remove --force "$wtabs" && echo "removed worktree $wtabs"
  if [[ -n "$branch" && "$branch" != "HEAD" ]]; then
    git branch -D "$branch" >/dev/null 2>&1 && echo "  deleted branch $branch"
  fi
done
git worktree prune
echo
echo "Pruned $removed finished worktree(s), kept $kept."

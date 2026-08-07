#!/usr/bin/env bash
set -euo pipefail

# dairy: generic, headless fallback runner for one-shot and unattended jobs.
# Primary interactive orchestration should use the native tandy-* subagents.

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
if [[ -n "${DELEGATE_MODELS_FILE:-}" ]]; then
  MODELS_FILE="$DELEGATE_MODELS_FILE"
elif [[ -f "$KIT_ROOT/config/models.env" ]]; then
  MODELS_FILE="$KIT_ROOT/config/models.env"
else
  MODELS_FILE="$SCRIPT_DIR/models.env"
fi

usage() {
  cat <<'HELP'
Usage:
  dairy.sh <workspace|readonly|full> [options]

Modes:
  workspace   read-write inside the project or worktree (default write access)
  readonly    no file or external-state changes
  full        unrestricted host access; explicit opt-in only

Prompt input (choose one):
  --prompt-file PATH       task prompt file
  --prompt TEXT            inline task
  --prompt-stdin           read task from stdin (also automatic for piped stdin)

Core options:
  --backend NAME           codex (default), claude, muse, or agy
  --profile NAME           backend-specific model profile from config/models.env:
                           codex: terra (default), luna, sol
                           muse: spark (default)
                           agy: flash-high (default), flash-low, pro-high
  --model ID               explicit per-run override; REQUIRED for --backend claude,
                           which has no profile mapping
  --effort LEVEL           explicit reasoning effort override
  --access MODE            read-only, workspace-write, or danger-full-access
  --sandbox MODE           compatibility alias for --access
  --project-root PATH      defaults to Git root or nearest project marker from cwd
  --worktree               create an isolated branch/worktree from current HEAD
  --dirty-policy MODE      fail (default) or ignore before worktree creation
  --no-auto-commit         leave worktree changes uncommitted
  --no-preamble            pass the task verbatim
  --fast                   enable Codex fast_mode for this run
  --json                   print machine-readable completion metadata
  --dry-run                resolve and print only; creates no files or worktrees

Mode aliases:
  workspace = write   -> workspace-write
  readonly  = read    -> read-only
  full                -> danger-full-access, explicit only

--access overrides the mode default for a single run.
HELP
}

load_env_file() {
  local file="$1" line key value
  [[ -f "$file" ]] || { echo "Missing config: $file" >&2; exit 2; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *=* ]] || { echo "Invalid config line: $line" >&2; exit 2; }
    key="${line%%=*}"; value="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo "Invalid config key: $key" >&2; exit 2; }
    value="${value#\"}"; value="${value%\"}"; value="${value#\'}"; value="${value%\'}"
    printf -v "$key" '%s' "$value"
  done < "$file"
}
load_env_file "$MODELS_FILE"

[[ $# -gt 0 ]] || { usage >&2; exit 2; }
case "$1" in
  -h|--help) usage; exit 0 ;;
  workspace|write) MODE="workspace"; ACCESS="${RUNNER_DEFAULT_WRITE_ACCESS:-workspace-write}" ;;
  readonly|read) MODE="readonly"; ACCESS="${RUNNER_DEFAULT_READ_ACCESS:-read-only}" ;;
  full) MODE="full"; ACCESS="danger-full-access" ;;
  *) echo "Unknown mode: $1 (expected workspace, readonly, or full)" >&2; usage >&2; exit 2 ;;
esac
shift

BACKEND="${DELEGATE_BACKEND:-${RUNNER_DEFAULT_BACKEND:-codex}}"
PROFILE="${DELEGATE_PROFILE:-}"
if [[ -n "$PROFILE" ]]; then PROFILE_EXPLICIT=1; else PROFILE_EXPLICIT=0; fi
MODEL=""
EFFORT=""
PROMPT_FILE=""
PROMPT_TEXT=""
PROMPT_STDIN=0
PROJECT_ROOT=""
WORKTREE=0
DIRTY_POLICY="fail"
AUTO_COMMIT=1
PREAMBLE=1
FAST=0
JSON_OUTPUT=0
DRY_RUN=0

need_value() { [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 2; }; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend) need_value "$@"; BACKEND="$2"; shift 2 ;;
    --profile) need_value "$@"; PROFILE="$2"; PROFILE_EXPLICIT=1; shift 2 ;;
    --model) need_value "$@"; MODEL="$2"; shift 2 ;;
    --effort) need_value "$@"; EFFORT="$2"; shift 2 ;;
    --prompt-file) need_value "$@"; PROMPT_FILE="$2"; shift 2 ;;
    --prompt) need_value "$@"; PROMPT_TEXT="$2"; shift 2 ;;
    --prompt-stdin) PROMPT_STDIN=1; shift ;;
    --project-root) need_value "$@"; PROJECT_ROOT="$2"; shift 2 ;;
    --access|--sandbox) need_value "$@"; ACCESS="$2"; shift 2 ;;
    --worktree) WORKTREE=1; shift ;;
    --dirty-policy) need_value "$@"; DIRTY_POLICY="$2"; shift 2 ;;
    --no-auto-commit) AUTO_COMMIT=0; shift ;;
    --no-preamble) PREAMBLE=0; shift ;;
    --fast) FAST=1; shift ;;
    --json) JSON_OUTPUT=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$BACKEND" in codex|claude|muse|agy) ;; *) echo "Unsupported backend: $BACKEND" >&2; exit 2 ;; esac
case "$ACCESS" in read-only|workspace-write|danger-full-access) ;; *) echo "Invalid access: $ACCESS" >&2; exit 2 ;; esac
case "$DIRTY_POLICY" in fail|ignore) ;; *) echo "dirty-policy must be fail or ignore" >&2; exit 2 ;; esac

if [[ "$BACKEND" == "codex" ]]; then
  [[ -n "$PROFILE" ]] || PROFILE="terra"
  case "$PROFILE" in terra|luna|sol) ;; *) echo "Codex profile must be terra, luna, or sol" >&2; exit 2 ;; esac
elif [[ "$BACKEND" == "muse" ]]; then
  [[ -n "$PROFILE" ]] || PROFILE="spark"
  case "$PROFILE" in spark) ;; *) echo "muse profile must be spark" >&2; exit 2 ;; esac
elif [[ "$BACKEND" == "agy" ]]; then
  [[ -n "$PROFILE" ]] || PROFILE="flash-high"
  case "$PROFILE" in
    terra) echo "Deprecated agy profile terra; use flash-high." >&2; PROFILE="flash-high" ;;
    luna) echo "Deprecated agy profile luna; use flash-low." >&2; PROFILE="flash-low" ;;
    sol) echo "Deprecated agy profile sol; use pro-high." >&2; PROFILE="pro-high" ;;
  esac
  case "$PROFILE" in flash-high|flash-low|pro-high) ;; *) echo "agy profile must be flash-high, flash-low, or pro-high" >&2; exit 2 ;; esac
elif [[ "$PROFILE_EXPLICIT" -eq 1 ]]; then
  echo "--profile resolves a model only for the codex and agy backends; config/models.env holds their IDs." >&2
  echo "For --backend $BACKEND, pass --model explicitly." >&2
  exit 2
else
  PROFILE=""
fi

# agy has no Codex-style filesystem sandbox. Headless agy is either plan
# (read-only, tool writes soft-denied) or --dangerously-skip-permissions
# (unrestricted and NOT workspace-confined). It cannot honor a confined
# workspace-write, so refuse it up front rather than granting host-wide writes
# under a "workspace" label.
if [[ "$BACKEND" == "agy" && "$ACCESS" != "read-only" && "$ACCESS" != "danger-full-access" ]]; then
  echo "agy has no confined workspace-write mode: headless agy is either plan (read-only) or" >&2
  echo "--dangerously-skip-permissions (unrestricted, not workspace-confined)." >&2
  echo "Run agy with readonly, or full for explicit unrestricted writes; use codex/claude for confined writes." >&2
  exit 2
fi

prompt_sources=0
[[ -n "$PROMPT_FILE" ]] && ((prompt_sources+=1)) || true
[[ -n "$PROMPT_TEXT" ]] && ((prompt_sources+=1)) || true
[[ "$PROMPT_STDIN" -eq 1 ]] && ((prompt_sources+=1)) || true
if [[ "$prompt_sources" -eq 0 && ! -t 0 ]]; then PROMPT_STDIN=1; prompt_sources=1; fi
[[ "$prompt_sources" -eq 1 ]] || { echo "Choose exactly one prompt source." >&2; exit 2; }
if [[ -n "$PROMPT_FILE" ]]; then
  [[ -f "$PROMPT_FILE" ]] || { echo "Prompt file not found: $PROMPT_FILE" >&2; exit 2; }
  TASK_PROMPT="$(cat "$PROMPT_FILE")"
elif [[ "$PROMPT_STDIN" -eq 1 ]]; then
  TASK_PROMPT="$(cat)"
else
  TASK_PROMPT="$PROMPT_TEXT"
fi
[[ -n "${TASK_PROMPT//[[:space:]]/}" ]] || { echo "Task prompt is empty." >&2; exit 2; }

profile_upper="$(printf '%s' "$PROFILE" | tr '[:lower:]-' '[:upper:]_')"
model_var="DELEGATE_MODEL_${profile_upper}"
effort_var="DELEGATE_EFFORT_${profile_upper}"
agy_model_var="DELEGATE_AGY_MODEL_${profile_upper}"
muse_model_var="DELEGATE_MUSE_MODEL_${profile_upper}"
muse_effort_var="DELEGATE_MUSE_EFFORT_${profile_upper}"
if [[ "$BACKEND" == "codex" ]]; then
  [[ -n "$MODEL" ]] || MODEL="${!model_var:-}"
  [[ -n "$MODEL" ]] || { echo "No model configured for profile $PROFILE" >&2; exit 2; }
  [[ -n "$EFFORT" ]] || EFFORT="${!effort_var:-high}"
elif [[ "$BACKEND" == "muse" ]]; then
  [[ -n "$MODEL" ]] || MODEL="${!muse_model_var:-}"
  [[ -n "$MODEL" ]] || { echo "No muse model configured for profile $PROFILE" >&2; exit 2; }
  [[ -n "$EFFORT" ]] || EFFORT="${!muse_effort_var:-high}"
elif [[ "$BACKEND" == "agy" ]]; then
  # agy slugs bake the effort tier into the name, so a profile resolves to a full
  # agy slug (DELEGATE_AGY_MODEL_<PROFILE>) and no separate --effort is sent.
  # --model overrides the profile for a single run.
  [[ -n "$MODEL" ]] || MODEL="${!agy_model_var:-}"
  [[ -n "$MODEL" ]] || { echo "No agy model configured for profile $PROFILE" >&2; exit 2; }
fi

find_project_root() {
  local start="$1" git_root d marker
  git_root="$(git -C "$start" rev-parse --show-toplevel 2>/dev/null || true)"
  if [[ -n "$git_root" ]]; then printf '%s\n' "$git_root"; return; fi
  d="$(cd "$start" && pwd -P)"
  while :; do
    for marker in pyproject.toml package.json Cargo.toml go.mod pom.xml build.gradle settings.gradle .git; do
      if [[ -e "$d/$marker" ]]; then printf '%s\n' "$d"; return; fi
    done
    [[ "$d" == "/" ]] && break
    d="$(dirname "$d")"
  done
  cd "$start" && pwd -P
}
PROJECT_ROOT="${PROJECT_ROOT:-$(find_project_root "$PWD")}"
[[ -d "$PROJECT_ROOT" ]] || { echo "Project root does not exist: $PROJECT_ROOT" >&2; exit 2; }
PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd -P)"

if [[ "$WORKTREE" -eq 1 && "$ACCESS" == "read-only" ]]; then
  echo "Worktree isolation is unnecessary for read-only mode; disabling it." >&2
  WORKTREE=0
fi

state_root="${XDG_STATE_HOME:-$HOME/.local/state}/delekit"
LOG_DIR="${DELEGATE_LOG_DIR:-$state_root/logs}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)_$$"
prefix="$LOG_DIR/${timestamp}_${MODE}_${BACKEND}"
prompt_log="${prefix}.prompt.md"
stdout_log="${prefix}.stdout.log"
stderr_log="${prefix}.stderr.log"
report_file="${prefix}.report.md"
status_file="${prefix}.status.json"
done_file="${prefix}.done"

EXEC_ROOT="$PROJECT_ROOT"
BRANCH=""
WORKTREE_DIR=""
BASE_SHA=""
if [[ "$WORKTREE" -eq 1 ]]; then
  command -v git >/dev/null 2>&1 || { echo "git is required for --worktree" >&2; exit 127; }
  git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "--worktree requires a Git repository" >&2; exit 2; }
  dirty="$(git -C "$PROJECT_ROOT" status --porcelain)"
  if [[ "$DIRTY_POLICY" == "fail" && -n "$dirty" ]]; then
    echo "Main checkout is dirty. Commit/stash changes or pass --dirty-policy ignore." >&2
    exit 3
  elif [[ "$DIRTY_POLICY" == "ignore" && -n "$dirty" ]]; then
    echo "Warning: worktree starts from committed HEAD; uncommitted parent changes are not included." >&2
  fi
  BASE_SHA="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
  BRANCH="delegate/${MODE}-${timestamp}"
  WORKTREE_DIR="$PROJECT_ROOT/.worktrees/${MODE}-${timestamp}"
  EXEC_ROOT="$WORKTREE_DIR"
fi

compose_prompt() {
  if [[ "$PREAMBLE" -eq 1 ]]; then
    case "$ACCESS" in
      read-only) printf '%s\n\n' '**Access: read-only.** Do not create, edit, or delete files. Return the complete result in the final message.' ;;
      workspace-write) printf '%s\n\n' '**Access: workspace-write.** Work only inside the current project or worktree. Return outcome, changed files, validation, and blockers in the final message.' ;;
      danger-full-access) printf '%s\n\n' '**Access: unrestricted and explicitly authorized for this run.** Minimize changes outside the project and report every external effect.' ;;
    esac
    if [[ "$WORKTREE" -eq 1 ]]; then
      printf '%s\n\n' '**Isolation: Git worktree.** Stay in the current worktree; do not switch branches, touch the main checkout, push, merge, or remove the worktree.'
    fi
  fi
  printf '%s\n' "$TASK_PROMPT"
}
COMPOSED_PROMPT="$(compose_prompt)"

json_escape() {
  local s="$1"
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\r'/\\r}; s=${s//$'\t'/\\t}
  printf '%s' "$s"
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  if [[ "$JSON_OUTPUT" -eq 1 ]]; then
    printf '{"dry_run":true,"backend":"%s","profile":"%s","model":"%s","effort":"%s","access":"%s","project_root":"%s","execution_root":"%s","worktree":%s,"report":"%s","prompt":"%s"}\n' \
      "$(json_escape "$BACKEND")" "$(json_escape "$PROFILE")" "$(json_escape "${MODEL:-}")" \
      "$(json_escape "${EFFORT:-}")" "$(json_escape "$ACCESS")" "$(json_escape "$PROJECT_ROOT")" \
      "$(json_escape "$EXEC_ROOT")" "$([[ "$WORKTREE" -eq 1 ]] && echo true || echo false)" \
      "$(json_escape "$report_file")" "$(json_escape "$COMPOSED_PROMPT")"
  else
    cat <<DRY
backend=$BACKEND
profile=$PROFILE
model=${MODEL:-<backend-default>}
effort=${EFFORT:-<backend-default>}
access=$ACCESS
project_root=$PROJECT_ROOT
execution_root=$EXEC_ROOT
worktree=$WORKTREE
report=$report_file
--- composed prompt ---
$COMPOSED_PROMPT
DRY
  fi
  exit 0
fi

command -v "$BACKEND" >/dev/null 2>&1 || { echo "$BACKEND CLI not found in PATH" >&2; exit 127; }
mkdir -p "$LOG_DIR"
TTL_DAYS="${RUNNER_LOG_TTL_DAYS:-7}"
[[ "$TTL_DAYS" =~ ^[0-9]+$ ]] || { echo "RUNNER_LOG_TTL_DAYS must be a nonnegative integer" >&2; exit 2; }
find "$LOG_DIR" -type f \( -name '*.stdout.log' -o -name '*.stderr.log' \) -mtime "+$TTL_DAYS" -delete 2>/dev/null || true

if [[ "$WORKTREE" -eq 1 ]]; then
  created_worktree="$(python3 "$KIT_ROOT/tools/worktree_manager.py" create \
    --project-root "$PROJECT_ROOT" --name "${MODE}-${timestamp}" \
    --branch "$BRANCH" --base HEAD)" \
    || { echo "Failed to create worktree: $WORKTREE_DIR" >&2; exit 4; }
  WORKTREE_DIR="$created_worktree"
  EXEC_ROOT="$WORKTREE_DIR"
fi
printf '%s\n' "$COMPOSED_PROMPT" > "$prompt_log"

export CI=1 GIT_TERMINAL_PROMPT=0 GIT_PAGER=cat PAGER=cat NO_COLOR=1
exit_code=0

case "$BACKEND" in
  codex)
    fast_args=(--disable fast_mode)
    [[ "$FAST" -eq 1 ]] && fast_args=(--enable fast_mode)
    args=(exec --cd "$EXEC_ROOT" --model "$MODEL" -c "model_reasoning_effort=$EFFORT" "${fast_args[@]}" --skip-git-repo-check --color never -o "$report_file")
    if [[ "$ACCESS" == "danger-full-access" ]]; then
      args+=(--dangerously-bypass-approvals-and-sandbox)
    else
      args+=(--sandbox "$ACCESS" -c approval_policy=never)
    fi
    codex "${args[@]}" - <<< "$COMPOSED_PROMPT" >"$stdout_log" 2>"$stderr_log" || exit_code=$?
    ;;
  claude)
    permission_mode="default"
    case "$ACCESS" in
      read-only) permission_mode="plan" ;;
      workspace-write) permission_mode="acceptEdits" ;;
      danger-full-access) permission_mode="bypassPermissions" ;;
    esac
    args=(-p - --output-format text --no-session-persistence --permission-mode "$permission_mode" --add-dir "$EXEC_ROOT")
    [[ -n "$MODEL" ]] && args+=(--model "$MODEL")
    [[ -n "$EFFORT" ]] && args+=(--effort "$EFFORT")
    (cd "$EXEC_ROOT" && env -u CLAUDECODE claude "${args[@]}" <<< "$COMPOSED_PROMPT") >"$stdout_log" 2>"$stderr_log" || exit_code=$?
    [[ -s "$stdout_log" ]] && cp "$stdout_log" "$report_file"
    ;;
  muse)
    # Muse CLI, headless `exec`: the final answer is plain text on stdout, and
    # the prompt comes from the composed prompt file already written above (no
    # stdin path, and this avoids an argv limit on long prompts).
    #
    # Approval and the sandbox are ON by default; approvals must be disabled or
    # a headless run would block on them. Measured against Muse 0.1.0 on macOS:
    # the default sandbox confines shell writes to the workspace plus temp dirs
    # (a $HOME write is denied, /tmp is allowed), which is the same shape codex
    # workspace-write has, so workspace-write maps to the default sandbox.
    # --disable-write only blocks the non-shell write tools; the shell can still
    # write, so read-only must also drop the shell to be honest about the label.
    args=(exec --model "$MODEL" --reasoning-effort "$EFFORT" --workspace "$EXEC_ROOT")
    case "$ACCESS" in
      read-only) args+=(--disable-approval --disable-write --disable-shell) ;;
      workspace-write) args+=(--disable-approval) ;;
      danger-full-access) args+=(--yolo) ;;
    esac
    args+=(--prompt-file "$prompt_log")
    (cd "$EXEC_ROOT" && muse "${args[@]}") >"$stdout_log" 2>"$stderr_log" || exit_code=$?
    [[ -s "$stdout_log" ]] && cp "$stdout_log" "$report_file"
    ;;
  agy)
    # Antigravity CLI, headless print mode (-p): the prompt is the flag's value and
    # the final answer is plain text on stdout (no stdin piping). Access is limited
    # to the two modes agy can actually enforce headlessly (workspace-write is
    # rejected earlier): read-only -> plan (writes soft-denied); full ->
    # --dangerously-skip-permissions (unrestricted).
    args=()
    case "$ACCESS" in
      read-only) args+=(--mode plan) ;;
      danger-full-access) args+=(--dangerously-skip-permissions) ;;
    esac
    args+=(--add-dir "$EXEC_ROOT")
    [[ -n "$MODEL" ]] && args+=(--model "$MODEL")
    [[ -n "$EFFORT" ]] && args+=(--effort "$EFFORT")
    args+=(-p "$COMPOSED_PROMPT")
    (cd "$EXEC_ROOT" && agy "${args[@]}") >"$stdout_log" 2>"$stderr_log" || exit_code=$?
    [[ -s "$stdout_log" ]] && cp "$stdout_log" "$report_file"
    ;;
esac

if [[ ! -s "$report_file" && -s "$stdout_log" ]]; then cp "$stdout_log" "$report_file"; fi

report_is_invalid() {
  local normalized=""
  [[ -s "$report_file" ]] || return 0
  normalized="$(tr -d '\r' < "$report_file" | sed '/^[[:space:]]*$/d')"
  [[ "$normalized" == "Execution error" ]]
}
if report_is_invalid; then
  echo "Delegate produced no valid final report; treating the run as failed." >&2
  if [[ ! -s "$report_file" ]]; then
    printf '%s\n' 'Delegate produced no valid final report. Inspect stdout/stderr logs.' > "$report_file"
  fi
  exit_code=1
fi

HEAD_SHA=""
COMMITS=""
if [[ "$WORKTREE" -eq 1 && -d "$WORKTREE_DIR" ]]; then
  if [[ "$AUTO_COMMIT" -eq 1 ]]; then
    git -C "$WORKTREE_DIR" add -A >/dev/null 2>&1 || true
    if ! git -C "$WORKTREE_DIR" diff --cached --quiet 2>/dev/null; then
      git -C "$WORKTREE_DIR" commit -q -m "delegate(${MODE}): ${timestamp}" >/dev/null 2>&1 \
        || git -C "$WORKTREE_DIR" -c user.name=delegate -c user.email=delegate@local commit -q -m "delegate(${MODE}): ${timestamp}" >/dev/null 2>&1 \
        || true
    fi
  fi
  HEAD_SHA="$(git -C "$WORKTREE_DIR" rev-parse HEAD 2>/dev/null || true)"
  COMMITS="$(git -C "$WORKTREE_DIR" rev-list --count "${BASE_SHA}..HEAD" 2>/dev/null || true)"
  cat >> "$report_file" <<HANDOFF

## Worktree handoff

- Worktree: $WORKTREE_DIR
- Branch: $BRANCH
- Base: $BASE_SHA
- Head: ${HEAD_SHA:-unknown}
- Commits: ${COMMITS:-unknown}

Review before merging or discarding. Cleanup is deliberately not automatic.
HANDOFF
fi

touch "$done_file"
printf '{"status":"%s","exit_code":%s,"backend":"%s","profile":"%s","model":"%s","access":"%s","project_root":"%s","execution_root":"%s","report":"%s","stdout":"%s","stderr":"%s","worktree":"%s","branch":"%s"}\n' \
  "$([[ "$exit_code" -eq 0 ]] && echo completed || echo failed)" "$exit_code" \
  "$(json_escape "$BACKEND")" "$(json_escape "$PROFILE")" "$(json_escape "${MODEL:-}")" "$(json_escape "$ACCESS")" \
  "$(json_escape "$PROJECT_ROOT")" "$(json_escape "$EXEC_ROOT")" "$(json_escape "$report_file")" \
  "$(json_escape "$stdout_log")" "$(json_escape "$stderr_log")" "$(json_escape "$WORKTREE_DIR")" \
  "$(json_escape "$BRANCH")" > "$status_file"

if [[ "$JSON_OUTPUT" -eq 1 ]]; then
  cat "$status_file"
else
  echo "Delegate $MODE $([[ "$exit_code" -eq 0 ]] && echo completed || echo failed)."
  echo "Report: $report_file"
  echo "Status: $status_file"
  [[ "$WORKTREE" -eq 1 ]] && {
    echo "Worktree: $WORKTREE_DIR"
    echo "Branch: $BRANCH"
    echo "Review: git -C \"$PROJECT_ROOT\" log --stat ${BASE_SHA}..${BRANCH}"
    echo "Merge:  git -C \"$PROJECT_ROOT\" merge --no-ff ${BRANCH}"
    echo "Clean:  git -C \"$PROJECT_ROOT\" worktree remove \"$WORKTREE_DIR\" && git -C \"$PROJECT_ROOT\" branch -d ${BRANCH}"
  }
fi

# Desktop notifications are opt-in: unattended jobs and test runs must not
# create ambient UI side effects merely because they finish.
if [[ "${DELEGATE_DESKTOP_NOTIFY:-0}" == "1" ]] && command -v osascript >/dev/null 2>&1; then
  osascript -e "display notification \"Report: $(basename "$report_file")\" with title \"delegate $MODE finished\"" >/dev/null 2>&1 || true
fi
exit "$exit_code"

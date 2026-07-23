# herd — detached delegate workers

**`herd`** spawns and herds detached headless workers: fire-and-forget agents on
Codex or Claude that keep running after the command returns, and that you can
check on, steer, and resume later — with no gateway and no Claude session.

## What it is

The missing delegation quadrant. Today delekit covers three:

|            | interactive (gateway + Claude session) | headless (no gateway) |
|------------|------------------------------------------|-----------------------|
| one-shot   | —                                        | `dairy`               |
| resumable/steerable/detached | `tandy` (native)             | **`herd`** ← this      |

`herd` gives you **detached, resumable, steerable** headless workers on Codex or
Claude, with **no gateway** and **no Claude session** — drivable from a bare
terminal, a script, CI, or a non-Claude orchestrator. It is the gateway-free
sibling of `tandy`, and the steerable sibling of `dairy`.

Backends: **codex and claude only.** (No grok, no gemini for the detached path —
gemini's CLI has no resume story we rely on.)

## Non-negotiable: sessions cannot leak

The whole point of building this instead of leaning on someone remembering to
clean up. Four layers, none depending on an agent or a human:

1. **Every run is born with two deadlines.**
   - `--stall-after` (default **60m**, env `DELEGATE_STALL_AFTER_S`) — the real
     safety net. No new backend output for this long → the detached helper kills
     the backend and writes `stalled`. Generous by default because a stall-kill
     is non-destructive and resumable (see "Recovery after a kill" below); it
     only needs to catch a dead/hung backend, not a working-but-quiet one.
   - `--deadline` (default **6h**, env `DELEGATE_MAX_WALLCLOCK_S`) — a high
     backstop so genuinely long agents run, but nothing runs *forever*. On expiry
     → kill + `failed` with `stall_reason: deadline`.
2. **Reap-on-invocation.** Every verb first sweeps *this device's* sessions:
   a process that is dead but has no terminal state is reconciled (`failed` /
   `stalled`) and marked `.done`. pid-guarded — never touches a live process.
3. **`prune`** reclaims terminal + idle session directories, dry-run by default,
   same guard shape and ergonomics as `prune-worktrees`. It reclaims `done` /
   `killed` (clean outcomes) past the idle window but **keeps `failed` /
   `stalled`** — the recoverable-unresolved ones whose `session_id` you may still
   resume — unless `--include-unresolved` is passed. `herd prune --apply` is a
   safe cron / launchd / Scheduled-Task job if you want cleanup to happen even
   when you never invoke `herd` interactively.
4. **`kill` = SIGINT → SIGKILL.** A worker is stopped, never orphaned.

Net: "a bunch of idle sessions" is structurally impossible — each is born with a
deadline and reaped by the next call or the scheduler — yet no reap silently
destroys a resumable session.

## Recovery after a kill

A stall-kill, deadline-kill, or explicit `kill` is **non-destructive**:

- **Conversation context survives.** The watchdog kills the OS process, but the
  `session_id` is already persisted in `meta.json` and the backend's own session
  store (Codex session store / Claude `--resume`) retains the thread. `herd send
  <task> "continue"` opens a fresh turn on the same session with full context.
- **Filesystem work survives.** Anything the worker already wrote is on disk (in
  the repo or its worktree); a kill does not roll it back.
- **Only the in-flight turn is lost** — the one call running at kill time — and
  the resume replaces it, exactly like a `--now` interrupt.

So a false-positive stall costs nothing but a `send` to continue. This is why
`prune` protects `failed`/`stalled` dirs (they hold the resume handle) and why a
generous `stall-after` is safe.

## Non-negotiable: the synced kit must not grow

- **Runtime state lives device-local, outside the git repo** — the same path
  `dairy` already uses:
  - macOS/Linux: `${XDG_STATE_HOME:-~/.local/state}/delekit/sessions/<device>/`
  - Windows: `%LOCALAPPDATA%\delekit\sessions\<device>\`
  So the git-synced kit never grows from runtime data; only source files land in
  the repo, and this feature is ~one Python file plus two thin shims.
- **Within local state, the disk hog is `events.jsonl`** (raw backend stream for
  `peek`). It is capped (rotate/truncate past `DELEGATE_EVENTS_MAX_MB`, default
  32) and TTL-expired (reuse `RUNNER_LOG_TTL_DAYS`). `prune` reclaims whole
  terminal dirs. Neither the kit nor local disk grows unbounded.
- **State never syncs across devices.** A pid or resume-id from the Mac is
  meaningless on Windows, and a live pidfile through OneDrive would conflict-copy.
  State is device-scoped (see below); control never crosses machines. The *kit*
  syncs via git; that is the only thing that crosses devices.

## Non-negotiable: no venv, mac + windows

One **stdlib-only** Python module — `tools/delegate_supervisor.py`
(`argparse`, `subprocess`, `signal`, `os`, `json`, `pathlib`, `hashlib`, `time`).
Python3 is already a kit dependency (`render_config.py`, `verify_kit.py`,
`seed_claude_context_cache.py`), so **no new dependency, no pip, no venv, no
vendored tree**. The hard cross-platform bits (process liveness, signals, atomic
writes) live in one shared file instead of duplicated across `bin/*.sh` and
`bin/*.ps1`. Thin shims `bin/herd.sh` / `bin/herd.ps1` just exec it.

## Device + owner scoping

- **Device id** from `device.env` (new `DELEKIT_DEVICE_ID`), falling back to a
  short hash of the hostname. State is partitioned per device.
- **Owner** = spawning cwd (mirrors franke's `CDX_OWNER`). `list` / `prune`
  default to your own owner; `--any-owner` widens within the device.
- Controlling or GCing another device's tasks is refused — you only see and act
  on `<device>/`.

## State layout

```
$STATE/delekit/sessions/<device>/<task>/
  meta.json     backend, model, effort, access, repo, exec_root, worktree,
                pid, session_id, state, owner, created_utc, deadline_utc,
                stall_after_s, stall_reason
  prompt.md     the composed work order (also the spec artifact for review)
  events.jsonl  raw backend stream (capped + TTL'd)
  report.md     last agent message
  status.json   machine snapshot, written atomically (temp + os.replace)
  .done         terminal marker
```

## States

`working` · `awaiting_reply` · `done` · `failed` · `stalled` · `killed`

Derived from: process liveness (`pid`), output age vs `stall_after`, `.done`
presence, and a `QUESTION:` sentinel in the last agent message → `awaiting_reply`.
Terminal states never silently flip back; a `send`/resume opens a fresh turn.

## Verbs

```
herd spawn <workspace|readonly|full> [core opts] (prompt | --prompt-file | stdin)
                         detach a worker; print task name + pid; return at once
herd list [--all] [--any-owner] [--json]      reap-then-list this device's tasks
herd status <task> [--json]                   cheap state probe
herd peek   <task> [--tail N] [--thinking]    recent events / raw stream (costly)
herd result <task> [--wait] [--timeout S]     final report
herd send   <task> [--now] (prompt | -f FILE) steer / answer; resumes via session_id
herd kill   <task>                            SIGINT then SIGKILL
herd prune  [--apply] [--idle-min N] [--any-owner]   GC terminal+idle dirs (dry-run default)
herd doctor                                   env / dirs / backend checks
```

Core opts mirror `dairy`: `--backend codex|claude`, `--profile terra|luna|sol`
(codex only, resolved from `config/models.env`; `terra` default), `--model`, `--effort`,
`--access`/`--sandbox`, `--worktree` + `--dirty-policy` + `--no-auto-commit`,
`--no-preamble`, `--json`. Access→sandbox/permission mapping and the access
preamble are lifted verbatim from `dairy` so the two runners stay consistent.

## Backend adapters

Both stream JSON so we can scrape the session id and observe activity live —
this is the one place we *cannot* reuse `dairy`, which uses `--output-format
text` and passes `--no-session-persistence`.

- **codex**
  - spawn:  `codex exec --json --cd <root> --model <m> -c model_reasoning_effort=<e> [--sandbox <a> -c approval_policy=never | --dangerously-bypass-approvals-and-sandbox]`
  - resume: `codex exec resume <session_id> --json ...`
  - parse: `session_id` / thread from the event stream; last agent message; event time.
- **claude**
  - spawn:  `claude -p --output-format stream-json --verbose --permission-mode <p> --add-dir <root> [--model <m>] [--effort <e>]`
  - resume: `claude -p --resume <session_id> --output-format stream-json ...`
  - **persistence stays ON** for this path (unlike `dairy`) — resume needs it.

## How detach works

`spawn` writes `prompt.md` + `meta.json`, then launches a `__run_turn` helper
with `Popen(..., start_new_session=True)` (POSIX) / `CREATE_NEW_PROCESS_GROUP`
+ `DETACHED_PROCESS` (Windows). The helper streams the backend into
`events.jsonl`, updates `report.md`, enforces `stall_after` + `deadline`,
captures `session_id` into `meta.json` (atomic), and drops `.done` on exit.
`spawn` returns the task name and pid immediately. Everything else reads disk.

## Files

Additive to delekit (nothing existing is rewritten by `herd` itself):

```
tools/delegate_supervisor.py          the supervisor (stdlib only)
bin/herd.sh  bin/herd.ps1             thin shims on PATH (wired by the installers)
tests/test_delegate_supervisor.py     reaper / prune / list / kill against fixtures
tests/test_delegate_backends.py       spawn/result/send/stall via a fake backend
tests/fake_backend.py                 a controllable codex/claude stand-in, no network
docs/detached-runner.md               this doc
```

The one shared change outside `herd`: `config/models.env` profiles are named for
their models (`terra`/`luna`/`sol`), so `herd`, `dairy`, and the `tandy` agents
speak one profile vocabulary.

## Testing

`python3 -m unittest tests.test_delegate_supervisor tests.test_delegate_backends`.
The backend tests point `DELEGATE_CODEX_BIN` / `DELEGATE_CLAUDE_BIN` at
`tests/fake_backend.py`, which emits each backend's streaming-JSON schema, so the
full detach → capture-session → report → resume path runs with no network. The
provider event schemas the adapters parse are the integration boundary to
re-verify after a Codex or Claude CLI upgrade.
```
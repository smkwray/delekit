# herd — detached delegate workers

**`herd`** spawns and herds detached headless workers: fire-and-forget agents on
Codex, Pi, Claude, Muse, Opencode, Grok Build, or Cursor Agent that keep running after the command returns, and that you can
check on, steer, and resume later — with no gateway and no Claude session.

## What it is

The missing delegation quadrant. Today delekit covers three:

|            | interactive (gateway + Claude session) | headless (no gateway) |
|------------|------------------------------------------|-----------------------|
| one-shot   | —                                        | `dairy`               |
| resumable/steerable/detached | `tandy` (native)             | **`herd`** ← this      |

`herd` gives you **detached, resumable, steerable** headless workers on Codex,
Pi, Claude, Muse, Opencode, Grok Build, or Cursor Agent, with **no gateway** and **no Claude session** — drivable from a
bare terminal, a script, CI, or a non-Claude orchestrator. It is the gateway-free
sibling of `tandy`, and the steerable sibling of `dairy`.

Backends: **codex, pi, claude, muse, opencode, grok, and cursor.** Gemini and Antigravity
(`agy`) remain excluded from the detached path because their headless CLIs do
not provide the session/event contract this supervisor needs.

Grok uses native `--output-format streaming-json`: text arrives in chunks,
`usage` marks response boundaries, and the terminal `end` carries the session
id but no answer text. Herd preallocates a UUID for the initial
`--session-id`, resumes with `--resume <session_id>`, and publishes only the
last complete response segment. The adapter uses `--prompt-file` to avoid argv
limits and maps read-only/full to Grok's native controls. `workspace-write`
is refused: Grok documents OS sandbox enforcement for Linux and macOS, not
Windows, and a built-in profile that cannot be applied warns and continues
unenforced. Grok's read-only mode independently exposes only
`read_file,grep,list_dir`, denies MCP tools, disables subagents and web search,
and therefore cannot run tests or shell commands; its OS sandbox is defense in
depth rather than the sole boundary.

Cursor Agent (`cursor-agent`, never PATH `agent`) streams Claude-shaped JSON:
every event carries `session_id`. A successful `result.subtype=success` authorizes
the turn; the publishable answer is the last matching-session `assistant`
message, not the terminal `result` string (which can concatenate earlier
assistant text). `--resume <id>` continues the chat. Intermediate assistant
text does not itself authorize `done`. A mismatched session is cancelled
immediately rather than left running until natural exit. Anything else fails as
`cursor-protocol` and does not publish intermediate assistant text or a
successful answer; `report.md` holds a failure diagnostic, and the previous
successful answer, when present, is retained as `previous-report.md`. The prompt
is stdin. Read-only is
`--force --mode plan` (write tools and shell redirects denied; a read-only
shell still works). `workspace-write` is refused: `--force --sandbox enabled`
wrote outside `--workspace`. Full is `--force --sandbox disabled`. Profiles
`grok`, `grok-fast`, and `auto` resolve from `config/models.env`. Cursor Agent is
live-qualified on macOS. The Windows PowerShell implementation is source-complete
but remains unqualified until native dairy and herd read-only/full receipts are
captured.

`muse exec --json` qualifies on both counts: every record carries
`stream.id` (the session id) and the turn's answer arrives as one
`run.terminal.completed` event. Resume is the same command plus `--session-id`,
so `herd send` continues the same conversation. Two muse-specific notes:

- **The prompt goes in by file, not stdin.** The adapter points `--prompt-file`
  at the `prompt.md` the supervisor already rewrites before every spawn and
  send, which also keeps long prompts off argv.
- **No separate reasoning stream.** Muse folds reasoning into the answer text,
  so `herd peek --thinking` stays empty for muse workers. `peek` itself still
  works.

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
2. **Reap-on-invocation.** `list` and `prune` sweep *this device's* sessions;
   `status` and `result` reconcile the task they name. A process that is dead but
   has no terminal state is reconciled (`failed` / `stalled`) and marked `.done`.
   pid-guarded — never touches a live process.
3. **`prune`** reclaims terminal + idle session directories, dry-run by default,
   same guard shape and ergonomics as `prune-worktrees`. It reclaims `done` /
   `killed` (clean outcomes) past the idle window but **keeps `failed` /
   `stalled`** — the recoverable-unresolved ones whose `session_id` you may still
   resume — unless `--include-unresolved` is passed. `herd prune --apply` is a
   safe cron / launchd / Scheduled-Task job if you want cleanup to happen even
   when you never invoke `herd` interactively.
4. **`kill` = SIGINT → whole-tree SIGKILL** (`taskkill /T /F` on Windows). A
   worker and the descendants that can still be found are stopped. This is
   best-effort, not a guarantee — see [Containment](#containment).

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
  the repo, and this feature is two small Python modules plus two thin shims.
- **Within local state, the disk hog is `events.jsonl`** (raw backend stream for
  `peek`). Compaction is **post-turn, best-effort**: the file is truncated to
  half of `DELEGATE_EVENTS_MAX_MB` (default 32) *after* the stream ends, so a
  long-running turn can exceed the cap for its whole duration and a helper that
  dies before compaction leaves it oversized. There is **no TTL and no live hard
  bound**. Reclamation happens when `prune` removes the terminal task directory.
  The kit itself never grows; local disk is bounded in the ordinary case, not
  guaranteed.
- **State never syncs across devices.** A pid or resume-id from one machine is
  meaningless on another. State is device-scoped (see below); control never
  crosses machines. The *kit* is the portable surface; runtime state stays local.

## Non-negotiable: no venv, mac + windows

Two **stdlib-only** Python modules — `tools/delegate_supervisor.py` and the
`tools/worktree_manager.py` it imports for worktree creation. Stdlib only
(`argparse`, `subprocess`, `signal`, `os`, `json`, `pathlib`, `hashlib`, `time`,
`tempfile`, `shutil`, `socket`, `re`, `threading`).
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
                session_id, state, owner, created_utc, deadline_utc,
                stall_after_s, stall_reason
  helper.json   the detached helper's pid
  child.json    the backend process's pid
  previous-report.md  the prior turn's answer, kept when a turn fails
  prompt.md     the composed work order (also the spec artifact for review)
  events.jsonl  raw backend stream (compacted after the turn; reclaimed by prune)
  report.md     last agent message
  status.json   machine snapshot, written atomically (temp + os.replace)
  .done         terminal marker
```

## Turn-success invariant

A turn counts as successful only when the backend exits 0 **and** produced a
final agent message. A backend that exits 0 having said nothing is marked
`failed` with `stall_reason: empty-report`, and `report.md` is replaced with a
diagnostic pointing at `events.jsonl`.

This is reachable, not theoretical: `opencode/nemotron-3.5-lightning-free` emits
`step_start` then `step_finish(reason="unknown")` and stops, and upstream
opencode has a matching report where an auto-rejected tool call leaves the model
with no final message. Without the invariant, herd marked that `done` — and on a
resume the *previous* turn's report stayed visible as the current result, so a
silent non-answer read as success twice over. dairy has always converted an
empty report into a failed status; herd now agrees.

**Auto-commit runs only after that invariant passes.** It used to run before the
turn was classified, so a stalled, deadlined, crashed, or answer-less turn still
committed whatever the worker had written. Uncommitted work is not lost — it
stays in the worktree for the deliberate review this doc already promises.

## Containment

`kill`, `--stall-after`, and `--deadline` stop the backend and the descendants
it is still possible to find, **including ones that left its process group**.
This is best-effort containment, not a guarantee: a process that double-forks
after the descendant snapshot, or daemonizes out of the tree entirely, can
survive it, and PID reuse means a captured pid is not a durable identity. It
closes the case that actually occurs — a tool child in its own session — and no
more than that. A process-group signal
alone is not enough: a tool child that calls `setsid` — which is how opencode's
POSIX shell tool launches commands — is no longer in the group, so signalling the
group would mark a task terminal while a detached shell kept writing to the repo.
The supervisor snapshots the descendant set before signalling (once the parent
exits, its children reparent and can no longer be found by walking down), then
escalates SIGINT → tree SIGKILL.

On Windows the escalation is `taskkill /T /F`, which acts on the process
relationships visible when it runs rather than on durable membership. A Job
Object with `KILL_ON_JOB_CLOSE` would be the stronger primitive; it is not
implemented. Measured on Windows: a full-access worker with two live shell
children was killed, both children died, and the interrupted side effect never
landed.

## Hard-kill and resume

A `kill`, stall-kill, or deadline-kill is non-destructive, and this is measured
rather than assumed. Both kill points were exercised against a real backend:

| kill point | survivors | side effect | `PRAGMA integrity_check` | resume |
| --- | --- | --- | --- | --- |
| mid-stream, tokens arriving | none | — | `ok` | same `session_id`, correct answer |
| mid-tool, a real shell child running | none | did **not** land | `ok` | same `session_id`, correct answer |

The interrupted turn is lost and the resume replaces it; the session store is
intact and the conversation continues. On Windows the same kill/resume cycle was
run through `herd`, with the same result.

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
herd result <task> [--wait] [--timeout S] [--json]
                                                final report/state
herd send   <task> [--now] (prompt | -f FILE) steer / answer; resumes via session_id
herd kill   <task>                            SIGINT then SIGKILL
herd prune  [--apply] [--idle-min N] [--any-owner]   GC terminal+idle dirs (dry-run default)
herd doctor                                   env / dirs / backend checks
```

Core opts mirror `dairy`: `--backend codex|pi|claude|muse|opencode|grok|cursor`, `--profile`
(backend-specific, resolved from `config/models.env`: codex and pi take
`terra|luna|sol` with `terra` default, muse takes `spark`, opencode takes
`ox|nemotron|hy3` with `ox` default, grok uses Grok's CLI default unless
`--model` is supplied, and cursor uses
`DELEGATE_CURSOR_PROFILES`), `--model`,
`--effort`, `--access`/`--sandbox`, `--worktree` + `--dirty-policy` +
`--no-auto-commit`, `--no-preamble`, `--json`. Access→sandbox/permission mapping
and the access preamble are lifted verbatim from `dairy` so the two runners stay
consistent — including muse's read-only caveat (it loses the shell; see
[dairy-runner.md](dairy-runner.md)).

Watchers should use `herd result <task> --wait --timeout S --json` and branch on
`status.state` (and `stall_reason` when present). A zero exit status means that
the result query ran; it does not mean the worker succeeded. Re-arm the watcher
if the state is still `working` at timeout.

`--worktree` creates `<project>/.worktrees/<name>` and requires `.worktrees/` in
the project's root `.gitignore`. It is ignored in read-only mode, where isolation
buys nothing.

**Computer use requires `full` mode**, exactly as in `dairy`: the sandbox in
`workspace`/`readonly` blocks `open -a`, `osascript`, and browser control, so GUI
tasks fail with permission errors rather than prompting. `full` runs unsandboxed
— grant it only when the task genuinely needs the machine. `herd` is the better
fit for GUI sessions than `dairy`: `send` steers the *same* worker across turns,
so it keeps driving the window it opened instead of starting blind.

## Backend adapters

All seven stream JSON so we can capture the session id and observe activity live.
Unlike `dairy`'s backend-specific one-shot paths, every `herd` adapter must
persist and recover a resumable session handle.

- **codex**
  - spawn:  `codex exec --json --cd <root> --model <m> -c model_reasoning_effort=<e> [--sandbox <a> -c approval_policy=never | --dangerously-bypass-approvals-and-sandbox]`
  - resume: `codex exec resume <session_id> --json ...`
  - parse: `session_id` / thread from the event stream; last agent message; event time.
- **pi**
  - spawn: `pi --mode json --provider openai-codex --session-dir <task>/pi-session ...`
  - resume: the same command plus `--session-id <session_id>`.
  - read-only exposes only `read,grep,find,ls`; workspace-write is refused because Pi has no sandbox; full is unrestricted.
  - ambient extensions, skills, and prompt templates are disabled; project context files still load. Pi has no separate reasoning stream, so `peek --thinking` stays empty.
- **claude**
  - spawn:  `claude -p --output-format stream-json --verbose --permission-mode <p> --add-dir <root> [--model <m>] [--effort <e>]`
  - resume: `claude -p --resume <session_id> --output-format stream-json ...`
  - **persistence stays ON** for this path (unlike `dairy`) — resume needs it.
- **opencode**
  - spawn:  `opencode --pure run --format json --auto --dir <root> --model <provider/model> [--variant <e>]`
  - resume: the same command plus `--session <session_id>`.
  - parse: `sessionID` is on **every** event, so the resume handle is available
    from the first line; the answer is the last `text` event's `part.text`. Each
    text part arrives as one complete event, so the last one is the whole answer
    rather than a fragment to reassemble.
  - **no version floor by default.** `DELEGATE_OPENCODE_MIN_VERSION` in
    `config/models.env` is empty, because pinning a version breaks anyone whose
    packaged install lags — Homebrew trailed npm by hours during development
    while a second machine already had a newer build than either. The knob
    exists for a future defect.

    Known defect, for the record: below 1.18.20, opencode's run loop
    answers permission events only for the ROOT session, so a `task` subagent's
    request is dropped. The measured consequence is not a hang, it is a
    **bypass**: with ambient config setting `agent.general.permission.bash` to
    `ask` and a delekit policy of `{"*":"allow"}`, a subagent on **1.18.15 ran
    the shell command anyway** and returned its output, while **1.18.20 rejected
    it**. That `ask` has to come from ambient configuration, and delekit's own
    policies are catch-all deny or catch-all allow, so the kit does not create
    one. Set the floor to `1.18.20` on a machine where an administrator injects
    `ask` rules through managed configuration. `herd doctor` reports the
    binary a spawn would actually launch — honouring `DELEGATE_OPENCODE_BIN` —
    with its version and whether it satisfies the floor.
  - **the policy is carried by a delekit-owned agent, selected with `--agent`.**
    A top-level `OPENCODE_PERMISSION` is merged into an agent first, and that
    agent's own permission block is appended after it with the last match
    winning — so an ordinary global `agent.build.permission` outranks it.
    Measured on 1.18.15 and 1.18.21: with such a global agent present a
    `read-only` run **created the file it was denied**, 2 runs out of 2. Defining
    the agent is not enough; it has to be *selected*, and dropping `--agent`
    reproduces the break. herd and both dairy runners define
    `delekit-readonly` / `delekit-full` through `OPENCODE_CONFIG_CONTENT` and
    pass `--agent`; spawn and every resume use the same name. Unlike redirecting
    the config roots, this hides no models.
  - **project configuration is disabled; global configuration is not.** A
    `.opencode/opencode.json` in the target repo was measured re-enabling the
    write tool on a run whose policy denied it — the repo is the surface delekit
    does not control, so every run sets `OPENCODE_DISABLE_PROJECT_CONFIG=1` and
    passes `--pure` to keep external plugins out. Each task also gets its own
    `OPENCODE_DB`, which is what stops concurrent workers colliding on `database
    is locked`. The inline overrides `OPENCODE_CONFIG`, `OPENCODE_CONFIG_CONTENT`
    and `OPENCODE_AUTH_CONTENT` are scrubbed from the child environment.

    **`HOME` and `XDG_CONFIG_HOME` are deliberately NOT redirected.** An earlier
    revision did redirect them, and it hid seven models from `opencode models`,
    two of them this kit's own defaults — it broke the feature to defend against
    the operator's own machine. Ambient global config, managed/system config, and
    global MCP definitions therefore still load. That is the operator's own
    surface, not an untrusted one; the repository is the boundary being enforced.

    Because `OPENCODE_DISABLE_PROJECT_CONFIG` is what carries that boundary, an
    opencode old enough to lack the flag would not have it. JSON `run` output
    alone is not the compatibility criterion.
  - **`DELEGATE_OPENCODE_BIN` picks the binary.** Useful when a packaged install
    lags: Homebrew's formula trailed the npm release by hours, and
    `opencode upgrade <version>` reported "Upgrade complete" against a
    brew-managed install without changing anything. Installing the wanted version
    elsewhere and pointing this at it leaves the system install alone.
  - **concurrency is bounded locally, not by the provider.** Every `opencode run`
    opens a SQLite session store. delekit points each task at its own
    `OPENCODE_DB`, so `herd`'s own workers no longer contend; anything sharing
    the default store from outside the kit still can, and dies with
    `database is locked`. See
    [dairy-runner.md](dairy-runner.md).
  - the prompt goes in on stdin and **stdin must reach EOF** or the run hangs
    forever with no output. `run_turn` writes it and closes the pipe, which is
    exactly the required shape. Opencode exposes no separate reasoning stream on
    the models measured, so `peek --thinking` stays empty, as it does for muse.

- **grok**
  - spawn: `grok --no-auto-update --prompt-file <task>/prompt.md --cwd <root> --output-format streaming-json --session-id <uuid> --model <m> --reasoning-effort <e> [sandbox flags]`
  - resume: the same command plus `--resume <session_id>`.
  - parse: accumulate `text.data` until each `usage` response boundary and keep
    only the latest complete segment, including a contentless segment. Require
    terminal `end.sessionId` to equal the preallocated UUID, require
    `end.stopReason=end_turn`, and reject residual text with no final `usage`
    boundary. Grok 0.2.116 is the herd minimum because that release added usage
    records to native `streaming-json`. The read-only tool allowlist is
    `read_file,grep,list_dir`, plus `--permission-mode dontAsk`, `--deny
    MCPTool(*)`, `--sandbox read-only`, `--no-subagents`, and
    `--disable-web-search`.

## How detach works

`spawn` writes `prompt.md` + `meta.json`, then launches a `__run_turn` helper
with `Popen(..., start_new_session=True)` (POSIX) / `CREATE_NEW_PROCESS_GROUP`
+ `CREATE_NO_WINDOW` (Windows). The helper's own backend child is launched the
same way, through the single `_child_popen_kwargs` helper. Both must get their
own process group on both platforms: `stop_pid` signals the *group*, so a child
sharing its parent's group would take the parent down with it when one task is
stopped. The helper streams the backend into
`events.jsonl`, updates `report.md`, enforces `stall_after` + `deadline`,
captures `session_id` into `meta.json` (atomic), and drops `.done` on exit.
`spawn` returns the task name and pid immediately. Everything else reads disk.

## Files

Additive to delekit (nothing existing is rewritten by `herd` itself):

```
tools/delegate_supervisor.py          the supervisor (stdlib only)
tools/worktree_manager.py             the single creator of project-local worktrees
bin/herd.sh  bin/herd.ps1             thin shims on PATH (wired by the installers)
tests/test_delegate_supervisor.py     reaper / prune / list / kill against fixtures
tests/test_delegate_backends.py       spawn/result/send/stall via a fake backend
tests/fake_backend.py                 a controllable codex/pi/claude/muse/opencode/grok/cursor stand-in, no network
docs/detached-runner.md               this doc
```

The one shared change outside `herd`: `config/models.env` profiles are named for
their models (`terra`/`luna`/`sol`), so `herd`, `dairy`, and the `tandy` agents
speak one profile vocabulary.

## Testing

`python3 -m unittest tests.test_delegate_supervisor tests.test_delegate_backends`.
The backend tests point the `DELEGATE_*_BIN` overrides at
`tests/fake_backend.py`, which emits each backend's streaming-JSON schema, so the
full detach → capture-session → report → resume path runs with no network. The
provider event schemas the adapters parse are the integration boundary to
re-verify after a backend CLI upgrade.
```

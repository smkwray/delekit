# Standalone runner

Use the `dairy` command for:

- CI, scheduled, or unattended one-shot work;
- direct Codex execution when proxy/native-agent translation is broken;
- a terminal without an interactive orchestrator;
- durable prompt/report/stdout/stderr/status artifacts for another process.

Do not use it merely because a native worker's assignment changed. Native agents can be messaged and resumed; a headless process cannot receive semantic course corrections.

## Compatibility names

```text
tandy -> write -> workspace-write
dairy -> read  -> read-only
full              danger-full-access
```

Tandy and Dairy are not personas. The task prompt defines the work. The uploaded Tandy default was unrestricted; the new write default is repository-confined `workspace-write`. Full access is explicit.

## Prompt overhead

The runner prepends one short access line, a backend limitation line for
read-only `pi`, `opencode`, and `grok`, and, when applicable, one worktree line. It does not send backend/model/log/report metadata to the model. Use `--no-preamble` or `-NoPreamble` when the task already carries the needed boundary.

## Profiles and central model mapping

Profiles are backend-specific and resolve through `config/models.env`. Codex and Pi share `terra` (default), `luna`, and `sol`; Pi pins the ChatGPT-subscription `openai-codex` provider rather than an API-key provider. Muse uses `spark` (default) = `muse-spark-1.2-contributor`, and like Codex takes a separate effort, so `--effort`/`-Effort` applies. Antigravity (`agy`) uses `flash-high` (default), `flash-low`, and `pro-high`; its slug carries the effort tier, so no separate `--effort` is sent. Opencode profiles come from `DELEGATE_OPENCODE_PROFILES` in `config/models.env` — that list *is* the profile set and its first entry is the default, so adding or renaming one needs no code change on either platform. An empty list is valid and makes opencode behave like `claude`, where `--model` is required. Grok Build uses Grok's CLI model default unless `--model`/`-Model` is supplied; no stale profile mapping is pinned in delekit. Cursor Agent (`--backend cursor`) is a different backend from Grok Build: it invokes `cursor-agent` (never PATH `agent`) and its profiles come from `DELEGATE_CURSOR_PROFILES` — shipped as `grok`, `grok-fast`, and `auto`. Effort and Fast are part of the catalog id, so `--effort` and Codex `--fast` are refused. The shipped opencode defaults sit on OpenCode Zen's free tier so a key with no credits still works; paid and OAuth models are reachable through an explicit `--model`. The old agy uses of `terra`, `luna`, and `sol` remain deprecated aliases for one migration window. Claude model choices remain explicit because its provider default is better handled by its own CLI.

**Muse tokens are discounted in exchange for training rights.** The
`-contributor` model is priced down because the provider may use session content
for product improvement — the CLI says so at startup. Keep private or client
material on another backend.

**Opencode is the reach backend.** One already-authenticated CLI fronts model
families no other delekit backend can get to, and its default profiles sit on
OpenCode Zen's free tier, so they cost nothing and work on an API key with no
credits. It pays for that reach with the
narrowest access surface of any backend here: two modes, not three.

## Examples

```bash
dairy write --profile terra --prompt-file task.md --worktree
dairy read --profile sol --prompt-stdin < audit.md
dairy read --backend pi --profile luna --prompt 'audit this checkout'
dairy read --backend agy --profile flash-high --prompt 'audit this checkout'
dairy write --backend muse --prompt-file task.md --worktree
dairy full --model explicit-provider-id --prompt 'authorized host task'
```

```powershell
dairy write -Profile terra -PromptFile task.md -Worktree
Get-Content audit.md -Raw | dairy read -Profile sol -PromptStdin
dairy read -Backend pi -Profile luna -Prompt 'audit this checkout'
dairy read -Backend agy -Profile flash-high -Prompt 'audit this checkout'
dairy write -Backend muse -PromptFile task.md -Worktree
dairy full -Model explicit-provider-id -Prompt 'authorized host task'
```

## Dry-run

```bash
dairy write --backend codex --prompt 'test' --worktree --dry-run --json
```

```powershell
dairy write -Backend codex -Prompt 'test' -Worktree -DryRun -Json
```

Dry-run resolves the prompt, model, access, project, and prospective worktree path but creates no log directory, report, branch, or worktree and does not require the backend CLI to be installed.

## Logs and status

Logs are device-local by default:

- macOS/Linux: `${XDG_STATE_HOME:-~/.local/state}/delekit/logs`
- Windows: `%LOCALAPPDATA%\delekit\logs`

Each run writes:

```text
.prompt.md
.stdout.log
.stderr.log
.report.md
.status.json
.done
```

Large stdout/stderr traces expire according to `RUNNER_LOG_TTL_DAYS`; prompts, reports, status, and completion markers remain until deliberately removed. Missing or one-line `Execution error` reports are converted into a failed status rather than being accepted as successful completion.

macOS desktop completion notifications are off by default. Set
`DELEGATE_DESKTOP_NOTIFY=1` for a run or shell when you explicitly want them.

## Worktree behavior

`--worktree`/`-Worktree`:

- requires a Git repository;
- fails on a dirty parent checkout by default;
- branches from committed `HEAD`;
- creates a distinct branch at `<project>/.worktrees/<name>` and requires
  `.worktrees/` in the project's root `.gitignore`;
- optionally auto-commits the worker's changes;
- leaves the worktree for deliberate review, merge, or discard.

Use `--dirty-policy ignore` or `-DirtyPolicy ignore` only with awareness that uncommitted parent changes are absent. Cleanup is never automatic because deletion must follow the integration decision.

## Permission behavior

Codex `read-only` and `workspace-write` runs use the selected sandbox with `approval_policy=never`, so blocked operations fail instead of prompting. `danger-full-access` uses Codex's explicit bypass flag. Other backends cannot reproduce every Codex sandbox boundary; use them only when their CLI's permission behavior is acceptable.

`pi` has no filesystem sandbox. For `read-only`, dairy removes shell and write tools and exposes only `read,grep,find,ls`; this prevents tool-driven writes but is not a confidentiality boundary against other user-readable paths. A short Pi-only preamble tells the worker to label command-dependent conclusions unverified. `workspace-write` is refused rather than mislabeled. `full` is unrestricted and explicit. Headless Pi also disables ambient extensions, skills, and prompt templates to reduce startup context and surprises, while normal project context such as `AGENTS.md` still loads. Run `herd doctor` to verify both `pi` and its `openai-codex` OAuth are ready.

`agy` has no Codex-style per-command sandbox. In headless mode it soft-denies any tool that would otherwise prompt (`write_file`, shell commands) unless `--dangerously-skip-permissions` is set — and that flag is all-or-nothing and **not** filesystem-confined (it will write outside the workspace). So the runner supports only two `agy` access modes: `read-only` → `--mode plan` (tool writes are soft-denied, so the run stays read-only), and `full` → `--dangerously-skip-permissions` (unrestricted). **`agy` cannot honor `workspace-write`** — it has no confined write mode — so `dairy workspace --backend agy` is refused up front; use `readonly`, or `full` for explicit unrestricted writes, or use codex/claude when you need confined workspace writes. The prompt is passed as the value of `-p`, not on stdin.

`muse` has approval and a sandbox ON by default, so every access mode disables approvals (a headless run would otherwise block on them) and then differs in what it leaves standing. Measured against Muse 0.1.0 on macOS, not inferred from flag names:

| dairy access | muse flags | what it actually enforces |
| --- | --- | --- |
| `workspace-write` | `--disable-approval` | Muse's own sandbox stays on. A shell write to `$HOME` is **denied**; the workspace and temp dirs (`/tmp`) are writable — the same shape as Codex `workspace-write`. |
| `read-only` | `--disable-approval --disable-write --disable-shell` | Write tools are policy-denied (`tool policy denied filesystem write`) and the shell is gone. File reads still work. |
| `full` | `--yolo` | Unrestricted and **not** workspace-confined — a `$HOME` write succeeds. |

**`read-only` on muse costs the shell.** `--disable-write` alone blocks only the non-shell write tools; the shell can still redirect into a file, so an honest read-only has to drop `--disable-shell` too. The delegate can read files but cannot run `git log`, `rg`, or a test command. When a read-only muse task needs the shell, use codex instead — its sandbox denies writes without removing the shell.

The prompt is passed via `--prompt-file` (the composed prompt log), not on stdin.

`opencode` has no filesystem sandbox and no confined write mode, so it supports
two access modes. Measured against opencode 1.18.x on macOS and Windows:

| dairy access | mechanism | what it actually enforces |
| --- | --- | --- |
| `read-only` | a delekit-owned primary agent (`--agent delekit-readonly`, defined through `OPENCODE_CONFIG_CONTENT`) carrying `{"*":"deny","read":{"*":"allow","mcp:*":"deny"},"glob":"allow","grep":"allow"}`, plus the same policy as top-level `OPENCODE_PERMISSION`, plus `--auto` | Deny by default, then allow back the read surface. Enumerating write tools is **not** a boundary — opencode leaves unlisted keys at allow, and a policy denying only `edit`/`write`/`bash` still let a "read-only" run reach the network via `websearch`. The `mcp:*` deny is nested **after** the read allow because the last matching rule wins: opencode's MCP resource operations ask under `read`, so a bare allow would let a delegate reading an untrusted repository pull data out of an operator-configured MCP server. |
| `workspace-write` | — | **Refused.** opencode's write confinement is a heuristic on the bash command string, not a filesystem boundary: `echo X > /abs/outside` is allowed while `cd /outside && echo X > f` is rejected, identically on two models. Same call already made for `pi` and `agy`. |
| `full` | a delekit-owned agent (`--agent delekit-full`) carrying `{"*":"allow"}`, plus the same as `OPENCODE_PERMISSION`, plus `--auto` | Unrestricted and not workspace-confined. |

**`read-only` costs the shell**, as muse's does: no shell, no subagents, no
tests. The runner adds a line telling the worker to mark command-dependent
claims unverified rather than narrow the task.

`grok` uses Grok Build's native sandbox and built-in tool allowlist:

| dairy access | Grok flags | what it actually enforces |
| --- | --- | --- |
| `read-only` | `--permission-mode dontAsk --sandbox read-only --tools read_file,grep,list_dir --deny 'MCPTool(*)' --no-subagents --disable-web-search` | The model receives only delekit's read/search built-ins; no shell, edit, subagent, web, or MCP tool is exposed. The built-in sandbox remains defense in depth, not the sole boundary. |
| `workspace-write` | — | **Refused.** Grok's built-in workspace profile has no Windows enforcement, and a built-in profile that cannot be applied warns and continues unenforced. That is not the cross-platform fail-closed boundary this label promises. |
| `full` | `--permission-mode bypassPermissions --sandbox off` | Unrestricted Grok sandbox; explicit full access only. |

The one-shot path uses `--prompt-file`, `--cwd`, and plain output. Herd's
resumable `streaming-json` adapter requires Grok 0.2.116 or later, the first
release with the usage records it uses as response boundaries. Authenticate the
installed `grok` CLI separately with `grok login` before running a live task;
installation and wiring do not perform that login.

`cursor` uses Cursor Agent (`cursor-agent`). It is not Grok Build and must not
be invoked as PATH `agent`. Measured against cursor-agent 2026.09.02-c22c1a3
on macOS. Cursor Agent is live-qualified on macOS. The Windows PowerShell
implementation is source-complete but remains unqualified until native dairy and
herd read-only/full receipts are captured.

| dairy access | cursor-agent flags | what it actually enforces |
| --- | --- | --- |
| `read-only` | `-p --trust --force --mode plan --sandbox enabled --workspace <root>` | Plan mode denies write tools and shell redirects; a read-only shell still works. `--force` is required: without it, plan mode hangs on a shell permission prompt. `--print` without `--force` still wrote a file. |
| `workspace-write` | — | **Refused.** `--force --sandbox enabled` wrote a file outside `--workspace`. |
| `full` | `-p --trust --force --sandbox disabled --workspace <root>` | Unrestricted and not workspace-confined. |

Dairy uses `--output-format text` (stdin prompt). Herd uses `stream-json` and
`--resume <session_id>` from the stream's `session_id`. A herd turn is not
`done` until a terminal `result.subtype=success` with a nonempty `result` and
matching `session_id`; truncated or error streams fail as `cursor-protocol`.
Authenticate with
`cursor-agent login` (or `CURSOR_API_KEY`) separately; wiring does not log in.
`auto` is a valid catalog id: the server picks a model per request, so runs
are not reproducible. There is no `auto-fast`.

**A top-level policy is not enough on its own.** opencode merges
`OPENCODE_PERMISSION` into an agent and then appends that agent's own
`agent.<name>.permission`, and the **last matching rule wins**. So an ordinary
global config — not a hostile one, just a user who once set
`agent.build.permission.bash = "allow"` — silently outranks it. Measured on
1.18.15 and 1.18.21: with such a global agent present, a run labelled
`read-only` **created the file it was supposed to be denied**, 2 runs out of 2.
Defining a delekit-owned agent is not sufficient either; it has to be *selected*.
Both runners therefore pass `--agent`, and dropping that flag reproduces the
break. Unlike redirecting the config roots, this hides no models.

**The permission value must be a JSON object.** `opencode debug agent <name>`
prints a rule-*array* shape, and that shape passed to `OPENCODE_PERMISSION` is
**silently ignored** — a run configured that way created the file it was meant to
be denied. It fails open with no warning, so both runners send the object form
and the suites pin it.

**Effort maps to `--variant`, validated against the model.** There is no
universal vocabulary and opencode ignores an unknown variant in silence, so the
runner enumerates the selected model's declared variants and fails loudly rather
than sending one that would do nothing.

The prompt is piped in, and stdin reaching EOF is load-bearing — see the known
issue below.

**Computer use requires `full` mode.** Launching or scripting GUI apps — `open -a`, `osascript`/AppleScript, browsers — is blocked by the sandbox in `workspace-write` and `read-only`, so those tasks fail with permission errors rather than prompting. `full` runs unsandboxed; grant it only when the task genuinely needs the machine, and expect the worker to report every external effect (the access preamble instructs it to).

---

# Known issues

## `--profile` resolves only for backends mapped in `config/models.env` **[all]**

**Symptom.** `dairy … --backend claude --profile sol` would otherwise run on the
CLI's default model while the status JSON reports `"profile":"sol"`.

**Cause.** `config/models.env` maps backend-specific profiles to Codex IDs
(`DELEGATE_MODEL_*`, shared by Pi), muse IDs (`DELEGATE_MUSE_MODEL_*`), agy IDs
(`DELEGATE_AGY_MODEL_*`), and opencode IDs (`DELEGATE_OPENCODE_MODEL_*`); Claude
has no such mapping.

**Fix.** Both runners resolve profiles for `codex`, `pi`, `muse`, `agy`,
`opencode`, and `cursor`, and both **fail** rather than guess for `--backend claude` — pass
`--model` explicitly there.

---

## `opencode` hangs forever if its stdin never reaches EOF **[opencode]**

**Symptom.** `herd` with `--backend opencode` (or any direct `opencode run`)
produces **zero bytes on stdout and stderr** and never exits. Nothing in the logs explains
it, because nothing is ever written.

**Cause.** `opencode run` accepts a piped prompt, so it reads stdin before
starting the turn. Given a stdin that stays open, it waits there indefinitely.
Measured on 1.18.15: identical read-a-file tasks were killed at 90s, 200s, and
300s with empty output, while the same task with stdin closed finished in 8s.
Opencode has **no wall-clock or idle timeout of its own**, so nothing breaks the
wait. Note that a *permission* prompt is not a second cause: headless opencode
auto-rejects an `external_directory` request rather than waiting on it. Stdin is
the hang, which also explains the intermittent "hangs for minutes on a prompt it
answers in six seconds" reports.

**Fix.** Both runners pass the prompt in and close stdin before the turn starts
— a heredoc in `dairy.sh`, a pipeline in `dairy.ps1`, `write`-then-`close` in the
supervisor. Keep it that way: never hand opencode an inherited or held-open
stdin. `herd` is additionally covered by `--stall-after`, which kills the whole
process tree and leaves the session resumable; **`dairy` has no timeout**, so a
one-shot run that does hang must be interrupted by the caller.

---

## Windows: three things that only break there **[opencode]**

All three were found by running `dairy` on a real Windows host, and all three
failed in a way macOS never shows:

1. **`opencode.exe` does not exist.** npm installs `opencode` *and*
   `opencode.cmd`, so an `.exe` lookup fails, and `Get-Command opencode
   -CommandType Application` returns **two** matches whose `.Source` joins into
   one unusable string. The runner prefers a `.cmd`/`.bat`/`.exe` match and takes
   exactly one.
2. **Native stderr is fatal under `$ErrorActionPreference = 'Stop'`.** opencode
   writes ANSI escapes to stderr even with `NO_COLOR` set, and PowerShell turns
   that into a terminating error — the run died with the escape sequence as its
   message. The preference is relaxed around the native call; the exit code is
   the failure signal, not stderr.
3. **An optional config value cannot be `-Default ''`.** `Get-ConfigValue`
   declares `[Parameter(Mandatory)][string]$Default`, which refuses an empty
   string, so optional keys read `$Config[...]` directly.

---

## Concurrent opencode workers contend on one local SQLite file **[opencode]**

**Symptom.** With several opencode workers running at once, one dies almost
immediately with `Error: Unexpected error  database is locked`. Raising
concurrency makes it more likely; the API itself never complains.

**Cause.** The limit is **local, not the provider's.** Opencode keeps session
state in SQLite at `~/.local/share/opencode/opencode.db`, and *every*
`opencode run` opens it — including runs started by other tools and other
sessions on the same machine. They contend for that one file no matter what the
service allows.

**Numbers** (measured by a sibling project on this machine, not by this kit's
own suite): the free tier served **24 concurrent tiny prompts with zero
failures**, throughput plateauing around 16. With realistic ~2,200-word prompts,
1 and 4 concurrent were clean and 8 produced one `database is locked` failure.
Roughly **4 concurrent per process is the clean zone**; note that two tools each
running 4 means 8 processes on the one file.

**Fix.** delekit gives every run its own `OPENCODE_DB`, which removes the
contention between delekit's own workers. The budget is still machine-wide for
anything *outside* the kit sharing the default store, so keep other concurrent
opencode use in mind. The failure is fast (~0.5s) and clears
immediately, so it is a retry rather than a wall:

- A collision **at spawn** dies before any event, so no `session_id` was ever
  captured and there is nothing to resume — re-spawn the task.
- A collision **on a resume** leaves the session intact, because `sessionID`
  arrives on the very first event of the original turn — `herd send <task>
  "continue"` retries it.

`herd` does not retry automatically. That is deliberate: a silent retry would
hide a real backend failure behind an identical-looking success, and herd's
contract is that a failed turn stays visible and resumable.

---

## `--variant` accepts anything and silently ignores what it doesn't know **[opencode]**

**Symptom.** A run reports the effort you asked for and behaves as though you
asked for nothing.

**Cause.** Opencode does not validate `--variant`. Measured on 1.18.15,
`--variant bogus-effort` exits 0, writes nothing to stderr, and returns a normal
answer.

**Fix.** delekit validates the value against the **selected model's declared
variants**, enumerated from `opencode models <provider> --verbose`; there is no
fixed vocabulary, and the sets genuinely differ per model. `--variant` is sent
**only when a run explicitly asks for an effort**. The
`DELEGATE_OPENCODE_VARIANT_*` keys in `config/models.env` are deliberately empty:
a pinned default would be a knob that silently does nothing on any model that
does not implement the tier.

---

## `OPENCODE_PERMISSION` fails open on the rule-array shape **[opencode]**

**Symptom.** A delegate labelled `read-only` creates files.

**Cause.** `opencode debug agent <name>` prints resolved permissions as a rule
array (`[{"permission":…,"action":…,"pattern":…}]`), which is the natural shape to
copy. `OPENCODE_PERMISSION` does **not** honor it: measured on 1.18.15, a run
denying `edit` and `bash` that way wrote the file anyway, via `bash` and
`apply_patch`, exit 0, no warning. The object form
(`{"edit":"deny","write":"deny","bash":"deny"}`) denies correctly, including
through a subagent spawned by the `task` tool.

**Fix.** Both runners send the object form, and `tests/test_delegate_backends.py`
plus `tests/test-dairy-runner.sh` assert it is a JSON object with a catch-all
`"*":"deny"`, the read/glob/grep allowlist, and the `mcp:*` deny nested after the
read allow — a mutation to the array form fails both suites. Do not extend the key
set without measuring: opencode ignores a permission value it cannot parse
rather than erroring, so an unrecognized key risks invalidating the whole policy.

---

## The runner does not use the gateway **[all]**

`dairy` shells out to the backend CLI with whatever environment it inherits. Run
from a normal terminal it uses your direct login; run from inside a gateway
session it inherits the proxy, and `--model` must then name something the
gateway serves (check `/v1/models`). This is by design — it is the fallback
path — but it surprises people who expect the runner to follow `ccg`.

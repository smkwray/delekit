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

The runner prepends one short access line and, when applicable, one worktree line. It does not send backend/model/log/report metadata to the model. Use `--no-preamble` or `-NoPreamble` when the task already carries the needed boundary.

## Profiles and central model mapping

Profiles are backend-specific and resolve through `config/models.env`. Codex uses `terra` (default), `luna`, and `sol`. Muse uses `spark` (default) = `muse-spark-1.2-contributor`, and like Codex takes a separate effort, so `--effort`/`-Effort` applies (muse accepts `none|minimal|low|medium|high|xhigh|ultra`; the profile default is `high`). Antigravity (`agy`) uses `flash-high` (default) = `gemini-3.6-flash-high`, `flash-low` = `gemini-3.6-flash-low`, and `pro-high` = `gemini-3.1-pro-high`. The agy slug carries the effort tier, so the runner sends no separate `--effort`. The old agy uses of `terra`, `luna`, and `sol` remain deprecated aliases for one migration window and are canonicalized in status output. `--model`/`-Model` (and `--effort`/`-Effort` for Codex and Muse) override a single run. Claude model choices remain explicit because its provider default is better handled by its own CLI; `agy models` lists valid `--model` slugs.

**Muse tokens are discounted in exchange for training rights.** The
`-contributor` model is priced down because the provider may use session content
for product improvement — the CLI says so at startup. Keep private or client
material on another backend.

## Examples

```bash
dairy write --profile terra --prompt-file task.md --worktree
dairy read --profile sol --prompt-stdin < audit.md
dairy read --backend agy --profile flash-high --prompt 'audit this checkout'
dairy write --backend muse --prompt-file task.md --worktree
dairy full --model explicit-provider-id --prompt 'authorized host task'
```

```powershell
dairy write -Profile terra -PromptFile task.md -Worktree
Get-Content audit.md -Raw | dairy read -Profile sol -PromptStdin
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

Codex `read-only` and `workspace-write` runs use the selected sandbox with `approval_policy=never`, so blocked operations fail instead of prompting. `danger-full-access` uses Codex's explicit bypass flag. Claude and Antigravity backends cannot reproduce every Codex sandbox boundary; use them only when their CLI's permission behavior is acceptable.

`agy` has no Codex-style per-command sandbox. In headless mode it soft-denies any tool that would otherwise prompt (`write_file`, shell commands) unless `--dangerously-skip-permissions` is set — and that flag is all-or-nothing and **not** filesystem-confined (it will write outside the workspace). So the runner supports only two `agy` access modes: `read-only` → `--mode plan` (tool writes are soft-denied, so the run stays read-only), and `full` → `--dangerously-skip-permissions` (unrestricted). **`agy` cannot honor `workspace-write`** — it has no confined write mode — so `dairy workspace --backend agy` is refused up front; use `readonly`, or `full` for explicit unrestricted writes, or use codex/claude when you need confined workspace writes. The prompt is passed as the value of `-p`, not on stdin.

`muse` has approval and a sandbox ON by default, so every access mode disables approvals (a headless run would otherwise block on them) and then differs in what it leaves standing. Measured against Muse 0.1.0 on macOS, not inferred from flag names:

| dairy access | muse flags | what it actually enforces |
| --- | --- | --- |
| `workspace-write` | `--disable-approval` | Muse's own sandbox stays on. A shell write to `$HOME` is **denied**; the workspace and temp dirs (`/tmp`) are writable — the same shape as Codex `workspace-write`. |
| `read-only` | `--disable-approval --disable-write --disable-shell` | Write tools are policy-denied (`tool policy denied filesystem write`) and the shell is gone. File reads still work. |
| `full` | `--yolo` | Unrestricted and **not** workspace-confined — a `$HOME` write succeeds. |

**`read-only` on muse costs the shell.** `--disable-write` alone blocks only the non-shell write tools; the shell can still redirect into a file, so an honest read-only has to drop `--disable-shell` too. The delegate can read files but cannot run `git log`, `rg`, or a test command. When a read-only muse task needs the shell, use codex instead — its sandbox denies writes without removing the shell.

The prompt is passed via `--prompt-file` (the composed prompt log), not on stdin.

**Computer use requires `full` mode.** Launching or scripting GUI apps — `open -a`, `osascript`/AppleScript, browsers — is blocked by the sandbox in `workspace-write` and `read-only`, so those tasks fail with permission errors rather than prompting. `full` runs unsandboxed; grant it only when the task genuinely needs the machine, and expect the worker to report every external effect (the access preamble instructs it to).

---

# Known issues

## `--profile` resolves only for backends mapped in `config/models.env` **[all]**

**Symptom.** `dairy … --backend claude --profile sol` would otherwise run on the
CLI's default model while the status JSON reports `"profile":"sol"`.

**Cause.** `config/models.env` maps backend-specific profiles to Codex IDs
(`DELEGATE_MODEL_*`), muse IDs (`DELEGATE_MUSE_MODEL_*`), and agy IDs
(`DELEGATE_AGY_MODEL_*`); Claude has no such mapping.

**Fix.** Both runners resolve profiles for the `codex`, `muse`, and `agy`
backends, and **fail** rather than guess for `--backend claude` — pass `--model`
explicitly there.

---

## The runner does not use the gateway **[all]**

`dairy` shells out to the backend CLI with whatever environment it inherits. Run
from a normal terminal it uses your direct login; run from inside a gateway
session it inherits the proxy, and `--model` must then name something the
gateway serves (check `/v1/models`). This is by design — it is the fallback
path — but it surprises people who expect the runner to follow `ccg`.

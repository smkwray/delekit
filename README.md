# delekit

Delegate coding work from Claude Code or the terminal, on macOS and Windows:

- **`tandy-*`** — ordinary Claude Code subagents running on GPT models, in the
  same session as a Claude parent, via a local
  [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) gateway.
  Interactive.
- **`dairy`** — a standalone CLI for one-shot, unattended, and CI jobs with
  Codex or Claude. No gateway required.
- **`herd`** — detached, resumable, steerable headless workers with Codex or
  Claude: spawn returns immediately, and you check on, steer, resume, or kill
  the worker later. No gateway, no Claude session. State is device-local and
  never synced.

Plus **`prune-worktrees`**, a safe cleaner for the isolated worktrees these
paths create.

```text
runtime-selected orchestrator (Opus, Sonnet, Fable — your choice)
        |
        +-- tandy-<profile>             one writer in the current checkout
        +-- tandy-<profile>-worktree    each simultaneous writer in its own worktree
        +-- tandy-<profile>-readonly    read-only analysis and shell inspection

profiles: terra (default), luna (fast/high-volume), sol (strongest)
```

Every capability exists for every profile, so `tandy-luna-readonly` and
`tandy-sol-worktree` are both valid. The agent definitions are capability
boundaries, not personas — the task message defines the work. Multiple workers
run under distinct runtime names, and the orchestrator can message or resume any
of them by agent ID.

`tandy` needs the gateway; `dairy`, `herd`, and `prune-worktrees` do not. If you
only want direct CLI execution, install the kit and skip the gateway steps.

## Why a gateway is required

Claude Code speaks the Anthropic Messages API, and `ANTHROPIC_BASE_URL` is
process-wide — a subagent cannot use a different endpoint from its parent. So
the whole session routes through one local gateway that serves both model
families at once. The trade-off: in a gateway session the claude.ai connectors
are unavailable, which is why the kit adds a *second* launcher rather than
replacing your existing one.

## Install

1. **Render and wire the kit**

   ```bash
   python3 tools/render_config.py
   ./bin/install-macos.sh            # or: .\bin\install-windows.ps1 -AddToUserPath
   ```

2. **Set up the gateway** — install CLIProxyAPI, fill
   `generated/cliproxy/config.template.yaml`, run both OAuth logins, and start
   the proxy at login. Full procedure: **[docs/gateway-setup.md](docs/gateway-setup.md)**.
   Platform specifics: [docs/macos.md](docs/macos.md), [docs/windows.md](docs/windows.md).

3. **Add the `ccg` launcher.** On Windows, `install-windows.ps1 -AddToUserPath`
   exposes the synced `bin/ccg.cmd` to both PowerShell and cmd.exe. On
   macOS/Linux, source `bin/ccg-snippet.sh`. Keep it *alongside* your normal
   direct launcher, then run the platform doctor to verify. On macOS/Linux,
   sourcing the sibling `global-agent-defaults/bin/agent-shell.sh` first also
   enables `ccgs`, an explicitly persistent `screen` variant; plain `ccg` stays
   nonpersistent.

Change models by editing `config/models.env` and re-rendering — never by editing
`generated/`.

## Documentation

| File | What it covers |
|---|---|
| [docs/gateway-setup.md](docs/gateway-setup.md) | The canonical per-device install |
| [docs/known-issues.md](docs/known-issues.md) | Traps that have actually bitten. Read before debugging |
| [docs/architecture.md](docs/architecture.md) | Design, prompt budget, why it stays maintainable |
| [docs/native-agents.md](docs/native-agents.md) | Delegating, resuming, smoke tests |
| [docs/dairy-runner.md](docs/dairy-runner.md) | The `dairy` CLI: modes, backends, known issues |
| [docs/detached-runner.md](docs/detached-runner.md) | The `herd` CLI: detached, resumable workers and their lifecycle |
| [docs/model-aliases.md](docs/model-aliases.md) | How aliases map to providers |
| [docs/permissions-and-stalls.md](docs/permissions-and-stalls.md) | Permission modes and avoiding hangs |
| [DEVICE-AGENT-INSTALL.md](DEVICE-AGENT-INSTALL.md) | A brief you can hand to a local coding agent to install this for you |

## Design notes

- The parent model is never hardcoded; `claudex` passes `--model` through
  unchanged, with an optional `DELEGATE_PARENT_MODEL` pin.
- Provider model IDs live in exactly one file, `config/models.env`. Agent files
  reference stable gateway aliases generated from it.
- The always-sent worker prompt is one short paragraph. The larger orchestration
  policy is an optional Skill, loaded only when invoked.
- Gateway aliases intentionally begin with `claude-`, because Claude Code filters
  gateway model discovery by that prefix.
- Credentials live in one local `device.env` per machine, never in the repo.
- Tandy defaults to the proven native 200k compact path. An isolated,
  undocumented `clientdata-272k` profile is opt-in and upgrade-gated; see
  [known issues](docs/known-issues.md).

Deliberately omitted: any large global `CLAUDE.md` block (subagents already
inherit the parent's hierarchy), and `CLAUDE_CODE_SUBAGENT_MODEL` (it would
override per-agent and per-invocation model selection).

## Compatibility

Routing Claude Code to non-Claude models through a third-party gateway is
outside Anthropic's supported model path. Use current Claude Code — the
per-invocation and resume behaviour this kit relies on requires **2.1.211 or
later**. Keep CLIProxyAPI current and re-run `tests/` plus the doctor script
after upgrading either.

## License

MIT — see [LICENSE](LICENSE).

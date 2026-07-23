# Gateway setup (per device)

This is the procedure that makes a **single Claude Code session able to mix model
families**: a Claude parent (Opus/Sonnet/Fable) that can spawn `tandy-*`
subagents running on the GPT models defined in `config/models.env`.

## Why a gateway is required

Claude Code speaks the Anthropic Messages API, and `ANTHROPIC_BASE_URL` is
process-wide. A subagent cannot use a different endpoint from its parent.
So to get non-Claude subagents, the **whole session** must route through one
local gateway (CLIProxyAPI) that serves both families at once:

```text
Claude Code (one session)
    -> http://127.0.0.1:8317   (CLIProxyAPI)
         |-- claude login  -> Opus / Sonnet / Fable
         |-- codex login   -> the delegate aliases (GPT models)
```

Consequence: in a gateway session the claude.ai connectors (Gmail/Drive MCP) are
disabled, because auth goes to the proxy instead of your claude.ai login. Keep a
second, non-gateway launcher for when you need those connectors.

## 1. Install CLIProxyAPI

Download the release for this platform from
<https://github.com/router-for-me/CLIProxyAPI/releases>, verify it against the
release `checksums.txt`, and unpack it to a local (not synced) directory.
Windows ships `cli-proxy-api.exe`; macOS/Linux ship `cli-proxy-api`.

Keep the binary and all credentials **outside** the synced kit.

## 2. Render this kit

```bash
python tools/render_config.py
```

This regenerates the agent files and writes
`generated/cliproxy/config.template.yaml` from `config/models.env`. That template
already contains the alias mapping and the catalog trimming; never hand-edit
`generated/` — change `config/models.env` and rerender.

Rendering only updates the synced kit. The proxy reads the per-device
`config.yaml` from step 3, so **every rerender that changes aliases or
exclusions must be followed by re-merging the fragment into that deployed
file** (it hot-reloads). Skipping the merge leaves the live catalog serving
the old aliases and every `tandy-*` spawn fails with `502 unknown provider`
— see [known-issues.md](known-issues.md).

## 3. Create the device config

Copy `generated/cliproxy/config.template.yaml` next to the binary as
`config.yaml`, then replace:

- `<AUTH_DIR>` with a local credential directory (Windows:
  `C:/Users/<you>/.cli-proxy-api`; macOS/Linux: `~/.cli-proxy-api`)
- `<CLIENT_KEY>` with a random string you generate locally, for example
  `python -c "import secrets;print('sk-local-'+secrets.token_hex(24))"`

Save that key: it is the token Claude Code presents to the proxy.
On Windows, use forward slashes in a double-quoted YAML path
(`C:/Users/<you>/.cli-proxy-api`). Backslashes introduce YAML escape sequences.

## 4. Authenticate the upstreams

Run once per provider, from the binary's directory:

```bash
cli-proxy-api -config config.yaml -codex-login  -no-browser   # GPT models
cli-proxy-api -config config.yaml -claude-login -no-browser   # Opus/Sonnet/Fable
```

Run both commands in a visible, interactive terminal. A login may ask you to
paste the final localhost redirect into that terminal even when the browser
opened normally. Let the Codex command finish before starting the Claude
command.

`-no-browser` prints an authorization URL instead of opening one. Visit it,
approve, and the local callback completes the login. Each writes a credential
JSON into `<AUTH_DIR>`; the running proxy watches that directory and hot-reloads.

Do not launch `ccg` until `<AUTH_DIR>` contains both a Codex credential and a
Claude credential. Codex authentication alone exposes the three delegate
aliases, but the Claude parent still fails with `502 unknown provider`.

The callback ports are fixed and must be free on the machine running the
browser: **1455** for Codex, **54545** for Claude. On macOS the credential JSONs
are written world-readable; follow with `chmod 600 <AUTH_DIR>/*.json`.

Use `-codex-device-login` instead if this machine has no browser.

## 5. Run the proxy, and start it at login

Start it with `cli-proxy-api -config config.yaml`.

To keep it running without a visible window:

- **Windows** — create a one-line launcher that runs the binary hidden and put a
  copy in the Startup folder (`shell:startup`). Build the command with
  `Chr(34)` for quoting rather than nested doubled quotes, which fails to parse.
- **macOS** — run `bin/install-launchd-macos.sh [PROXY_DIR]`. It writes and
  bootstraps a `launchd` user agent with `RunAtLoad`/`KeepAlive`, logs to
  `~/Library/Logs/cli-proxy-api.log`, and refuses a binary that lives inside a
  synced folder. `--uninstall` reverses it. See [macos.md](macos.md).
- **Linux** — use a systemd user unit or your usual supervisor.

## 6. Point Claude Code at the gateway

The installer creates a local `device.env` outside the synced kit. Fill it:

```text
ANTHROPIC_BASE_URL=http://127.0.0.1:8317
ANTHROPIC_AUTH_TOKEN=<the CLIENT_KEY from step 3>
CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1
CLAUDE_CODE_ATTRIBUTION_HEADER=0
ENABLE_TOOL_SEARCH=false
# Optional experimental Tandy profile; omit for the production 200k fallback:
# DELEKIT_TANDY_CONTEXT_MODE=clientdata-272k
```

The 272k mode requires `ANTHROPIC_AUTH_TOKEN` rather than `ANTHROPIC_API_KEY`.
It seeds undocumented, Sonnet-4.6-family metadata in an isolated Claude profile
and must be revalidated after every Claude Code update. It does not cap Opus
4.8, Fable 5, or Sonnet 5 `[1m]` parents; do not use a Sonnet 4.6 parent with
this mode. Remove the line to return immediately to native 200k compaction.
On verification, lower `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` only for a disposable
test session, require one agent ID plus a `compact_boundary` and a post-boundary
tool call, then unset the override. Never persist the test override.

## 7. Install the kit and the launcher

Run `bin/install-windows.ps1` or `bin/install-macos.sh` to link the generated
agents into the normal Claude configuration directory and the isolated
`delekit/claude-profile` used by the optional 272k mode.

Then add a **separate** launcher so your normal one stays direct-to-Anthropic.

- Windows: run `bin/install-windows.ps1 -AddToUserPath`; the synced
  `bin/ccg.cmd` works in PowerShell and cmd.exe.
- bash/zsh: `bin/ccg-snippet.sh`

Both define `ccg`, which loads `device.env` for that launch only and leaves the
calling shell's environment unchanged: PowerShell saves and restores each
variable, and the POSIX version is a subshell function — `ccg() ( ... )`. Do not
"simplify" it to a brace body; the gateway variables would then persist and
every later plain `claude` in that terminal would keep routing through the proxy
without saying so. Keep your existing alias untouched as the non-gateway
fallback.

## 8. Verify

```bash
curl -s http://127.0.0.1:8317/v1/models -H "Authorization: Bearer <CLIENT_KEY>"
```

Expect only the trimmed catalog: the latest Opus, the latest Sonnet, Fable, and
the three delegate aliases. Then check routing end to end:

```bash
curl -s http://127.0.0.1:8317/v1/messages \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" \
  -H "x-api-key: <CLIENT_KEY>" \
  -d '{"model":"claude-sonnet-4-6-tandy-terra","max_tokens":32,"messages":[{"role":"user","content":"Say: ok"}]}'
```

The response `model` field should report the requested
`claude-sonnet-4-6-tandy-*` alias. This is intentional: `force-mapping` keeps
the client-visible identity stable so Claude Code recognizes the Sonnet 4.6
family and uses native preflight compaction. The full alias still selects the
GPT model at the proxy. Use the proxy debug log to confirm that the request
actually reached the upstream GPT model. Repeat with a Claude model ID to
confirm the other channel.

Finally open a new terminal, run `ccg`, and ask the session to use
`tandy-terra-readonly`. The subagent runs on the GPT profile while the parent stays
on Claude. The subagent's reply alone does not prove this — the parent could
have answered itself. To confirm the split, append `debug: true` to the proxy's
`config.yaml` (it hot-reloads), rerun, and check that the log shows both the
parent's Claude model and the delegate's upstream provider model. Remove the
line afterward.

Expect a warning on every gateway launch that claude.ai connectors are disabled
because another auth source takes precedence. That is the documented trade-off
from the top of this document, not a misconfiguration.

## Troubleshooting

- **Empty model list right after a restart** — the catalog loads a moment after
  the port opens. Retry before concluding the config is wrong.
- **"bind: only one usage of each socket address"** — an instance is already
  running; stop it before starting another.
- **A model you expect is missing** — check `CLIPROXY_EXCLUDE_*` in
  `config/models.env`, then rerender and restart.
- **`502 unknown provider` for the parent after delegate aliases appear** — the
  Codex login completed but the Claude login did not. Confirm that `<AUTH_DIR>`
  contains credentials for both providers, then start a new `ccg` session.
- **Subagent model not found** — the session was not launched through `ccg`, so
  it never reached the gateway.

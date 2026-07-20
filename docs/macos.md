# macOS specifics

The install procedure itself is in [gateway-setup.md](gateway-setup.md). This
file covers only what differs on macOS.

## Wire the kit

```bash
chmod +x bin/*.sh tests/*.sh
./bin/install-macos.sh
```

Creates:

```text
~/.claude/agents/delegate-kit                  symlink to generated agents
~/.claude/skills/orchestrate-delegates         symlink to the generated Skill
~/.local/bin/claudex                           symlink to the launcher
~/.config/delegate-kit/device.env              local-only gateway settings
```

Ensure `~/.local/bin` is on `PATH`. The installer does not modify shell
profiles.

Because the agents are symlinked, `python3 tools/render_config.py` alone
propagates a model change — no reinstall, just a new session. Use
`./bin/install-macos.sh --copy` only where directory symlinks are unsuitable;
copies must be reinstalled after each render.

If `~/.claude/agents` did not exist before the install, start one new Claude
Code session so the directory is picked up.

## Installing CLIProxyAPI

Apple Silicon takes the `darwin_aarch64` release asset; Intel takes
`darwin_amd64`. Always verify against the release `checksums.txt`:

```bash
grep darwin_aarch64 checksums.txt | shasum -a 256 -c -
```

The Go release is ad-hoc linker-signed, and a `curl` download carries no
`com.apple.quarantine` attribute, so Gatekeeper does not block it. If you
download through a browser instead, clear the attribute deliberately:

```bash
xattr -d com.apple.quarantine <path>/cli-proxy-api
```

Credentials land in the auth directory world-readable. Tighten them every time:

```bash
chmod 700 ~/.cli-proxy-api && chmod 600 ~/.cli-proxy-api/*.json
```

## Running the proxy at login

`launchd` replaces the Windows Startup-folder step:

```bash
./bin/install-launchd-macos.sh [PROXY_DIR]     # default: ~/.local/share/cli-proxy-api
./bin/install-launchd-macos.sh --uninstall
```

It writes `~/Library/LaunchAgents/com.router-for-me.cli-proxy-api.plist` with
`RunAtLoad` and `KeepAlive`, logs to `~/Library/Logs/cli-proxy-api.log`, and
boots any existing agent out first so a rerun replaces the running instance
rather than colliding on the port. It refuses to supervise a binary inside a
cloud-sync folder, because an evicted placeholder makes launchd fail while the
install still looks complete.

```bash
launchctl print gui/$(id -u)/com.router-for-me.cli-proxy-api | grep -E 'state|pid'
lsof -nP -iTCP:8317 -sTCP:LISTEN
```

## Adding `ccg`

Source the snippet from `~/.zshrc`, pointing at wherever you cloned the kit:

```bash
_ccg_snippet="/path/to/claude-delegate-kit/bin/ccg-snippet.sh"
[ -r "$_ccg_snippet" ] && . "$_ccg_snippet"
unset _ccg_snippet
```

The `[ -r ... ]` guard matters if the kit lives on a cloud-sync mount: an
unhydrated file then leaves the shell starting normally, with only `ccg`
missing.

`ccg` must stay a **subshell** function — `ccg() ( … )`, not `ccg() { … }` — or
it leaks the gateway token into your terminal. Verify on any machine you set up:

```bash
zsh -ic 'ccg --version >/dev/null 2>&1; echo "${ANTHROPIC_BASE_URL:-unset}"'   # -> unset
```

## Verify

```bash
./bin/doctor-macos.sh
```

Every line should read `OK`, including the delegate aliases. Extract the client
key with a parser rather than `tr -d ' -"'`, which strips the hyphens out of the
key itself:

```bash
KEY=$(python3 -c "import re,pathlib;print(re.search(r'^api-keys:\n\s+- \"([^\"]+)\"',(pathlib.Path.home()/'.local/share/cli-proxy-api/config.yaml').read_text(),re.M).group(1))")
```

## Expected noise

- **"claude.ai connectors are disabled because ANTHROPIC_API_KEY or another auth
  source is set"** on every gateway launch. This is the documented trade-off, not
  a fault. Use your direct launcher when you need those connectors.
- A `404 | HEAD "/"` line in the proxy log at session start — Claude Code probes
  the base URL and the proxy serves no root route.

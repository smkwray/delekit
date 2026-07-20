# Known issues and traps

Every entry here was hit and fixed on a real device. Read this before
debugging anything; most "the kit is broken" reports are one of these.

Platform tags: **[all]**, **[posix]** (macOS/Linux/git-bash), **[macos]**,
**[windows]**.

---

## A `/model` pick inside a gateway session breaks every other session **[all]**

**Symptom.** After using `ccg`, an unrelated launch fails with *"There's an issue
with the selected model (claude-delegate-…). It may not exist or you may not
have access to it."* Your normal launcher, a bare `claude`, and
and any headless runner that shells out to `claude` all break at once.

**Cause.** Claude Code persists a `/model` choice into the **global user
settings file** (`~/.claude/settings.json`, `%USERPROFILE%\.claude\settings.json`),
not into the session. Gateway aliases exist only behind the proxy, so every
direct-to-Anthropic launch afterwards asks Anthropic for a model it has never
heard of.

**Fix.** Pin the parent model per launcher, so each is self-consistent whatever
the settings file holds:

- Gateway launcher: set `DELEGATE_PARENT_MODEL` in the local `device.env`.
  `claudex` injects it as `--model` unless you passed `--model` yourself.
- Non-gateway launcher: pin its own model on its command line the same way.

Repair a poisoned settings file by setting `"model"` back to a real Anthropic ID
(or deleting the key).

**Do not** solve this by picking delegate aliases from `/model`. The alias
belongs in agent frontmatter, where the render already puts it.

---

## `ccg` leaking gateway credentials into the shell **[posix]**

**Symptom.** A plain `claude` in the same terminal silently keeps routing
through the proxy long after `ccg` exited.

**Cause.** A brace-body function (`ccg() { … }`) with `set -a` exports
`ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` into the calling shell.

**Fix.** `bin/ccg-snippet.sh` defines a **subshell** function — `ccg() ( … )`.
Never convert it to a brace body. The PowerShell snippet solves the same problem
by saving and restoring each variable in a `finally` block.

Verify on every device:

```bash
zsh -ic 'ccg --version >/dev/null 2>&1; echo "${ANTHROPIC_BASE_URL:-unset}"'   # -> unset
```

---

## Agent names versus model aliases **[all]**

Three names, one path, and only the first is ever typed:

```text
tandy-<profile>[-capability]   agent name        <- you use this
claude-delegate-<profile>      gateway model ID  <- only in agent frontmatter
<provider model id>            upstream model    <- only in config/models.env
```

The alias must begin with `claude-` or Claude Code's gateway discovery filters it
out of the catalog. A bare `delegate-<profile>` is **not** a valid name for
anything. The Agent tool's `model` parameter rejects gateway aliases, which is why
the profile has to be baked into the agent name.

---

## A delegate's own reply does not prove which model ran **[all]**

The orchestrator can answer in the delegate's voice. To actually verify the
split, add `debug: true` to the proxy's `config.yaml` (it hot-reloads), run
again, and read the request log: parent requests carry the Claude model ID,
delegate requests carry the upstream provider ID from `generated/model-map.tsv`.
Remove the line afterwards. Never report a working gateway without this check.

---

## "claude.ai connectors are disabled" on every gateway launch **[all]**

Expected, not a fault. Gateway auth replaces your claude.ai login for that
session, so the Gmail/Drive MCP connectors go away. This is precisely why the
kit insists you keep a second, non-gateway launcher.

A `404 | HEAD "/"` line in the proxy log at session start is also normal —
Claude Code probes the base URL and the proxy serves no root route.

---

## OAuth logins need free local ports and a local browser **[all]**

The login subcommands block on a **localhost callback**: Codex on **1455**,
Claude on **54545**. Both must be free, and the browser must run on the same
machine. `-no-browser` prints the URL instead of opening one; open it yourself
(`open "<url>"` on macOS, `Start-Process "<url>"` on Windows) when driving the
install from a non-interactive shell. Use `-codex-device-login` where no browser
exists at all.

---

## Credential file permissions **[posix]**

The login writes credential JSON into the auth directory **world-readable**.
Tighten it every time:

```bash
chmod 700 ~/.cli-proxy-api && chmod 600 ~/.cli-proxy-api/*.json
```

---

## Never put the proxy in the synced folder **[all]**

The binary, its `config.yaml` (which carries the client key), and the auth
directory are per-device and must stay local. A synced binary can be an evicted
cloud placeholder at boot, so the supervisor fails while the install still looks
complete. `bin/install-launchd-macos.sh` refuses a path under a known sync root
for exactly this reason.

---

## Extracting the client key **[posix]**

`tr -d ' -"'` strips the hyphens out of `sk-local-…` and yields "Invalid API
key". Parse the YAML instead:

```bash
KEY=$(python3 -c "import re,pathlib;print(re.search(r'^api-keys:\n\s+- \"([^\"]+)\"',(pathlib.Path.home()/'.local/share/cli-proxy-api/config.yaml').read_text(),re.M).group(1))")
```

---

## Empty model list right after a restart **[all]**

The catalog loads a moment after the port opens. Retry before concluding the
config is wrong. A genuinely missing model is usually a `CLIPROXY_EXCLUDE_*`
pattern in `config/models.env` — edit there, rerender, restart.

---

## A misspelled spawn parameter silently downgrades a delegate **[all]**

**Symptom.** A delegate appears to run, but on the session's own model, in the
main checkout, with no worktree. Nothing errors.

**Cause.** The Agent tool ignores unrecognised parameters. `subject_type:
tandy-sol-worktree` therefore does not fail — `subagent_type` is simply never
set, and the spawn falls back to a generic subagent. The transcript looks
normal.

**Fix.** `bin/hooks/verify-delegate-spawn.py` is a PreToolUse hook that denies
this shape. Register it against the `Agent` matcher:

```json
{"hooks": {"PreToolUse": [{"matcher": "Agent", "hooks": [
  {"type": "command", "command": "python3 /path/to/bin/hooks/verify-delegate-spawn.py"}]}]}}
```

It runs two independent checks, because whether the harness strips unknown keys
before hooks run is undocumented: it denies a near-miss key name if one is
visible, **and** denies any spawn whose prompt names a delegate while
`subagent_type` is generic. The second check holds either way.

**Verifying by hand.** Ask the delegate to state its model. A real one answers
with its gateway alias (`claude-delegate-*`); a silent fallback answers with the
session model. The proxy request log is the independent witness — if no upstream
provider request appears, no delegate ran.

Note the contrast: naming the *right* parameter in a non-gateway session fails
**loudly** ("issue with the selected model"). Only the parameter-name mistake is
silent.

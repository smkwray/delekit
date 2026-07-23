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

## Every model shows a 200k context limit through the gateway **[all]**

**Symptom.** `/context` in a `ccg` session reports "482.6k/200k tokens (241%)"
in red for a model that is 1M direct-to-Anthropic. The label follows the model
(switching Opus↔Fable mid-session moves the name but not the 200k), so it looks
like a per-model or proxy limit. It is neither.

**Cause.** With a custom `ANTHROPIC_BASE_URL`, Claude Code does not apply its
built-in per-model context windows; every model gets a flat 200k budget. The
proxy is not the limiter — it forwards the `context-1m` beta header untouched
(a 482k session keeps getting HTTP 200 upstream).

**Fix.** Use the `[1m]`-suffixed model ID, which restores the 1M budget for a
1M-capable Claude model. These exact IDs were verified on 2026-07-21:

- `claude-opus-4-8[1m]`
- `claude-fable-5[1m]`
- `claude-sonnet-5[1m]`

Set one durably as `DELEGATE_PARENT_MODEL` in the local `device.env`, or select
the suffixed ID per session. Short aliases such as `/model fable[1m]` also
expand to the full ID. The picker's plain "From gateway" rows stay 200k.

The suffix is client-side only: Claude Code strips it from the wire and sends
the clean ID plus the `context-1m` beta header. Verify with `/context` — expect
the suffixed ID and "/1m tokens". A literal `model[1m]` sent to the proxy
(e.g. via curl) 502s as an unknown model; that is expected and does not mean
the fix failed.

**Trap.** The delegate aliases route to non-Claude upstreams, so `[1m]` does
not apply to them; their windows are whatever the provider grants the upstream
model.

---

## A `[1m]` parent that compacts at 650k (or any sub-1M number) **[all]**

**Symptom.** `/model fable[1m]` (or any `[1m]` ID) is accepted, `/context`
still reports the 1M window, but the auto-compact line shows a smaller number —
e.g. "Auto-compact window: 650k tokens" — and the session compacts there.
Meanwhile another launcher appears to get the full window, which makes it look
like a launcher or gateway difference. It is neither.

**Cause.** The `/autocompact <tokens>` command persists `autoCompactWindow`
into the **global user settings file** (`~/.claude/settings.json`,
`%USERPROFILE%\.claude\settings.json`), exactly like `/model` does (see the
first entry above). That value caps the compact window of **every** later
session on any model, including `[1m]` parents. The model's context window is
untouched — only compaction fires early, so the meter looks right until you
notice the auto-compact line. `claudex` deliberately unsets the equivalent
environment variable (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`) but cannot drop a
settings-file key; this is the remaining silent cap. Hit and fixed 2026-07-23:
a forgotten `/autocompact 650000` capped `claude-fable-5[1m]` at 650k.

**Fix.** Run `/autocompact auto`, or delete the `autoCompactWindow` key from
the settings file. In the isolated 272k profile the same key lives in
`delekit/claude-profile/settings.json` instead. The doctor scripts flag a
sub-1M cap whenever `DELEGATE_PARENT_MODEL` carries `[1m]`.

---

## The deployed proxy config silently drifts from the rendered template **[all]**

**Symptom.** After an alias change in `config/models.env` plus a rerender, a
`tandy-*` spawn dies with *"502 unknown provider for model
claude-sonnet-4-6-tandy-…"* while the parent keeps working, and `/v1/models`
still lists the **old** alias names.

**Cause.** `tools/render_config.py` rewrites `generated/` inside the synced
kit, but the proxy reads a per-device `config.yaml` next to the binary — which
the kit intentionally never touches (it carries the client key). Step 2→3 of
the gateway setup is a manual copy; every later rerender needs the same manual
re-merge, and nothing failed loudly until a delegate was actually spawned.
Hit 2026-07-23: the alias rename in `d67c83d` (`claude-delegate-*` →
`claude-sonnet-4-6-tandy-*`, adding `force-mapping`) was rendered but never
merged into the live config, so every Tandy spawn 502'd.

**Fix.** After **every** rerender that changes aliases or exclusions, merge
`generated/cliproxy/oauth-model-alias.yaml` (and the exclusion block from
`generated/cliproxy/config.template.yaml`) into the deployed `config.yaml`.
CLIProxyAPI hot-reloads it; no restart needed. Then verify `/v1/models` serves
the aliases `config/models.env` names. The doctor scripts now fail (not warn)
when a configured alias is missing from the live catalog.

---

## Tandy reaches the provider limit without compacting **[all]**

**Symptom.** A long `tandy-*` run ends with *"Your input exceeds the context
window of this model"*. Its transcript has no `compact_boundary`, even though
Claude Code documents automatic compaction for subagents.

**Cause.** Claude Code gives an unknown gateway alias a nominal 200k window but
leaves it on reactive compaction: it waits for the provider to report a
recognized prompt-too-long error. CLIProxyAPI translates the Codex context
overflow to an ordinary HTTP 400 with *"Your input exceeds the context
window"*. Claude Code does not classify that combination as prompt-too-long,
so the subagent ends instead of compacting. The visible 200k `/context` budget
alone is therefore not proof that a custom subagent will compact.

**Fix.** Tandy's client aliases use the recognized
`claude-sonnet-4-6-tandy-*` family. Claude Code consequently applies its native
200k Sonnet 4.6 preflight path and compacts before the provider rejects the
request. Every Codex OAuth alias also sets `force-mapping: true` so responses
retain the full client-visible alias. In a live native test, the same Tandy
subagent compacted from 21,538 to 2,636 tokens under a deliberately lowered
test threshold, continued working, and ended normally.

The production fallback remains the proven 200k path. An opt-in
`DELEKIT_TANDY_CONTEXT_MODE=clientdata-272k` profile seeds undocumented Claude
Code client-data/cache fields, giving canonical Sonnet 4.6 both a 272k assumed
maximum and a proactive 272k compact window. The launcher isolates that state
under the local `delekit/claude-profile`, requires token authentication, and
removes the process-wide context variables. Opus 4.8, Fable 5, and Sonnet 5
`[1m]` parents use other canonical families and retain their full windows.

This is family-scoped, not alias-scoped: a Sonnet 4.6 parent in the isolated
profile will also compact at 272k. It relies on the internal
`kelp_forest_sonnet`, `rowan_thicket`, and `autoCompactWindowsCache` fields and
must be revalidated after every Claude Code update. Claude Code 2.1.217 on
macOS was verified to show `29k/272k`, a 33k reserve, and an `auto (272k)`
window for `claude-sonnet-4-6-tandy-luna`. A live CLIProxyAPI/native-agent test
then produced one automatic boundary from 26,337 to 2,218 tokens in agent
`af037d0e90b92b144`; that same agent executed a Bash tool after the boundary and
returned its preserved nonce plus `TANDY_DONE`. The parent remained Opus 4.8
`[1m]`. This transcript proof—not the meter alone—is the acceptance gate.

There are intentionally nine Tandy definitions: Terra, Luna, and Sol, each in
current-checkout writer, isolated-worktree writer, and readonly form. Those
capability suffixes change permissions/isolation only; all nine canonicalize to
Sonnet 4.6 and therefore share this compact-window behavior. The renderer test
requires that exact set, so a stale or extra definition fails validation.

Do not substitute `CLAUDE_CODE_MAX_CONTEXT_TOKENS=272000`; an unknown alias
still stays reactive. Do not set `CLAUDE_CODE_AUTO_COMPACT_WINDOW=272000`; it
is process-wide and caps the 1M parent too. A synthetic overflow immediately
after only one tool/result group also cannot compact; Claude Code needs enough
compactable history.

After an alias or proxy update, verify `/v1/models` contains all three
`claude-sonnet-4-6-tandy-*` aliases and that a fresh native Tandy transcript
records a `compact_boundary` and continues in the same agent. Existing failed
agents keep the old model identity and oversized history; start a fresh agent.
To roll back the experiment, remove `DELEKIT_TANDY_CONTEXT_MODE` from the local
`device.env`; the ordinary 200k profile is unchanged.

---

## Subagents that fall back to Haiku 502 through the gateway **[all]**

**Symptom.** A subagent dies with *"502 unknown provider for model
claude-haiku-4-5-…"* in a gateway session, while the parent keeps working.

**Cause.** `CLIPROXY_EXCLUDE_CLAUDE` deliberately drops `*haiku*` to keep the
`/model` picker short, but the exclusion also removes it from routing. Some
built-in agents and small/fast-model fallbacks request Haiku implicitly.

**Fix.** The default profile omits Haiku. If a workflow requires it, remove
`*haiku*` from `CLIPROXY_EXCLUDE_CLAUDE` in `config/models.env`, rerender, and
restart the proxy.

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

Run the login itself in a visible, interactive terminal. Some releases ask for
the final callback URL on stdin. A still-running login process with no new JSON
in the auth directory is waiting, not authenticated. Codex authentication alone
exposes the delegate aliases; without a Claude credential, the parent fails with
`502 unknown provider`.

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

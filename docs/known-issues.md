# Known issues and traps

Every entry here was hit and fixed on a real device. Read this before
debugging anything; most "the kit is broken" reports are one of these.

Platform tags: **[all]**, **[posix]** (macOS/Linux/git-bash), **[macos]**,
**[windows]**.

## Start here — the ones that bite most

| Symptom | Entry |
|---|---|
| A delegate ran on the wrong model, silently | [A misspelled spawn parameter silently downgrades a delegate](#a-misspelled-spawn-parameter-silently-downgrades-a-delegate-all) · [A delegate's own reply does not prove which model ran](#a-delegates-own-reply-does-not-prove-which-model-ran-all) |
| Sessions broke after picking a model in-session | [A `/model` pick inside a gateway session breaks every other session](#a-model-pick-inside-a-gateway-session-breaks-every-other-session-all) |
| Context limit looks wrong (200k, or compacts early) | [Every model shows a 200k context limit](#every-model-shows-a-200k-context-limit-through-the-gateway-all) · [A `[1m]` parent that compacts at 650k](#a-1m-parent-that-compacts-at-650k-or-any-sub-1m-number-all) |
| Tandy dropped from 272k to 200k | [`/login` inside a `ccg` session silently kills the 272k Tandy window](#login-inside-a-ccg-session-silently-kills-the-272k-tandy-window-all) |
| 502s through the gateway | [A brand-new Anthropic model 502s](#a-brand-new-anthropic-model-502s-through-the-gateway-all) · [Subagents that fall back to Haiku 502](#subagents-that-fall-back-to-haiku-502-through-the-gateway-all) |
| Config changes had no effect | [The deployed proxy config silently drifts from the rendered template](#the-deployed-proxy-config-silently-drifts-from-the-rendered-template-all) |
| Models missing from `/v1/models` | [Models disappear as you test them](#models-disappear-from-v1models-as-you-test-them-all) · [Empty model list right after a restart](#empty-model-list-right-after-a-restart-all) |

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

## A brand-new Anthropic model 502s through the gateway **[all]**

**Symptom.** A just-released Claude model works in a direct (`ccc`) session but,
selected in a `ccg` session, every turn fails with *"502 unknown provider for
model claude-opus-5"* (retrying 1/10…). `/v1/models` on the proxy does not list
it. Hit 2026-07-24 with **Opus 5** on release day.

**Cause.** CLIProxyAPI's Claude catalog is **not** discovered from your account —
it is a fixed list: an embedded `models.json` plus a remote feed
(`raw.githubusercontent.com/router-for-me/models`, refreshed every 3h, which
overrides the embedded copy). Both lag new Anthropic models — even the same-day
release binary (v7.2.98) lacked `claude-opus-5`. The OAuth Claude channel has no
config-level way to add a model (`oauth-model-alias.claude` only aliases an
already-served model; there is no include-list), so the router rejects the
unknown model before it ever reaches Anthropic. `ccc` works because it talks to
Anthropic directly, bypassing the proxy's catalog.

**Fix.** Build a patched proxy binary that re-injects the missing model into the
catalog after every load (embedded and each remote refresh), cloning an existing
sibling so it inherits the current context window / thinking budget and — by
keeping `type: "claude"` — routes to your Claude credential:

```bash
bin/build-cliproxy-opus5.sh          # macOS: clones the pinned tag, applies the
                                     # patch, go-builds, installs, restarts launchd
```

```powershell
bin\build-cliproxy-opus5.ps1         # Windows: same, for the versioned install
                                     # layout and the Startup .vbs launcher
```

The change is `patches/cliproxy-claude-opus-5.patch` (a new `bridge_models.go`
plus two one-line call sites). It keeps the normal remote refresh on, so all
other models still update; the injected entry is a no-op the moment the upstream
catalog adds the model. **To bridge the next new model**, add one
`ensureClonedModel(...)` line to `ensureBridgeModels` in the patch. **To retire
it**, once a stock CLIProxyAPI release lists the model, reinstall a stock binary
and delete the patch + both scripts.

The patch itself is platform-neutral (pure Go, `internal/registry/` only, no
cgo) — it applied unmodified to a `v7.2.98` checkout on Windows and builds with
`go build ./cmd/server`. Only the *wrapper* differs, in three ways the `.ps1`
handles and the `.sh` does not:

- **Version-pinned install dir.** Windows keeps the proxy in
  `%LOCALAPPDATA%\CLIProxyAPI\<tag>\`, so a tag bump is a *new directory*, not
  an in-place binary swap.
- **`config.yaml` must be carried forward.** It lives beside the binary and
  holds the client key, so a new version directory starts without it. `auth-dir`
  points at the version-independent `..\auth`, so credentials survive untouched.
- **No launchd.** A hidden `.vbs` in the Startup folder pins the absolute binary
  and config paths; it has to be rewritten for the new directory. Build the
  command with `Chr(34)` for quoting (see [windows.md](windows.md)).

Verified on Windows 2026-07-24: `v7.2.98-opus5-bridge`, `/v1/models` lists
`claude-opus-5`.

**Reading the verification correctly.** A `502 unknown provider` is the
bridge failing. A **401 `OAuth access token has been revoked`**, or
`auth_unavailable: no auth available (providers=claude, model=claude-opus-5)`,
is the bridge *working* — the proxy resolved the model to the Claude provider
and forwarded it upstream, and the **credential** is what failed. Confirm by
calling a model that predates the patch (`claude-opus-4-8`): if it fails the
same way, the patch is fine and the Claude OAuth token needs re-issuing. A
Codex-backed `claude-sonnet-4-6-tandy-*` alias still answering proves the proxy
itself is healthy. Re-authenticate with:

```powershell
& "$env:LOCALAPPDATA\CLIProxyAPI\<tag>\cli-proxy-api.exe" -claude-login -config "$env:LOCALAPPDATA\CLIProxyAPI\<tag>\config.yaml"
```

Note that an OAuth login on another device can revoke this device's token for
the same account — a gateway that worked yesterday and 401s today, on *every*
Claude model at once, usually means exactly that rather than a proxy fault.

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

`DELEKIT_TANDY_CONTEXT_MODE=clientdata-272k` is the **default**, and
`native-200k` is the fallback. The 272k mode seeds undocumented Claude Code
client-data/cache fields, giving canonical Sonnet 4.6 both a 272k assumed maximum
and a proactive 272k compact window. The launcher isolates that state under the
local `delekit/claude-profile`, requires token authentication, and removes the
process-wide context variables. Opus 4.8, Fable 5, and Sonnet 5 `[1m]` parents use
other canonical families and retain their full windows.

This is family-scoped, not alias-scoped: a Sonnet 4.6 parent in the isolated
profile will also compact at 272k. It relies on the internal
`kelp_forest_sonnet`, `rowan_thicket`, and `autoCompactWindowsCache` fields. The
thing that actually breaks it is a first-party credential landing in the profile,
not a Claude Code upgrade — the bundle logic for these fields was byte-identical
across 2.1.217 and 2.1.219. The launcher now enforces the invariant on every run
(see the `/login` entry above), so treat an unexplained drop to 200k as profile
pollution first. Claude Code 2.1.217 on
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

## `/login` inside a `ccg` session silently kills the 272k Tandy window **[all]**

**Symptom.** Tandy delegates that were compacting at 272k drop to 200k and stay
there. Re-seeding turns the doctor green but the next session is back to 200k.
Another device on the same kit and Claude Code version still works, so it looks
like a version or platform difference. It is neither. Hit 2026-07-24 on bthc.

**Cause.** The seed only holds while the isolated profile has **no first-party
credential**. Claude Code treats these launches as `firstParty` (its internal
`"gateway"` mode is gated on `CLAUDE_CODE_USE_GATEWAY`, which the launchers
deliberately do not set — `ANTHROPIC_BASE_URL` is not consulted). Without a saved
credential the first-party bootstrap fetch skips and nothing touches the seed.
`/login` inside a gateway session writes one into the profile, which switches the
bootstrap writer on; it then overwrites `autoCompactWindowsCache` with the
server's value **once per model switch**. The `clientDataCache` fields survive, so
the damage looks partial and the doctor's cache check keeps passing.

**Fix — automatic.** `tools/seed_claude_context_cache.py` quarantines any
credential it finds and strips the cached identity keys, so every `claudex` launch
in `clientdata-272k` mode restores the invariant and prints one line when it acts.
Nothing to do by hand. See that file's docstring for the mechanism and
`tests/test_seed_context_cache.py` for the enforced behaviour.

**Never run `/login` in a `ccg` session.** If the gateway is refusing Claude
models, the credential to renew is **CLIProxyAPI's**, not Claude Code's — use the
proxy binary's `-claude-login` (see the two entries above).

**Trap.** `OK 272k cache` proves only that the seed is *present* — it stayed green
throughout this failure. The acceptance gate is a live transcript with a
`compact_boundary`, the same agent id, and a tool call after the boundary.

---

## Models disappear from `/v1/models` as you test them **[all]**

**Symptom.** `/v1/models` lists a model; you call it; the call fails on auth; the
model is then **gone from the next `/v1/models`**. Test a few models and the
catalog shrinks each time, so it looks like the catalog is corrupting itself — or,
right after deploying the Opus 5 bridge, like the patch is being undone by the
3-hourly remote refresh. It is neither. Hit 2026-07-24 on bthc.

**Cause.** CLIProxyAPI puts a model's auth into **cooldown** when an upstream
call fails, and a model with no available auth stops being advertised. So the
catalog is not shrinking at random — it is shrinking in exactly the order you
exercised the models. One dead credential therefore looks like progressive
catalog decay.

**Fix.** Fix the credential, then **restart the proxy** — the cooldown is
in-memory, and a restart republishes the full catalog. (Verified: a catalog down
to 4 models came back to 7, including `claude-opus-5`, on restart alone.)

**Do not** use this symptom to diagnose the bridge. The decisive test is to call
a **stock catalog model that predates the patch** — `claude-fable-5` did the same
thing, which proves the mechanism has nothing to do with the injected entry.
Conversely, a Codex-backed `claude-sonnet-4-6-tandy-*` alias keeps working
throughout, because it authenticates against a different credential — that
contrast is the fastest way to tell "one credential is dead" from "the proxy is
broken".

---

## Empty model list right after a restart **[all]**

The catalog loads a moment after the port opens. Retry before concluding the
config is wrong. A genuinely missing model is usually a `CLIPROXY_EXCLUDE_*`
pattern in `config/models.env` — edit there, rerender, restart. A model that
vanished *after* you called it is the cooldown entry above, not this one.

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

It runs three independent checks. The first two guard the `subject_type` typo,
because whether the harness strips unknown keys before hooks run is undocumented:
it denies a near-miss key name if one is visible, **and** denies any spawn whose
prompt names a delegate while `subagent_type` is generic. The second check holds
either way. The third denies a per-invocation `model` on a `tandy-*` spawn: the
Agent tool's `model` slot only takes built-in names (sonnet/opus/haiku/fable) and
outranks the agent's frontmatter alias, so a value there would silently route the
delegate off its gateway profile.

**Verifying by hand.** Ask the delegate to state its model. A real one answers
with its gateway alias (`claude-delegate-*`); a silent fallback answers with the
session model. The proxy request log is the independent witness — if no upstream
provider request appears, no delegate ran.

Note the contrast: naming the *right* parameter in a non-gateway session fails
**loudly** ("issue with the selected model"). Only the parameter-name mistake is
silent.

# Known issues and traps

Every entry here was hit and fixed on a real device. Read this before
debugging anything; most "the kit is broken" reports are one of these.

Platform tags: **[all]**, **[posix]** (macOS/Linux/git-bash), **[macos]**,
**[windows]**.

## Start here — the ones that bite most

| Symptom | Entry |
|---|---|
| A delegate ran on the wrong model, silently | [A `model` on a delegate spawn silently reroutes it](#a-model-on-a-delegate-spawn-silently-reroutes-it-all) · [A misspelled spawn parameter silently downgrades a delegate](#a-misspelled-spawn-parameter-silently-downgrades-a-delegate-all) · [A delegate's own reply does not prove which model ran](#a-delegates-own-reply-does-not-prove-which-model-ran-all) |
| A delegate ran at the wrong reasoning effort | [`effort: xhigh` on a subagent (clamp fixed 2026-08)](#effort-xhigh-on-a-subagent-was-silently-high-fixed-all) |
| A subagent needs more than 200k of context | [Native 1M delegates](#native-1m-delegates-opus5-1m-and-fable5-1m-all) |
| Spawns refused with none running; `herd` still works | [Delegation stops mid-session at 200 spawns](#delegation-stops-mid-session-at-200-spawns-all) |
| A setting you disabled globally is back in `ccg` | [The gateway profile does not inherit `~/.claude/settings.json`](#the-gateway-profile-does-not-inherit-claudesettingsjson-all) |
| Sessions broke after picking a model in-session | [A `/model` pick inside a gateway session breaks every other session](#a-model-pick-inside-a-gateway-session-breaks-every-other-session-all) |
| Context limit looks wrong (200k, or compacts early) | [Every model shows a 200k context limit](#every-model-shows-a-200k-context-limit-through-the-gateway-all) · [A `[1m]` parent that compacts at 650k](#a-1m-parent-that-compacts-at-650k-or-any-sub-1m-number-all) |
| Fable has no 1M row in `/model` | [Fable has no 1M row in the picker — type it instead](#fable-has-no-1m-row-in-the-model-picker--type-it-instead-all) |
| `502 unknown provider for model fable-5` | [Type the ID exactly](#every-model-shows-a-200k-context-limit-through-the-gateway-all) |
| Tandy dropped from 272k to 200k | [`/login` inside a `ccg` session silently kills the 272k Tandy window](#login-inside-a-ccg-session-silently-kills-the-272k-tandy-window-all) |
| 502s through the gateway | [A brand-new Anthropic model 502s](#a-brand-new-anthropic-model-502s-through-the-gateway-all) · [Subagents that fall back to Haiku 502](#subagents-that-fall-back-to-haiku-502-through-the-gateway-all) |
| Config changes had no effect | [The deployed proxy config silently drifts from the rendered template](#the-deployed-proxy-config-silently-drifts-from-the-rendered-template-all) |
| `ccg` prompts for permissions, or auto mode blocks tool calls | [Windows `ccg` did not run in bypass permissions](#windows-ccg-did-not-run-in-bypass-permissions-windows) |
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

**Type the ID exactly.** Only three spellings reach 1M Fable: the full
`claude-fable-5[1m]`, the bare family alias `fable[1m]`, and (200k) the plain
`claude-fable-5`. `fable-5[1m]`, `fable-[1m]`, and `fable-5-[1m]` are **not**
aliases — Claude Code passes them through untouched and the proxy answers
`502 unknown provider for model fable-5`. The 502 names the model *after* suffix
stripping, so the error text never shows the `[1m]` you typed and reads like a
gateway fault. It is a typo: only the bare family word (`fable`, `opus`,
`sonnet`, `haiku`) takes a version-less short alias.

The suffix is client-side only: Claude Code strips it from the wire and sends
the clean ID plus the `context-1m` beta header. Verify with `/context` — expect
the suffixed ID and "/1m tokens". A literal `model[1m]` sent to the proxy
(e.g. via curl) 502s as an unknown model; that is expected and does not mean
the fix failed.

**Trap.** The delegate aliases route to non-Claude upstreams, so `[1m]` does
not apply to them; their windows are whatever the provider grants the upstream
model.

---

## Fable has no 1M row in the `/model` picker — type it instead **[all]**

**Symptom.** `/model` lists one `Claude Fable 5 — From gateway` row and no "(1M
context)" row beside it, while Opus and Sonnet 5 each get both. Picking the
Fable row gives a 200k session. Hit 2026-08-01.

**Workaround — the only one that works.** Type the model instead of picking it:
`/model fable[1m]` (or `claude-fable-5[1m]`). Better, set it once as
`DELEGATE_PARENT_MODEL` in the local `device.env` so every `ccg` launch starts
there and the picker never comes up.

**Cause — two client-side rules, and the second is unfixable from the proxy.**
Behind a gateway the picker is built purely from `/v1/models` (the built-in
Opus/Sonnet 1M rows render only for a first-party base URL), and "(1M context)"
is added only to an ID *literally ending in* `[1m]`. The bridge patch therefore
clones `claude-fable-5` to `claude-fable-5[1m]` so both IDs are advertised —
necessary, but **not sufficient**:

Claude Code applies a **Fable-specific dedup rule** that collapses any two
`claude-fable-5` IDs into a single row, *ignoring* the `[1m]` suffix, and keeps
whichever it encounters first. Opus and Sonnet use the general comparator, which
respects the suffix — which is exactly why they get two rows and Fable gets one.
The rule matches every real Fable spelling (dated, `-v`, `[1m]`, `[2m]`,
`anthropic.`-prefixed), so no alternate ID escapes it while still *being* Fable.

Ordering cannot rescue it either: Claude Code **reorders the catalog** when it
writes `<profile>/cache/gateway-models.json`, so the proxy's order does not
survive (verified — the proxy emits `[1m]` first and the cache still records
plain first). Under any string ordering the plain ID sorts before the suffixed
one anyway, since it is a prefix. Nor can the plain ID simply be dropped: the
client strips `[1m]` before the wire, so plain is the ID actually sent, and
`oauth-excluded-models` removes routing along with the listing.

**What the clone still buys.** `claude-fable-5[1m]` is a valid, routable
catalog entry, so typing it (or `fable[1m]`) resolves and gives a real 1M
session. Only the *picker row* is lost. Keep the clone; it costs nothing and is
what makes the typed ID work.

**Verifying.** The suffixed ID is for the *client*, not for curl: calling
`claude-fable-5[1m]` directly fails with `auth_unavailable` (no upstream model
by that name) and puts that entry into the cooldown described below, so it
disappears from the next `/v1/models` until the proxy restarts. Verify through
Claude Code instead — `claude --model 'claude-fable-5[1m]' -p ...` — then
confirm with `/context` that it reports `/1m tokens`.

**Re-test after a Claude Code upgrade.** If the Fable dedup special-case is
dropped or taught about `[1m]`, the second row appears on its own with no proxy
change — the catalog entry is already there.

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

The same mechanism serves a second purpose: cloning a served model to its
`[1m]`-suffixed ID, so that ID is routable and `/model claude-fable-5[1m]`
resolves. It does **not** get Fable a 1M row in the picker — a client-side
dedup rule collapses that row regardless; see
[the picker entry](#fable-has-no-1m-row-in-the-model-picker--type-it-instead-all).
The clone is additive and must stay additive — the plain ID is what actually
goes on the wire.

**When editing the patch, fix the hunk header.** `@@ -0,0 +1,N @@` must equal the
number of added lines. Adding lines without bumping `N` truncates the file at
apply time and the build fails with a bare `syntax error: unexpected EOF` in
`bridge_models.go` — which reads like a Go mistake, not a patch mistake.

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

## Windows `ccg` did not run in bypass permissions **[windows]**

**Symptom.** `ccg` on Windows prompts for permissions, and in auto mode tool
calls fail outright with *"claude-opus-5[1m] is temporarily unavailable, so auto
mode cannot determine the safety of Bash right now."* The same `ccg` on macOS
never prompts. Hit 2026-07-25.

**Cause.** Two entry points, one behavior, only one of them implemented it.
`bin/ccg-snippet.sh` execs `claudex --dangerously-skip-permissions`; the Windows
chain (`ccg.cmd` → `ccg-launch.ps1` → `claudex.ps1`) passed the user's arguments
straight through, so the session fell back to whatever the profile's
`settings.json` held. Auto mode then made every Bash call depend on a classifier
round-trip to the session model *through the gateway* — one upstream blip or
proxy hiccup and the tool call is blocked rather than merely slow.

**Fix.** `ccg-launch.ps1` injects `--dangerously-skip-permissions` unless the
caller already passed it or an explicit `--permission-mode` (Claude Code rejects
the two together, so `ccg --permission-mode plan` must stay untouched). A test in
`tests/test_render_config.py` asserts both entry points carry the flag. Verify by
shimming `claude` on `PATH` and reading the recorded argv — the launch line
should read `--model <parent> --dangerously-skip-permissions …`.

Do not treat this as the fix for a real classifier failure: if `ccc` sessions
also report a model unavailable, check `/v1/models` and probe `/v1/messages`
first (see the 502 entries above).

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

## The gateway profile does not inherit `~/.claude/settings.json` **[all]**

**Symptom.** A setting turned off once in `~/.claude/settings.json` is still
active in every `ccg` session. Hit 2026-07-30 with commit attribution: the
`Co-Authored-By:` trailer was disabled globally, yet gateway sessions kept adding
it to commits.

**Cause.** `clientdata-272k` mode points `CLAUDE_CONFIG_DIR` at
`<device>/delekit/claude-profile` (`bin/claudex.sh`, `bin/claudex.ps1`). Settings
are read from *that* directory, and it is a **separate profile, not an overlay** —
the installer creates it empty, so it inherits nothing from `~/.claude`. Anything
configured in the normal profile has to be repeated here. The isolation is
deliberate (it is what keeps first-party credentials out; see the `/login` entry),
so this is a consequence of the design rather than a bug in it.

**Fix.** `tools/seed_claude_context_cache.py` writes
`attribution: {commit: "", pr: ""}` into the profile on every launch, and drops
the deprecated `includeCoAuthoredBy` key, which conflicts with it. An explicit
non-empty string is left alone, so a deliberate choice still wins.

For any *other* setting you want in gateway sessions, add it to the profile's own
`settings.json`. Only correctness-critical settings belong in the seeder;
preferences (theme, voice, plugins) are reasonable to diverge.

**Trap.** `~/.claude/settings.json` is still the file that matters for `ccc` and a
bare `claude`. The two profiles drift independently, and neither reports the
other's values, so "I already turned that off" is not evidence for the profile you
are actually running in. Check with `echo $CLAUDE_CONFIG_DIR`.

---

## `effort: xhigh` on a subagent was silently `high` — fixed **[all]**

> **Fixed upstream; re-measured 2026-08-06 on Claude Code 2.1.223.** With the
> parent pinned to `--effort low`, a subagent declaring `effort: xhigh` now puts
> `xhigh` on the wire, so it is neither clamped nor inherited. The native
> profiles use it (`opus5-1m` runs `xhigh`). `DELEGATE_EFFORT_*` stays
> restricted to low/medium/high/max, because those keys are *also* passed to the
> `dairy`/`herd` backend CLIs, where the same word means something else — that
> restriction is about the shared vocabulary, no longer about this clamp. The
> history below is kept because the measurement method is what makes it
> re-checkable after the next upgrade.

**Original symptom.** An agent file carries `effort: xhigh`. Nothing errors, the
spawn works, and the file keeps reading `xhigh` forever — but the delegate
reasons at `high`. Hit 2026-07-30 while evaluating a move to `xhigh` for the luna
and terra profiles.

**Cause.** Claude Code clamped `xhigh` to `high` **on the subagent path**. This
was not a frontmatter parsing fault and not the gateway:

- The parent honours it. `claude -p --effort xhigh` puts `"effort": "xhigh"` on
  the wire, and the CLI validates the vocabulary (`--effort bogus` warns *"Valid
  values: low, medium, high, xhigh, max"*).
- The clamp follows the *path*, not the source. With the parent at `xhigh` and a
  delegate declaring **no** effort at all, the parent request carries `xhigh`
  while the delegate request in the same session carries `high`.
- It is specific to this one value. Measured with a logging pass-through in front
  of the proxy, parent pinned at `medium`:

  ```text
  frontmatter          delegate request
  (absent)      ->     medium     inherits the parent
  low           ->     low
  high          ->     high
  xhigh         ->     high       <- clamped
  max           ->     max
  ```

**Fix at the time.** Do not write `xhigh` in an agent file.
`tools/render_config.py` rejects it in `DELEGATE_EFFORT_*` so this fails at
render time instead of silently downgrading. Use `high`, or `max` when a profile
genuinely warrants it. **Superseded** — see the note at the top of this entry;
`DELEGATE_NATIVE_EFFORT_*` accepts `xhigh` today.

**The 2026-08-06 re-measurement.** Same method, parent pinned `low`, subagent
requests separated from parent ones by the `X-Claude-Code-Agent-Id` header
(without that split the two are easy to confuse — a parent at `xhigh` makes
every row look like `xhigh`):

```text
frontmatter          delegate request
xhigh         ->     xhigh      <- no longer clamped
max           ->     max
medium        ->     medium
```

**Where effort actually rides.** `output_config.effort`, not a top-level field —
grepping a request body for `"effort"` at the top level finds nothing and looks
like effort was dropped entirely. `CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1` (the
launcher default) is what gets it sent for custom gateway aliases at all.

**Re-test after a Claude Code upgrade.** It was a bug, and it was fixed — so the
same check now guards against it *returning*. The check is a pass-through logger
reading `output_config.effort` on the delegate's request, split from the parent's
by `X-Claude-Code-Agent-Id`; the delegate's own answer cannot tell you, exactly
as with the model identity below.

**Scope.** This is the `tandy-*` path only. `dairy` and `herd` shell out to the
backend CLI with `-c model_reasoning_effort=<level>`, where `xhigh` is accepted
normally — so the same word means two different things on the two paths, which is
why `DELEGATE_EFFORT_*` is restricted to values that are safe for both.

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

## A `model` on a delegate spawn silently reroutes it **[all]**

**Symptom.** A delegate spawns, works, and reports normally — on the wrong
model, billed to the wrong account, and without the worktree it asked for.
Nothing errors. Hit 2026-07-29; found 2026-08-06 while investigating something
else.

**Scale when it went unnoticed.** One session, 200 spawns, **42 of them
misrouted**:

```text
 97  tandy-sol            spawn model=(none)  -> claude-sonnet-4-6-tandy-sol
 24  tandy-terra          spawn model=(none)  -> claude-sonnet-4-6-tandy-terra
 23  tandy-sol-readonly   spawn model=(none)  -> claude-sonnet-4-6-tandy-sol
 15  tandy-sol-worktree   spawn model=sonnet  -> claude-sonnet-5      <- misrouted
 15  tandy-sol            spawn model=sonnet  -> claude-sonnet-5      <- misrouted
 11  tandy-sol-readonly   spawn model=sonnet  -> claude-sonnet-5      <- misrouted
```

**Cause.** Model resolution is *per-invocation model* > *agent-file alias* >
*parent*. The Agent tool's `model` field accepts only built-in names
(sonnet/opus/haiku/fable), never a gateway alias — so any value there outranks
the delegate's whole reason for existing. `claude-sonnet-4-6-tandy-*` is
`owned_by=openai` and force-mapped to a GPT model on Codex quota;
`claude-sonnet-5` has no alias entry and routes to the Claude OAuth credential.
The 16 `-worktree` spawns also lost their isolation and wrote to the main
checkout.

**Why it stayed invisible.** The `tandy-*` name appears in the transcript, the
UI, and the agent list either way. Only the wire model differs. A delegate's own
reply cannot settle it (see below), and the `model: sonnet` field reads like a
deliberate choice rather than an override.

**Fix.** `bin/hooks/verify-delegate-spawn.py` check C denies any spawn that
names a kit-managed agent *and* sets `model`. The hook existed when this
happened — it had simply never been registered, so it protected nothing.
`tools/seed_claude_context_cache.py` now registers it into the gateway profile
on every launch, and `tools/verify_kit.py` fails when the live profile lacks it.
A hook that is documented but unwired is not a control.

**Confirming it yourself.** The spawn parameters and the wire model are recorded
separately, so they can be compared after the fact:

```bash
D=<profile>/projects/<project>/<session-id>/subagents
python3 - <<'PY'
import json, glob, re, collections
rows = collections.Counter()
for meta in glob.glob("$D/*.meta.json"):
    d = json.load(open(meta))
    wire = set()
    for line in open(meta.replace(".meta.json", ".jsonl"), errors="ignore"):
        wire |= set(re.findall(r'"model":"([^"]*)"', line))
    rows[(d.get("agentType"), d.get("model"), tuple(sorted(wire)))] += 1
for k, v in rows.most_common(): print(v, k)
PY
```

A row whose `agentType` is a delegate but whose wire model is not that
delegate's alias is a misroute. The proxy request log is the independent
witness: a genuine tandy call reaches the Codex upstream, a misrouted one does
not appear there at all.

---

## Native 1M delegates: `opus5-1m` and `fable5-1m` **[all]**

A subagent cannot borrow its parent's context window, so a 1M parent still gets
200k delegates. These profiles exist for the case where the *input* is the
problem — a file set or transcript too large for a 200k worker:

```text
opus5-1m[-readonly|-worktree]    claude-opus-5[1m]     effort xhigh
fable5-1m[-readonly|-worktree]   claude-fable-5[1m]    effort medium
```

They are **not** `tandy-*`, deliberately: `tandy` means Codex-backed throughout
this kit and in the `dairy`/`herd` profile vocabulary, and these run Anthropic
models on Claude quota. Reach for one when the context genuinely demands it, not
for bulk delegation.

**The `[1m]` suffix is load-bearing and client-side.** Claude Code strips it
before the wire but uses it to select the 1M window, so `claude-opus-5` and
`claude-opus-5[1m]` are the same upstream model with different client
behaviour — a bare `--model opus` resolves to the plain id and gives a silent
200k session. Both ids must therefore exist in the catalog;
`patches/cliproxy-claude-opus-5.patch` clones them. Unlike Fable, whose picker
rows Claude Code collapses with a family-specific dedup, Opus uses the general
comparator, so the Opus clone does produce a real "(1M context)" row.

Verify with `--effort low` on the parent and a wire capture: the delegate's
request should carry its own model and effort, not the parent's.

---

## A misspelled spawn parameter silently downgrades a delegate **[all]**

**Symptom.** A delegate appears to run, but on the session's own model, in the
main checkout, with no worktree. Nothing errors.

**Cause.** The Agent tool ignores unrecognised parameters. `subject_type:
tandy-sol-worktree` therefore does not fail — `subagent_type` is simply never
set, and the spawn falls back to a generic subagent. The transcript looks
normal.

**Fix.** `bin/hooks/verify-delegate-spawn.py` is a PreToolUse hook that denies
this shape. `tools/seed_claude_context_cache.py` registers it into the gateway
profile on every `claudex` launch, so no manual step is needed; it adds to any
existing `PreToolUse` list rather than replacing it, and repoints itself if the
kit moves. To register it in another profile by hand:

```json
{"hooks": {"PreToolUse": [{"matcher": "Agent", "hooks": [
  {"type": "command", "command": "python3 /path/to/bin/hooks/verify-delegate-spawn.py"}]}]}}
```

Registration is the whole control: for five days this hook existed, was
documented here, and was wired into nothing — during which 42 delegates ran off
-profile (see the entry above). `tools/verify_kit.py` now fails when
`CLAUDE_CONFIG_DIR` names a profile that lacks it.

It runs three independent checks. The first two guard the `subject_type` typo,
because whether the harness strips unknown keys before hooks run is undocumented:
it denies a near-miss key name if one is visible, **and** denies any spawn whose
prompt names a delegate while `subagent_type` is generic. The second check holds
either way. The third denies a per-invocation `model` on a spawn that names any
kit-managed agent: the Agent tool's `model` slot only takes built-in names
(sonnet/opus/haiku/fable) and outranks the agent's frontmatter alias, so a value
there would silently route the delegate off its profile.

The managed set is read from `generated/claude/agents/` at hook time (via
`DELEKIT_ROOT`, exported by the launchers), so adding or renaming a profile does
not need a second list updated here — and the unprefixed native agents
(`opus5-1m`, `fable5-1m`) are covered by the same checks as `tandy-*`.

**Verifying by hand.** Ask the delegate to state its model. A real one answers
with its gateway alias (`claude-delegate-*`); a silent fallback answers with the
session model. The proxy request log is the independent witness — if no upstream
provider request appears, no delegate ran.

Note the contrast: naming the *right* parameter in a non-gateway session fails
**loudly** ("issue with the selected model"). Only the parameter-name mistake is
silent.

---

## Delegation stops mid-session at 200 spawns **[all]**

**Symptom.** Partway through a long session every `Agent` spawn is refused with
*"Subagent spawn limit reached (200 of 200 agents spawned)"* — including the
first spawn of a given kind, and with **no delegates running**. `herd`/`dairy`
delegation still works, so the session concludes the native path is broken for
whichever model it happened to try next.

**Cause.** `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION` (default 200) caps *lifetime*
spawns, not concurrent ones. The counter increments once per spawn and is never
decremented, so finished delegates keep occupying their slot forever. It also
increments *before* the agent launches, so a delegate that dies immediately — a
502 on a stale alias, say — still consumes a slot permanently.

Two things make this read as a model-specific failure when it is not:

- The concurrency cap is a **different** check with a different message
  (*"Concurrent subagent limit reached"*, `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`,
  default 20). Seeing the lifetime message while nothing is running is expected.
- `herd`/`dairy` keep working because they are external CLI processes launched
  through the shell, not `Agent` spawns. They never touch the counter. So the
  contrast between "native spawn fails, herd works" says nothing about models.

**Fix.** The launchers set `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION=1000`, with
`device.env` free to override. This cannot rescue a session already in progress —
the value is read at launch — so relaunch. Concurrency is unaffected, so the
higher ceiling does not increase parallel load or rate-limit pressure.

**Confirming it rather than guessing.** Each spawn writes a transcript pair into
the session's `subagents/` directory, and the `.json` sidecar names the agent
type. Count them:

```bash
D=~/.config/delekit/claude-profile/projects/<project>/<session-id>/subagents
ls "$D"/*.jsonl | wc -l            # 200 means the cap is real, not spurious
python3 -c "import json,glob,collections;print(collections.Counter(json.load(open(f))['agentType'] for f in glob.glob('$D/*.json')))"
```

**`/clear` is not a workaround.** It does reset the counter, but only when no
non-exempt task entries remain registered; leftover background-agent entries
block the reset. It also discards the conversation, which is usually the thing
worth keeping.

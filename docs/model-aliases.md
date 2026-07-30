# Model aliases and upgrades

## Stable contract

The kit exposes three client-facing profiles, named for their models rather than
for a speed or mode:

| Profile | Config key | Intended use |
|---|---|---|
| terra | `DELEGATE_ALIAS_TERRA` | normal implementation and analysis (default) |
| luna | `DELEGATE_ALIAS_LUNA` | mechanical or high-volume work |
| sol | `DELEGATE_ALIAS_SOL` | difficult architecture, debugging, or challenge |

Actual provider model IDs live only in the corresponding `DELEGATE_MODEL_*` lines. Generated files may repeat them as outputs, but no source template or launcher pins them.

## Change a model

1. Edit one `DELEGATE_MODEL_*` line in `config/models.env`.
2. Run `python tools/render_config.py`.
3. Merge the refreshed generated alias fragment into CLIProxyAPI.
4. Restart the proxy.
5. Run the doctor and native-agent smoke tests.

No agent, Skill, prompt convention, or launcher needs editing.

## Change an alias

Aliases are the kit's internal API. Keep them stable where possible. When changing one, rerender, update the proxy, restart Claude Code, and update any natural-language convention that mentions the old alias.

The native Tandy aliases use the `claude-sonnet-4-6-tandy-*` namespace even
though their provider models are GPT. Claude Code canonicalizes that prefix to
the recognized Sonnet 4.6 family and therefore runs native preflight compaction
for the subagent's 200k client window. CLIProxyAPI still routes the full alias
to the `DELEGATE_MODEL_*` target, and `force-mapping` preserves the alias in the
response so Claude Code does not lose that identity.

Do not replace this with an unknown gateway alias merely to change the context
meter. Unknown aliases use reactive compaction. The optional
`clientdata-272k` mode keeps this recognized alias and seeds the canonical
Sonnet 4.6 maximum and compact window inside a dedicated Claude profile. That
scope is why Opus 4.8, Fable 5, and Sonnet 5 `[1m]` parents remain unaffected;
a Sonnet 4.6 parent would not. A process-wide `CLAUDE_CODE_AUTO_COMPACT_WINDOW`
would cap every parent and is removed by the launcher.

The default aliases begin with `claude-`. Claude Code's gateway model discovery currently filters `/v1/models` and adds only IDs beginning with `claude` or `anthropic` to `/model`.

## OAuth versus API keys

CLIProxyAPI's top-level `oauth-model-alias` applies to OAuth/login channels. It does not configure `codex-api-key` credentials. API-key credentials need equivalent entries in their own `models:` list. The renderer emits one fragment for each connection type.

Use unique client-visible aliases. Duplicate names across upstreams can make routing and diagnostics ambiguous.

## Model resolution

The launcher removes `CLAUDE_CODE_SUBAGENT_MODEL`. Current Claude Code then resolves a custom subagent model in this order:

1. per-invocation model supplied by the orchestrator;
2. the agent file's `model:` alias;
3. the parent model.

The per-invocation choice persists when that worker receives follow-up messages or resumes.

## Effort

Agent files carry `effort:` from `DELEGATE_EFFORT_<PROFILE>` in
`config/models.env` — the same keys the headless runners use, so `tandy`,
`dairy`, and `herd` cannot disagree about what a profile means. Change the effort
where you change the model, then rerender.

They previously omitted it, on the theory that a delegate would inherit the
session effort unless the orchestrator chose one per invocation. The Agent tool
has no `effort` parameter, so that second half was never true: every profile
simply ran at whatever the session was set to, and the profile choice moved only
the model.

`xhigh` is rejected at render time. Claude Code clamps it to `high` for
subagents, so an agent file naming it would report an effort it never sends — see
[known-issues.md](known-issues.md). Effort travels as `output_config.effort`.

The launcher defaults `CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1`, which tells current Claude Code to send effort for custom gateway model IDs while still excluding known incompatible Claude models. Override it in the local `device.env` only after testing a provider that rejects the parameter.

## Gateway cache and tool search

The launcher defaults:

```text
CLAUDE_CODE_ATTRIBUTION_HEADER=0
ENABLE_TOOL_SEARCH=false
```

Omitting the changing attribution block improves gateway prompt-cache reuse. Tool search stays off because a non-first-party `ANTHROPIC_BASE_URL` requires the gateway to forward Anthropic `tool_reference` blocks. After a compatibility test, set `ENABLE_TOOL_SEARCH=true`, `auto`, or `auto:N` in the local `device.env` if reducing upfront MCP schemas is valuable.


## Concurrency

The launcher deliberately does not set `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`. Claude Code currently defaults to 10 parallel read-only tools and subagents, while an appropriate cap depends on provider quotas, proxy capacity, machine load, and the workload. Set it only in the local `device.env` after observing actual contention; do not bake it into the synced model map.

## Gateway compatibility fallback

If a proxy rejects Anthropic beta headers or beta tool-schema fields, try this only in the local `device.env`:

```text
CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
```

This is a compatibility escape hatch, not a default. It also forces all MCP tools to load up front and disables MCP tool search, so remove it after the gateway is fixed.

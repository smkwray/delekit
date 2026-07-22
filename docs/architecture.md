# Architecture

## Decision

Use **ordinary custom Claude Code subagents routed through CLIProxyAPI** for interactive orchestration. Keep the standalone runner as a fallback.

```text
runtime-selected Claude orchestrator
        |
        +-- tandy-<profile>             shared checkout; one writer
        +-- tandy-<profile>-worktree    isolated checkout; each concurrent writer
        +-- tandy-<profile>-readonly    read-only analysis and shell inspection

profiles: terra (default), luna (fast/high-volume), sol (strongest)
```

Each invocation has a fresh context and an agent ID. Several instances of one definition can run under unique names. The delegation message supplies all task semantics, so there are no hardcoded review, rescue, implementation, or research modes.

## Why these definitions

They are capability boundaries rather than personas, and the same three are
generated for every model profile:

- `tandy-<profile>`: broad normal tools for the only writer in a checkout.
- `tandy-<profile>-worktree`: the same behavior with native `isolation: worktree`.
- `tandy-<profile>-readonly`: file tools plus read-only `Bash` under `permissionMode: plan`; no source edits.

Capability is expressed statically; the model profile is chosen by picking the
agent, because the Agent tool's `model` parameter cannot take a gateway alias.

Isolation and a read-only tool allowlist are worth expressing statically. Model strength, task role, runtime name, foreground/background execution, and the actual assignment remain dynamic. Current Claude Code usually backgrounds subagents, while still allowing the orchestrator to block when it needs an immediate result.

## Lean prompt budget

Each worker receives one short paragraph containing only universal rules: stay in scope, use non-interactive commands, report blockers, avoid external side effects, and summarize completion. Writers also exclude nested `Agent` calls and all MCP tools by default, keeping delegation shallow and avoiding repeated connector schemas.

The fuller coordination policy is an optional Skill. It enters the orchestrator's context only when invoked. Do not copy it into a global `CLAUDE.md`: ordinary custom subagents inherit the full CLAUDE.md hierarchy that the parent session loads.

## Model indirection

```text
config/models.env
        |
        +-- rendered Claude agent files using stable aliases
        +-- rendered CLIProxyAPI aliases using provider model IDs
```

The parent model is outside this map. `claudex --model opus`, `claudex --model fable`, or `/model` selects the orchestrator. A worker uses its agent default unless the orchestrator supplies another stable alias for that invocation.

Stable aliases begin with `claude-` because Claude Code gateway discovery currently includes only model IDs beginning with `claude` or `anthropic`.

## Steering and resumption

The orchestrator keeps each returned agent ID and uses `SendMessage` for course corrections and follow-on work. Current Claude Code can resume completed workers and workers stopped by `TaskStop` under the same ID without enabling agent teams. A manually cancelled worker may first need a message typed into its transcript.

A message is task direction, not authorization. It cannot approve a permission prompt or change the worker's permission settings.

## Permission shape

Normal writers use `acceptEdits`, never `bypassPermissions`. Background permission prompts surface in the parent session on current Claude Code versions.

## Unsupported integration boundary

Anthropic documents gateways that expose compatible API formats, but routing Claude Code to non-Claude models depends on a third-party translation layer. CLIProxyAPI must preserve the evolving Messages, tools, streaming, effort, and subagent protocol. Treat upgrades as integration changes and run the validation checklist.

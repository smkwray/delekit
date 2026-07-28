# Native-agent operation

## Basic delegation

```text
Use tandy-terra-readonly in the background. Name it parser-fix. Give it the parser bug,
owned files, acceptance criteria, and exact validation. Keep its agent ID.
```

The definition is generic. The assignment can be implementation, investigation, testing, documentation, review, or any combination.

## Multiple arbitrary workers

```text
Use three named workers:
- api-worker: tandy-terra-worktree, owns server/api/**
- ui-worker: tandy-terra-worktree, owns web/components/**
- test-audit: tandy-terra-readonly, inspects coverage only

Use the default profile unless the work clearly warrants fast or deep. Keep
all IDs, send changed requirements to the existing worker, and integrate the
two worktrees serially after review.
```

Use hub-and-spoke coordination through `main` by default. Workers can receive a sibling roster when `SendMessage` is available and agents are named, but the roster is a startup snapshot and peer negotiation can blur ownership.

## Per-invocation model profile

The profile comes from the agent definition's `model:` frontmatter, which may name
a gateway alias. Claude Code honors that alias: a spawned `tandy-terra-readonly`
reaches the default profile while its parent stays on the session model, and both
appear as separate upstream requests at the proxy.

The Agent tool's `model` parameter is a **different** mechanism and only accepts
built-in names (sonnet, opus, haiku, fable). A gateway alias cannot be passed
there, so a profile is selected by spawning the agent type that carries it:

```text
Use tandy-sol for the concurrency fix.
Use tandy-luna for the mechanical rename.
```

A generic subagent has no alias in its definition and therefore inherits the
session model. If delegated work appears to run on the parent's model, check that
a `tandy-*` agent type was actually requested. `claude-delegate-*` names a
gateway model, never an agent; see [known-issues.md](known-issues.md).

Provider model IDs should not appear in task prompts. Invoke `/orchestrate-delegates` when the orchestrator needs the exact current alias names in context.

## Course correction

```text
Send parser-fix this update: the public API must remain backward-compatible.
Reconcile it with current work instead of spawning a replacement.
```

A running worker treats the message as task direction. A completed worker or one stopped with `TaskStop` auto-resumes under the same ID. A worker may not process the message until its current tool call returns.

## Hung command

Ask the orchestrator to:

1. use `TaskStop` on the worker;
2. send the same agent ID a non-interactive replacement command;
3. continue with the existing context.

Avoid manual `x` cancellation when automatic resumption matters. After manual cancellation, open the worker transcript and type a follow-up to clear the stop.

## Worktrees

Native `isolation: worktree` uses Delekit's scoped `WorktreeCreate` hook to place each checkout at `<project>/.worktrees/<name>`, starting from current committed `HEAD`. The project must list `.worktrees/` in its root `.gitignore`; otherwise creation fails before making a branch or directory.

Uncommitted parent files are not inherited. Commit a checkpoint first, provide a patch explicitly, or keep the task in the current checkout. Delekit preserves Claude's `.worktreeinclude` behavior for explicitly listed ignored inputs; use it narrowly and never copy secrets casually.

Worktrees prevent live filesystem collisions, not merge conflicts. Give concurrent writers disjoint ownership and integrate serially.

## Why ordinary subagents instead of agent teams

Ordinary custom subagents now provide named workers, concurrent execution, permission prompts in the parent session, `SendMessage`, resumption, and worktree isolation. Agent teams add a shared task protocol and direct teammate coordination, but also add experimental surface area that is unnecessary for a strong central orchestrator.

## Smoke tests

In a fresh session, confirm a delegate runs and can be resumed:

```text
Use tandy-terra-readonly in the background. Name it smoke-test. Have it report
this repository's root and test layout without changing anything. Keep its
agent ID.
```

```text
Send smoke-test a follow-up asking it to identify the narrowest test command,
preserving the same agent context and ID.
```

In a disposable Git repository, confirm isolation:

```text
Use tandy-terra-worktree, name it worktree-smoke, create one temporary text file,
report the worktree and branch, and do not merge or discard it.
```

Confirm any permission prompt names the requesting worker. Review and clean up
the disposable worktree deliberately.

## Cleaning up worktrees

Native `tandy-*-worktree`, `dairy --worktree`, and `herd --worktree` all use
`<project>/.worktrees/<name>`. Changed worktrees remain for deliberate review
because their work may be unmerged. They accumulate.

`prune-worktrees` removes only the finished ones, deterministically. A worktree
is removed only when its commits are already in the main branch **and** it has no
uncommitted tracked changes, no idle activity in the last 30 minutes, and no
untracked files beyond known build cruft (`.venv`, `__pycache__`, …). Everything
else is kept with the reason printed.

```bash
prune-worktrees                 # dry run from the current repo
prune-worktrees --apply         # remove the finished ones
prune-worktrees --apply --idle-min 120   # only touch worktrees idle 2h+
prune-worktrees --keep-untracked         # treat any untracked file as work
```

It is safe to run any time, including while other agents work — the idle guard
and the merged/clean checks keep it from touching a live or unmerged worktree.
Prefer running it over telling an agent to "clean up when done": the guarantee is
in the tool, not in the agent remembering.

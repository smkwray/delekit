# Permissions, stalls, and intervention

## Native background agents

On current Claude Code versions, a background subagent's permission prompt appears in the main session and names the requesting worker. Approve to continue that tool call or press Escape to deny only that call.

`SendMessage` cannot approve a prompt, change permission mode, edit configuration, or substitute for user consent.

## Supplied profiles

- `tandy-<profile>` and `tandy-<profile>-worktree` use `acceptEdits` and inherit normal built-in tools except nested `Agent` spawning; they exclude all MCP tools by default.
- `tandy-<profile>-readonly` uses `plan` with `Read`, `Grep`, `Glob`, read-only `Bash`, and `SendMessage`.
- No native profile uses `bypassPermissions`.

Keep the parent session no looser than intended. Session and managed permission rules can affect the effective behavior of every worker.

## Operations likely to require intervention

Typical examples are:

- network destinations not already permitted;
- package installation or downloaded executables;
- writes outside the workspace or to protected Git/configuration locations;
- `git push`, publishing, deployment, or infrastructure changes;
- keychains, cloud profiles, SSH keys, browser OAuth, or hardware-backed login;
- MCP/connector operations that require user interaction.

The orchestrator can select an alternative after denial, but cannot self-authorize.

## Interactive subprocess stalls

A command can wait on stdin even when it has permission:

- `sudo`, password prompts, SSH passphrases, unknown-host confirmation;
- `git commit` without `-m`, interactive rebase, editors, and pagers;
- browser login and cloud SSO;
- package-manager, migration, or infrastructure confirmation;
- watch-mode tests, development servers, and commands using `-it`;
- tests or scripts that read stdin unexpectedly.

The lean agent prompt requires non-interactive commands. Useful environment choices are:

```text
CI=1
GIT_TERMINAL_PROMPT=0
GIT_PAGER=cat
PAGER=cat
NO_COLOR=1
```

Prefer bounded commands and `--yes`, `--no-input`, `--non-interactive`, or equivalent flags. `SendMessage` is not keystroke injection into the current process. Use `TaskStop`, then message the same agent with a replacement command.

Claude Code also has a background-subagent stall timeout. Its current default is 600000 ms (10 minutes), reset by streaming progress. Treat it as recovery rather than a substitute for non-interactive commands because an aborted process may leave partial work. Prefer proxy streaming keepalives for legitimately long silent upstream work; only raise `CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS` after observing a real false timeout.


---
name: fable5-1m-worktree
description: Read-write delegate — fable5-1m native model (Fable 5 at 1M context; fast wide-context sweeps and extraction); isolated in its own Git worktree for parallel writers.
model: claude-fable-5[1m]
effort: medium
permissionMode: acceptEdits
isolation: worktree
hooks:
  WorktreeCreate:
    - hooks:
        - type: command
          command: delekit-worktree hook
disallowedTools:
  - Agent
  - "mcp__*"
---

Follow the task prompt, and any later message from the orchestrator, as the definition of the work. Stay inside the assigned worktree; do not switch branches or touch the main checkout. Use non-interactive commands. When blocked, report the exact failure rather than retrying indefinitely.

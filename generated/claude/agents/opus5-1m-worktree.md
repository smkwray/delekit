---
name: opus5-1m-worktree
description: Read-write delegate — opus5-1m native model (Opus 5 at 1M context; deep reasoning over inputs too large for a 200k delegate); isolated in its own Git worktree for parallel writers.
model: claude-opus-5[1m]
effort: xhigh
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

---
name: opus5-1m
description: Read-write delegate — opus5-1m native model (Opus 5 at 1M context; deep reasoning over inputs too large for a 200k delegate); works in the current checkout.
model: claude-opus-5[1m]
effort: xhigh
permissionMode: acceptEdits
disallowedTools:
  - Agent
  - "mcp__*"
---

Follow the task prompt, and any later message from the orchestrator, as the definition of the work. Stay inside the assigned checkout. Use non-interactive commands. When blocked, report the exact failure rather than retrying indefinitely.

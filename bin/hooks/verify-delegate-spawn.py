#!/usr/bin/env python3
"""PreToolUse hook: stop a delegate spawn from silently becoming a generic one.

The Agent tool ignores unrecognised parameters. Passing `subject_type` instead
of `subagent_type` therefore does not error — the spawn quietly falls back to a
generic subagent on the session's own model, in the main checkout, with no
worktree isolation. Nothing in the transcript says so.

This hook denies that specific shape. It is deliberately narrow: it only fires
when a spawn *asks for* a delegate and *is not going to get one*.

Two independent checks, so it works regardless of whether the harness strips
unknown keys before hooks run (undocumented, and it may change):

  A. a key that looks like a misspelling of subagent_type is present
  B. subagent_type is missing/generic while the request names a delegate agent

Register in settings.json:

  "hooks": {
    "PreToolUse": [
      {"matcher": "Agent",
       "hooks": [{"type": "command",
                  "command": "python3 /path/to/verify-delegate-spawn.py"}]}
    ]
  }
"""
from __future__ import annotations

import difflib
import json
import os
import pathlib
import re
import sys

PREFIX = os.environ.get("DELEGATE_AGENT_PREFIX", "tandy")
GENERIC = {"general-purpose", "Explore", "Plan", ""}
LOG = pathlib.Path(os.environ.get(
    "DELEGATE_SPAWN_LOG",
    pathlib.Path.home() / ".local/state/delekit/spawn-audit.jsonl"))


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0                      # never break a session on malformed input
    if event.get("tool_name") != "Agent":
        return 0
    ti = event.get("tool_input") or {}

    # Record what the hook actually receives. This is the only way to learn
    # whether unknown keys survive to hook time; the behaviour is undocumented.
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps({"keys": sorted(ti),
                                 "subagent_type": ti.get("subagent_type")}) + "\n")
    except Exception:
        pass

    requested = ti.get("subagent_type") or ""

    # --- A. a near-miss key name is present -------------------------------
    for key in ti:
        if key == "subagent_type":
            continue
        if difflib.SequenceMatcher(None, key.lower().replace("_", ""),
                                   "subagenttype").ratio() > 0.75:
            deny(f"The Agent tool takes `subagent_type`, not `{key}`. Unknown "
                 f"parameters are ignored silently, so this spawn would have "
                 f"become a generic subagent on the session model with no "
                 f"worktree. Re-issue with subagent_type.")

    # --- C. a per-invocation model would override the delegate's profile --
    # Model resolution is per-invocation model > agent-file alias, and the Agent
    # tool's model field only accepts sonnet/opus/haiku/fable — never a gateway
    # profile alias. So a model here silently routes a delegate off its profile
    # onto a non-gateway model, defeating the whole point of naming the agent.
    if requested.startswith(f"{PREFIX}-") and ti.get("model"):
        deny(f"{requested} pins its model through its agent-file alias, but this "
             f"spawn also sets model={ti.get('model')!r}. The per-invocation "
             f"model outranks the alias, and the Agent tool only accepts "
             f"sonnet/opus/haiku/fable, so the delegate would run off-profile on "
             f"a non-gateway model. Drop the model field — the profile comes "
             f"from the agent name.")

    # --- B. a delegate is described but not actually requested ------------
    if requested in GENERIC:
        blob = " ".join(str(ti.get(k, "")) for k in ("description", "prompt"))
        named = re.search(rf"\b{re.escape(PREFIX)}-[a-z0-9-]+", blob)
        if named:
            deny(f"This spawn names {named.group(0)} but subagent_type is "
                 f"'{requested or 'unset'}', so it would run as a generic "
                 f"subagent on the session model — wrong model, and no worktree "
                 f"isolation. Set subagent_type={named.group(0)} explicitly, or "
                 f"remove the delegate name from the prompt if a generic "
                 f"subagent is genuinely what you want.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

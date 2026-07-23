#!/usr/bin/env python3
"""A fake codex/claude CLI for herd tests. No network.

Detects which backend it is impersonating from argv (codex uses `exec`, claude
uses `-p`) and emits that backend's streaming-JSON event schema, then exits.

Env knobs:
  FAKE_HANG=1   emit the session id, then sleep without further output
                (exercises the stall watchdog).
"""
import json
import os
import sys
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    argv = sys.argv[1:]
    is_claude = "-p" in argv
    is_resume = ("resume" in argv) or ("--resume" in argv)
    prompt = sys.stdin.read()
    first_line = (prompt.strip().splitlines() or [""])[-1][:80]
    sid = "sess-fake-0001"

    if is_claude:
        emit({"type": "system", "subtype": "init", "session_id": sid})
    else:
        emit({"session_id": sid})

    if os.environ.get("FAKE_HANG") == "1":
        time.sleep(30)
        return 0

    if "ASKQ" in prompt:
        body = "QUESTION: which config file should I edit?"
    elif is_resume:
        body = "resumed: " + first_line
    else:
        body = "did: " + first_line

    if is_claude:
        emit({"type": "assistant", "session_id": sid,
              "message": {"content": [{"type": "text", "text": body}]}})
        emit({"type": "result", "subtype": "success", "session_id": sid, "result": body})
    else:
        emit({"msg": {"type": "agent_reasoning", "text": "thinking about it"}})
        emit({"type": "agent_message", "message": body})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

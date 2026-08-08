#!/usr/bin/env python3
"""A fake codex/pi/claude/muse CLI for herd tests. No network.

Detects which backend it is impersonating from argv (codex uses `exec` with a
`-` stdin prompt, pi uses `--mode json`, claude uses `-p`, muse uses
`--prompt-file`) and emits that
backend's streaming-JSON event schema, then exits.

Env knobs:
  FAKE_HANG=1   emit the session id, then sleep without further output
                (exercises the stall watchdog).
  FAKE_DELAY_S  pause after the session id before completing the turn
                (exercises a blocking completion watcher).
"""
import json
import os
import sys
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def muse_envelope(payload_type, payload, sid):
    return {"schema_version": 1, "stream": {"kind": "session", "id": sid},
            "record_type": "event", "payload_type": payload_type, "payload": payload}


def main():
    argv = sys.argv[1:]
    is_pi = "--mode" in argv and argv[argv.index("--mode") + 1] == "json"
    is_claude = "-p" in argv
    is_muse = "--prompt-file" in argv
    is_resume = ("resume" in argv) or ("--resume" in argv) or ("--session-id" in argv)
    sid = "sess-fake-0001"

    if is_muse:
        # Muse reads the prompt from a file and never from stdin.
        with open(argv[argv.index("--prompt-file") + 1], encoding="utf-8") as fh:
            prompt = fh.read()
    else:
        prompt = sys.stdin.read()
    first_line = (prompt.strip().splitlines() or [""])[-1][:80]

    if is_pi:
        emit({"type": "session", "version": 3, "id": sid})
    elif is_claude:
        emit({"type": "system", "subtype": "init", "session_id": sid})
    elif is_muse:
        emit(muse_envelope("run.lifecycle.started", {"kind": "run_started"}, sid))
    else:
        emit({"session_id": sid})

    if os.environ.get("FAKE_HANG") == "1":
        time.sleep(30)
        return 0

    delay = float(os.environ.get("FAKE_DELAY_S", "0"))
    if delay > 0:
        time.sleep(delay)

    if "ASKQ" in prompt:
        body = "QUESTION: which config file should I edit?"
    elif is_resume:
        body = "resumed: " + first_line
    else:
        body = "did: " + first_line

    if is_pi:
        emit({"type": "message_end", "message": {"role": "assistant",
              "content": [{"type": "text", "text": body}]}})
    elif is_claude:
        emit({"type": "assistant", "session_id": sid,
              "message": {"content": [{"type": "text", "text": body}]}})
        emit({"type": "result", "subtype": "success", "session_id": sid, "result": body})
    elif is_muse:
        # Deltas repeat the answer in chunks; only the terminal event is the
        # report, which is what MuseBackend.parse must pick up.
        emit(muse_envelope("run.output.delta", {"kind": "run_output_delta", "text": body[:4]}, sid))
        emit(muse_envelope("run.terminal.completed",
                           {"kind": "run_terminal", "terminal": "completed", "text": body}, sid))
    else:
        emit({"msg": {"type": "agent_reasoning", "text": "thinking about it"}})
        emit({"type": "agent_message", "message": body})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

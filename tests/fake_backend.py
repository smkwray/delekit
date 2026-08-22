#!/usr/bin/env python3
"""A fake codex/pi/claude/muse/opencode CLI for herd tests. No network.

Detects which backend it is impersonating from argv (codex uses `exec` with a
`-` stdin prompt, pi uses `--mode json`, claude uses `-p`, muse uses
`--prompt-file`, opencode uses `run --format json`) and emits that
backend's streaming-JSON event schema, then exits.

Env knobs:
  FAKE_HANG=1   emit the session id, then sleep without further output
                (exercises the stall watchdog).
  FAKE_DELAY_S  pause after the session id before completing the turn
                (exercises a blocking completion watcher).
  FAKE_EMPTY=1  emit the session id, then exit 0 with no agent message at all
                (a real opencode free-tier model does exactly this).
  FAKE_EXIT_CODE  emit the session id and a message, then exit with this code
                (exercises the nonzero-exit terminal path).
  FAKE_ENV_DUMP  write the environment the backend ACTUALLY received to this
                path, as JSON. The only way a test can observe what crossed the
                detached-child handoff.
  FAKE_WRITE_FILE  create this file in the cwd (the execution root) before
                finishing, so worktree auto-commit behaviour is observable.
  FAKE_ORPHAN_HOLDS_PIPE=1  answer normally, detach a setsid grandchild that
                INHERITS stdout, then exit 0 -- the shape opencode's shell tool
                produces, which leaves the reader blocked on a pipe nobody will
                close.
  FAKE_SPAWN_ORPHAN=1  detach a grandchild into its own process group, then hang
                (opencode's shell tool launches commands exactly this way).
                Writes its pid to FAKE_ORPHAN_PIDFILE.
"""
import json
import os
import subprocess
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

    dump = os.environ.get("FAKE_ENV_DUMP")
    if dump:
        keys = ("OPENCODE_PERMISSION", "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DB",
                "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_AUTH_CONTENT")
        with open(dump, "w", encoding="utf-8") as fh:
            json.dump({k: os.environ.get(k) for k in keys}, fh)

    # The opencode adapter probes the CLI before it spawns a turn: once for the
    # version floor, and once per explicit --effort to enumerate the selected
    # model's declared variants. The stand-in has to answer both or every
    # opencode test fails on the probe rather than on the behaviour under test.
    if "--version" in argv:
        print(os.environ.get("FAKE_OPENCODE_VERSION", "1.18.20"))
        return 0
    if "models" in argv:
        variants = [v for v in os.environ.get("FAKE_VARIANTS", "low,high").split(",") if v]
        print(json.dumps({"id": os.environ.get("FAKE_MODEL_ID", "m"),
                          "providerID": "fake",
                          "variants": {v: {} for v in variants}}, indent=2))
        return 0
    is_pi = "--mode" in argv and argv[argv.index("--mode") + 1] == "json"
    is_claude = "-p" in argv
    is_muse = "--prompt-file" in argv
    is_opencode = "run" in argv and "--format" in argv
    is_resume = ("resume" in argv) or ("--resume" in argv) or ("--session-id" in argv) or ("--session" in argv)
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
    elif is_opencode:
        # Opencode stamps sessionID on every event, so the resume handle is
        # available from the first line rather than a dedicated init event.
        emit({"type": "step_start", "sessionID": sid,
              "part": {"type": "step-start", "sessionID": sid}})
    else:
        emit({"session_id": sid})

    if os.environ.get("FAKE_SPAWN_ORPHAN") == "1":
        # A grandchild in its OWN process group, the way opencode's POSIX shell
        # tool launches commands. A watchdog that signals only the direct child
        # leaves this alive and still writing after the task goes terminal.
        code = ("import os,sys,time\n"
                "open(sys.argv[1],'w').write(str(os.getpid()))\n"
                "time.sleep(120)\n")
        kwargs = {"start_new_session": True} if os.name != "nt" else {}
        subprocess.Popen([sys.executable, "-c", code, os.environ["FAKE_ORPHAN_PIDFILE"]], **kwargs)
        time.sleep(120)
        return 0

    if os.environ.get("FAKE_HANG") == "1":
        time.sleep(30)
        return 0

    if os.environ.get("FAKE_EMPTY") == "1":
        return 0

    if os.environ.get("FAKE_ORPHAN_HOLDS_PIPE") == "1":
        # The grandchild deliberately inherits this process's stdout, so the
        # supervisor's reader stays blocked after we exit.
        code = "import os,sys,time\ntime.sleep(90)\n"
        kwargs = {"start_new_session": True} if os.name != "nt" else {}
        subprocess.Popen([sys.executable, "-c", code], **kwargs)

    written = os.environ.get("FAKE_WRITE_FILE")
    if written:
        with open(written, "w", encoding="utf-8") as fh:
            fh.write("worker output\n")

    rc = os.environ.get("FAKE_EXIT_CODE")
    if rc:
        return int(rc)

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
    elif is_opencode:
        # Each text part arrives as one complete event, so the last one is the
        # whole final answer -- never a fragment to be reassembled.
        emit({"type": "text", "sessionID": sid,
              "part": {"type": "text", "text": body}})
        emit({"type": "step_finish", "sessionID": sid,
              "part": {"type": "step-finish", "reason": "stop"}})
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

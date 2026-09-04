#!/usr/bin/env python3
"""A fake codex/pi/claude/muse/opencode/grok/cursor CLI for herd tests. No network.

Detects which backend it is impersonating from argv (codex uses `exec` with a
`-` stdin prompt, pi uses `--mode json`, claude uses `-p`, cursor uses `--trust`,
muse uses `--prompt-file`, grok uses `--output-format streaming-json`, opencode
uses `run --format json`) and emits that backend's streaming-JSON event schema,
then exits.

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
  FAKE_GROK_VERSION  version reported to Grok's compatibility probe.
  FAKE_GROK_MISSING_SESSION=1  omit sessionId from Grok's terminal end event.
  FAKE_GROK_EMPTY_FINAL=1  finish Grok with a contentless final response.
  FAKE_GROK_STOP_REASON  override Grok's terminal stop reason.
  FAKE_GROK_OMIT_FINAL_USAGE=1  omit Grok's final usage response boundary.
  FAKE_CURSOR_RESULT_ERROR=1  emit Cursor assistant text then result.subtype=error.
  FAKE_CURSOR_OMIT_RESULT=1  emit Cursor assistant text then EOF with no result.
  FAKE_CURSOR_EMPTY_RESULT=1  emit a Cursor success result with an empty answer.
  FAKE_CURSOR_SESSION_MISMATCH=1  emit a different session_id than --resume.
  FAKE_CURSOR_MISMATCH_SIDE_EFFECT=1  after a mismatched init, wait then write
                FAKE_WRITE_FILE (exercises abort-before-side-effect).
  FAKE_CURSOR_MULTI_ASSISTANT=1  emit a pre-tool assistant, a final assistant,
                then a concatenated successful result.
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
        # One stand-in serves both version probes. Grok-specific tests override
        # FAKE_GROK_VERSION; opencode keeps its existing knob.
        print(os.environ.get("FAKE_GROK_VERSION",
                             os.environ.get("FAKE_OPENCODE_VERSION", "1.18.20")))
        return 0
    if "models" in argv:
        variants = [v for v in os.environ.get("FAKE_VARIANTS", "low,high").split(",") if v]
        print(json.dumps({"id": os.environ.get("FAKE_MODEL_ID", "m"),
                          "providerID": "fake",
                          "variants": {v: {} for v in variants}}, indent=2))
        return 0
    is_pi = "--mode" in argv and argv[argv.index("--mode") + 1] == "json"
    # `--trust` is Cursor-only. Claude also uses `-p` and the same stream-json
    # shape, so Cursor must be distinguished before the Claude test.
    is_cursor = "--trust" in argv
    is_claude = "-p" in argv and not is_cursor
    is_grok = ("--output-format" in argv and
               argv[argv.index("--output-format") + 1] == "streaming-json")
    is_muse = "--prompt-file" in argv and not is_grok
    is_opencode = "run" in argv and "--format" in argv
    is_resume = (("resume" in argv) or ("--resume" in argv) or ("--session" in argv) or
                 ("--session-id" in argv and not is_grok))
    if is_grok and "--session-id" in argv and "--resume" in argv:
        print("fake grok: --session-id and --resume are mutually exclusive", file=sys.stderr)
        return 2
    if is_grok and "--session-id" in argv:
        sid = argv[argv.index("--session-id") + 1]
    elif is_grok and "--resume" in argv:
        sid = argv[argv.index("--resume") + 1]
    elif (is_cursor or is_claude) and "--resume" in argv:
        sid = argv[argv.index("--resume") + 1]
    else:
        sid = "sess-fake-0001"
    if is_cursor and os.environ.get("FAKE_CURSOR_SESSION_MISMATCH") == "1":
        sid = "sess-mismatch-0002"

    if is_muse or is_grok:
        # Muse and Grok read the prompt from a file and never from stdin.
        with open(argv[argv.index("--prompt-file") + 1], encoding="utf-8") as fh:
            prompt = fh.read()
    else:
        prompt = sys.stdin.read()
    first_line = (prompt.strip().splitlines() or [""])[-1][:80]

    if is_pi:
        emit({"type": "session", "version": 3, "id": sid})
    elif is_claude or is_cursor:
        emit({"type": "system", "subtype": "init", "session_id": sid})
    elif is_muse:
        emit(muse_envelope("run.lifecycle.started", {"kind": "run_started"}, sid))
    elif is_grok:
        # Native streaming-json does not require an init event; the session id
        # is returned on the terminal end event.
        pass
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
    side_effect_after_init = (
        is_cursor and os.environ.get("FAKE_CURSOR_MISMATCH_SIDE_EFFECT") == "1")
    if written and not side_effect_after_init:
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
    elif is_cursor:
        if os.environ.get("FAKE_CURSOR_MISMATCH_SIDE_EFFECT") == "1":
            time.sleep(0.5)
            if written:
                with open(written, "w", encoding="utf-8") as fh:
                    fh.write("mismatch side effect\n")
        pretool = os.environ.get("FAKE_CURSOR_MULTI_ASSISTANT") == "1"
        if pretool:
            emit({"type": "assistant", "session_id": sid,
                  "message": {"content": [{"type": "text", "text": "I'll create the file now."}]}})
        emit({"type": "assistant", "session_id": sid,
              "message": {"content": [{"type": "text", "text": body}]}})
        if os.environ.get("FAKE_CURSOR_OMIT_RESULT") == "1":
            return 0
        subtype = "error" if os.environ.get("FAKE_CURSOR_RESULT_ERROR") == "1" else "success"
        result_text = "" if os.environ.get("FAKE_CURSOR_EMPTY_RESULT") == "1" else body
        if pretool and subtype == "success" and result_text:
            result_text = "I'll create the file now." + body
        emit({"type": "result", "subtype": subtype,
              "is_error": subtype == "error", "session_id": sid, "result": result_text})
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
    elif is_grok:
        # Two model responses separated by usage: only the post-tool/final
        # response may become herd's report. Native streaming-json emits one
        # usage record per response and then a terminal end event.
        emit({"type": "text", "data": "pre-tool response"})
        emit({"type": "usage", "messageId": "resp-1", "stopReason": "tool_use"})
        stop_reason = os.environ.get("FAKE_GROK_STOP_REASON", "end_turn")
        if os.environ.get("FAKE_GROK_EMPTY_FINAL") != "1":
            emit({"type": "text", "data": body})
        if os.environ.get("FAKE_GROK_OMIT_FINAL_USAGE") != "1":
            emit({"type": "usage", "messageId": "resp-2", "stopReason": stop_reason})
        end_sid = ("00000000-0000-4000-8000-000000000000"
                   if os.environ.get("FAKE_GROK_SESSION_MISMATCH") == "1" else sid)
        end = {"type": "end", "stopReason": stop_reason, "requestId": "req-fake-1"}
        if os.environ.get("FAKE_GROK_MISSING_SESSION") != "1":
            end["sessionId"] = end_sid
        emit(end)
    else:
        emit({"msg": {"type": "agent_reasoning", "text": "thinking about it"}})
        emit({"type": "agent_message", "message": body})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Step-2+ tests: backend adapters, the detached turn helper, spawn/result/send.

No network. A fake codex/pi/claude/muse CLI (tests/fake_backend.py) is pointed at via
the backend executable overrides, so
spawn runs the real detached helper against a controllable JSON event stream.
"""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import delegate_supervisor as ds  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_backend.py"


def quiet_main(argv):
    """Run the CLI, swallowing its stdout status line for clean test output."""
    with contextlib.redirect_stdout(io.StringIO()):
        return ds.main(argv)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        warnings.simplefilter("ignore", ResourceWarning)
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = tempfile.TemporaryDirectory()
        os.environ["DELEGATE_STATE_DIR"] = self.tmp.name
        os.environ["DELEKIT_DEVICE_ID"] = "test-device"
        # Wrap the fake so it is invoked as its own executable (shebang-based).
        st = os.stat(FAKE)
        os.chmod(FAKE, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["DELEGATE_CODEX_BIN"] = str(FAKE)
        os.environ["DELEGATE_PI_BIN"] = str(FAKE)
        os.environ["DELEGATE_CLAUDE_BIN"] = str(FAKE)
        os.environ["DELEGATE_MUSE_BIN"] = str(FAKE)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.proj.cleanup()
        for k in ("DELEGATE_STATE_DIR", "DELEKIT_DEVICE_ID", "DELEGATE_CODEX_BIN",
                  "DELEGATE_PI_BIN", "DELEGATE_CLAUDE_BIN", "DELEGATE_MUSE_BIN",
                  "FAKE_HANG", "FAKE_DELAY_S"):
            os.environ.pop(k, None)

    def wait_done(self, task, timeout=20.0):
        marker = ds.task_dir(task) / ".done"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if marker.exists():
                return True
            time.sleep(0.1)
        return False

    def spawn(self, *extra, prompt="do the thing", name="t1", backend="codex", mode="workspace"):
        argv = ["spawn", mode, prompt, "--json", "--project-root", self.proj.name,
                "--model", "m", "--name", name, "--backend", backend,
                "--no-preamble", "--stall-after", "60", "--deadline", "600", *extra]
        return quiet_main(argv)


class TestParsers(unittest.TestCase):
    def test_codex_fast_mode_is_explicit(self):
        b = ds.BACKENDS["codex"]
        self.assertEqual(b.fast_args({"fast": True}), ["--enable", "fast_mode"])
        self.assertEqual(b.fast_args({"fast": False}), ["--disable", "fast_mode"])
        self.assertEqual(b.fast_args({}), ["--disable", "fast_mode"])

    def test_codex_parser_session_and_message(self):
        b = ds.BACKENDS["codex"]
        self.assertEqual(b.parse({"session_id": "s1"}).get("session_id"), "s1")
        self.assertEqual(b.parse({"type": "agent_message", "message": "hi"}).get("message"), "hi")
        self.assertEqual(b.parse({"msg": {"type": "agent_reasoning", "text": "why"}}).get("thinking"), "why")

    def test_claude_parser_session_and_result(self):
        b = ds.BACKENDS["claude"]
        self.assertEqual(b.parse({"type": "system", "session_id": "s2"}).get("session_id"), "s2")
        self.assertEqual(b.parse({"type": "result", "result": "final"}).get("message"), "final")
        asst = {"type": "assistant", "message": {"content": [{"type": "text", "text": "mid"}]}}
        self.assertEqual(b.parse(asst).get("message"), "mid")

    def test_pi_parser_session_and_final_message(self):
        b = ds.BACKENDS["pi"]
        self.assertEqual(b.parse({"type": "session", "id": "s-pi"}).get("session_id"), "s-pi")
        event = {"type": "message_end", "message": {"role": "assistant",
                 "content": [{"type": "text", "text": "final"}]}}
        self.assertEqual(b.parse(event).get("message"), "final")

    def test_pi_access_mapping(self):
        b = ds.BACKENDS["pi"]
        self.assertEqual(b.sandbox_args("read-only"), ["--tools", "read,grep,find,ls"])
        self.assertEqual(b.sandbox_args("danger-full-access"), [])
        with self.assertRaises(ds.HerdError):
            b.sandbox_args("workspace-write")

    def test_muse_parser_session_and_terminal_text(self):
        b = ds.BACKENDS["muse"]
        env = {"stream": {"kind": "session", "id": "s3"}, "payload_type": "run.lifecycle.started",
               "payload": {"kind": "run_started"}}
        self.assertEqual(b.parse(env).get("session_id"), "s3")
        term = {"stream": {"kind": "session", "id": "s3"}, "payload_type": "run.terminal.completed",
                "payload": {"kind": "run_terminal", "terminal": "completed", "text": "final"}}
        self.assertEqual(b.parse(term).get("message"), "final")

    def test_muse_parser_ignores_streaming_deltas(self):
        # Deltas repeat the answer in chunks; treating them as messages would
        # leave a truncated last chunk as the report.
        b = ds.BACKENDS["muse"]
        delta = {"stream": {"kind": "session", "id": "s3"}, "payload_type": "run.output.delta",
                 "payload": {"kind": "run_output_delta", "text": "fin"}}
        self.assertIsNone(b.parse(delta).get("message"))
        # A run stream must not be mistaken for the session id.
        run = {"stream": {"kind": "run", "id": "r1"}, "payload_type": "run.output.delta", "payload": {}}
        self.assertIsNone(b.parse(run).get("session_id"))

    def test_muse_access_mapping(self):
        b = ds.BACKENDS["muse"]
        self.assertEqual(b.sandbox_args("danger-full-access"), ["--yolo"])
        self.assertIn("--disable-shell", b.sandbox_args("read-only"))
        self.assertIn("--disable-write", b.sandbox_args("read-only"))
        # workspace-write keeps muse's own sandbox on: only approvals are off.
        self.assertEqual(b.sandbox_args("workspace-write"), ["--disable-approval"])


class TestSpawnResult(Base):
    def test_codex_spawn_runs_and_reports(self):
        self.spawn(name="cdx")
        self.assertTrue(self.wait_done("cdx"), "helper did not finish")
        payload = ds.reconcile(ds.task_dir("cdx"))
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["session_id"], "sess-fake-0001")
        report = (ds.task_dir("cdx") / "report.md").read_text()
        self.assertIn("do the thing", report)

    def test_claude_spawn_runs_and_reports(self):
        self.spawn(name="cl", backend="claude")
        self.assertTrue(self.wait_done("cl"))
        payload = ds.reconcile(ds.task_dir("cl"))
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["session_id"], "sess-fake-0001")

    def test_pi_spawn_runs_and_reports(self):
        self.spawn(name="pi", backend="pi", mode="readonly")
        self.assertTrue(self.wait_done("pi"))
        payload = ds.reconcile(ds.task_dir("pi"))
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["session_id"], "sess-fake-0001")
        self.assertIn("do the thing", (ds.task_dir("pi") / "report.md").read_text())

    def test_pi_default_preamble_discloses_missing_shell(self):
        quiet_main(["spawn", "readonly", "inspect", "--json", "--project-root", self.proj.name,
                    "--model", "m", "--name", "pi-pre", "--backend", "pi",
                    "--stall-after", "60", "--deadline", "600"])
        self.assertTrue(self.wait_done("pi-pre"))
        prompt = (ds.task_dir("pi-pre") / "prompt.md").read_text()
        self.assertIn("no shell or test execution", prompt)

    def test_pi_workspace_is_refused_before_state(self):
        rc = self.spawn(name="pi-ws", backend="pi")
        self.assertEqual(rc, 2)
        self.assertFalse(ds.task_dir("pi-ws").exists())

    def test_muse_spawn_runs_and_reports(self):
        self.spawn(name="ms", backend="muse")
        self.assertTrue(self.wait_done("ms"))
        payload = ds.reconcile(ds.task_dir("ms"))
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["session_id"], "sess-fake-0001")
        # The prompt reached muse through --prompt-file, not stdin.
        self.assertIn("do the thing", (ds.task_dir("ms") / "report.md").read_text())

    def test_question_marks_awaiting_reply(self):
        self.spawn(prompt="please ASKQ now", name="q1")
        self.assertTrue(self.wait_done("q1"))
        payload = ds.reconcile(ds.task_dir("q1"))
        self.assertEqual(payload["state"], "awaiting_reply")

    def test_result_wait_blocks_until_the_turn_completes(self):
        os.environ["FAKE_DELAY_S"] = "0.6"
        self.spawn(name="watched")
        output = io.StringIO()
        started = time.monotonic()
        with contextlib.redirect_stdout(output):
            rc = ds.main(["result", "watched", "--wait", "--timeout", "5", "--json"])
        elapsed = time.monotonic() - started
        payload = json.loads(output.getvalue())
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertEqual(payload["status"]["state"], "done")
        self.assertIn("do the thing", payload["report"])

    def test_duplicate_task_name_rejected(self):
        self.spawn(name="dup")
        self.assertTrue(self.wait_done("dup"))
        rc = self.spawn(name="dup")
        self.assertEqual(rc, 2)


class TestSendResume(Base):
    def test_send_resumes_same_session(self):
        self.spawn(prompt="first turn", name="r1")
        self.assertTrue(self.wait_done("r1"))
        rc = quiet_main(["send", "r1", "second turn", "--json", "--no-preamble"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.wait_done("r1"))
        payload = ds.reconcile(ds.task_dir("r1"))
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["session_id"], "sess-fake-0001")
        report = (ds.task_dir("r1") / "report.md").read_text()
        self.assertIn("resumed:", report)

    def test_muse_send_resumes_via_session_id(self):
        self.spawn(prompt="first turn", name="mr", backend="muse")
        self.assertTrue(self.wait_done("mr"))
        self.assertEqual(quiet_main(["send", "mr", "second turn", "--json", "--no-preamble"]), 0)
        self.assertTrue(self.wait_done("mr"))
        payload = ds.reconcile(ds.task_dir("mr"))
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["session_id"], "sess-fake-0001")
        self.assertIn("resumed:", (ds.task_dir("mr") / "report.md").read_text())

    def test_pi_send_resumes_via_session_id(self):
        self.spawn(prompt="first turn", name="pr", backend="pi", mode="readonly")
        self.assertTrue(self.wait_done("pr"))
        self.assertEqual(quiet_main(["send", "pr", "second turn", "--json", "--no-preamble"]), 0)
        self.assertTrue(self.wait_done("pr"))
        payload = ds.reconcile(ds.task_dir("pr"))
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["session_id"], "sess-fake-0001")
        self.assertIn("resumed:", (ds.task_dir("pr") / "report.md").read_text())

    def test_send_without_session_is_error(self):
        # Fabricate a task that never captured a session id.
        tdir = ds.sessions_dir() / "nosess"
        tdir.mkdir(parents=True)
        ds.atomic_write_json(tdir / "meta.json", {
            "task": "nosess", "state": "failed", "backend": "codex", "model": "m",
            "access": "workspace-write", "exec_root": self.proj.name, "repo": self.proj.name,
            "owner": ds.owner_id(), "pid": None, "session_id": None,
        })
        (tdir / ".done").touch()
        rc = quiet_main(["send", "nosess", "hello", "--json"])
        self.assertEqual(rc, 9)


class TestStallWatchdog(Base):
    def test_no_output_marks_stalled(self):
        os.environ["FAKE_HANG"] = "1"
        argv = ["spawn", "workspace", "will hang", "--json", "--project-root", self.proj.name,
                "--model", "m", "--name", "hang", "--backend", "codex", "--no-preamble",
                "--stall-after", "2", "--deadline", "600"]
        quiet_main(argv)
        self.assertTrue(self.wait_done("hang", timeout=20), "watchdog did not settle the task")
        payload = ds.reconcile(ds.task_dir("hang"))
        self.assertEqual(payload["state"], "stalled")
        self.assertEqual(payload["stall_reason"], "no-output")


if __name__ == "__main__":
    unittest.main(verbosity=2)

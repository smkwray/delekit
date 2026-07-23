#!/usr/bin/env python3
"""Step-2+ tests: backend adapters, the detached turn helper, spawn/result/send.

No network. A fake codex/claude CLI (tests/fake_backend.py) is pointed at via the
DELEGATE_CODEX_BIN / DELEGATE_CLAUDE_BIN overrides, so spawn runs the real
detached helper against a controllable JSON event stream.
"""
import contextlib
import io
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
        os.environ["DELEGATE_CLAUDE_BIN"] = str(FAKE)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.proj.cleanup()
        for k in ("DELEGATE_STATE_DIR", "DELEKIT_DEVICE_ID", "DELEGATE_CODEX_BIN",
                  "DELEGATE_CLAUDE_BIN", "FAKE_HANG"):
            os.environ.pop(k, None)

    def wait_done(self, task, timeout=20.0):
        marker = ds.task_dir(task) / ".done"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if marker.exists():
                return True
            time.sleep(0.1)
        return False

    def spawn(self, *extra, prompt="do the thing", name="t1", backend="codex"):
        argv = ["spawn", "workspace", prompt, "--json", "--project-root", self.proj.name,
                "--model", "m", "--name", name, "--backend", backend,
                "--no-preamble", "--stall-after", "60", "--deadline", "600", *extra]
        return quiet_main(argv)


class TestParsers(unittest.TestCase):
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

    def test_question_marks_awaiting_reply(self):
        self.spawn(prompt="please ASKQ now", name="q1")
        self.assertTrue(self.wait_done("q1"))
        payload = ds.reconcile(ds.task_dir("q1"))
        self.assertEqual(payload["state"], "awaiting_reply")

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

#!/usr/bin/env python3
"""Step-1 tests for tools/delegate_supervisor.py. Stdlib only, no network."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import delegate_supervisor as ds  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DELEGATE_STATE_DIR"] = self.tmp.name
        os.environ["DELEKIT_DEVICE_ID"] = "test-device"
        self.owner = ds.owner_id()

    def tearDown(self) -> None:
        self.tmp.cleanup()
        for k in ("DELEGATE_STATE_DIR", "DELEKIT_DEVICE_ID"):
            os.environ.pop(k, None)

    def make(self, task, state="working", pid=None, done=False, age_s=0, owner=None,
             stall_after=3600, deadline_in=3600, question=False):
        tdir = ds.sessions_dir() / task
        tdir.mkdir(parents=True, exist_ok=True)
        meta = {
            "task": task, "state": state, "backend": "codex", "model": "m",
            "access": "workspace-write", "repo": "/repo",
            "owner": owner if owner is not None else self.owner,
            "pid": pid, "session_id": "sess-" + task,
            "created_utc": ds.now() - age_s,
            "deadline_utc": ds.now() + deadline_in,
            "stall_after_s": stall_after,
        }
        ds.atomic_write_json(tdir / "meta.json", meta)
        (tdir / "report.md").write_text("QUESTION: need input" if question else "work", encoding="utf-8")
        if done:
            (tdir / ".done").touch()
        if age_s:
            old = ds.now() - age_s
            for n in ("meta.json", "report.md"):
                os.utime(tdir / n, (old, old))
        return tdir

    def run_cmd(self, argv):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return ds.main(argv)


class TestReconcile(Base):
    def test_dead_pid_without_marker_becomes_failed(self):
        self.make("t1", state="working", pid=999999999, done=False)
        payload = ds.reconcile(ds.task_dir("t1"), enforce=False)
        self.assertEqual(payload["state"], "failed")
        self.assertTrue((ds.task_dir("t1") / ".done").exists())

    def test_done_terminal_is_stable(self):
        self.make("t2", state="done", pid=None, done=True)
        payload = ds.reconcile(ds.task_dir("t2"), enforce=False)
        self.assertEqual(payload["state"], "done")

    def test_alive_stalled_marks_stalled(self):
        # alive pid (this test process), no output for longer than stall_after
        self.make("t3", state="working", pid=os.getpid(), done=False,
                  age_s=200, stall_after=100)
        payload = ds.reconcile(ds.task_dir("t3"), enforce=False)
        self.assertEqual(payload["state"], "stalled")

    def test_alive_deadline_exceeded_marks_failed(self):
        self.make("t4", state="working", pid=os.getpid(), done=False, deadline_in=-1)
        payload = ds.reconcile(ds.task_dir("t4"), enforce=False)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["stall_reason"], "deadline")

    def test_question_becomes_awaiting_reply(self):
        self.make("t5", state="working", pid=os.getpid(), done=False, question=True)
        payload = ds.reconcile(ds.task_dir("t5"), enforce=False)
        self.assertEqual(payload["state"], "awaiting_reply")


class TestPrune(Base):
    def test_prune_keeps_failed_and_stalled(self):
        self.make("done1", state="done", pid=None, done=True, age_s=9999)
        self.make("failed1", state="failed", pid=None, done=True, age_s=9999)
        self.make("stalled1", state="stalled", pid=None, done=True, age_s=9999)
        args = ds.build_parser().parse_args(["prune", "--json", "--idle-min", "0"])
        import io
        buf = io.StringIO(); old = sys.stdout; sys.stdout = buf
        try:
            args.func(args)
        finally:
            sys.stdout = old
        res = json.loads(buf.getvalue())
        self.assertIn("done1", res["reclaim"])
        kept = {n for n, _ in res["kept"]}
        self.assertIn("failed1", kept)
        self.assertIn("stalled1", kept)

    def test_include_unresolved_reclaims_them(self):
        self.make("stalled2", state="stalled", pid=None, done=True, age_s=9999)
        args = ds.build_parser().parse_args(
            ["prune", "--json", "--idle-min", "0", "--include-unresolved"])
        import io
        buf = io.StringIO(); old = sys.stdout; sys.stdout = buf
        try:
            args.func(args)
        finally:
            sys.stdout = old
        res = json.loads(buf.getvalue())
        self.assertIn("stalled2", res["reclaim"])

    def test_prune_never_removes_live(self):
        self.make("live1", state="working", pid=os.getpid(), done=False)
        args = ds.build_parser().parse_args(["prune", "--apply", "--json", "--idle-min", "0"])
        import io
        buf = io.StringIO(); old = sys.stdout; sys.stdout = buf
        try:
            args.func(args)
        finally:
            sys.stdout = old
        self.assertTrue(ds.task_dir("live1").is_dir())

    def test_prune_respects_idle_window(self):
        self.make("fresh", state="done", pid=None, done=True, age_s=0)
        args = ds.build_parser().parse_args(["prune", "--json", "--idle-min", "30"])
        import io
        buf = io.StringIO(); old = sys.stdout; sys.stdout = buf
        try:
            args.func(args)
        finally:
            sys.stdout = old
        res = json.loads(buf.getvalue())
        self.assertNotIn("fresh", res["reclaim"])


class TestListKill(Base):
    def test_list_hides_clean_by_default_and_owner_scopes(self):
        self.make("mine", state="stalled", pid=None, done=True)
        self.make("done_hidden", state="done", pid=None, done=True)
        self.make("other", state="stalled", pid=None, done=True, owner="/somewhere/else")
        args = ds.build_parser().parse_args(["list", "--json"])
        import io
        buf = io.StringIO(); old = sys.stdout; sys.stdout = buf
        try:
            args.func(args)
        finally:
            sys.stdout = old
        names = {r["task"] for r in json.loads(buf.getvalue())}
        self.assertEqual(names, {"mine"})

    def test_kill_marks_killed(self):
        self.make("k1", state="working", pid=None, done=False)
        self.run_cmd(["kill", "k1", "--json"])
        meta = ds.read_json(ds.task_dir("k1") / "meta.json")
        self.assertEqual(meta["state"], "killed")
        self.assertTrue((ds.task_dir("k1") / ".done").exists())


class TestAtomicIO(Base):
    def test_atomic_write_roundtrip(self):
        p = Path(self.tmp.name) / "x.json"
        ds.atomic_write_json(p, {"a": 1})
        self.assertEqual(ds.read_json(p), {"a": 1})

    def test_read_json_bad_returns_none(self):
        p = Path(self.tmp.name) / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertIsNone(ds.read_json(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)

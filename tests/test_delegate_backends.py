#!/usr/bin/env python3
"""Step-2+ tests: backend adapters, the detached turn helper, spawn/result/send.

No network. A fake codex/pi/claude/muse/opencode CLI (tests/fake_backend.py) is pointed at via
the backend executable overrides, so
spawn runs the real detached helper against a controllable JSON event stream.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
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
        os.environ["DELEGATE_OPENCODE_BIN"] = str(FAKE)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.proj.cleanup()
        for k in ("DELEGATE_STATE_DIR", "DELEKIT_DEVICE_ID", "DELEGATE_CODEX_BIN",
                  "DELEGATE_PI_BIN", "DELEGATE_CLAUDE_BIN", "DELEGATE_MUSE_BIN",
                  "DELEGATE_OPENCODE_BIN",
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


class TestOpencodeAdapter(Base):
    # Base, not TestCase: without it these tests fall back to whatever `opencode`
    # happens to be installed on the host, so the suite's result depended on the
    # author's machine rather than on the controlled fake.
    """Opencode's traps are all silent ones, so each is pinned by a test.

    Every assertion here corresponds to a measured failure mode where the wrong
    behaviour produces no error at all -- a policy that grants writes, an effort
    that does nothing, a report that is a fragment. A regression would otherwise
    ship looking healthy.
    """

    def test_parser_takes_session_from_any_event(self):
        # sessionID rides on every event, so the resume handle must be captured
        # from the first line rather than a dedicated init event.
        b = ds.BACKENDS["opencode"]
        step = {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}}
        self.assertEqual(b.parse(step).get("session_id"), "ses_1")
        self.assertIsNone(b.parse(step).get("message"))

    def test_parser_reads_text_part_and_ignores_step_events(self):
        # A step_finish carries no answer; treating one as the message would
        # overwrite the real report with an empty string.
        b = ds.BACKENDS["opencode"]
        txt = {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "final answer"}}
        self.assertEqual(b.parse(txt).get("message"), "final answer")
        fin = {"type": "step_finish", "sessionID": "ses_1", "part": {"type": "step-finish", "reason": "stop"}}
        self.assertIsNone(b.parse(fin).get("message"))
        self.assertIsNone(b.parse({"type": "text", "sessionID": "s", "part": None}).get("message"))

    def test_read_only_policy_denies_by_default(self):
        # THE critical one, and it has two halves.
        #
        # (a) Shape: opencode silently ignores the rule-ARRAY permission form --
        # the shape `opencode debug agent` prints -- and such a run FAILS OPEN,
        # so the policy must stay a JSON object.
        #
        # (b) Default: an enumerate-the-write-tools policy is not a boundary,
        # because opencode leaves unlisted keys at allow. Measured on 1.18.15, a
        # policy denying edit/write/bash still let a "read-only" run reach the
        # network via websearch, and `task`, `skill`, MCP and custom tools were
        # open too. Only deny-by-default plus a read allowlist closes it.
        b = ds.BACKENDS["opencode"]
        # Assert the policy the child actually RECEIVES, not the constant. The
        # earlier version read READ_ONLY_PERMISSION directly and stayed green
        # when the OPENCODE_PERMISSION export was deleted from isolation_env --
        # that is, with the entire read-only boundary removed.
        exported = b.env_overrides({"task": "polwire", "access": "read-only"})["OPENCODE_PERMISSION"]
        self.assertEqual(json.loads(exported), json.loads(b.READ_ONLY_PERMISSION),
                         "the read-only policy must reach the backend environment")
        self.assertEqual(
            json.loads(b.env_overrides({"task": "polwire", "access": "danger-full-access"})
                       ["OPENCODE_PERMISSION"]),
            json.loads(b.FULL_PERMISSION))
        policy = json.loads(exported)
        self.assertIsInstance(policy, dict, "array-form permissions are ignored by opencode and fail open")
        self.assertEqual(policy.get("*"), "deny", "unlisted tools must not default to allow")
        self.assertEqual(list(policy)[0], "*", "the catch-all must come first; last match wins")
        self.assertEqual(policy.get("glob"), "allow")
        self.assertEqual(policy.get("grep"), "allow")
        read = policy.get("read") or {}
        self.assertEqual(read.get("*"), "allow")
        # `read` also covers opencode's MCP resource operations, so a bare allow
        # would let a delegate reading an untrusted repo pull data out of an
        # operator-configured MCP server. Order matters: last match wins, so the
        # deny has to come after the allow.
        self.assertEqual(read.get("mcp:*"), "deny")
        self.assertGreater(list(read).index("mcp:*"), list(read).index("*"))
        for opened in ("bash", "task", "webfetch", "websearch", "skill", "edit"):
            self.assertNotIn(opened, policy, f"{opened} must stay denied by the catch-all")

    def test_pure_flag_precedes_the_subcommand(self):
        # `--pure` keeps external plugins out, and it is a GLOBAL flag: after the
        # subcommand opencode would reject it, losing the isolation silently.
        b = ds.BACKENDS["opencode"]
        argv = b.spawn_cmd({"exec_root": "/tmp/x", "model": "m", "access": "read-only", "prompt": "p"})[0]
        self.assertEqual(argv[1], "--pure")
        self.assertEqual(argv[2], "run")

    def test_access_mapping(self):
        b = ds.BACKENDS["opencode"]
        # --auto on both supported modes: it cannot override an explicit deny
        # (a denied rule returns before any permission event is published) and it
        # clears residual `ask` states that would block a headless turn.
        self.assertEqual(b.sandbox_args("read-only"), ["--auto"])
        self.assertEqual(b.sandbox_args("danger-full-access"), ["--auto"])
        # Opencode's bash confinement is a heuristic on the command string, not a
        # filesystem boundary, so there is no honest confined write mode to
        # offer -- the same refusal pi and agy already carry.
        with self.assertRaises(ds.HerdError):
            b.sandbox_args("workspace-write")

    def test_variant_is_sent_only_when_requested(self):
        # Opencode accepts any --variant string and silently ignores an unknown
        # one, so a tier nobody asked for would be an invisible no-op.
        b = ds.BACKENDS["opencode"]
        meta = {"exec_root": "/tmp/x", "model": "opencode/x-preview-f-free", "access": "read-only", "prompt": "p"}
        self.assertNotIn("--variant", b.spawn_cmd(meta)[0])
        argv = b.spawn_cmd({**meta, "effort": "high"})[0]
        self.assertEqual(argv[argv.index("--variant") + 1], "high")

    def test_prompt_goes_on_stdin_not_argv(self):
        # `opencode run` blocks forever on a stdin that never reaches EOF, so the
        # supervisor's write-then-close path is the required shape; it also keeps
        # long prompts off argv.
        b = ds.BACKENDS["opencode"]
        argv, stdin_text, _ = b.spawn_cmd(
            {"exec_root": "/tmp/x", "model": "m", "access": "read-only", "prompt": "the task"})
        self.assertEqual(stdin_text, "the task")
        self.assertNotIn("the task", argv)

    def _models_env(self, body):
        fh = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        fh.write(body)
        fh.close()
        os.environ["DELEGATE_MODELS_FILE"] = fh.name
        self.addCleanup(lambda: os.environ.pop("DELEGATE_MODELS_FILE", None))
        self.addCleanup(lambda: os.unlink(fh.name))

    def test_profile_set_comes_from_config_not_code(self):
        # Opencode fronts several providers and its free tier rotates, so the
        # profile set has to be editable without touching either runner. This
        # asserts profiles the source has never heard of resolve correctly, which
        # is the whole reason the list lives in config/models.env.
        self._models_env(
            "DELEGATE_OPENCODE_PROFILES=go-kimi, alpha\n"
            "DELEGATE_OPENCODE_MODEL_GO_KIMI=opencode-go/kimi-k9\n"
            "DELEGATE_OPENCODE_MODEL_ALPHA=vendor/alpha-preview\n")
        self.assertEqual(ds.opencode_profiles(), ["go-kimi", "alpha"])
        # The first listed profile is the default.
        self.assertEqual(ds.resolve_model_effort("opencode", "terra", False, None, None),
                         ("opencode-go/kimi-k9", None))
        # A dash in a profile name maps to an underscore in the config key.
        self.assertEqual(ds.resolve_model_effort("opencode", "go-kimi", True, None, None),
                         ("opencode-go/kimi-k9", None))
        self.assertEqual(ds.resolve_model_effort("opencode", "alpha", True, None, None),
                         ("vendor/alpha-preview", None))
        with self.assertRaises(ds.HerdError):
            ds.resolve_model_effort("opencode", "ox", True, None, None)

    def test_empty_profile_list_requires_an_explicit_model(self):
        # The zero-maintenance option: pin nothing in the kit and name the model
        # per call, exactly as the claude backend already works.
        self._models_env("DELEGATE_OPENCODE_PROFILES=\n")
        self.assertEqual(ds.opencode_profiles(), [])
        with self.assertRaises(ds.HerdError):
            ds.resolve_model_effort("opencode", "terra", False, None, None)
        self.assertEqual(ds.resolve_model_effort("opencode", "terra", False, "vendor/pinned", None),
                         ("vendor/pinned", None))

    def test_resume_uses_session_flag(self):
        b = ds.BACKENDS["opencode"]
        argv = b.resume_cmd({"exec_root": "/tmp/x", "model": "m", "access": "read-only",
                             "prompt": "p", "session_id": "ses_9"})[0]
        self.assertEqual(argv[argv.index("--session") + 1], "ses_9")


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

    def test_opencode_spawn_runs_and_reports(self):
        self.spawn(name="oc", backend="opencode", mode="readonly")
        self.assertTrue(self.wait_done("oc"))
        payload = ds.reconcile(ds.task_dir("oc"))
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["session_id"], "sess-fake-0001")
        self.assertIn("do the thing", (ds.task_dir("oc") / "report.md").read_text())

    def test_detached_backend_actually_receives_the_isolation_env(self):
        # The oracle the unit assertions cannot provide. env_overrides() can be
        # correct while nothing reaches the child: deleting
        # `env.update(backend.env_overrides(meta))` from run_turn left the
        # policy unit test AND the ordinary spawn test green. This crosses the
        # whole handoff -- env_overrides -> env.update -> env_unset -> Popen ->
        # detached backend -- and reads what the backend actually saw.
        dump = Path(self.tmp.name) / "envdump.json"
        os.environ["FAKE_ENV_DUMP"] = str(dump)
        # Inherited inline overrides must be scrubbed, so plant them first.
        os.environ["OPENCODE_CONFIG"] = "/tmp/hostile-config.json"
        os.environ["OPENCODE_CONFIG_CONTENT"] = '{"agent":{"build":{"permission":{"*":"allow"}}}}'
        os.environ["OPENCODE_AUTH_CONTENT"] = "hostile"
        try:
            self.spawn(name="envoracle", backend="opencode", mode="readonly")
            self.assertTrue(self.wait_done("envoracle"))
        finally:
            for k in ("FAKE_ENV_DUMP", "OPENCODE_CONFIG",
                      "OPENCODE_CONFIG_CONTENT", "OPENCODE_AUTH_CONTENT"):
                os.environ.pop(k, None)
        self.assertTrue(dump.exists(), "the backend never recorded its environment")
        seen = json.loads(dump.read_text())
        self.assertEqual(json.loads(seen["OPENCODE_PERMISSION"] or "null"),
                         json.loads(ds.BACKENDS["opencode"].READ_ONLY_PERMISSION),
                         "the read-only policy did not reach the detached backend")
        self.assertEqual(seen["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        self.assertIn("envoracle", seen["OPENCODE_DB"] or "")
        for scrubbed in ("OPENCODE_CONFIG", "OPENCODE_AUTH_CONTENT"):
            self.assertIsNone(seen[scrubbed],
                              f"{scrubbed} was inherited into the backend despite env_unset")
        # OPENCODE_CONFIG_CONTENT is ours now, and it must define the agent the
        # run selects. The top-level policy alone does not bind: an agent's own
        # permission block is merged after it and the last match wins, so an
        # ordinary global `agent.build.permission` outranked it. Measured -- with
        # one present, a read-only run created the file it was denied.
        cfg = json.loads(seen["OPENCODE_CONFIG_CONTENT"] or "{}")
        agent = ds.BACKENDS["opencode"].agent_name("read-only")
        self.assertIn(agent, cfg.get("agent", {}),
                      "the delekit-owned agent never reached the backend")
        self.assertEqual(cfg["agent"][agent]["permission"],
                         json.loads(ds.BACKENDS["opencode"].READ_ONLY_PERMISSION))

    def test_opencode_workspace_is_refused_before_state(self):
        # The refusal must land before any task directory exists, so a rejected
        # run leaves nothing to reap.
        rc = self.spawn(name="oc-ws", backend="opencode")
        self.assertEqual(rc, 2)
        self.assertFalse(ds.task_dir("oc-ws").exists())

    def test_opencode_read_only_preamble_discloses_missing_shell(self):
        # Denying bash is what makes read-only honest, and it costs the delegate
        # git, ripgrep, and tests. An undisclosed loss invites a delegate that
        # quietly narrows the task to whatever it can still verify.
        quiet_main(["spawn", "readonly", "inspect", "--json", "--project-root", self.proj.name,
                    "--model", "m", "--name", "oc-pre", "--backend", "opencode",
                    "--stall-after", "60", "--deadline", "600"])
        self.assertTrue(self.wait_done("oc-pre"))
        prompt = (ds.task_dir("oc-pre") / "prompt.md").read_text()
        self.assertIn("cannot run git, ripgrep, or tests", prompt)

    def test_turn_with_no_agent_message_fails_loudly(self):
        # A backend can exit 0 having said nothing at all -- measured on the real
        # `opencode/nemotron-3.5-lightning-free`, which emits a step_start and a
        # step_finish and stops. dairy already treats an empty report as failed,
        # so herd must too: marking it `done` would hand the caller a silent
        # non-answer that reads as success.
        os.environ["FAKE_EMPTY"] = "1"
        try:
            self.spawn(name="empty", backend="opencode", mode="readonly")
            self.assertTrue(self.wait_done("empty"))
            payload = ds.reconcile(ds.task_dir("empty"))
        finally:
            os.environ.pop("FAKE_EMPTY", None)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["stall_reason"], "empty-report")
        self.assertIn("no final message", (ds.task_dir("empty") / "report.md").read_text())

    def _floor(self, value):
        fh = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        fh.write(f"DELEGATE_OPENCODE_MIN_VERSION={value}\n"
                 "DELEGATE_OPENCODE_PROFILES=ox\n"
                 "DELEGATE_OPENCODE_MODEL_OX=vendor/ox\n")
        fh.close()
        os.environ["DELEGATE_MODELS_FILE"] = fh.name
        self.addCleanup(lambda: os.environ.pop("DELEGATE_MODELS_FILE", None))
        self.addCleanup(lambda: os.unlink(fh.name))

    def test_opencode_below_the_configured_floor_is_refused(self):
        # An untested-old binary must fail loudly before any task state exists,
        # rather than be discovered by a worker wedging later.
        self._floor("1.18.15")
        os.environ["FAKE_OPENCODE_VERSION"] = "1.17.0"
        try:
            rc = self.spawn(name="oc-old", backend="opencode", mode="readonly")
        finally:
            os.environ.pop("FAKE_OPENCODE_VERSION", None)
        self.assertEqual(rc, 7)
        self.assertFalse(ds.task_dir("oc-old").exists())

    def test_version_floor_is_config_not_code(self):
        # Versions move constantly, so raising or lowering the floor must never
        # need a source edit on two platforms -- the same reason the profile list
        # is data. A floor the source has never heard of has to take effect.
        self._floor("9.9.9")
        os.environ["FAKE_OPENCODE_VERSION"] = "1.18.20"
        try:
            self.assertEqual(ds.opencode_min_version(), (9, 9, 9))
            self.assertEqual(self.spawn(name="oc-floor", backend="opencode", mode="readonly"), 7)
        finally:
            os.environ.pop("FAKE_OPENCODE_VERSION", None)

    def test_empty_floor_disables_the_check(self):
        # The escape hatch: an operator who decides the guard is wrong must be
        # able to switch it off without patching the kit.
        self._floor("")
        os.environ["FAKE_OPENCODE_VERSION"] = "0.0.1"
        try:
            self.assertIsNone(ds.opencode_min_version())
            self.assertEqual(self.spawn(name="oc-nofloor", backend="opencode", mode="readonly"), 0)
            self.assertTrue(self.wait_done("oc-nofloor"))
        finally:
            os.environ.pop("FAKE_OPENCODE_VERSION", None)

    def test_effort_is_checked_against_the_models_declared_variants(self):
        # opencode ignores an unknown --variant in silence and there is no
        # universal vocabulary, so an effort it cannot honour has to fail loudly
        # instead of quietly doing nothing. A declared one must pass through.
        os.environ["FAKE_VARIANTS"] = "low,high"
        try:
            self.assertEqual(self.spawn(name="oc-bad", backend="opencode", mode="readonly",
                                        *["--effort", "medium"]), 2)
            self.assertFalse(ds.task_dir("oc-bad").exists())
            self.assertEqual(self.spawn(name="oc-ok", backend="opencode", mode="readonly",
                                        *["--effort", "high"]), 0)
            self.assertTrue(self.wait_done("oc-ok"))
        finally:
            os.environ.pop("FAKE_VARIANTS", None)

    def test_effort_refused_when_the_model_declares_none(self):
        # A model with no variants would swallow any effort silently.
        os.environ["FAKE_VARIANTS"] = ""
        try:
            self.assertEqual(self.spawn(name="oc-none", backend="opencode", mode="readonly",
                                        *["--effort", "high"]), 2)
        finally:
            os.environ.pop("FAKE_VARIANTS", None)

    def test_watchdog_kills_detached_grandchildren(self):
        # opencode's shell tool detaches commands into their own process group.
        # The watchdog used to signal only the direct child, so a stall-kill
        # could mark a task terminal while a detached shell kept writing to the
        # repo. Containment has to cover the whole tree or the terminal state is
        # a lie about what is still running.
        if os.name == "nt":
            self.skipTest("POSIX process-group semantics")
        pidfile = os.path.join(self.tmp.name, "orphan.pid")
        os.environ["FAKE_SPAWN_ORPHAN"] = "1"
        os.environ["FAKE_ORPHAN_PIDFILE"] = pidfile
        try:
            self.spawn(name="orphan", backend="opencode", mode="readonly",
                       *["--stall-after", "2"])
            deadline = time.time() + 30
            while time.time() < deadline and not os.path.exists(pidfile):
                time.sleep(0.2)
            self.assertTrue(os.path.exists(pidfile), "fake backend never detached a grandchild")
            orphan = int(open(pidfile).read().strip())
            self.assertTrue(self.wait_done("orphan", timeout=40))
        finally:
            os.environ.pop("FAKE_SPAWN_ORPHAN", None)
            os.environ.pop("FAKE_ORPHAN_PIDFILE", None)
        gone = time.time() + 15
        while time.time() < gone and ds.pid_alive(orphan):
            time.sleep(0.2)
        alive = ds.pid_alive(orphan)
        if alive:
            os.kill(orphan, 9)
        self.assertFalse(alive, "a detached grandchild outlived the watchdog kill")

    def test_failed_resume_never_shows_the_previous_turns_answer(self):
        # report.md means "this turn's result". Leaving the old text in place let
        # a failed resume present the PRECEDING successful answer as current --
        # reproduced at exit 7, where the task went failed/exit-7 while report.md
        # still held turn one's answer. Every non-success path must replace it.
        self.spawn(prompt="first turn", name="fresh", backend="opencode", mode="readonly")
        self.assertTrue(self.wait_done("fresh"))
        report = ds.task_dir("fresh") / "report.md"
        self.assertIn("first turn", report.read_text())
        os.environ["FAKE_EXIT_CODE"] = "7"
        try:
            quiet_main(["send", "fresh", "second turn", "--json", "--no-preamble"])
            self.assertTrue(self.wait_done("fresh"))
        finally:
            os.environ.pop("FAKE_EXIT_CODE", None)
        payload = ds.reconcile(ds.task_dir("fresh"))
        self.assertEqual(payload["state"], "failed")
        self.assertNotIn("first turn", report.read_text(),
                         "a failed turn must not present the previous answer as its result")
        self.assertIn("exited 7", report.read_text())
        # The prior answer is preserved beside it rather than destroyed.
        self.assertIn("first turn", (ds.task_dir("fresh") / "previous-report.md").read_text())

    def test_send_rotates_the_report_before_the_helper_can_fail(self):
        # The decisive case the earlier fixture could not reach, because it
        # deleted report.md in its own setup. A completed turn leaves a non-empty
        # report; `send` then changes state and launches a helper. If the helper
        # never reaches run_turn -- launch failure, import error, killed at
        # start -- the OLD answer is still there when reconciliation settles the
        # turn failed, and `result` presents the preceding success as this turn's
        # result. Rotation therefore happens at the COMMAND boundary, not inside
        # the child.
        self.spawn(prompt="first turn", name="rot", backend="opencode", mode="readonly")
        self.assertTrue(self.wait_done("rot"))
        tdir = ds.task_dir("rot")
        report = tdir / "report.md"
        self.assertIn("first turn", report.read_text())

        # Break the PINNED binary, not the env var: since the admitted path is
        # recorded in meta.json, that is what the helper will try to launch.
        meta = ds.read_json(tdir / "meta.json")
        meta["backend_bin"] = str(Path(self.tmp.name) / "no-such-binary")
        ds.atomic_write_json(tdir / "meta.json", meta)
        quiet_main(["send", "rot", "second turn", "--json", "--no-preamble"])
        self.assertTrue(self.wait_done("rot"))
        payload = ds.reconcile(tdir)
        self.assertEqual(payload["state"], "failed")
        current = report.read_text() if report.exists() else ""
        self.assertNotIn("first turn", current,
                         "a failed turn must never present the preceding answer as its result")
        self.assertIn("first turn", (tdir / "previous-report.md").read_text())

    def test_startup_failure_never_leaves_the_previous_answer_standing(self):
        # report.md is rotated BEFORE anything that can fail. A turn that never
        # starts -- missing binary, bad exec root -- used to exit with the prior
        # answer still in place, so `result` returned a stale success for a run
        # that never ran.
        self.spawn(prompt="first turn", name="startfail", backend="opencode", mode="readonly")
        self.assertTrue(self.wait_done("startfail"))
        report = ds.task_dir("startfail") / "report.md"
        self.assertIn("first turn", report.read_text())
        meta = ds.read_json(ds.task_dir("startfail") / "meta.json")
        meta["backend_bin"] = str(Path(self.tmp.name) / "definitely-not-here")
        ds.atomic_write_json(ds.task_dir("startfail") / "meta.json", meta)
        quiet_main(["send", "startfail", "second turn", "--json", "--no-preamble"])
        self.assertTrue(self.wait_done("startfail"))
        payload = ds.reconcile(ds.task_dir("startfail"))
        self.assertEqual(payload["state"], "failed")
        self.assertNotIn("first turn", report.read_text(),
                         "a turn that never started must not present the previous answer")
        self.assertIn("first turn", (ds.task_dir("startfail") / "previous-report.md").read_text())

    def test_crash_after_backend_start_still_settles_the_turn(self):
        # A backend that started successfully can still crash the runner: a
        # parser exception, an events.jsonl write error, a proc.wait error.
        # Those escaped run_turn entirely, so the helper exited with no current
        # report and `result` printed an empty placeholder for a crashed turn.
        # run_turn is driven in-process here, because monkeypatching the parser
        # in this process cannot reach a detached helper.
        self.spawn(name="crash", backend="opencode", mode="readonly")
        self.assertTrue(self.wait_done("crash"))
        tdir = ds.task_dir("crash")
        (tdir / ".done").unlink()
        original = ds.BACKENDS["opencode"].parse

        def boom(obj):
            raise RuntimeError("parser boom")

        ds.BACKENDS["opencode"].parse = boom
        try:
            rc = ds.run_turn("crash", "spawn")
        finally:
            ds.BACKENDS["opencode"].parse = original
        self.assertEqual(rc, 1)
        payload = ds.reconcile(tdir)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["stall_reason"], "helper-error")
        report = (tdir / "report.md").read_text()
        self.assertIn("parser boom", report)
        self.assertIn("failed inside the runner", report)

    def test_dead_helper_never_leaves_an_empty_or_stale_report(self):
        # If the helper dies without settling -- killed from outside, OOM --
        # reconcile marked it failed but wrote no report, so `result` returned an
        # empty placeholder. It now writes a diagnostic.
        self.spawn(prompt="first turn", name="deadh", backend="opencode", mode="readonly")
        self.assertTrue(self.wait_done("deadh"))
        tdir = ds.task_dir("deadh")
        # Simulate a helper that exited mid-turn: no marker, no report, dead pid.
        (tdir / ".done").unlink()
        (tdir / "report.md").unlink()
        meta = ds.read_json(tdir / "meta.json")
        meta["state"] = "working"
        meta["pid"] = 999999
        ds.atomic_write_json(tdir / "meta.json", meta)
        # helper_pid reads helper.json first, then falls back to meta["pid"].
        ds.atomic_write_json(tdir / "helper.json", {"helper_pid": 999999})
        ds.atomic_write_json(tdir / "child.json", {"helper_pid": 999999, "backend_pid": 999998})
        payload = ds.reconcile(tdir)
        self.assertEqual(payload["state"], "failed")
        self.assertIn("without settling", (tdir / "report.md").read_text())

    def _git_project(self):
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", root, *cmd], check=True, capture_output=True)
        Path(root, ".gitignore").write_text(".worktrees/\n")
        Path(root, "seed.txt").write_text("seed\n")
        subprocess.run(["git", "-C", root, "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", root, "commit", "-qm", "init"], check=True, capture_output=True)
        return root

    @staticmethod
    def _commits(repo):
        out = subprocess.run(["git", "-C", repo, "rev-list", "--count", "HEAD"],
                             capture_output=True, text=True)
        return int((out.stdout or "0").strip() or 0)

    def test_worktree_autocommit_only_on_success(self):
        # The earlier version monkeypatched _maybe_autocommit and asserted on its
        # own list -- but the turn runs in a DETACHED process importing its own
        # module, so the patch could never be observed and the assertion was
        # guaranteed to pass. Reverting the repair left the suite green. This
        # drives the real path with git as the oracle, on BOTH sides: a failed
        # turn must not commit, and a successful one must.
        proj = self._git_project()
        base = self._commits(proj)
        os.environ["FAKE_WRITE_FILE"] = "worker-output.txt"
        try:
            os.environ["FAKE_EXIT_CODE"] = "3"
            try:
                self.spawn(name="wt-fail", backend="codex", mode="workspace",
                           *["--worktree", "--project-root", proj])
                self.assertTrue(self.wait_done("wt-fail"))
            finally:
                os.environ.pop("FAKE_EXIT_CODE", None)
            self.assertEqual(ds.reconcile(ds.task_dir("wt-fail"))["state"], "failed")
            wt_bad = ds.read_json(ds.task_dir("wt-fail") / "meta.json")["exec_root"]
            self.assertTrue(Path(wt_bad, "worker-output.txt").exists(),
                            "the backend must actually have written into the worktree")
            self.assertEqual(self._commits(wt_bad), base,
                             "a failed turn must not commit the worker's partial output")

            self.spawn(name="wt-ok", backend="codex", mode="workspace",
                       *["--worktree", "--project-root", proj])
            self.assertTrue(self.wait_done("wt-ok"))
            self.assertEqual(ds.reconcile(ds.task_dir("wt-ok"))["state"], "done")
            wt_ok = ds.read_json(ds.task_dir("wt-ok") / "meta.json")["exec_root"]
            self.assertGreater(self._commits(wt_ok), base,
                               "a successful turn must commit the worktree")
        finally:
            os.environ.pop("FAKE_WRITE_FILE", None)

    def test_descendant_holding_stdout_cannot_hang_the_turn(self):
        # opencode's shell tool launches commands with setsid, and such a child
        # inherits the backend's stdout. When the backend exits, the supervisor's
        # reader stays blocked on a pipe nobody will close. With the watchdog
        # stopping at child exit there was no stall and no deadline left, so the
        # helper hung forever with a COMPLETE answer sitting unreported -- until
        # an outside reconcile SIGINT'd it and overwrote that answer with a crash
        # diagnostic. Reproduced against the real detached path before the fix.
        if os.name == "nt":
            self.skipTest("POSIX setsid semantics")
        os.environ["FAKE_ORPHAN_HOLDS_PIPE"] = "1"
        try:
            self.spawn(prompt="answer me", name="pipehold", backend="opencode", mode="readonly")
            settled = self.wait_done("pipehold", timeout=ds.PIPE_DRAIN_GRACE_S + 25)
        finally:
            os.environ.pop("FAKE_ORPHAN_HOLDS_PIPE", None)
        self.assertTrue(settled, "the turn never settled: the reader was still blocked on the pipe")
        payload = ds.reconcile(ds.task_dir("pipehold"))
        self.assertEqual(payload["state"], "done",
                         "a completed answer must not be lost to a held-open pipe")
        self.assertIn("answer me", (ds.task_dir("pipehold") / "report.md").read_text())

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

    def test_opencode_send_resumes_via_session_id(self):
        self.spawn(prompt="first turn", name="ocr", backend="opencode", mode="readonly")
        self.assertTrue(self.wait_done("ocr"))
        self.assertEqual(quiet_main(["send", "ocr", "second turn", "--json", "--no-preamble"]), 0)
        self.assertTrue(self.wait_done("ocr"))
        payload = ds.reconcile(ds.task_dir("ocr"))
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["session_id"], "sess-fake-0001")
        self.assertIn("resumed:", (ds.task_dir("ocr") / "report.md").read_text())

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

#!/usr/bin/env python3
"""herd: detached, resumable, steerable headless delegate workers.

Backends: codex, pi, claude, muse, opencode, grok.

Stdlib only. See docs/detached-runner.md for the design. This is the shared
cross-platform core; bin/herd.sh and bin/herd.ps1 are thin shims onto it.

Backends stream JSON so the turn helper can capture the session id (for resume)
and observe activity (for the stall watchdog). The exact provider event schema
is an integration boundary: the parsers below accept a tolerant superset and are
the point to re-verify after a backend CLI upgrade.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any

from worktree_manager import WorktreeError, create_worktree as create_project_worktree

KIT_ROOT = Path(__file__).resolve().parents[1]

TERMINAL_STATES = {"done", "failed", "stalled", "killed"}
RESUMABLE_UNRESOLVED = {"failed", "stalled", "awaiting_reply"}
RECLAIMABLE_CLEAN = {"done", "killed"}
NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
QUESTION_RE = re.compile(r"(^|\n)\s*QUESTION:", re.IGNORECASE)

PIPE_DRAIN_GRACE_S = 10
EVENT_QUEUE_MAXSIZE = 2000
# How long to wait for a backend to exit once its stream has ended.
PROCESS_EXIT_GRACE_S = 30
# `ps` timeout used by the descendant scan; the watcher join must exceed it.
PS_SCAN_TIMEOUT_S = 10
# How much of a live events.jsonl `peek` will read to find its tail.
PEEK_TAIL_BYTES = 4 << 20
# A single JSONL record larger than this is not an event, it is a runaway; keep
# a bounded prefix as evidence rather than buffering it whole.
MAX_EVENT_BYTES = 1 << 20
DEFAULT_STALL_AFTER_S = int(os.environ.get("DELEGATE_STALL_AFTER_S", "3600"))
DEFAULT_DEADLINE_S = int(os.environ.get("DELEGATE_MAX_WALLCLOCK_S", str(6 * 3600)))
DEFAULT_IDLE_MIN = 30
EVENTS_MAX_MB = int(os.environ.get("DELEGATE_EVENTS_MAX_MB", "32"))


class HerdError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# Environment / paths
# --------------------------------------------------------------------------- #
def state_root() -> Path:
    override = os.environ.get("DELEGATE_STATE_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return Path(base) / "delekit"
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "delekit"


def device_id() -> str:
    explicit = os.environ.get("DELEKIT_DEVICE_ID")
    raw = explicit if explicit else "dev-" + hashlib.sha256(socket.gethostname().encode()).hexdigest()[:8]
    cleaned = NAME_RE.sub("-", raw).strip("-")
    return cleaned or "dev-unknown"


def sessions_dir() -> Path:
    return state_root() / "sessions" / device_id()


def owner_id() -> str:
    return os.path.realpath(os.getcwd())


def sanitize_task(name: str) -> str:
    cleaned = NAME_RE.sub("-", name).strip("-")
    if not cleaned:
        raise HerdError(2, f"invalid task name: {name!r}")
    return cleaned


def default_task_name() -> str:
    return "herd-" + time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"


def load_models_env() -> dict[str, str]:
    path = os.environ.get("DELEGATE_MODELS_FILE")
    p = Path(path) if path else KIT_ROOT / "config" / "models.env"
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# Atomic state I/O
# --------------------------------------------------------------------------- #
def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- #
# Process liveness (cross-platform)
# --------------------------------------------------------------------------- #
def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _descendant_pids(root: int) -> list[int]:
    """Every live descendant of root. Empty when the process table is unreadable."""
    if os.name == "nt":
        return []
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,ppid="], capture_output=True,
                             text=True, timeout=PS_SCAN_TIMEOUT_S).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            kid, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(parent, []).append(kid)
    found: list[int] = []
    queue = [root]
    while queue:
        for kid in children.get(queue.pop(), ()):
            if kid not in found and kid != root:
                found.append(kid)
                queue.append(kid)
    return found


def kill_process_tree(pid: int, known: list[int] | None = None) -> None:
    """Kill a process and every descendant, including ones that left its group.

    A process-group signal only reaches processes still in that group, and a
    tool child that calls setsid is not -- which is exactly how opencode's POSIX
    shell tool launches commands. Without this walk a stall- or deadline-kill
    marks a task terminal while a detached shell keeps writing to the repo, so
    the terminal state lies about what is still running.
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
        return
    for child in (known or []) + _descendant_pids(pid):
        _signal_quiet(child, signal.SIGKILL)
    _killpg_quiet(pid, signal.SIGKILL)
    _signal_quiet(pid, signal.SIGKILL)


def stop_pid(pid: Any) -> None:
    """Stop a detached helper and its whole process TREE. No-op if gone."""
    if not pid_alive(pid):
        return
    pid = int(pid)
    # Snapshot descendants first: once the parent exits they are reparented to
    # init and can no longer be found by walking down from this pid.
    tree = _descendant_pids(pid)
    if os.name == "nt":
        _signal_quiet(pid, signal.CTRL_BREAK_EVENT)
    else:
        _killpg_quiet(pid, signal.SIGINT)
    if os.name == "nt":
        # CTRL_BREAK_EVENT only reaches processes sharing the caller's console,
        # and the child is launched with CREATE_NO_WINDOW, so it has none. On
        # top of that _descendant_pids returns [] here, so the liveness loop
        # below would see an empty tree, observe the root exit, and return
        # WITHOUT ever calling taskkill -- leaving the children alive. taskkill
        # /T /F is the only real containment primitive on this platform, so it
        # runs immediately rather than as a five-second fallback.
        kill_process_tree(pid, tree)
        return
    deadline = now() + 5
    while now() < deadline:
        if not pid_alive(pid) and not any(pid_alive(k) for k in tree):
            return
        time.sleep(0.2)
    kill_process_tree(pid, tree)


def _killpg_quiet(pid: int, sig: int) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _signal_quiet(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Backend adapters
# --------------------------------------------------------------------------- #
def _text_from_content(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        joined = "".join(parts).strip()
        return joined or None
    return None


class Backend:
    name = ""
    bin_env = ""

    def resolved_bin(self, meta: dict[str, Any]) -> str:
        """The exact binary this task was admitted with, if one was pinned.

        Spawn validates a path (version floor, variant enumeration) and then the
        DETACHED helper used to resolve again, and `send` a third time -- so a
        PATH change, an override change, or a reinstall between turns could run a
        binary that was never checked. Pinning it in meta.json closes that.
        """
        pinned = meta.get("backend_bin")
        if isinstance(pinned, str) and pinned:
            return os.path.abspath(pinned)
        return self.locate_bin()

    def locate_bin(self) -> str:
        override = os.environ.get(self.bin_env)
        if override:
            if not (os.path.isfile(override) and os.access(override, os.X_OK)):
                raise HerdError(7, f"{self.bin_env}={override} is not executable")
            return os.path.abspath(override)
        found = shutil.which(self.name)
        if not found:
            raise HerdError(7, f"{self.name} CLI not found on PATH (or set {self.bin_env})")
        # Absolute: backends run with cwd=exec_root, where a relative path is a
        # different file, or none.
        return os.path.abspath(found)

    def sandbox_args(self, access: str) -> list[str]:
        raise NotImplementedError

    def env_overrides(self, meta: dict[str, Any]) -> dict[str, str]:
        """Extra environment for the backend child.

        Only opencode needs this: its access boundary is an environment
        variable rather than a flag. Everything else keeps the inherited env.
        """
        return {}

    def env_unset(self, meta: dict[str, Any]) -> list[str]:
        """Inherited variables that must NOT reach the backend child."""
        return []

    def spawn_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        raise NotImplementedError

    def resume_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        raise NotImplementedError

    def parse(self, obj: dict[str, Any]) -> dict[str, str]:
        raise NotImplementedError


class CodexBackend(Backend):
    name = "codex"
    bin_env = "DELEGATE_CODEX_BIN"

    def sandbox_args(self, access: str) -> list[str]:
        if access == "danger-full-access":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        return ["--sandbox", access, "-c", "approval_policy=never"]

    def fast_args(self, meta: dict[str, Any]) -> list[str]:
        # Explicit opt-in only: fast_mode is disabled unless the run asked for
        # it, so it can never be inherited from ambient Codex config. Mirrors
        # bin/dairy.sh.
        return ["--enable" if meta.get("fast") else "--disable", "fast_mode"]

    def spawn_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        exec_root = meta["exec_root"]
        argv = [self.locate_bin(), "exec", "--json", "--cd", exec_root,
                "--model", meta["model"], "-c", f"model_reasoning_effort={meta['effort']}",
                "--skip-git-repo-check", "--color", "never"]
        argv += self.fast_args(meta)
        argv += self.sandbox_args(meta["access"])
        argv.append("-")
        return argv, meta["prompt"], exec_root

    def resume_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        # `codex exec resume` (>=0.145) accepts only a narrow flag set: no
        # --sandbox/--cd/--color. Sandbox and approvals go through -c overrides;
        # cwd comes from the Popen cwd. Verified against codex-cli 0.145.0.
        exec_root = meta["exec_root"]
        argv = [self.locate_bin(), "exec", "resume", "--json",
                "--model", meta["model"], "-c", f"model_reasoning_effort={meta['effort']}",
                "--skip-git-repo-check"]
        argv += self.fast_args(meta)
        if meta["access"] == "danger-full-access":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            argv += ["-c", f"sandbox_mode={meta['access']}", "-c", "approval_policy=never"]
        argv += [meta["session_id"], "-"]
        return argv, meta["prompt"], exec_root

    def parse(self, obj: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for k in ("session_id", "sessionId", "thread_id", "conversation_id"):
            if isinstance(obj.get(k), str):
                out["session_id"] = obj[k]
        sess = obj.get("session")
        if isinstance(sess, dict) and isinstance(sess.get("id"), str):
            out["session_id"] = sess["id"]
        # codex-cli >= 0.14x: {"type":"item.completed","item":{"type":"agent_message","text":...}}
        item = obj.get("item")
        if obj.get("type") in ("item.completed", "item.updated") and isinstance(item, dict):
            itype = item.get("item_type") or item.get("type")
            if itype == "agent_message" and isinstance(item.get("text"), str):
                out["message"] = item["text"]
            elif itype == "reasoning" and isinstance(item.get("text"), str):
                out["thinking"] = item["text"]
            return out
        # older event shapes: {"msg":{"type":"agent_message","message":...}} and friends
        msg = obj.get("msg") if isinstance(obj.get("msg"), dict) else obj
        mtype = msg.get("type")
        if mtype in ("agent_message", "assistant_message") and isinstance(msg.get("message"), str):
            out["message"] = msg["message"]
        elif isinstance(obj.get("agent_message"), str):
            out["message"] = obj["agent_message"]
        if mtype in ("agent_reasoning", "reasoning") and isinstance(msg.get("text"), str):
            out["thinking"] = msg["text"]
        return out


class PiBackend(Backend):
    """Pi coding agent using the ChatGPT-backed ``openai-codex`` provider.

    Pi has no filesystem sandbox. Read-only therefore removes shell and write
    tools; workspace-write is rejected before task state is created. Full mode
    is deliberately unrestricted. Ambient Pi extensions, skills, and prompt
    templates are disabled so headless jobs stay deterministic and lean, while
    project context files such as AGENTS.md still load normally.
    """

    name = "pi"
    bin_env = "DELEGATE_PI_BIN"

    def sandbox_args(self, access: str) -> list[str]:
        if access == "read-only":
            return ["--tools", "read,grep,find,ls"]
        if access == "danger-full-access":
            return []
        raise HerdError(2, "pi has no confined workspace-write mode; use readonly or explicit full")

    def _base(self, meta: dict[str, Any]) -> list[str]:
        session_dir = task_dir(meta["task"]) / "pi-session"
        argv = [self.locate_bin(), "--mode", "json", "--provider", "openai-codex",
                "--model", meta["model"], "--thinking", meta["effort"],
                "--session-dir", str(session_dir), "--no-approve", "--no-extensions",
                "--no-skills", "--no-prompt-templates"]
        argv += self.sandbox_args(meta["access"])
        return argv

    def spawn_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        return self._base(meta), meta["prompt"], meta["exec_root"]

    def resume_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        argv = self._base(meta) + ["--session-id", meta["session_id"]]
        return argv, meta["prompt"], meta["exec_root"]

    def parse(self, obj: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        if obj.get("type") == "session" and isinstance(obj.get("id"), str):
            out["session_id"] = obj["id"]
        if obj.get("type") == "message_end":
            message = obj.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                text = _text_from_content(message.get("content"))
                if text:
                    out["message"] = text
        return out


class ClaudeBackend(Backend):
    name = "claude"
    bin_env = "DELEGATE_CLAUDE_BIN"

    def permission_mode(self, access: str) -> str:
        return {"read-only": "plan", "workspace-write": "acceptEdits",
                "danger-full-access": "bypassPermissions"}.get(access, "default")

    def _base(self, meta: dict[str, Any]) -> list[str]:
        argv = [self.locate_bin(), "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", self.permission_mode(meta["access"]), "--add-dir", meta["exec_root"]]
        if meta.get("model"):
            argv += ["--model", meta["model"]]
        if meta.get("effort"):
            argv += ["--effort", meta["effort"]]
        return argv

    def spawn_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        return self._base(meta), meta["prompt"], meta["exec_root"]

    def resume_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        return self._base(meta) + ["--resume", meta["session_id"]], meta["prompt"], meta["exec_root"]

    def parse(self, obj: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        if isinstance(obj.get("session_id"), str):
            out["session_id"] = obj["session_id"]
        t = obj.get("type")
        if t == "assistant":
            text = _text_from_content((obj.get("message") or {}).get("content"))
            if text:
                out["message"] = text
        elif t == "result" and isinstance(obj.get("result"), str):
            out["message"] = obj["result"]
        elif t == "thinking" and isinstance(obj.get("thinking"), str):
            out["thinking"] = obj["thinking"]
        return out


class MuseBackend(Backend):
    """Muse CLI (`muse exec --json`).

    Resume is the same command with `--session-id`: muse continues that session
    and re-emits the same id, so spawn and resume differ only by that flag.

    Muse reads the prompt from a file, not stdin. The supervisor rewrites
    prompt.md in the task dir before every spawn and send, so pointing
    --prompt-file at it also keeps long prompts off argv.

    Access mapping (measured against Muse 0.1.0 on macOS): approval and the
    sandbox are ON by default, and approvals must be disabled or a headless run
    blocks. The default sandbox confines shell writes to the workspace plus temp
    dirs -- a $HOME write is denied -- which is the shape codex workspace-write
    has. --disable-write stops only the non-shell write tools, so read-only also
    drops the shell rather than overstating the label.
    """

    name = "muse"
    bin_env = "DELEGATE_MUSE_BIN"

    def sandbox_args(self, access: str) -> list[str]:
        if access == "danger-full-access":
            return ["--yolo"]
        if access == "read-only":
            return ["--disable-approval", "--disable-write", "--disable-shell"]
        return ["--disable-approval"]

    def _base(self, meta: dict[str, Any]) -> list[str]:
        exec_root = meta["exec_root"]
        argv = [self.locate_bin(), "exec", "--json", "--workspace", exec_root]
        if meta.get("model"):
            argv += ["--model", meta["model"]]
        if meta.get("effort"):
            argv += ["--reasoning-effort", meta["effort"]]
        argv += self.sandbox_args(meta["access"])
        argv += ["--prompt-file", str(task_dir(meta["task"]) / "prompt.md")]
        return argv

    def spawn_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        return self._base(meta), "", meta["exec_root"]

    def resume_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        return self._base(meta) + ["--session-id", meta["session_id"]], "", meta["exec_root"]

    def parse(self, obj: dict[str, Any]) -> dict[str, str]:
        # Envelope: {"stream":{"kind":"session","id":...},"payload_type":...,
        #            "payload":{...}}. The turn's answer arrives once, as
        # run.terminal.completed; run.output.delta carries the same text in
        # chunks, so taking only the terminal event avoids duplicating it.
        # Muse exposes no separate reasoning stream, so `peek --thinking` stays
        # empty for this backend.
        out: dict[str, str] = {}
        stream = obj.get("stream")
        if isinstance(stream, dict) and stream.get("kind") == "session" and isinstance(stream.get("id"), str):
            out["session_id"] = stream["id"]
        payload = obj.get("payload")
        if str(obj.get("payload_type", "")).startswith("run.terminal.") and isinstance(payload, dict):
            if isinstance(payload.get("text"), str):
                out["message"] = payload["text"]
        return out


class OpencodeBackend(Backend):
    """Opencode CLI (`opencode run --format json`).

    Every event carries a top-level `sessionID`, so the resume handle is on the
    very first line; `--session <id>` continues that session with full context
    and re-emits the same id. Each `text` part arrives as one complete event, so
    the last one is the final answer rather than a fragment.

    Two measured traps drive this adapter (opencode 1.18.15, macOS):

    - **stdin must reach EOF, or the run hangs forever.** `opencode run` accepts
      a piped prompt, so a child whose stdin stays open never starts its turn:
      probe runs produced zero bytes on stdout *and* stderr and had to be killed
      at 90-300s, while the identical task with stdin closed finished in 8s.
      `run_turn` writes the prompt and closes stdin, which is exactly the shape
      required -- do not change it to an inherited or held-open pipe. `--auto` is
      NOT the variable here and does not fix it.
    - **`OPENCODE_PERMISSION` honours the object form and silently ignores the
      rule-array form** -- which is the shape `opencode debug agent` prints as
      resolved config. The array form fails OPEN: a run denying edit+bash that
      way still created the file. Read-only therefore sends the exact object
      form measured to deny, and nothing else.

    Access mapping: read-only denies edit/write/bash, which costs the delegate a
    shell exactly as muse's read-only does; workspace-write is refused before any
    task state exists, because the bash tool writes outside `--dir` unprompted;
    full is deliberately unrestricted.
    """

    name = "opencode"
    bin_env = "DELEGATE_OPENCODE_BIN"

    # Deny by default, then allow back the read surface. Enumerating the write
    # tools is NOT sound: opencode's permission object leaves unlisted keys at
    # allow, so `task`, `webfetch`, `websearch`, `skill`, MCP tools and any
    # custom tool under .opencode/tool/ stay open. Measured on 1.18.15 -- a
    # policy denying edit/write/bash still let a "read-only" run reach the
    # network through websearch. Note `edit` is the real built-in key: the
    # `write` and `apply_patch` tools both resolve through it.
    # `read` covers opencode's MCP resource operations (list_mcp_resources,
    # list_mcp_resource_templates, read_mcp_resource), which ask under patterns
    # like `mcp:<server>:...`. A bare read allow would let a delegate reading an
    # untrusted repository pull private data out of an MCP server the OPERATOR
    # configured globally -- a confused deputy, not a repo escalation. The deny
    # must come AFTER the allow, because the last matching rule wins.
    READ_ONLY_PERMISSION = ('{"*":"deny","read":{"*":"allow","mcp:*":"deny"},'
                            '"glob":"allow","grep":"allow"}')
    FULL_PERMISSION = '{"*":"allow"}'

    # A delekit-owned primary agent carries the policy, and the run selects it by
    # name. OPENCODE_PERMISSION alone is NOT sufficient: opencode merges the
    # top-level permission into an agent first and appends that agent's own
    # `agent.<name>.permission` afterwards, and the LAST matching rule wins. So
    # an ordinary global config -- not a hostile one, just a user who set
    # `agent.build.permission.bash = "allow"` once -- silently outranks it.
    # Measured: with such a global agent present, a run labelled read-only
    # created the file it was supposed to be denied. Selecting our own agent
    # closes it, and unlike redirecting the config roots it does not hide any
    # models (62 either way).
    AGENT_NAMES = {"read-only": "delekit-readonly", "danger-full-access": "delekit-full"}

    def agent_name(self, access: str) -> str:
        return self.AGENT_NAMES[access]

    def agent_config(self, access: str) -> str:
        policy = self.READ_ONLY_PERMISSION if access == "read-only" else self.FULL_PERMISSION
        return json.dumps({"agent": {self.agent_name(access): {
            "mode": "primary", "permission": json.loads(policy)}}})

    def sandbox_args(self, access: str) -> list[str]:
        if access == "workspace-write":
            raise HerdError(2, "opencode has no confined workspace-write mode; use readonly or explicit full")
        # --auto on every supported mode. It cannot override an explicit deny --
        # a denied rule returns before any permission event is published -- and it
        # clears the residual `ask` states that would otherwise block a headless
        # turn waiting for an approval nobody can give.
        return ["--auto"]

    def env_overrides(self, meta: dict[str, Any]) -> dict[str, str]:
        return self.isolation_env(task_dir(meta["task"]), meta["access"])

    def isolation_env(self, root: Path, access: str | None = None) -> dict[str, str]:
        """Per-run environment, shared by the turn and the variant preflight.

        Deliberately modest, and the same shape the other backends use. An
        earlier revision also redirected HOME and XDG_CONFIG_HOME to fence off
        ambient global config. That is dropped: it hid seven models from
        `opencode models`, two of them this kit's own defaults, so it broke the
        feature to defend against the operator's own configuration. Project
        config stays disabled and plugins stay suppressed, which is what actually
        blocked the override that was measured.
        """
        root.mkdir(parents=True, exist_ok=True)
        return {
            # Measured: a `.opencode/opencode.json` in the TARGET REPO re-enabled
            # the write tool on a run whose policy denied it. The repo is the
            # surface delekit does not control, and this is the flag that stops
            # it. Ambient global config is the operator's own and is left alone.
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            # Task-local session store. Every `opencode run` opens one SQLite DB,
            # which is what makes concurrent workers collide on `database is
            # locked`; per-task keeps them independent and prune reclaims it.
            "OPENCODE_DB": str(root / "opencode.db"),
            # Both layers: the named agent is what actually binds (last match
            # wins), and the top-level policy stays as defence in depth.
            **({"OPENCODE_PERMISSION": self.READ_ONLY_PERMISSION,
                "OPENCODE_CONFIG_CONTENT": self.agent_config(access)} if access == "read-only"
               else {"OPENCODE_PERMISSION": self.FULL_PERMISSION,
                     "OPENCODE_CONFIG_CONTENT": self.agent_config(access)}
               if access == "danger-full-access" else {}),
        }

    def env_unset(self, meta: dict[str, Any]) -> list[str]:
        # Inline config and auth beat every file-based setting, so an inherited
        # value would walk straight past the project-config flag above.
        # OPENCODE_CONFIG_CONTENT is no longer scrubbed: isolation_env sets it to
        # our own agent definition, which replaces whatever was inherited.
        return ["OPENCODE_CONFIG", "OPENCODE_AUTH_CONTENT"]

    def _base(self, meta: dict[str, Any]) -> list[str]:
        # `--pure` is a global flag and must precede the subcommand.
        argv = [self.resolved_bin(meta), "--pure", "run", "--format", "json",
                "--agent", self.agent_name(meta["access"]), "--dir", meta["exec_root"]]
        if meta.get("model"):
            argv += ["--model", meta["model"]]
        # Only sent when a run asked for it: opencode does not validate
        # --variant, so a pinned default would be a silent no-op on any model
        # that does not implement the tier. See config/models.env.
        if meta.get("effort"):
            argv += ["--variant", meta["effort"]]
        argv += self.sandbox_args(meta["access"])
        return argv

    def spawn_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        return self._base(meta), meta["prompt"], meta["exec_root"]

    def resume_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        return self._base(meta) + ["--session", meta["session_id"]], meta["prompt"], meta["exec_root"]

    def parse(self, obj: dict[str, Any]) -> dict[str, str]:
        # {"type":"text","sessionID":"ses_...","part":{"type":"text","text":...}}
        # Opencode exposes no separate reasoning stream on the models measured,
        # so `peek --thinking` stays empty for this backend, as it does for muse.
        out: dict[str, str] = {}
        if isinstance(obj.get("sessionID"), str):
            out["session_id"] = obj["sessionID"]
        part = obj.get("part")
        if not isinstance(part, dict):
            return out
        if obj.get("type") == "text" and isinstance(part.get("text"), str):
            out["message"] = part["text"]
        elif obj.get("type") == "reasoning" and isinstance(part.get("text"), str):
            out["thinking"] = part["text"]
        return out


class GrokBackend(Backend):
    """Grok Build CLI in native headless streaming-JSON mode.

    Native `streaming-json` emits response text in chunks, a `usage` boundary
    after each model response, and a terminal `end` event carrying the session
    id but no answer text. The turn reducer therefore keeps only the last
    complete response segment. The prompt is a file so long tasks never depend
    on shell argv limits.
    """

    name = "grok"
    bin_env = "DELEGATE_GROK_BIN"

    def sandbox_args(self, access: str) -> list[str]:
        if access == "read-only":
            # `dontAsk` makes an unexpected permission request fail closed;
            # always-approve is deliberately not used for read-only. MCP is
            # denied explicitly because --tools controls built-ins only.
            return ["--permission-mode", "dontAsk", "--sandbox", "read-only",
                    "--tools", "read_file,grep,list_dir", "--deny", "MCPTool(*)",
                    "--no-subagents", "--disable-web-search"]
        if access == "workspace-write":
            raise HerdError(2, "grok has no cross-platform fail-closed workspace-write mode: "
                               "its built-in workspace sandbox is unavailable on Windows and "
                               "built-in profiles continue unenforced when application fails. "
                               "Use readonly, or full for explicit unrestricted writes; "
                               "use codex/claude for confined writes")
        if access == "danger-full-access":
            return ["--permission-mode", "bypassPermissions", "--sandbox", "off"]
        raise HerdError(2, f"unsupported grok access mode: {access}")

    def _base(self, meta: dict[str, Any]) -> list[str]:
        argv = [self.resolved_bin(meta), "--no-auto-update", "--prompt-file",
                str(task_dir(meta["task"]) / "prompt.md"), "--cwd", meta["exec_root"],
                "--output-format", "streaming-json"]
        if meta.get("model"):
            argv += ["--model", meta["model"]]
        if meta.get("effort"):
            argv += ["--reasoning-effort", meta["effort"]]
        argv += self.sandbox_args(meta["access"])
        return argv

    def spawn_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        return self._base(meta) + ["--session-id", meta["session_id"]], "", meta["exec_root"]

    def resume_cmd(self, meta: dict[str, Any]) -> tuple[list[str], str, str]:
        return self._base(meta) + ["--resume", meta["session_id"]], "", meta["exec_root"]

    def parse(self, obj: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for key in ("session_id", "sessionId"):
            if isinstance(obj.get(key), str):
                out["session_id"] = obj[key]
                break
        if obj.get("type") == "text" and isinstance(obj.get("data"), str):
            out["message_delta"] = obj["data"]
        elif obj.get("type") == "usage":
            out["response_boundary"] = "1"
        elif obj.get("type") == "end":
            out["terminal"] = "1"
            if isinstance(obj.get("sessionId"), str):
                out["terminal_session_id"] = obj["sessionId"]
            if isinstance(obj.get("stopReason"), str):
                out["terminal_stop_reason"] = obj["stopReason"]
        elif obj.get("type") == "thought" and isinstance(obj.get("data"), str):
            out["thinking"] = obj["data"]
        elif obj.get("type") == "error":
            out["error"] = str(obj.get("message") or "Grok emitted an error event")
        return out


BACKENDS: dict[str, Backend] = {
    "codex": CodexBackend(), "pi": PiBackend(), "claude": ClaudeBackend(), "muse": MuseBackend(),
    "opencode": OpencodeBackend(), "grok": GrokBackend(),
}


# --------------------------------------------------------------------------- #
# Session model
# --------------------------------------------------------------------------- #
def task_dir(task: str) -> Path:
    return sessions_dir() / sanitize_task(task)


def output_mtime(tdir: Path) -> float | None:
    mtimes = []
    for name in ("events.jsonl", "report.md", "meta.json"):
        try:
            mtimes.append((tdir / name).stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else None


def has_question(tdir: Path) -> bool:
    try:
        return QUESTION_RE.search((tdir / "report.md").read_text(encoding="utf-8", errors="replace")) is not None
    except OSError:
        return False


def _child_popen_kwargs(*, new_process_group: bool = False) -> dict[str, Any]:
    """Popen options for a child we may later signal as a group.

    `new_process_group` must be honoured on BOTH platforms. stop_pid signals the
    whole group (`killpg` on POSIX, `CTRL_BREAK_EVENT` on Windows), so a child
    left in its parent's group takes the parent down with it: killing one task
    would SIGINT the helper, and under a test runner it kills the runner itself.
    A Windows-only implementation looks correct because the Windows flag is the
    visible one, while the POSIX no-op fails silently.
    """
    if os.name != "nt":
        return {"start_new_session": True} if new_process_group else {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    flags = subprocess.CREATE_NO_WINDOW
    if new_process_group:
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    return {"creationflags": flags, "startupinfo": startupinfo}


def backend_pid(tdir: Path) -> int | None:
    child = read_json(tdir / "child.json") or {}
    pid = child.get("backend_pid")
    return pid if isinstance(pid, int) else None


def helper_pid(tdir: Path, meta: dict[str, Any]) -> int | None:
    helper = read_json(tdir / "helper.json") or {}
    pid = helper.get("helper_pid")
    if isinstance(pid, int):
        return pid
    legacy = meta.get("pid")
    return legacy if isinstance(legacy, int) else None


def task_process_alive(tdir: Path, meta: dict[str, Any]) -> bool:
    return pid_alive(helper_pid(tdir, meta)) or pid_alive(backend_pid(tdir))


def stop_task_processes(tdir: Path, meta: dict[str, Any]) -> None:
    # Stop the backend as well as the helper. A crashed helper must not leave a
    # live provider child behind or make a concurrent resume look safe.
    for pid in (backend_pid(tdir), helper_pid(tdir, meta)):
        stop_pid(pid)


def status_payload(tdir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    mtime = output_mtime(tdir)
    child_pid = backend_pid(tdir)
    supervisor_pid = helper_pid(tdir, meta)
    payload = {
        "task": tdir.name, "state": meta.get("state", "working"),
        "backend": meta.get("backend"), "model": meta.get("model"), "access": meta.get("access"),
        "repo": meta.get("repo"), "owner": meta.get("owner"), "pid": supervisor_pid,
        "pid_alive": pid_alive(supervisor_pid), "session_id": meta.get("session_id"),
        "backend_pid": child_pid, "backend_pid_alive": pid_alive(child_pid),
        "created_utc": meta.get("created_utc"),
        "last_output_age_s": None if mtime is None else max(0, int(now() - mtime)),
    }
    if "stall_reason" in meta:
        payload["stall_reason"] = meta["stall_reason"]
    return payload


def write_status(tdir: Path, meta: dict[str, Any]) -> None:
    atomic_write_json(tdir / "status.json", status_payload(tdir, meta))


def mark_terminal(tdir: Path, meta: dict[str, Any], state: str, reason: str | None) -> None:
    meta["state"] = state
    if reason:
        meta["stall_reason"] = reason
    atomic_write_json(tdir / "meta.json", meta)
    (tdir / ".done").touch()
    write_status(tdir, meta)


def rotate_report(tdir: Path, strict: bool = False) -> str | None:
    """Move the current report aside so it cannot be read as the next turn's.

    Idempotent: with no report there is nothing to move. Returns the text that
    was rotated, if any, so a caller can mention it in a diagnostic.
    """
    report = tdir / "report.md"
    try:
        if not report.exists():
            return None
        text = report.read_text(encoding="utf-8")
        if text.strip():
            (tdir / "previous-report.md").write_text(text, encoding="utf-8")
        report.unlink()
        return text
    except OSError as exc:
        # Swallowing this at the command boundary was fail-OPEN: an unwritable
        # previous-report.md left the preceding answer in place, the turn started
        # anyway, and a later failure served that answer as this turn's result.
        if strict:
            raise HerdError(5, f"could not rotate the previous report aside: {exc}. "
                               "Refusing to start a turn that could present it as its own result")
        return None


def _is_zombie(pid: Any) -> bool:
    """True for a process that has exited but not yet been reaped.

    `kill(pid, 0)` succeeds for a zombie, so a liveness check alone reports a
    dead-but-unreaped child as running. It cannot execute anything and cannot
    touch the checkout, so for containment purposes it is stopped.
    """
    if os.name == "nt" or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out.startswith("Z")


def _tracked_pids(tdir: Path, meta: dict[str, Any]) -> list[int]:
    return [pid for pid in (helper_pid(tdir, meta), backend_pid(tdir)) if isinstance(pid, int)]


def _confirm_stopped(tdir: Path, meta: dict[str, Any], timeout: float = 5.0) -> bool:
    """True once nothing tracked for this task is alive.

    `stop_task_processes` returns nothing, so reconciliation used to write a
    terminal state immediately after asking -- recording `failed/deadline` while
    a tracked process was demonstrably still running and still able to modify the
    checkout.
    """
    def still_running() -> bool:
        return any(pid_alive(pid) and not _is_zombie(pid) for pid in _tracked_pids(tdir, meta))

    deadline = now() + timeout
    while now() < deadline:
        if not still_running():
            return True
        time.sleep(0.2)
    return not still_running()


def _write_missing_report(tdir: Path, detail: str, replace_stale: bool = False) -> None:
    """Give a turn settled from outside run_turn a report describing THIS turn.

    run_turn rotates report.md away at turn start, so a turn reconciled from
    outside would otherwise settle with no report at all.

    `replace_stale` is for the dead-helper path specifically. `send` clears
    `.done` before the child starts, and the rotation happens inside the child,
    so a helper that dies in between leaves the PREVIOUS turn's answer in place
    -- and it would be served verbatim as this turn's result under a `failed`
    state. Reproduced. In that case the old text is moved to previous-report.md
    and replaced. The deadline and stall paths pass False, because there the
    helper may legitimately have written this turn's answer already.
    """
    report = tdir / "report.md"
    try:
        existing = report.read_text(encoding="utf-8") if report.exists() else ""
        if existing.strip():
            if not replace_stale:
                return
            (tdir / "previous-report.md").write_text(existing, encoding="utf-8")
        report.write_text(f"{detail} Inspect events.jsonl and stderr.log.\n", encoding="utf-8")
    except OSError:
        pass


def reconcile(tdir: Path, enforce: bool = True) -> dict[str, Any] | None:
    meta = read_json(tdir / "meta.json")
    if meta is None:
        return None
    # .done means the turn helper settled this turn; trust the recorded state.
    if (tdir / ".done").exists():
        return status_payload(tdir, meta)

    pid = helper_pid(tdir, meta)
    child_pid = backend_pid(tdir)
    deadline = meta.get("deadline_utc")
    if isinstance(deadline, (int, float)) and now() > deadline:
        if enforce:
            stop_task_processes(tdir, meta)
            if not _confirm_stopped(tdir, meta):
                # Terminal state must not be a lie. A failed or partial stop with
                # `.done` written lets prune reclaim the directory and `send`
                # race a second turn into a session the first is still using.
                live = dict(meta)
                live["state"] = "working"
                live["stall_reason"] = "stop-failed"
                return status_payload(tdir, live)
        _write_missing_report(tdir, "Delegate exceeded its deadline and was stopped.")
        mark_terminal(tdir, meta, "failed", "deadline")
        return status_payload(tdir, meta)

    stall_after = int(meta.get("stall_after_s") or DEFAULT_STALL_AFTER_S)
    mtime = output_mtime(tdir)
    if mtime is not None and (now() - mtime) > stall_after:
        if enforce:
            stop_task_processes(tdir, meta)
            if not _confirm_stopped(tdir, meta):
                live = dict(meta)
                live["state"] = "working"
                live["stall_reason"] = "stop-failed"
                return status_payload(tdir, live)
        _write_missing_report(tdir, "Delegate produced no output within the stall window and was stopped.")
        mark_terminal(tdir, meta, "stalled", "no-output")
        return status_payload(tdir, meta)

    if not pid_alive(pid) and pid_alive(child_pid):
        # The provider child can outlive a failed helper on Windows. Keep the
        # task non-resumable until that exact child exits; result --wait will
        # then wake and report the failed supervisor instead of racing a second
        # turn into the same checkout/session.
        live = dict(meta)
        live["state"] = "working"
        live["stall_reason"] = "supervisor-exited-backend-still-running"
        return status_payload(tdir, live)
    if not pid_alive(pid):
        # The helper died without settling the turn -- killed from outside, OOM,
        # or an error that escaped even the guarded body. report.md is either
        # absent (rotated away at turn start) or, worse, still holding an older
        # answer, so `result` would present a stale success for a crashed turn.
        # Replace it with a diagnostic; the unsettled prior answer, if any, is
        # preserved as previous-report.md by the rotation.
        _write_missing_report(tdir, "Delegate helper exited without settling the turn.",
                              replace_stale=True)
        mark_terminal(tdir, meta, "failed", "process-exited-without-marker")
        return status_payload(tdir, meta)

    if has_question(tdir):
        meta["state"] = "awaiting_reply"
        atomic_write_json(tdir / "meta.json", meta)
    return status_payload(tdir, meta)


def reap(enforce: bool = True) -> list[dict[str, Any]]:
    root = sessions_dir()
    out = []
    if not root.is_dir():
        return out
    for tdir in sorted(root.iterdir()):
        if tdir.is_dir():
            payload = reconcile(tdir, enforce=enforce)
            if payload is not None:
                out.append(payload)
    return out


# --------------------------------------------------------------------------- #
# The detached turn helper
# --------------------------------------------------------------------------- #
def _tail_bytes(path: Path, limit: int) -> bytes:
    """The last `limit` bytes, read by seeking rather than loading the file.

    Reading a multi-gigabyte events.jsonl whole -- merely to keep its tail --
    raised MemoryError, and because that happens during post-turn compaction it
    turned an otherwise COMPLETED turn into failed/helper-error and replaced its
    answer with a diagnostic.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        start = max(0, fh.tell() - limit)
        fh.seek(start)
        data = fh.read()
    if start:                       # drop the partial first record
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""
    return data


def cap_events(tdir: Path) -> None:
    p = tdir / "events.jsonl"
    try:
        cap = EVENTS_MAX_MB * 1024 * 1024
        if p.stat().st_size <= cap:
            return
        keep = _tail_bytes(p, cap // 2)
        tmp = p.with_suffix(".jsonl.tmp")
        tmp.write_bytes(b"# ...truncated...\n" + keep)
        os.replace(tmp, p)
    except (OSError, MemoryError):
        # Compaction is housekeeping. It must never change the outcome of a turn
        # that already produced an answer.
        pass


def run_turn(task: str, mode: str) -> int:
    tdir = task_dir(task)
    meta = read_json(tdir / "meta.json")
    if meta is None:
        return 6
    backend = BACKENDS[meta["backend"]]
    # Rotate the previous turn's answer FIRST, before anything that can fail.
    # report.md means "the current turn's result", and every step below can
    # raise -- a missing backend binary, an unreadable exec root, an environment
    # builder error -- which would leave the preceding answer standing as if it
    # were this turn's. Doing it after Popen was the bug.
    stale_report = rotate_report(tdir)

    proc: subprocess.Popen | None = None
    try:
        argv, stdin_text, cwd = backend.spawn_cmd(meta) if mode == "spawn" else backend.resume_cmd(meta)
        env = dict(os.environ, CI="1", GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", PAGER="cat", NO_COLOR="1")
        env.pop("CLAUDECODE", None)
        env.update(backend.env_overrides(meta))
        for name in backend.env_unset(meta):
            env.pop(name, None)
        stderr_f = open(tdir / "stderr.log", "ab")
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_f,
            cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
            **_child_popen_kwargs(new_process_group=True),
        )
    except BaseException as exc:                                # noqa: BLE001 - including SIGINT
        # BaseException here too: the stop primitive SIGINTs this helper, and an
        # interrupt landing during Popen would otherwise escape with the backend
        # possibly already detached into its own session.
        if proc is not None:
            try:
                _terminate_child(proc)
            except BaseException:                               # noqa: BLE001
                pass
        # A turn that never started is a failed turn, and it must say so here.
        # Without this the helper exits with the report already rotated away and
        # nothing written in its place, so `result` returns an empty answer for a
        # run that never ran.
        kept = " The preceding turn's answer is kept as previous-report.md." if stale_report else ""
        (tdir / "report.md").write_text(
            f"Delegate failed to start: {type(exc).__name__}: {exc}. "
            f"Inspect stderr.log.{kept}\n", encoding="utf-8")
        mark_terminal(tdir, meta, "failed", "startup-error")
        return 1
    def fail(state: str, reason: str, detail: str) -> None:
        """Terminal failure: the report must describe THIS turn, never the last.

        Every non-success path writes a diagnostic, because any path that left
        report.md alone would resurrect the previous turn's answer as the
        current result. The prior answer is kept beside it as
        previous-report.md rather than destroyed.
        """
        kept = " The preceding turn's answer is kept as previous-report.md." if stale_report else ""
        (tdir / "report.md").write_text(f"{detail} Inspect events.jsonl.{kept}\n", encoding="utf-8")
        mark_terminal(tdir, meta, state, reason)

    # Everything from here to the terminal classification is guarded. Startup was
    # already covered, but the turn ITSELF was not: a parser exception, an
    # events.jsonl write error, a metadata-write failure, or a `proc.wait` error
    # would escape run_turn, leave the provider running, and exit the helper with
    # no current report -- so `result` printed an empty placeholder for a turn
    # that had actually crashed. `main()` only catches HerdError, so nothing else
    # caught these.
    try:
        atomic_write_json(tdir / "child.json", {
            "helper_pid": os.getpid(), "backend_pid": proc.pid, "started_utc": now(),
        })
        # Fed from a daemon thread rather than inline. A backend that never reads
        # a large prompt blocks this write until the pipe buffer drains, and
        # inline it blocked BEFORE the watchdog existed -- no stall, no deadline,
        # forever.
        def feed_stdin() -> None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        Thread(target=feed_stdin, daemon=True).start()

        last = [now()]
        kill_reason: list[str | None] = [None]
        stall_after = int(meta.get("stall_after_s") or DEFAULT_STALL_AFTER_S)
        deadline = meta.get("deadline_utc")

        # The watchdog must outlive the backend process, not stop with it. A tool
        # child that called setsid -- which is how opencode's shell tool launches
        # commands -- inherits our stdout pipe and keeps it open after the
        # backend exits, so `for line in proc.stdout` blocks forever. Ending the
        # watchdog at child exit left that turn with no stall and no deadline:
        # reproduced, the helper hung with a COMPLETE answer sitting unreported
        # in events.jsonl until an outside reconcile SIGINT'd it, which then
        # overwrote the answer with a crash diagnostic. So the loop runs until
        # the READER finishes, and it force-closes the pipe when the backend has
        # exited but a descendant is still holding it.
        finished = Event()
        exited_at: list[float | None] = [None]
        stream_error = [False]
        tracked: set[int] = set()

        def watch() -> None:
            # An exception here would kill only this thread, leaving the turn
            # with no deadline and no stall enforcement. Fail closed instead.
            try:
                while not finished.wait(1):
                    alive = proc.poll() is None
                    if alive:
                        # Snapshot descendants while they are still reachable
                        # from our pid; once the backend exits they reparent and
                        # cannot be found by walking down.
                        tracked.update(_descendant_pids(proc.pid))
                        # Re-poll: that scan shells out to `ps` and can take
                        # seconds. Deciding on the stale value armed a stall
                        # against a backend that had already finished cleanly,
                        # replacing its answer with a "was killed" diagnostic.
                        alive = proc.poll() is None
                    # Only arm a kill while the backend is actually running --
                    # otherwise a check landing after a clean exit would overwrite
                    # a finished answer with a false "was killed" diagnostic.
                    if alive and isinstance(deadline, (int, float)) and now() > deadline:
                        kill_reason[0] = "deadline"
                        _terminate_child(proc)
                        return
                    if alive and now() - last[0] > stall_after:
                        kill_reason[0] = "no-output"
                        _terminate_child(proc)
                        return
                    if not alive:
                        if exited_at[0] is None:
                            exited_at[0] = now()
                        elif now() - exited_at[0] > PIPE_DRAIN_GRACE_S:
                            # The backend is gone but the stream has not ended,
                            # so something it spawned inherited the pipe. Reap the
                            # descendants we managed to record; the reader stops
                            # on its own timeout regardless (see the pump below),
                            # because closing a pipe from another thread does not
                            # reliably wake a blocking POSIX read.
                            kill_process_tree(proc.pid, sorted(tracked))
                            return
            except Exception:                                   # noqa: BLE001
                kill_reason[0] = "watchdog-error"
                try:
                    _terminate_child(proc)
                except Exception:                               # noqa: BLE001
                    pass

        watcher = Thread(target=watch, daemon=True)
        watcher.start()

        # The stream is pumped by a daemon thread and consumed with a timeout.
        # Iterating proc.stdout directly blocks forever when a descendant
        # inherited the pipe and the backend has exited -- and no cross-thread
        # close reliably wakes that read. The pump may stay blocked on such a
        # pipe; it is a daemon, so it cannot keep the helper alive.
        # Bounded on purpose. Queue() defaults to maxsize=0, i.e. unlimited, so
        # the pump would happily buffer an entire runaway stream in memory --
        # backpressure the direct pipe reader used to provide for free.
        lines: Queue = Queue(maxsize=EVENT_QUEUE_MAXSIZE)

        def pump() -> None:
            try:
                for raw in proc.stdout:
                    if len(raw) > MAX_EVENT_BYTES:
                        raw = raw[:MAX_EVENT_BYTES] + "\n"
                    lines.put(raw)
            except Exception:                                   # noqa: BLE001
                # Distinct from EOF: a read failure is a stream error, and
                # settling it as an ordinary end would hide a truncated turn.
                stream_error[0] = True
            finally:
                lines.put(None)                                 # end-of-stream

        Thread(target=pump, daemon=True).start()

        last_message: str | None = None
        grok_response_chunks: list[str] = []
        grok_last_complete: str | None = None
        grok_terminal = False
        protocol_error: list[str | None] = [None]
        saw_eof = False
        with open(tdir / "events.jsonl", "a", encoding="utf-8") as events:
            while True:
                try:
                    line = lines.get(timeout=1)
                except Empty:
                    # Track the child's exit here rather than trusting the
                    # watchdog thread: the reader must be able to end the turn
                    # on its own even if the watchdog has already returned.
                    if proc.poll() is not None:
                        if exited_at[0] is None:
                            exited_at[0] = now()
                        if now() - exited_at[0] > PIPE_DRAIN_GRACE_S:
                            break                               # descendant holds the pipe
                    continue
                if line is None:
                    saw_eof = True
                    break
                line = line.strip()
                if not line:
                    continue
                events.write(line + "\n")
                events.flush()
                last[0] = now()
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                parsed = backend.parse(obj)
                emitted_session = parsed.get("session_id")
                if emitted_session:
                    if backend.name == "grok" and meta.get("session_id"):
                        if emitted_session != meta["session_id"]:
                            protocol_error[0] = (
                                f"Grok emitted session {emitted_session}, expected {meta['session_id']}.")
                    elif not meta.get("session_id"):
                        meta["session_id"] = emitted_session
                        atomic_write_json(tdir / "meta.json", meta)
                if backend.name == "grok":
                    if "message_delta" in parsed:
                        grok_response_chunks.append(parsed["message_delta"])
                    if parsed.get("response_boundary"):
                        # Every usage event is a response boundary, including a
                        # completed response with no text. Recording the empty
                        # segment is load-bearing: otherwise a contentless final
                        # response re-publishes the preceding pre-tool text.
                        grok_last_complete = "".join(grok_response_chunks)
                        grok_response_chunks.clear()
                    if parsed.get("terminal"):
                        grok_terminal = True
                        terminal_session = parsed.get("terminal_session_id")
                        if not terminal_session:
                            protocol_error[0] = protocol_error[0] or (
                                "Grok terminal end event omitted sessionId.")
                        elif meta.get("session_id") and terminal_session != meta["session_id"]:
                            protocol_error[0] = protocol_error[0] or (
                                f"Grok terminal session {terminal_session}, expected {meta['session_id']}.")
                        terminal_reason = parsed.get("terminal_stop_reason")
                        if not terminal_reason:
                            protocol_error[0] = protocol_error[0] or (
                                "Grok terminal end event omitted stopReason.")
                        elif terminal_reason != "end_turn":
                            protocol_error[0] = protocol_error[0] or (
                                f"Grok ended with non-success stopReason={terminal_reason}.")
                        if grok_response_chunks:
                            protocol_error[0] = protocol_error[0] or (
                                "Grok emitted terminal end before the final usage response boundary.")
                    if parsed.get("error"):
                        protocol_error[0] = protocol_error[0] or parsed["error"]
                elif parsed.get("message"):
                    last_message = parsed["message"]
                elif parsed.get("message_delta"):
                    last_message = (last_message or "") + parsed["message_delta"]

        # Keep the watchdog armed across the wait. The stream can end before the
        # process does -- a backend that closes stdout and lingers -- and an
        # unbounded wait() with the watchdog already retired left the turn with
        # neither a stall nor a deadline.
        try:
            rc = proc.wait(timeout=PROCESS_EXIT_GRACE_S)
        except subprocess.TimeoutExpired:
            kill_reason[0] = kill_reason[0] or "no-exit"
            _terminate_child(proc)
            try:
                rc = proc.wait(timeout=PROCESS_EXIT_GRACE_S)
            except subprocess.TimeoutExpired:
                rc = -1
        finally:
            finished.set()
        # Deterministic cleanup owned by the main thread, so it cannot depend on
        # whether the consumer or the watcher won the drain-grace race.
        if tracked:
            kill_process_tree(proc.pid, sorted(tracked))
        # Close only when the stream actually ended. If we bailed out on the
        # held-open-pipe timeout the pump thread is still inside a blocking read
        # and holds the buffer lock, so closing here blocks the MAIN thread --
        # which is exactly the hang this rework exists to remove. The pump is a
        # daemon; leaving it is what lets the helper exit.
        if saw_eof:
            try:
                proc.stdout.close()
            except Exception:                                   # noqa: BLE001
                pass
        stderr_f.close()
        watcher.join(timeout=PS_SCAN_TIMEOUT_S + 5)
        if backend.name == "grok":
            if grok_response_chunks and protocol_error[0] is None:
                protocol_error[0] = (
                    "Grok stream ended with text that had no usage response boundary.")
            # Empty final responses are valid protocol events but not successful
            # delegate reports. Preserve no earlier segment: classification below
            # must produce empty-report rather than a stale pre-tool answer.
            last_message = grok_last_complete if (grok_last_complete or "").strip() else None
            if not grok_terminal and protocol_error[0] is None:
                protocol_error[0] = "Grok stream ended without its terminal end event."
        if last_message is not None:
            (tdir / "report.md").write_text(last_message, encoding="utf-8")
        cap_events(tdir)

        if stream_error[0]:
            # Any read failure fails the turn, even when text was captured first.
            # Gating this on `last_message is None` meant one good event followed
            # by an OSError produced state `done` with a TRUNCATED answer -- a
            # defect this repair introduced, so it is not pre-existing.
            fail("failed", "stream-error",
                 "The delegate's output stream failed mid-turn; the answer is incomplete.")
        elif protocol_error[0] is not None:
            fail("failed", "grok-protocol", f"Grok protocol error: {protocol_error[0]}")
        elif kill_reason[0] == "no-exit":
            fail("failed", "no-exit",
                 "Delegate closed its output but did not exit; it was stopped.")
        elif kill_reason[0] == "watchdog-error":
            fail("failed", "watchdog-error",
                 "The runner's stall/deadline watchdog failed, so the delegate was stopped.")
        elif kill_reason[0] == "deadline":
            fail("failed", "deadline", "Delegate exceeded its deadline and was killed.")
        elif kill_reason[0] == "no-output":
            fail("stalled", "no-output", "Delegate produced no output within the stall window and was killed.")
        elif rc != 0:
            fail("failed", f"exit-{rc}", f"Delegate exited {rc} without completing the turn.")
        elif last_message is None:
            # A backend that exits 0 having said nothing is a failure, not a
            # completion. This is reachable, not theoretical: measured on
            # `opencode/nemotron-3.5-lightning-free`, which emits step_start then
            # step_finish(reason="unknown"), writes no answer, and exits 0. dairy
            # already converts an empty or `Execution error` report into a failed
            # status, so herd has to agree or the same run looks done here and failed
            # there. The marker replaces report.md because report.md means "the last
            # turn's answer", and this turn had none; events.jsonl keeps the history.
            fail("failed", "empty-report", "Delegate produced no final message; treating the turn as failed.")
        else:
            # Auto-commit only once the turn is known good. It used to run before
            # classification, so a stalled, deadlined, crashed, or answer-less turn
            # still committed whatever the worker had written -- publishing partial
            # work under a failed status. Uncommitted work is not lost; it stays in
            # the worktree for the deliberate review the docs already promise.
            _maybe_autocommit(meta)
            mark_terminal(tdir, meta, "awaiting_reply" if has_question(tdir) else "done", None)
        return 0
    except BaseException as exc:                                # noqa: BLE001 - including SIGINT
        # BaseException, not Exception: this kit's own stop primitive sends
        # SIGINT to the helper's process group, which Python raises as
        # KeyboardInterrupt -- a BaseException that would unwind straight past an
        # `except Exception` handler, leaving the provider running and the turn
        # unsettled. Stop the provider first: an unhandled helper error must not
        # leave a detached backend writing to the repo with nothing supervising.
        try:
            _terminate_child(proc)
        except Exception:                                       # noqa: BLE001
            pass
        try:
            fail("failed", "helper-error",
                 f"Delegate turn failed inside the runner: {type(exc).__name__}: {exc}.")
        except Exception:                                       # noqa: BLE001
            mark_terminal(tdir, meta, "failed", "helper-error")
        return 1


def _terminate_child(proc: subprocess.Popen) -> None:
    """Stop the backend AND everything it detached.

    This used to signal only the direct child. Backends launch tool
    subprocesses in their own process groups -- opencode's shell tool does
    exactly that -- so killing the child alone lets a stall- or deadline-kill
    mark a task terminal while a detached shell keeps writing to the repo. The
    child is spawned with new_process_group=True, so its pid leads the group and
    stop_pid's group signal reaches the whole tree.
    """
    stop_pid(proc.pid)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


def _maybe_autocommit(meta: dict[str, Any]) -> None:
    if not (meta.get("worktree") and meta.get("auto_commit", True)):
        return
    wt = meta.get("exec_root")
    if not wt or not os.path.isdir(wt):
        return
    try:
        subprocess.run(["git", "-C", wt, "add", "-A"], check=False, capture_output=True)
        staged = subprocess.run(["git", "-C", wt, "diff", "--cached", "--quiet"], capture_output=True)
        if staged.returncode != 0:
            subprocess.run(["git", "-C", wt, "-c", "user.name=delegate", "-c", "user.email=delegate@local",
                            "commit", "-q", "-m", f"herd({meta['task']}): {time.strftime('%Y%m%dT%H%M%S')}"],
                           check=False, capture_output=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Spawn / send helpers
# --------------------------------------------------------------------------- #
ACCESS_BY_MODE = {"workspace": "workspace-write", "write": "workspace-write",
                  "readonly": "read-only", "read": "read-only", "full": "danger-full-access"}

PREAMBLE = {
    "read-only": "**Access: read-only.** Do not create, edit, or delete files. Return the complete result in the final message.",
    "workspace-write": "**Access: workspace-write.** Work only inside the current project or worktree. Return outcome, changed files, validation, and blockers in the final message.",
    "danger-full-access": "**Access: unrestricted and explicitly authorized for this run.** Minimize changes outside the project and report every external effect.",
}
WORKTREE_LINE = "**Isolation: Git worktree.** Stay in the current worktree; do not switch branches, touch the main checkout, push, merge, or remove the worktree."
PI_READONLY_LINE = "**Pi limitation:** Read-only Pi has file/search tools but no shell or test execution. Do not narrow scope; mark command-dependent claims unverified and return NO-GO when they are decisive."
# Read-only opencode must deny bash, because opencode's shell can redirect into a
# file -- the same trade muse read-only makes. The delegate therefore loses git,
# ripgrep, and test execution, and must say so instead of quietly shrinking the
# task to what it can still check.
OPENCODE_READONLY_LINE = "**Opencode limitation:** Read-only opencode has file/search tools but no shell, so it cannot run git, ripgrep, or tests. Do not narrow scope; mark command-dependent claims unverified and return NO-GO when they are decisive."
GROK_READONLY_LINE = "**Grok limitation:** Read-only Grok has file/search tools but no shell, write tools, subagents, web search, or test execution. Do not narrow scope; mark command-dependent claims unverified and return NO-GO when they are decisive."


def find_project_root(start: str) -> str:
    try:
        top = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
        if top.returncode == 0 and top.stdout.strip():
            return top.stdout.strip()
    except OSError:
        pass
    d = os.path.realpath(start)
    markers = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml", ".git")
    while True:
        if any(os.path.exists(os.path.join(d, m)) for m in markers):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.realpath(start)
        d = parent


def resolve_prompt(args: argparse.Namespace) -> str:
    sources = [bool(args.prompt), bool(args.prompt_file), bool(getattr(args, "prompt_stdin", False))]
    if sum(sources) == 0 and not sys.stdin.isatty():
        args.prompt_stdin = True
    elif sum(sources) > 1:
        raise HerdError(2, "choose exactly one prompt source")
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8")
    if getattr(args, "prompt_stdin", False):
        return sys.stdin.read()
    if args.prompt:
        return args.prompt
    raise HerdError(2, "no prompt provided")


# Profile == env suffix (lower-cased) == tandy agent name, matching dairy and
# config/models.env. terra is the default.
PROFILES = ("terra", "luna", "sol")
DEFAULT_PROFILE = "terra"

# Muse profiles are backend-specific and named for the model family. `spark` is
# the default; an unqualified --profile terra with --backend muse is
# indistinguishable from the default and resolves to spark.
MUSE_PROFILES = ("spark",)
MUSE_DEFAULT_PROFILE = "spark"

# Opencode's profile set is DATA, not code: it lives in config/models.env and is
# read at run time. Opencode fronts several providers at once and its free tier
# rotates, so a hardcoded tuple would need a code edit -- on two platforms -- every
# time a slug moved. An empty list is valid and makes opencode behave like the
# claude backend, where --model is required and the kit pins nothing.
OPENCODE_PROFILES_KEY = "DELEGATE_OPENCODE_PROFILES"


def opencode_profiles(env: dict[str, str] | None = None) -> list[str]:
    """Opencode profile names from config/models.env; the first is the default."""
    env = load_models_env() if env is None else env
    return [n.strip() for n in env.get(OPENCODE_PROFILES_KEY, "").split(",") if n.strip()]
# The opencode version floor is DATA, in config/models.env, not a constant here:
# versions move constantly, and raising, lowering, or removing the floor must not
# need a code edit on two platforms. An empty setting disables the check.
# It is a SECURITY floor: below 1.18.20 a child session's permission request is
# dropped rather than answered, and the measured result is a bypass, not a hang.
OPENCODE_MIN_VERSION_KEY = "DELEGATE_OPENCODE_MIN_VERSION"


def opencode_min_version(env: dict[str, str] | None = None) -> tuple[int, ...] | None:
    env = load_models_env() if env is None else env
    raw = (env.get(OPENCODE_MIN_VERSION_KEY) or "").strip()
    if not raw:
        return None
    parts = raw.split(".")
    if not all(x.isdigit() for x in parts):
        raise HerdError(2, f"{OPENCODE_MIN_VERSION_KEY} must be a dotted version or empty, got {raw!r}")
    return tuple(int(x) for x in parts)


def opencode_version(bin_path: str) -> tuple[int, ...] | None:
    """The binary's own reported version, or None if it did not report one.

    A failed probe must NOT satisfy a floor. Reading stderr too, and ignoring the
    exit status, meant a command that errored while printing some other version
    -- a Node runtime banner, a shim's diagnostic -- could pass the check. Only a
    clean exit counts, and only stdout is parsed.
    """
    try:
        out = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out.stdout or "")
    return tuple(int(g) for g in m.groups()) if m else None


def require_opencode_version(bin_path: str) -> None:
    floor = opencode_min_version()
    if floor is None:
        return
    want = ".".join(str(n) for n in floor)
    got = opencode_version(bin_path)
    if got is None:
        raise HerdError(7, f"could not determine the opencode version; {OPENCODE_MIN_VERSION_KEY} "
                           f"requires {want} or later (clear it to disable this check)")
    if got < floor:
        have = ".".join(str(n) for n in got)
        raise HerdError(7, f"opencode {have} is older than {OPENCODE_MIN_VERSION_KEY}={want}: below it the "
                           "run loop only answers ROOT-session permission events, so a `task` subagent's "
                           "request is dropped and the tool runs anyway -- a silent permission bypass "
                           "(measured: a subagent shell ran under a policy that said ask). "
                           "Upgrade opencode, point DELEGATE_OPENCODE_BIN at a newer binary, or lower "
                           f"{OPENCODE_MIN_VERSION_KEY} in config/models.env")


def _opencode_doctor_fields() -> dict[str, Any]:
    """Version facts for the opencode binary a spawn would really launch."""
    try:
        resolved = BACKENDS["opencode"].locate_bin()
    except HerdError:
        resolved = None
    version = ".".join(str(n) for n in (opencode_version(resolved) or ())) if resolved else None
    return {
        "opencode_bin": resolved,
        "opencode_version": version or None,
        "opencode_min_version": ".".join(str(n) for n in (opencode_min_version() or ())) or None,
        "opencode_version_ok": bool(resolved) and (
            opencode_min_version() is None or (opencode_version(resolved) or ()) >= opencode_min_version()),
    }


# Grok 0.2.116 is the first release whose native streaming-json includes
# per-response usage boundaries. Herd relies on those boundaries to discard
# pre-tool assistant text and retain only the final model response.
GROK_MIN_VERSION = (0, 2, 116)


def grok_version(bin_path: str) -> tuple[int, ...] | None:
    """The Grok binary's reported semantic version, or None on a failed probe."""
    try:
        out = subprocess.run([bin_path, "--no-auto-update", "--version"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    match = re.search(r"(?:^|\s)(\d+)\.(\d+)\.(\d+)(?:\s|$)", out.stdout or "")
    return tuple(int(group) for group in match.groups()) if match else None


def require_grok_version(bin_path: str) -> None:
    want = ".".join(str(n) for n in GROK_MIN_VERSION)
    got = grok_version(bin_path)
    if got is None:
        raise HerdError(7, f"could not determine the Grok version; herd requires {want} or later "
                           "for native streaming-json response boundaries")
    if got < GROK_MIN_VERSION:
        have = ".".join(str(n) for n in got)
        raise HerdError(7, f"Grok {have} is older than the herd minimum {want}: "
                           "earlier streaming-json did not include the usage boundaries "
                           "needed to select the final response. Upgrade Grok or point "
                           "DELEGATE_GROK_BIN at a newer binary")


def _grok_doctor_fields() -> dict[str, Any]:
    """Version facts for the Grok binary a spawn would really launch."""
    try:
        resolved = BACKENDS["grok"].locate_bin()
    except HerdError:
        resolved = None
    parsed = grok_version(resolved) if resolved else None
    return {
        "grok_bin": resolved,
        "grok_version": ".".join(str(n) for n in parsed) if parsed else None,
        "grok_min_version": ".".join(str(n) for n in GROK_MIN_VERSION),
        "grok_version_ok": bool(parsed) and parsed >= GROK_MIN_VERSION,
    }


def opencode_model_variants(bin_path: str, model: str, env: dict[str, str]) -> list[str] | None:
    """Variant keys the selected model declares, or None if enumeration failed.

    Runs with the same per-run environment as the turn, and with `--pure`,
    because `opencode models` initializes Provider -> Config/Auth/Plugin.

    There is no universal effort vocabulary and opencode ignores an unknown
    --variant in silence, so enumerating is the only way an unsupported value
    becomes an error instead of a no-op. Measured on one provider: hy3-free
    declares low/medium/high, x-preview-f-free low/high/max, and
    nemotron-3-ultra-free none at all.
    """
    provider = model.partition("/")[0]
    slug = model.partition("/")[2] or model
    try:
        out = subprocess.run([bin_path, "--pure", "models", provider, "--verbose"],
                             capture_output=True, text=True, timeout=120, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    text, dec, i = out.stdout or "", json.JSONDecoder(), 0
    while True:
        j = text.find("{", i)
        if j < 0:
            return None                       # model absent from the catalogue
        try:
            obj, end = dec.raw_decode(text[j:])
        except ValueError:
            i = j + 1
            continue
        i = j + end
        if isinstance(obj, dict) and obj.get("id") == slug and obj.get("providerID"):
            return list((obj.get("variants") or {}).keys())


def validate_opencode_effort(bin_path: str, model: str, effort: str) -> None:
    """Fail loudly rather than let opencode silently ignore an unknown effort."""
    backend = BACKENDS["opencode"]
    with tempfile.TemporaryDirectory(prefix="delekit-oc-preflight-") as tmp:
        env = dict(os.environ)
        env.update(backend.isolation_env(Path(tmp)))
        for name in backend.env_unset({}):
            env.pop(name, None)
        variants = opencode_model_variants(bin_path, model, env)
    if variants is None:
        raise HerdError(2, f"could not enumerate variants for {model}; opencode ignores an unknown "
                           "--variant silently, so the effort cannot be sent unchecked")
    if not variants:
        raise HerdError(2, f"opencode model {model} declares no variants, so --effort would be "
                           "silently ignored. Omit --effort, or choose a model that declares one")
    if effort not in variants:
        raise HerdError(2, f"opencode model {model} does not declare variant {effort!r}. "
                           f"Declared: {', '.join(variants)}")


def resolve_model_effort(backend: str, profile: str, profile_explicit: bool,
                         model: str | None, effort: str | None) -> tuple[str | None, str | None]:
    env = load_models_env()
    if backend in ("codex", "pi"):
        if profile not in PROFILES:
            raise HerdError(2, f"{backend} profile must be one of {', '.join(PROFILES)}")
        key = profile.upper()
        model = model or env.get(f"DELEGATE_MODEL_{key}")
        effort = effort or env.get(f"DELEGATE_EFFORT_{key}", "high")
        if not model:
            raise HerdError(2, f"no model configured for profile {profile}")
        return model, effort
    if backend == "muse":
        prof = profile if profile_explicit else MUSE_DEFAULT_PROFILE
        if prof not in MUSE_PROFILES:
            raise HerdError(2, f"muse profile must be one of {', '.join(MUSE_PROFILES)}")
        key = prof.upper().replace("-", "_")
        model = model or env.get(f"DELEGATE_MUSE_MODEL_{key}")
        effort = effort or env.get(f"DELEGATE_MUSE_EFFORT_{key}", "high")
        if not model:
            raise HerdError(2, f"no muse model configured for profile {prof}")
        return model, effort
    if backend == "opencode":
        profiles = opencode_profiles(env)
        if profile_explicit:
            if profile not in profiles:
                listed = ", ".join(profiles) if profiles else "<none configured>"
                raise HerdError(2, f"opencode profile must be one of {listed} "
                                   f"(set {OPENCODE_PROFILES_KEY} in config/models.env)")
            prof = profile
        else:
            prof = profiles[0] if profiles else ""
        if prof:
            key = prof.upper().replace("-", "_")
            model = model or env.get(f"DELEGATE_OPENCODE_MODEL_{key}")
            # No default: opencode does not validate --variant, so an unrequested
            # tier would be a silent no-op rather than an error.
            effort = effort or env.get(f"DELEGATE_OPENCODE_VARIANT_{key}") or None
        if not model:
            raise HerdError(2, "no opencode model: pass --model, or set "
                               f"{OPENCODE_PROFILES_KEY} and DELEGATE_OPENCODE_MODEL_* "
                               "in config/models.env")
        # The effort is validated against the SELECTED MODEL's declared variants
        # at spawn time, not against a static list -- there is no universal set.
        return model, effort
    if profile_explicit:
        raise HerdError(2, f"--profile resolves a model only for codex, muse, and opencode; "
                           f"pass --model for --backend {backend}")
    return model, effort


def create_worktree(project_root: str, task: str, dirty_policy: str) -> tuple[str, str]:
    inside = subprocess.run(["git", "-C", project_root, "rev-parse", "--is-inside-work-tree"], capture_output=True)
    if inside.returncode != 0:
        raise HerdError(2, "--worktree requires a Git repository")
    dirty = subprocess.run(["git", "-C", project_root, "status", "--porcelain"], capture_output=True, text=True).stdout
    if dirty.strip() and dirty_policy == "fail":
        raise HerdError(3, "main checkout is dirty; commit/stash or pass --dirty-policy ignore")
    branch = f"delegate/herd-{task}"
    try:
        wt_dir = create_project_worktree(project_root, f"herd-{task}", branch)
    except WorktreeError as exc:
        raise HerdError(4, f"failed to create worktree: {exc}") from exc
    return str(wt_dir), branch


# Keep launched helpers referenced so the intentionally-detached Popen objects
# are not finalized (and warned about) while the supervisor process is alive.
_LAUNCHED: list[subprocess.Popen] = []


def launch_helper(task: str, mode: str) -> int:
    env = dict(os.environ, DELEGATE_STATE_DIR=str(state_root()), DELEKIT_DEVICE_ID=device_id())
    cmd = [sys.executable, os.path.abspath(__file__), "__run_turn", "--task", task, "--mode", mode]
    kwargs: dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, env=env)
    kwargs.update(_child_popen_kwargs(new_process_group=True))
    proc = subprocess.Popen(cmd, **kwargs)
    atomic_write_json(task_dir(task) / "helper.json", {
        "helper_pid": proc.pid, "started_utc": now(), "mode": mode,
    })
    _LAUNCHED.append(proc)
    return proc.pid


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def emit(args: argparse.Namespace, payload: Any, human: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload))
    else:
        print(human)


def resolve_owner_filter(args: argparse.Namespace) -> str | None:
    return None if getattr(args, "any_owner", False) else owner_id()


def cmd_spawn(args: argparse.Namespace) -> int:
    if args.backend not in BACKENDS:
        raise HerdError(2, f"unsupported backend: {args.backend} ({', '.join(BACKENDS)})")
    access = args.access or ACCESS_BY_MODE[args.mode]
    if access not in PREAMBLE:
        raise HerdError(2, f"invalid access: {access}")
    if args.backend == "pi" and access == "workspace-write":
        raise HerdError(2, "pi has no confined workspace-write mode; use readonly, or full for explicit unrestricted access")
    if args.backend == "pi" and args.fast:
        raise HerdError(2, "--fast is a Codex backend option and is not supported by pi")
    # Measured, not inferred: opencode's bash tool wrote outside --dir unprompted
    # under default permissions, so there is no confined write mode to label.
    if args.backend == "opencode" and access == "workspace-write":
        raise HerdError(2, "opencode has no confined workspace-write mode: its shell writes outside --dir. "
                           "Use readonly, or full for explicit unrestricted writes; use codex/claude for confined writes")
    if args.backend == "grok" and access == "workspace-write":
        raise HerdError(2, "grok has no cross-platform fail-closed workspace-write mode: its built-in "
                           "sandbox is unsupported on Windows and built-in profiles continue unenforced "
                           "when application fails. Use readonly, or full for explicit unrestricted writes; "
                           "use codex/claude for confined writes")
    if args.backend in ("opencode", "grok") and args.fast:
        raise HerdError(2, f"--fast is a Codex backend option and is not supported by {args.backend}")
    prompt = resolve_prompt(args)
    if not prompt.strip():
        raise HerdError(2, "task prompt is empty")
    # Explicitness is tracked by `--profile` defaulting to None, not by comparing
    # against the default value: an explicitly typed `--profile terra` used to be
    # indistinguishable from no flag at all, so it was silently ignored for
    # backends that have no `terra`.
    model, effort = resolve_model_effort(args.backend, args.profile or DEFAULT_PROFILE,
                                         args.profile is not None, args.model, args.effort)
    admitted_bin = None
    if args.backend in ("opencode", "grok"):
        admitted_bin = BACKENDS[args.backend].locate_bin()
    if args.backend == "opencode":
        require_opencode_version(admitted_bin)
        if effort:
            validate_opencode_effort(admitted_bin, model, effort)
    if args.backend == "grok":
        require_grok_version(admitted_bin)
    project_root = args.project_root or find_project_root(os.getcwd())
    project_root = os.path.realpath(project_root)
    if not os.path.isdir(project_root):
        raise HerdError(2, f"project root does not exist: {project_root}")

    task = sanitize_task(args.name) if args.name else default_task_name()
    tdir = task_dir(task)
    if tdir.exists():
        raise HerdError(2, f"task already exists: {task}")

    worktree = args.worktree and access != "read-only"
    if args.worktree and not worktree:
        # dairy warns for exactly this case; herd used to drop it silently.
        print("Worktree isolation is unnecessary for read-only mode; disabling it.", file=sys.stderr)
    exec_root, branch = project_root, None
    if worktree:
        exec_root, branch = create_worktree(project_root, task, args.dirty_policy)

    grok_session_id = str(uuid.uuid4()) if args.backend == "grok" else None
    composed = prompt
    if not args.no_preamble:
        head = PREAMBLE[access]
        if args.backend == "pi" and access == "read-only":
            head += "\n\n" + PI_READONLY_LINE
        if args.backend == "opencode" and access == "read-only":
            head += "\n\n" + OPENCODE_READONLY_LINE
        if args.backend == "grok" and access == "read-only":
            head += "\n\n" + GROK_READONLY_LINE
        if worktree:
            head += "\n\n" + WORKTREE_LINE
        composed = head + "\n\n" + prompt

    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "prompt.md").write_text(composed, encoding="utf-8")
    meta = {
        "task": task, "state": "working", "backend": args.backend, "model": model, "effort": effort,
        # The exact binary this task was admitted with; the helper and every
        # resume use it rather than resolving again.
        "backend_bin": admitted_bin,
        "access": access, "repo": project_root, "exec_root": exec_root, "worktree": worktree,
        "branch": branch, "auto_commit": not args.no_auto_commit, "prompt": composed,
        "fast": bool(getattr(args, "fast", False)),
        "pid": None, "session_id": grok_session_id, "owner": owner_id(), "created_utc": now(),
        "stall_after_s": int(args.stall_after), "deadline_utc": now() + int(args.deadline),
    }
    atomic_write_json(tdir / "meta.json", meta)
    write_status(tdir, meta)

    pid = launch_helper(task, "spawn")
    current = read_json(tdir / "meta.json") or meta
    write_status(tdir, current)
    emit(args, status_payload(tdir, current), f"{task} working pid={pid} backend={args.backend} repo={project_root}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    tdir = task_dir(args.task)
    if not tdir.is_dir():
        raise HerdError(6, f"no such task on this device: {args.task}")
    meta = read_json(tdir / "meta.json")
    if meta is None:
        raise HerdError(6, f"task metadata missing: {args.task}")
    if not meta.get("session_id"):
        raise HerdError(9, "no session to resume (worker never emitted a session id)")

    running = task_process_alive(tdir, meta) and not (tdir / ".done").exists()
    if running:
        if not args.now:
            raise HerdError(9, "task is working; pass --now to interrupt the current turn")
        stop_task_processes(tdir, meta)
        for _ in range(25):
            if not task_process_alive(tdir, meta):
                break
            time.sleep(0.2)
        if task_process_alive(tdir, meta):
            # Launching a second turn into the same checkout and session while
            # the first is demonstrably still running is worse than refusing.
            raise HerdError(9, "the previous turn is still running after a stop attempt; "
                               "not starting a second turn into the same session")

    prompt = resolve_prompt(args)
    if not prompt.strip():
        raise HerdError(2, "message is empty")
    if not args.no_preamble:
        prompt = "[continue] Same rules as before; escalate with QUESTION: if blocked.\n\n" + prompt

    # A resume is a fresh turn against the same binary, so re-check the floor.
    # Previously `send` validated nothing: the floor, the binary, or the model
    # catalogue could all change between turns and the next turn ran unchecked.
    if meta.get("backend") == "opencode":
        require_opencode_version(BACKENDS["opencode"].resolved_bin(meta))
    if meta.get("backend") == "grok":
        require_grok_version(BACKENDS["grok"].resolved_bin(meta))

    # Rotate the previous answer HERE, before any state change or helper launch.
    # run_turn also rotates, but that happens inside the child: if the helper
    # fails to launch, dies before reaching run_turn, or cannot import, the old
    # answer is still in place when reconciliation settles the turn failed -- and
    # `result` then presents the PRECEDING success as this turn's result.
    rotate_report(tdir, strict=True)

    meta["prompt"] = prompt
    meta["state"] = "working"
    meta.pop("stall_reason", None)
    meta["deadline_utc"] = now() + int(args.deadline)
    meta["stall_after_s"] = int(args.stall_after)
    try:
        (tdir / ".done").unlink()
    except OSError:
        pass
    (tdir / "prompt.md").write_text(prompt, encoding="utf-8")
    atomic_write_json(tdir / "meta.json", meta)
    write_status(tdir, meta)

    pid = launch_helper(args.task, "resume")
    current = read_json(tdir / "meta.json") or meta
    emit(args, status_payload(tdir, current), f"{args.task} working pid={pid} (resumed)")
    return 0


def cmd_result(args: argparse.Namespace) -> int:
    tdir = task_dir(args.task)
    if not tdir.is_dir():
        raise HerdError(6, f"no such task on this device: {args.task}")
    settled = TERMINAL_STATES | {"awaiting_reply"}
    payload = reconcile(tdir)
    if args.wait:
        deadline = now() + int(args.timeout)
        while payload["state"] not in settled and now() < deadline:
            time.sleep(1.0)
            payload = reconcile(tdir)
    report = ""
    try:
        report = (tdir / "report.md").read_text(encoding="utf-8")
    except OSError:
        pass
    if getattr(args, "json", False):
        print(json.dumps({"status": payload, "report": report}))
    else:
        print(report or f"(no report yet; state={payload['state']})")
    return 0


def cmd_peek(args: argparse.Namespace) -> int:
    tdir = task_dir(args.task)
    if not tdir.is_dir():
        raise HerdError(6, f"no such task on this device: {args.task}")
    try:
        # Bounded read: `peek` used to load the whole live stream just to show
        # its tail, which is the same MemoryError shape cap_events had.
        raw = _tail_bytes(tdir / "events.jsonl", PEEK_TAIL_BYTES)
        lines = raw.decode("utf-8", errors="replace").splitlines()
    except (OSError, MemoryError):
        lines = []
    tail = lines[-int(args.tail):] if args.tail else lines
    if args.thinking is not None:
        backend = BACKENDS.get((read_json(tdir / "meta.json") or {}).get("backend", "codex"), BACKENDS["codex"])
        shown = 0
        for ln in tail:
            try:
                th = backend.parse(json.loads(ln)).get("thinking")
            except json.JSONDecodeError:
                th = None
            if th:
                print(th)
                shown += 1
                if shown >= args.thinking:
                    break
        return 0
    for ln in tail:
        print(ln)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = reap()
    owner = resolve_owner_filter(args)
    if owner is not None:
        rows = [r for r in rows if r.get("owner") == owner]
    if not getattr(args, "all", False):
        rows = [r for r in rows if r["state"] not in RECLAIMABLE_CLEAN]
    if getattr(args, "json", False):
        print(json.dumps(rows))
        return 0
    if not rows:
        print("no tasks")
        return 0
    for r in rows:
        age = "-" if r["last_output_age_s"] is None else f"{r['last_output_age_s']}s"
        print(f"{r['task']:<26} {r['state']:<14} {r['backend'] or '-':<7} age={age:<7} {r['repo'] or ''}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    tdir = task_dir(args.task)
    if not tdir.is_dir():
        raise HerdError(6, f"no such task on this device: {args.task}")
    payload = reconcile(tdir)
    emit(args, payload, f"{payload['task']} {payload['state']} pid_alive={payload['pid_alive']} {payload['repo'] or ''}")
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    tdir = task_dir(args.task)
    if not tdir.is_dir():
        raise HerdError(6, f"no such task on this device: {args.task}")
    meta = read_json(tdir / "meta.json") or {}
    stop_task_processes(tdir, meta)
    # Confirm death before recording a terminal state. Writing `killed` while the
    # tracked processes are demonstrably still running is a false terminal state:
    # `prune` may then reclaim the directory and `send` may race a second turn
    # into a session the old one is still using.
    for _ in range(25):
        if not task_process_alive(tdir, meta):
            break
        time.sleep(0.2)
    if task_process_alive(tdir, meta):
        raise HerdError(9, f"{args.task}: tracked processes are still alive after SIGINT and SIGKILL; "
                           "not recording a terminal state. Inspect them before retrying")
    mark_terminal(tdir, meta, "killed", "user-kill")
    emit(args, status_payload(tdir, meta), f"{args.task} killed")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    reap()
    root = sessions_dir()
    owner = resolve_owner_filter(args)
    idle_s = max(0, int(args.idle_min)) * 60
    reclaim, kept = [], []
    if root.is_dir():
        for tdir in sorted(root.iterdir()):
            if not tdir.is_dir():
                continue
            meta = read_json(tdir / "meta.json") or {}
            state = meta.get("state", "working")
            if owner is not None and meta.get("owner") != owner:
                continue
            # meta["pid"] is written as None at spawn and never updated -- the live
            # pids are in helper.json and child.json -- so testing it made this gate
            # always false, i.e. no liveness protection at all.
            if state not in TERMINAL_STATES or task_process_alive(tdir, meta):
                kept.append((tdir.name, f"live/{state}"))
                continue
            mtime = output_mtime(tdir)
            if mtime is not None and (now() - mtime) < idle_s:
                kept.append((tdir.name, f"idle<{args.idle_min}m"))
                continue
            if state in RESUMABLE_UNRESOLVED and not args.include_unresolved:
                kept.append((tdir.name, f"{state} (resumable; --include-unresolved to reclaim)"))
                continue
            reclaim.append(tdir)
    if getattr(args, "json", False):
        print(json.dumps({"reclaim": [t.name for t in reclaim], "kept": kept, "applied": bool(args.apply)}))
    else:
        for name, why in kept:
            print(f"keep    {name:<26} {why}")
        for t in reclaim:
            print(f"{'remove ' if args.apply else 'would  '}{t.name}")
    if args.apply:
        for t in reclaim:
            shutil.rmtree(t, ignore_errors=True)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    pi_bin = shutil.which("pi")
    pi_auth_ready = False
    pi_auth_status = "pi-not-found"
    if pi_bin:
        try:
            proc = subprocess.run(
                [pi_bin, "auth", "check", "--provider", "openai-codex", "--json", "--no-refresh"],
                capture_output=True, text=True, timeout=15,
            )
            data = json.loads(proc.stdout) if proc.stdout.strip() else {}
            pi_auth_status = str(data.get("status", f"exit-{proc.returncode}"))
            pi_auth_ready = proc.returncode == 0 and pi_auth_status == "ready"
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            pi_auth_status = type(exc).__name__
    checks = {
        "device_id": device_id(), "state_root": str(state_root()), "sessions_dir": str(sessions_dir()),
        "sessions_dir_exists": sessions_dir().is_dir(), "owner": owner_id(), "python": sys.version.split()[0],
        "codex_on_path": shutil.which("codex") is not None, "pi_on_path": pi_bin is not None,
        "pi_chatgpt_auth_ready": pi_auth_ready, "pi_chatgpt_auth_status": pi_auth_status,
        "claude_on_path": shutil.which("claude") is not None,
        "muse_on_path": shutil.which("muse") is not None,
        "opencode_on_path": shutil.which("opencode") is not None,
        "grok_on_path": shutil.which("grok") is not None,
        # Report the binary a spawn would ACTUALLY use, which is the
        # DELEGATE_OPENCODE_BIN override when one is set -- doctor reporting the
        # PATH copy while spawn used another is exactly the kind of mismatch that
        # sends someone debugging the wrong install. Surfaced because the floor
        # is a hard refusal when set: below 1.18.20 a `task` subagent's permission
        # request is dropped rather than answered, and the tool runs anyway.
        **_opencode_doctor_fields(),
        **_grok_doctor_fields(),
        "models_env": str(KIT_ROOT / "config" / "models.env"),
        "default_stall_after_s": DEFAULT_STALL_AFTER_S, "default_deadline_s": DEFAULT_DEADLINE_S,
    }
    if getattr(args, "json", False):
        print(json.dumps(checks))
    else:
        for k, v in checks.items():
            print(f"{k}: {v}")
    return 0


def cmd_run_turn(args: argparse.Namespace) -> int:
    return run_turn(args.task, args.mode)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herd", description="Detached delegate workers (codex/pi/claude/muse/opencode/grok).")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_json(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--json", action="store_true")

    def add_prompt(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("prompt", nargs="?")
        sp.add_argument("-f", "--prompt-file", dest="prompt_file")
        sp.add_argument("--prompt-stdin", action="store_true")

    def add_deadlines(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--stall-after", type=int, default=DEFAULT_STALL_AFTER_S)
        sp.add_argument("--deadline", type=int, default=DEFAULT_DEADLINE_S)

    sp = sub.add_parser("spawn"); add_json(sp)
    sp.add_argument("mode", choices=list(ACCESS_BY_MODE))
    add_prompt(sp); add_deadlines(sp)
    sp.add_argument("--backend", default="codex")
    # Opencode's names come from config/models.env, so the accepted set is built
    # at parse time rather than frozen in the source.
    sp.add_argument("--profile", default=None,
                    choices=list(PROFILES) + list(MUSE_PROFILES) + opencode_profiles())
    sp.add_argument("--model"); sp.add_argument("--effort")
    sp.add_argument("--access", "--sandbox", dest="access")
    sp.add_argument("--project-root", dest="project_root")
    sp.add_argument("--worktree", action="store_true")
    sp.add_argument("--dirty-policy", default="fail", choices=["fail", "ignore"])
    sp.add_argument("--no-auto-commit", action="store_true")
    sp.add_argument("--no-preamble", action="store_true")
    sp.add_argument("--fast", action="store_true",
                    help="enable Codex fast_mode for this run (explicit opt-in; codex backend only)")
    sp.add_argument("--name")
    sp.set_defaults(func=cmd_spawn)

    sp = sub.add_parser("send"); add_json(sp); add_deadlines(sp)
    sp.add_argument("task"); add_prompt(sp)
    sp.add_argument("--now", action="store_true")
    sp.add_argument("--no-preamble", action="store_true")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("result"); add_json(sp)
    sp.add_argument("task"); sp.add_argument("--wait", action="store_true")
    sp.add_argument("--timeout", type=int, default=3600)
    sp.set_defaults(func=cmd_result)

    sp = sub.add_parser("peek"); add_json(sp)
    sp.add_argument("task"); sp.add_argument("--tail", type=int, default=15)
    sp.add_argument("--thinking", nargs="?", const=50, type=int)
    sp.set_defaults(func=cmd_peek)

    sp = sub.add_parser("list"); add_json(sp)
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--any-owner", dest="any_owner", action="store_true")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("status"); add_json(sp)
    sp.add_argument("task"); sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("kill"); add_json(sp)
    sp.add_argument("task"); sp.set_defaults(func=cmd_kill)

    sp = sub.add_parser("prune"); add_json(sp)
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--idle-min", type=int, default=DEFAULT_IDLE_MIN)
    sp.add_argument("--any-owner", dest="any_owner", action="store_true")
    sp.add_argument("--include-unresolved", action="store_true")
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser("doctor"); add_json(sp); sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("__run_turn")
    sp.add_argument("--task", required=True)
    sp.add_argument("--mode", required=True, choices=["spawn", "resume"])
    sp.set_defaults(func=cmd_run_turn)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except HerdError as exc:
        print(f"herd: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())

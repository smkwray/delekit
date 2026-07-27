#!/usr/bin/env python3
"""herd: detached, resumable, steerable headless delegate workers (codex/claude).

Stdlib only. See docs/detached-runner.md for the design. This is the shared
cross-platform core; bin/herd.sh and bin/herd.ps1 are thin shims onto it.

Backends stream JSON so the turn helper can capture the session id (for resume)
and observe activity (for the stall watchdog). The exact provider event schema
is an integration boundary: the parsers below accept a tolerant superset and are
the point to re-verify after a Codex/Claude CLI upgrade.
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
import time
from pathlib import Path
from threading import Thread
from typing import Any

KIT_ROOT = Path(__file__).resolve().parents[1]

TERMINAL_STATES = {"done", "failed", "stalled", "killed"}
RESUMABLE_UNRESOLVED = {"failed", "stalled", "awaiting_reply"}
RECLAIMABLE_CLEAN = {"done", "killed"}
NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
QUESTION_RE = re.compile(r"(^|\n)\s*QUESTION:", re.IGNORECASE)

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


def stop_pid(pid: Any) -> None:
    """Stop a detached helper and its whole process group. No-op if gone."""
    if not pid_alive(pid):
        return
    pid = int(pid)
    if os.name == "nt":
        _signal_quiet(pid, signal.CTRL_BREAK_EVENT)
    else:
        _killpg_quiet(pid, signal.SIGINT)
    deadline = now() + 5
    while now() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.2)
    if os.name == "nt":
        _signal_quiet(pid, signal.SIGTERM)
    else:
        _killpg_quiet(pid, signal.SIGKILL)


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

    def locate_bin(self) -> str:
        override = os.environ.get(self.bin_env)
        if override:
            if not (os.path.isfile(override) and os.access(override, os.X_OK)):
                raise HerdError(7, f"{self.bin_env}={override} is not executable")
            return override
        found = shutil.which(self.name)
        if not found:
            raise HerdError(7, f"{self.name} CLI not found on PATH (or set {self.bin_env})")
        return found

    def sandbox_args(self, access: str) -> list[str]:
        raise NotImplementedError

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


BACKENDS: dict[str, Backend] = {"codex": CodexBackend(), "claude": ClaudeBackend()}


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


def status_payload(tdir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    mtime = output_mtime(tdir)
    payload = {
        "task": tdir.name, "state": meta.get("state", "working"),
        "backend": meta.get("backend"), "model": meta.get("model"), "access": meta.get("access"),
        "repo": meta.get("repo"), "owner": meta.get("owner"), "pid": meta.get("pid"),
        "pid_alive": pid_alive(meta.get("pid")), "session_id": meta.get("session_id"),
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


def reconcile(tdir: Path, enforce: bool = True) -> dict[str, Any] | None:
    meta = read_json(tdir / "meta.json")
    if meta is None:
        return None
    # .done means the turn helper settled this turn; trust the recorded state.
    if (tdir / ".done").exists():
        return status_payload(tdir, meta)

    pid = meta.get("pid")
    if not pid_alive(pid):
        mark_terminal(tdir, meta, "failed", "process-exited-without-marker")
        return status_payload(tdir, meta)

    deadline = meta.get("deadline_utc")
    if isinstance(deadline, (int, float)) and now() > deadline:
        if enforce:
            stop_pid(pid)
        mark_terminal(tdir, meta, "failed", "deadline")
        return status_payload(tdir, meta)

    stall_after = int(meta.get("stall_after_s") or DEFAULT_STALL_AFTER_S)
    mtime = output_mtime(tdir)
    if mtime is not None and (now() - mtime) > stall_after:
        if enforce:
            stop_pid(pid)
        mark_terminal(tdir, meta, "stalled", "no-output")
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
def cap_events(tdir: Path) -> None:
    p = tdir / "events.jsonl"
    try:
        if p.stat().st_size <= EVENTS_MAX_MB * 1024 * 1024:
            return
        keep = p.read_bytes()[-(EVENTS_MAX_MB * 1024 * 1024 // 2):]
        p.write_bytes(b"# ...truncated...\n" + keep)
    except OSError:
        pass


def run_turn(task: str, mode: str) -> int:
    tdir = task_dir(task)
    meta = read_json(tdir / "meta.json")
    if meta is None:
        return 6
    backend = BACKENDS[meta["backend"]]
    argv, stdin_text, cwd = backend.spawn_cmd(meta) if mode == "spawn" else backend.resume_cmd(meta)

    env = dict(os.environ, CI="1", GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", PAGER="cat", NO_COLOR="1")
    env.pop("CLAUDECODE", None)
    stderr_f = open(tdir / "stderr.log", "ab")
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_f,
                            cwd=cwd, env=env, text=True, bufsize=1)
    try:
        proc.stdin.write(stdin_text)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass

    last = [now()]
    kill_reason: list[str | None] = [None]
    stall_after = int(meta.get("stall_after_s") or DEFAULT_STALL_AFTER_S)
    deadline = meta.get("deadline_utc")

    def watch() -> None:
        while proc.poll() is None:
            time.sleep(1)
            if isinstance(deadline, (int, float)) and now() > deadline:
                kill_reason[0] = "deadline"
                _terminate_child(proc)
                return
            if now() - last[0] > stall_after:
                kill_reason[0] = "no-output"
                _terminate_child(proc)
                return

    watcher = Thread(target=watch, daemon=True)
    watcher.start()

    last_message: str | None = None
    with open(tdir / "events.jsonl", "a", encoding="utf-8") as events:
        for line in proc.stdout:
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
            if parsed.get("session_id") and not meta.get("session_id"):
                meta["session_id"] = parsed["session_id"]
                atomic_write_json(tdir / "meta.json", meta)
            if parsed.get("message"):
                last_message = parsed["message"]

    rc = proc.wait()
    watcher.join(timeout=2)
    if last_message is not None:
        (tdir / "report.md").write_text(last_message, encoding="utf-8")
    cap_events(tdir)
    _maybe_autocommit(meta)

    if kill_reason[0] == "deadline":
        mark_terminal(tdir, meta, "failed", "deadline")
    elif kill_reason[0] == "no-output":
        mark_terminal(tdir, meta, "stalled", "no-output")
    elif rc != 0:
        mark_terminal(tdir, meta, "failed", f"exit-{rc}")
    else:
        mark_terminal(tdir, meta, "awaiting_reply" if has_question(tdir) else "done", None)
    return 0


def _terminate_child(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
    except OSError:
        return
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


def resolve_model_effort(backend: str, profile: str, profile_explicit: bool,
                         model: str | None, effort: str | None) -> tuple[str | None, str | None]:
    env = load_models_env()
    if backend == "codex":
        key = profile.upper()
        model = model or env.get(f"DELEGATE_MODEL_{key}")
        effort = effort or env.get(f"DELEGATE_EFFORT_{key}", "high")
        if not model:
            raise HerdError(2, f"no model configured for profile {profile}")
        return model, effort
    if profile_explicit:
        raise HerdError(2, f"--profile resolves a model only for codex; pass --model for --backend {backend}")
    return model, effort


def create_worktree(project_root: str, task: str, dirty_policy: str) -> tuple[str, str]:
    inside = subprocess.run(["git", "-C", project_root, "rev-parse", "--is-inside-work-tree"], capture_output=True)
    if inside.returncode != 0:
        raise HerdError(2, "--worktree requires a Git repository")
    dirty = subprocess.run(["git", "-C", project_root, "status", "--porcelain"], capture_output=True, text=True).stdout
    if dirty.strip() and dirty_policy == "fail":
        raise HerdError(3, "main checkout is dirty; commit/stash or pass --dirty-policy ignore")
    branch = f"delegate/herd-{task}"
    wt_root = os.environ.get("DELEGATE_WORKTREE_DIR") or os.path.join(os.path.dirname(project_root), ".delegate-worktrees")
    wt_dir = os.path.join(wt_root, os.path.basename(project_root), f"herd-{task}")
    os.makedirs(os.path.dirname(wt_dir), exist_ok=True)
    add = subprocess.run(["git", "-C", project_root, "worktree", "add", "-b", branch, wt_dir, "HEAD"], capture_output=True, text=True)
    if add.returncode != 0:
        raise HerdError(4, f"failed to create worktree: {add.stderr.strip()}")
    return wt_dir, branch


# Keep launched helpers referenced so the intentionally-detached Popen objects
# are not finalized (and warned about) while the supervisor process is alive.
_LAUNCHED: list[subprocess.Popen] = []


def launch_helper(task: str, mode: str) -> int:
    env = dict(os.environ, DELEGATE_STATE_DIR=str(state_root()), DELEKIT_DEVICE_ID=device_id())
    cmd = [sys.executable, os.path.abspath(__file__), "__run_turn", "--task", task, "--mode", mode]
    kwargs: dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, env=env)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
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
        raise HerdError(2, f"unsupported backend: {args.backend} (codex or claude)")
    access = args.access or ACCESS_BY_MODE[args.mode]
    if access not in PREAMBLE:
        raise HerdError(2, f"invalid access: {access}")
    prompt = resolve_prompt(args)
    if not prompt.strip():
        raise HerdError(2, "task prompt is empty")
    model, effort = resolve_model_effort(args.backend, args.profile, args.profile != DEFAULT_PROFILE,
                                         args.model, args.effort)
    project_root = args.project_root or find_project_root(os.getcwd())
    project_root = os.path.realpath(project_root)
    if not os.path.isdir(project_root):
        raise HerdError(2, f"project root does not exist: {project_root}")

    task = sanitize_task(args.name) if args.name else default_task_name()
    tdir = task_dir(task)
    if tdir.exists():
        raise HerdError(2, f"task already exists: {task}")

    worktree = args.worktree and access != "read-only"
    exec_root, branch = project_root, None
    if worktree:
        exec_root, branch = create_worktree(project_root, task, args.dirty_policy)

    composed = prompt
    if not args.no_preamble:
        head = PREAMBLE[access] + ("\n\n" + WORKTREE_LINE if worktree else "")
        composed = head + "\n\n" + prompt

    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "prompt.md").write_text(composed, encoding="utf-8")
    meta = {
        "task": task, "state": "working", "backend": args.backend, "model": model, "effort": effort,
        "access": access, "repo": project_root, "exec_root": exec_root, "worktree": worktree,
        "branch": branch, "auto_commit": not args.no_auto_commit, "prompt": composed,
        "fast": bool(getattr(args, "fast", False)),
        "pid": None, "session_id": None, "owner": owner_id(), "created_utc": now(),
        "stall_after_s": int(args.stall_after), "deadline_utc": now() + int(args.deadline),
    }
    atomic_write_json(tdir / "meta.json", meta)
    write_status(tdir, meta)

    pid = launch_helper(task, "spawn")
    meta["pid"] = pid
    atomic_write_json(tdir / "meta.json", meta)
    write_status(tdir, meta)
    emit(args, status_payload(tdir, meta), f"{task} working pid={pid} backend={args.backend} repo={project_root}")
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

    running = pid_alive(meta.get("pid")) and not (tdir / ".done").exists()
    if running:
        if not args.now:
            raise HerdError(9, "task is working; pass --now to interrupt the current turn")
        stop_pid(meta.get("pid"))
        for _ in range(25):
            if not pid_alive(meta.get("pid")):
                break
            time.sleep(0.2)

    prompt = resolve_prompt(args)
    if not prompt.strip():
        raise HerdError(2, "message is empty")
    if not args.no_preamble:
        prompt = "[continue] Same rules as before; escalate with QUESTION: if blocked.\n\n" + prompt

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
    meta["pid"] = pid
    atomic_write_json(tdir / "meta.json", meta)
    emit(args, status_payload(tdir, meta), f"{args.task} working pid={pid} (resumed)")
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
        lines = (tdir / "events.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
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
    stop_pid(meta.get("pid"))
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
            if state not in TERMINAL_STATES or pid_alive(meta.get("pid")):
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
    checks = {
        "device_id": device_id(), "state_root": str(state_root()), "sessions_dir": str(sessions_dir()),
        "sessions_dir_exists": sessions_dir().is_dir(), "owner": owner_id(), "python": sys.version.split()[0],
        "codex_on_path": shutil.which("codex") is not None, "claude_on_path": shutil.which("claude") is not None,
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
    parser = argparse.ArgumentParser(prog="herd", description="Detached delegate workers (codex/claude).")
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
    sp.add_argument("--profile", default=DEFAULT_PROFILE, choices=list(PROFILES))
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

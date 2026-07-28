#!/usr/bin/env python3
"""Create every Delekit Git worktree under <project>/.worktrees/."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


class WorktreeError(RuntimeError):
    pass


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
IGNORE_FORMS = {".worktrees", ".worktrees/", "/.worktrees", "/.worktrees/"}


def _git(project_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(project_root), *args],
        text=True,
        capture_output=True,
    )


def git_root(start: str | Path) -> Path:
    result = _git(Path(start), "rev-parse", "--show-toplevel")
    if result.returncode:
        raise WorktreeError("worktree creation requires a Git repository")
    return Path(result.stdout.strip()).resolve()


def require_project_ignore(project_root: Path) -> None:
    ignore = project_root / ".gitignore"
    if not ignore.is_file():
        raise WorktreeError(
            f"{ignore} must contain .worktrees/ before creating delegate worktrees"
        )
    patterns = {
        line.strip()
        for line in ignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if patterns.isdisjoint(IGNORE_FORMS):
        raise WorktreeError(
            f"{ignore} must contain .worktrees/ before creating delegate worktrees"
        )


def create_worktree(
    project_root: str | Path,
    name: str,
    branch: str,
    base: str = "HEAD",
) -> Path:
    root = git_root(project_root)
    if not SAFE_NAME.fullmatch(name) or name in {".", ".."}:
        raise WorktreeError(f"invalid worktree name: {name!r}")
    require_project_ignore(root)

    worktree_root = root / ".worktrees"
    if worktree_root.is_symlink():
        raise WorktreeError(f"refusing symlinked worktree root: {worktree_root}")
    worktree_root.mkdir(exist_ok=True)
    target = worktree_root / name
    if target.exists():
        raise WorktreeError(f"worktree path already exists: {target}")

    result = _git(root, "worktree", "add", "-b", branch, str(target), base)
    if result.returncode:
        raise WorktreeError(result.stderr.strip() or "git worktree add failed")
    try:
        copy_worktree_includes(root, target)
    except (OSError, WorktreeError):
        _git(root, "worktree", "remove", "--force", str(target))
        _git(root, "branch", "-D", branch)
        raise
    return target.resolve()


def _listed_paths(project_root: Path, *exclude_args: str) -> set[str]:
    result = _git(project_root, "ls-files", "-o", "-i", "-z", *exclude_args)
    if result.returncode:
        raise WorktreeError(result.stderr.strip() or "git ls-files failed")
    return {item for item in result.stdout.split("\0") if item}


def copy_worktree_includes(project_root: Path, target: Path) -> None:
    include = project_root / ".worktreeinclude"
    if not include.is_file():
        return
    ignored = _listed_paths(project_root, "--exclude-standard")
    requested = _listed_paths(project_root, "--exclude-from=.worktreeinclude")
    for relative in sorted(ignored & requested):
        if relative == ".worktrees" or relative.startswith(".worktrees/"):
            continue
        source = project_root / relative
        destination = target / relative
        if not source.is_file() and not source.is_symlink():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            destination.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, destination)


def create_from_hook(payload: dict[str, object]) -> Path:
    if payload.get("hook_event_name") != "WorktreeCreate":
        raise WorktreeError("expected a WorktreeCreate hook payload")
    cwd = payload.get("cwd")
    requested = payload.get("name")
    if not isinstance(cwd, str) or not isinstance(requested, str):
        raise WorktreeError("WorktreeCreate payload requires string cwd and name")

    suffix = uuid.uuid4().hex[:8]
    name = f"{requested}-{suffix}"
    branch = f"delegate/{name}"
    return create_worktree(cwd, name, branch)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--project-root", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--branch", required=True)
    create.add_argument("--base", default="HEAD")
    sub.add_parser("hook")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "hook":
            target = create_from_hook(json.load(sys.stdin))
        else:
            target = create_worktree(
                args.project_root,
                args.name,
                args.branch,
                args.base,
            )
    except (WorktreeError, json.JSONDecodeError) as exc:
        print(f"delekit worktree: {exc}", file=sys.stderr)
        return 2
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

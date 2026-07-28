from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from worktree_manager import WorktreeError, create_from_hook, create_worktree  # noqa: E402


class WorktreeManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        (self.repo / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", ".gitignore"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "base"],
            check=True,
        )
        self.created: list[tuple[Path, str]] = []

    def tearDown(self) -> None:
        for path, branch in reversed(self.created):
            subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(path)],
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(self.repo), "branch", "-D", branch],
                check=False,
                capture_output=True,
            )
        self.temp.cleanup()

    def test_direct_creation_uses_project_local_root(self) -> None:
        path = create_worktree(self.repo, "direct-test", "delegate/direct-test")
        self.created.append((path, "delegate/direct-test"))
        self.assertEqual(path.parent, (self.repo / ".worktrees").resolve())
        self.assertTrue(path.is_dir())

    def test_hook_creation_is_unique_and_project_local(self) -> None:
        payload = {
            "hook_event_name": "WorktreeCreate",
            "cwd": str(self.repo),
            "name": "tandy-terra-worktree",
        }
        first = create_from_hook(json.loads(json.dumps(payload)))
        second = create_from_hook(json.loads(json.dumps(payload)))
        self.created.extend(
            [
                (first, f"delegate/{first.name}"),
                (second, f"delegate/{second.name}"),
            ]
        )
        self.assertEqual(first.parent, (self.repo / ".worktrees").resolve())
        self.assertEqual(second.parent, (self.repo / ".worktrees").resolve())
        self.assertNotEqual(first, second)

    def test_explicit_ignored_inputs_are_copied(self) -> None:
        (self.repo / ".gitignore").write_text(
            ".worktrees/\n.env.local\n",
            encoding="utf-8",
        )
        (self.repo / ".worktreeinclude").write_text(".env.local\n", encoding="utf-8")
        (self.repo / ".env.local").write_text("local-only\n", encoding="utf-8")
        path = create_worktree(self.repo, "includes", "delegate/includes")
        self.created.append((path, "delegate/includes"))
        self.assertEqual(
            (path / ".env.local").read_text(encoding="utf-8"),
            "local-only\n",
        )

    def test_creation_requires_root_gitignore_rule(self) -> None:
        (self.repo / ".gitignore").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(WorktreeError, r"must contain \.worktrees/"):
            create_worktree(self.repo, "blocked", "delegate/blocked")
        self.assertFalse((self.repo / ".worktrees").exists())


if __name__ == "__main__":
    unittest.main()

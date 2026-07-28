#!/usr/bin/env python3
"""Static, dependency-free verification for the portable delegate kit."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    "README.md",
    "DEVICE-AGENT-INSTALL.md",
    "docs/known-issues.md",
    "LICENSE",
    "config/models.env",
    "config/cliproxy-local.fragment.yaml",
    "generated/claude/skills/orchestrate-delegates/SKILL.md",
    "generated/cliproxy/oauth-model-alias.yaml",
    "bin/claudex.sh",
    "bin/dairy.sh",
    "bin/dairy.ps1",
    "bin/prune-worktrees.sh",
    "tools/worktree_manager.py",
    "bin/claudex.ps1",
    "bin/ccg.cmd",
    "bin/ccg-launch.ps1",
    "bin/ccg-snippet.sh",
    "bin/ccg-snippet.ps1",
    "bin/install-launchd-macos.sh",
    "tools/seed_claude_context_cache.py",
    "tests/test_seed_context_cache.py",
    "tests/test-macos-install.sh",
    "tests/smoke-test.ps1",
]


def fail(message: str, failures: list[str]) -> None:
    failures.append(message)


def main() -> int:
    failures: list[str] = []
    for relative in REQUIRED:
        if not (ROOT / relative).is_file():
            fail(f"missing: {relative}", failures)

    render = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "render_config.py"), "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if render.returncode:
        fail(render.stdout + render.stderr, failures)

    for path in (ROOT / "generated" / "claude" / "agents").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        if "{{" in text:
            fail(f"unresolved placeholder in {path.relative_to(ROOT)}", failures)
        if not text.startswith("---\n"):
            fail(f"missing YAML frontmatter in {path.relative_to(ROOT)}", failures)
        if "name:" not in text or "description:" not in text or "model:" not in text:
            fail(f"incomplete agent frontmatter in {path.relative_to(ROOT)}", failures)
    # Capability boundaries are checked by permission mode, not by agent name, so
    # adding a profile or capability does not require editing this script.
    agents = sorted((ROOT / "generated" / "claude" / "agents").glob("*.md"))
    if not agents:
        fail("no generated agent definitions", failures)
    writers = 0
    readers = 0
    for path in agents:
        text = path.read_text(encoding="utf-8")
        if "permissionMode: acceptEdits" in text:
            writers += 1
            if "- Agent" not in text or '"mcp__*"' not in text:
                fail(f"writer missing shallow/local tool boundary: {path.relative_to(ROOT)}", failures)
        elif "permissionMode: plan" in text:
            readers += 1
            for token in ("  - Read", "  - Bash", "  - SendMessage"):
                if token not in text:
                    fail(f"read-only agent missing {token}: {path.relative_to(ROOT)}", failures)
        else:
            fail(f"agent declares no known permission mode: {path.relative_to(ROOT)}", failures)
    if not writers:
        fail("no write-capable agent generated", failures)
    if not readers:
        fail("no read-only agent generated", failures)

    for path in (ROOT / "generated" / "claude" / "skills").glob("**/*"):
        if path.is_file() and "{{" in path.read_text(encoding="utf-8"):
            fail(f"unresolved placeholder in {path.relative_to(ROOT)}", failures)


    # Keep always-sent custom-agent bodies lean. Descriptions/frontmatter are
    # excluded because they serve model routing and capability configuration.
    for path in (ROOT / "generated" / "claude" / "agents").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        body = parts[2].strip() if len(parts) == 3 else ""
        words = re.findall(r"\b[\w'-]+\b", body)
        if len(words) > 80:
            fail(f"agent body exceeds 80 words ({len(words)}): {path.relative_to(ROOT)}", failures)

    launcher_checks = {
        "CLAUDE_CODE_SUBAGENT_MODEL": "unset/remove global subagent override",
        "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT": "forward effort for custom aliases",
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "gateway cache optimization",
        "ENABLE_TOOL_SEARCH": "conservative gateway tool-search policy",
        "DELEGATE_PARENT_MODEL": "parent-model pin against global /model contamination",
        "DELEKIT_TANDY_CONTEXT_MODE": "opt-in isolated 272k Tandy profile",
        "seed_claude_context_cache.py": "seed opt-in Tandy client data",
    }
    for relative in ("bin/claudex.sh", "bin/claudex.ps1"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        for token, purpose in launcher_checks.items():
            if token not in text:
                fail(f"{relative} missing {purpose}: {token}", failures)

    # Provider IDs may appear only in the source-of-truth file, generated output,
    # reference copies supplied by the user, and documentation that explicitly
    # discusses the migration. Scripts/templates must never pin them.
    provider_pattern = re.compile(r"\bgpt-[A-Za-z0-9._-]+")
    allowed_roots = {
        ROOT / "config" / "models.env",
    }
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() in {".zip", ".pyc"}:
            continue
        if "generated" in path.parts or "reference" in path.parts or "patches" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if provider_pattern.search(text) and path not in allowed_roots:
            fail(f"provider model ID leaked outside central config: {path.relative_to(ROOT)}", failures)


    if failures:
        print("VERIFY FAILED")
        for item in failures:
            print(f"- {item.rstrip()}")
        return 1
    print("VERIFY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

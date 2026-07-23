#!/usr/bin/env python3
"""Seed Claude Code's isolated gateway profile with Tandy's 272k window."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

WINDOW = 272_000
FAMILY = "claude-sonnet-4-6"


def patch_client_data(value: Any) -> dict[str, Any]:
    payload = dict(value) if isinstance(value, dict) else {}
    payload["kelp_forest_sonnet"] = str(WINDOW)
    raw_rowan = payload.get("rowan_thicket")
    rowan = dict(raw_rowan) if isinstance(raw_rowan, dict) else {}
    rowan[FAMILY] = WINDOW
    payload["rowan_thicket"] = rowan
    return payload


def patch_document(document: dict[str, Any]) -> dict[str, Any]:
    patched = copy.deepcopy(document)
    patched["clientDataCache"] = patch_client_data(patched.get("clientDataCache"))

    slots = patched.get("clientDataCacheSlots")
    if isinstance(slots, dict):
        for slot in slots.values():
            if isinstance(slot, dict):
                slot["data"] = patch_client_data(slot.get("data"))

    raw_windows = patched.get("autoCompactWindowsCache")
    windows = dict(raw_windows) if isinstance(raw_windows, dict) else {}
    windows[FAMILY] = WINDOW
    patched["autoCompactWindowsCache"] = windows
    return patched


def seed(config_dir: Path) -> bool:
    """Patch config_dir/.claude.json atomically; return whether it changed."""
    config_dir.mkdir(parents=True, exist_ok=True)
    state_path = config_dir / ".claude.json"

    if state_path.exists():
        try:
            document = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read {state_path}: {exc}") from exc
        if not isinstance(document, dict):
            raise RuntimeError(f"{state_path}: expected a top-level JSON object")
        mode = state_path.stat().st_mode & 0o777
    else:
        document = {}
        mode = 0o600

    patched = patch_document(document)
    if patched == document:
        return False

    fd, temporary_name = tempfile.mkstemp(prefix=".claude.json.", dir=config_dir)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(patched, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, state_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return True


def main() -> None:
    raw_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not raw_config_dir:
        raise SystemExit("CLAUDE_CONFIG_DIR must be set")
    try:
        seed(Path(raw_config_dir).expanduser())
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()

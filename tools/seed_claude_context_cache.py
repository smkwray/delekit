#!/usr/bin/env python3
"""Enforce the isolated gateway profile's invariants on every launch.

Tandy's 272k window. The seed only holds while the profile has **no
first-party credential**. Claude Code treats these launches as `firstParty` (its
internal "gateway" mode is gated on CLAUDE_CODE_USE_GATEWAY, which the launchers
deliberately do not set), so a saved login here activates the first-party
bootstrap writer, which overwrites autoCompactWindowsCache with the server's
value on every model switch. Gateway inference never needs that credential - it
authenticates with ANTHROPIC_AUTH_TOKEN - so we quarantine it and keep the
profile identity-free. That is why `/login` inside a gateway session silently
drops Tandy to 200k.

Commit attribution. Settings are per-profile and the installer creates this one
empty, so disabling the `Co-Authored-By:` trailer in ~/.claude/settings.json has
no effect on gateway sessions - the trailer silently returns for every one.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

WINDOW = 272_000
FAMILY = "claude-sonnet-4-6"
# Claude Code's first-party identity inside a profile: a saved OAuth credential
# file plus the account keys it caches in .claude.json.
CREDENTIAL_FILE = ".credentials.json"
IDENTITY_KEYS = ("oauthAccount", "userID")
# Commit/PR attribution is per-profile, and this profile is created empty by the
# installer -- so a `Co-Authored-By:` trailer the user already disabled in
# ~/.claude/settings.json comes back for every gateway session. Empty strings
# suppress it; the deprecated `includeCoAuthoredBy` boolean conflicts with this
# key, so it is removed rather than left to fight with it.
SETTINGS_FILE = "settings.json"
ATTRIBUTION = {"commit": "", "pr": ""}


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
    # Drop any cached first-party identity; see the module docstring.
    for key in IDENTITY_KEYS:
        patched.pop(key, None)
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

    write_json(state_path, patched, mode)
    return True


def seed_attribution(config_dir: Path) -> bool:
    """Disable commit/PR attribution in the profile; return whether it changed.

    Only writes the key when it is absent or wrong, so a deliberate override is
    the one thing this cannot silently undo -- an explicit non-empty string is
    left alone.
    """
    settings_path = config_dir / SETTINGS_FILE
    if settings_path.exists():
        try:
            document = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read {settings_path}: {exc}") from exc
        if not isinstance(document, dict):
            raise RuntimeError(f"{settings_path}: expected a top-level JSON object")
        mode = settings_path.stat().st_mode & 0o777
    else:
        document = {}
        mode = 0o600

    patched = copy.deepcopy(document)
    patched.pop("includeCoAuthoredBy", None)  # deprecated; conflicts with attribution
    attribution = patched.get("attribution")
    if not isinstance(attribution, dict):
        attribution = {}
    else:
        attribution = dict(attribution)
    for key, value in ATTRIBUTION.items():
        attribution.setdefault(key, value)
    patched["attribution"] = attribution
    if patched == document:
        return False

    write_json(settings_path, patched, mode)
    return True


def write_json(path: Path, payload: dict[str, Any], mode: int) -> None:
    """Replace path atomically, so a crash cannot leave a half-written file."""
    fd, temporary_name = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def quarantine_credential(config_dir: Path) -> Path | None:
    """Move a saved first-party credential out of the profile; return where."""
    credential = config_dir / CREDENTIAL_FILE
    if not credential.exists():
        return None
    quarantine = config_dir / "quarantined-firstparty-auth"
    quarantine.mkdir(parents=True, exist_ok=True)
    target = quarantine / CREDENTIAL_FILE
    if target.exists():  # keep every copy; never clobber an earlier rescue
        stamp = int(credential.stat().st_mtime)
        target = quarantine / f"{CREDENTIAL_FILE}.{stamp}"
    shutil.move(str(credential), str(target))
    return target


def main() -> None:
    raw_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not raw_config_dir:
        raise SystemExit("CLAUDE_CONFIG_DIR must be set")
    config_dir = Path(raw_config_dir).expanduser()
    try:
        moved = quarantine_credential(config_dir)
        seed(config_dir)
        seed_attribution(config_dir)
    except (OSError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    if moved is not None:
        # Only speaks up when it actually acted, so this stays signal.
        print(
            f"delekit: quarantined a first-party login found in the gateway profile -> {moved}\n"
            "delekit: it would have reset Tandy's 272k window on every model switch. "
            "Do not run /login inside a gateway session; the gateway uses "
            "ANTHROPIC_AUTH_TOKEN, and CLIProxyAPI's own credential is renewed "
            "with the proxy binary's -claude-login."
        )


if __name__ == "__main__":
    main()

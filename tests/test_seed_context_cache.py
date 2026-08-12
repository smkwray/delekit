from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tools.seed_claude_context_cache import (
    CREDENTIAL_FILE,
    FAMILY,
    HOOK_EVENT,
    HOOK_MATCHER,
    HOOK_SCRIPT,
    SETTINGS_FILE,
    WINDOW,
    patch_document,
    quarantine_credential,
    seed,
    seed_attribution,
    seed_hooks,
)


class SeedContextCacheTest(unittest.TestCase):
    def test_patch_preserves_unrelated_state_and_updates_every_cache(self) -> None:
        original = {
            "theme": "dark",
            "clientDataCache": {"keep": 1},
            "clientDataCacheSlots": {
                "active": {"at": 123, "data": {"keep_slot": 2}},
                "malformed": "leave-me-alone",
            },
            "autoCompactWindowsCache": {"claude-sonnet-5": 967000},
        }
        patched = patch_document(original)

        self.assertEqual(original["clientDataCache"], {"keep": 1})
        self.assertEqual(patched["theme"], "dark")
        self.assertEqual(patched["clientDataCache"]["keep"], 1)
        self.assertEqual(patched["clientDataCache"]["kelp_forest_sonnet"], str(WINDOW))
        self.assertEqual(patched["clientDataCache"]["rowan_thicket"][FAMILY], WINDOW)
        self.assertEqual(patched["clientDataCacheSlots"]["active"]["at"], 123)
        slot_data = patched["clientDataCacheSlots"]["active"]["data"]
        self.assertEqual(slot_data["keep_slot"], 2)
        self.assertEqual(slot_data["kelp_forest_sonnet"], str(WINDOW))
        self.assertEqual(slot_data["rowan_thicket"][FAMILY], WINDOW)
        self.assertEqual(patched["clientDataCacheSlots"]["malformed"], "leave-me-alone")
        self.assertEqual(patched["autoCompactWindowsCache"]["claude-sonnet-5"], 967000)
        self.assertEqual(patched["autoCompactWindowsCache"][FAMILY], WINDOW)

    def test_seed_is_atomic_idempotent_and_preserves_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config_dir = Path(raw)
            state_path = config_dir / ".claude.json"
            state_path.write_text('{"theme":"light"}\n', encoding="utf-8")
            os.chmod(state_path, 0o640)

            self.assertTrue(seed(config_dir))
            first_mtime = state_path.stat().st_mtime_ns
            self.assertFalse(seed(config_dir))
            self.assertEqual(state_path.stat().st_mtime_ns, first_mtime)
            # Windows exposes only its read-only attribute through os.chmod,
            # so it cannot preserve POSIX group/other mode bits.
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o640)
            self.assertEqual(json.loads(state_path.read_text())["theme"], "light")

    def test_patch_strips_first_party_identity(self) -> None:
        # A saved login in this profile switches on Claude Code's first-party
        # bootstrap writer, which resets autoCompactWindowsCache on every model
        # switch. The profile must stay identity-free for the 272k seed to hold.
        patched = patch_document(
            {"oauthAccount": {"emailAddress": "x@y.z"}, "userID": "u1", "theme": "dark"}
        )
        self.assertNotIn("oauthAccount", patched)
        self.assertNotIn("userID", patched)
        self.assertEqual(patched["theme"], "dark")

    def test_quarantine_moves_credential_and_never_clobbers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config_dir = Path(raw)
            credential = config_dir / CREDENTIAL_FILE
            quarantine = config_dir / "quarantined-firstparty-auth"

            self.assertIsNone(quarantine_credential(config_dir))

            credential.write_text('{"claudeAiOauth":{"accessToken":"a"}}', encoding="utf-8")
            moved = quarantine_credential(config_dir)
            self.assertIsNotNone(moved)
            self.assertFalse(credential.exists())
            self.assertEqual(moved.parent, quarantine)
            self.assertIn("accessToken", moved.read_text(encoding="utf-8"))

            # A second login must not overwrite the first rescued copy.
            credential.write_text('{"claudeAiOauth":{"accessToken":"b"}}', encoding="utf-8")
            second = quarantine_credential(config_dir)
            self.assertNotEqual(second, moved)
            self.assertIn("a", moved.read_text(encoding="utf-8"))
            self.assertIn("b", second.read_text(encoding="utf-8"))

    def test_attribution_is_disabled_in_a_fresh_profile(self) -> None:
        # The installer creates this profile empty, so a trailer disabled in
        # ~/.claude/settings.json returns for every gateway session without this.
        with tempfile.TemporaryDirectory() as raw:
            config_dir = Path(raw)
            settings_path = config_dir / SETTINGS_FILE

            self.assertTrue(seed_attribution(config_dir))
            document = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(document["attribution"], {"commit": "", "pr": ""})
            # Idempotent: a second launch must not rewrite the file.
            first_mtime = settings_path.stat().st_mtime_ns
            self.assertFalse(seed_attribution(config_dir))
            self.assertEqual(settings_path.stat().st_mtime_ns, first_mtime)

    def test_attribution_preserves_other_settings_and_drops_the_deprecated_key(self) -> None:
        # includeCoAuthoredBy conflicts with attribution, so leaving both in
        # place makes the effective behaviour depend on precedence we do not own.
        with tempfile.TemporaryDirectory() as raw:
            config_dir = Path(raw)
            settings_path = config_dir / SETTINGS_FILE
            settings_path.write_text(
                json.dumps({"theme": "dark", "includeCoAuthoredBy": True}), encoding="utf-8"
            )

            self.assertTrue(seed_attribution(config_dir))
            document = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(document["theme"], "dark")
            self.assertNotIn("includeCoAuthoredBy", document)
            self.assertEqual(document["attribution"], {"commit": "", "pr": ""})

    def test_attribution_does_not_override_a_deliberate_choice(self) -> None:
        # Suppression is the default, not a policy: an explicit string stands.
        with tempfile.TemporaryDirectory() as raw:
            config_dir = Path(raw)
            settings_path = config_dir / SETTINGS_FILE
            settings_path.write_text(
                json.dumps({"attribution": {"commit": "Made by me"}}), encoding="utf-8"
            )

            seed_attribution(config_dir)
            document = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(document["attribution"]["commit"], "Made by me")
            self.assertEqual(document["attribution"]["pr"], "")

    def test_attribution_refuses_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            settings_path = Path(raw) / SETTINGS_FILE
            settings_path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Cannot read"):
                seed_attribution(Path(raw))
            self.assertEqual(settings_path.read_text(encoding="utf-8"), "not json")

    def _spawn_hooks(self, document: dict) -> list:
        entries = (document.get("hooks") or {}).get(HOOK_EVENT) or []
        return [entry for entry in entries
                if any(HOOK_SCRIPT in inner.get("command", "")
                       for inner in entry.get("hooks", []))]

    def test_spawn_hook_is_registered_exactly_once(self) -> None:
        # The hook only protects a session that registers it. It shipped
        # unwired for five days, during which one session sent 42 of 200
        # delegates to the wrong model, account, and checkout in silence.
        with tempfile.TemporaryDirectory() as raw:
            config_dir = Path(raw)
            settings_path = config_dir / SETTINGS_FILE

            self.assertTrue(seed_hooks(config_dir, Path("/kit")))
            document = json.loads(settings_path.read_text(encoding="utf-8"))
            ours = self._spawn_hooks(document)
            self.assertEqual(len(ours), 1)
            self.assertEqual(ours[0]["matcher"], HOOK_MATCHER)
            self.assertIn("/kit", ours[0]["hooks"][0]["command"])

            # Idempotent: a relaunch must not rewrite the file or duplicate.
            first_mtime = settings_path.stat().st_mtime_ns
            self.assertFalse(seed_hooks(config_dir, Path("/kit")))
            self.assertEqual(settings_path.stat().st_mtime_ns, first_mtime)

    def test_spawn_hook_preserves_other_hooks_and_follows_a_moved_kit(self) -> None:
        # Registering must not evict hooks the user configured, and a kit that
        # moves must be repointed rather than left stale beside a new entry.
        with tempfile.TemporaryDirectory() as raw:
            config_dir = Path(raw)
            settings_path = config_dir / SETTINGS_FILE
            settings_path.write_text(json.dumps({
                "theme": "dark",
                "hooks": {HOOK_EVENT: [
                    {"matcher": "Bash",
                     "hooks": [{"type": "command", "command": "audit.py"}]},
                ]},
            }), encoding="utf-8")

            seed_hooks(config_dir, Path("/kit"))
            self.assertTrue(seed_hooks(config_dir, Path("/moved")))
            document = json.loads(settings_path.read_text(encoding="utf-8"))

            self.assertEqual(document["theme"], "dark")
            entries = document["hooks"][HOOK_EVENT]
            self.assertTrue(any(entry["matcher"] == "Bash" for entry in entries))
            ours = self._spawn_hooks(document)
            self.assertEqual(len(ours), 1)
            self.assertIn("/moved", ours[0]["hooks"][0]["command"])

    def test_spawn_hook_migrates_duplicate_legacy_windows_paths(self) -> None:
        # Released Windows registrations used backslashes. Match both spellings
        # so an upgrade removes every stale copy before installing the new path.
        with tempfile.TemporaryDirectory() as raw:
            config_dir = Path(raw)
            settings_path = config_dir / SETTINGS_FILE
            unrelated = {"matcher": "Bash",
                         "hooks": [{"type": "command", "command": "audit.py"}]}
            legacy = [
                {"matcher": HOOK_MATCHER,
                 "hooks": [{"type": "command",
                            "command": r"python3 C:\old\kit\bin\hooks\verify-delegate-spawn.py"}]},
                {"matcher": HOOK_MATCHER,
                 "hooks": [{"type": "command",
                            "command": r"python3 D:\older\kit\bin\hooks\verify-delegate-spawn.py"}]},
            ]
            settings_path.write_text(json.dumps({
                "theme": "dark", "hooks": {HOOK_EVENT: [unrelated, *legacy]},
            }), encoding="utf-8")

            new_root = Path("/new/kit")
            self.assertTrue(seed_hooks(config_dir, new_root))
            document = json.loads(settings_path.read_text(encoding="utf-8"))
            entries = document["hooks"][HOOK_EVENT]
            self.assertEqual(len(entries), 2)
            self.assertIn(unrelated, entries)
            ours = self._spawn_hooks(document)
            self.assertEqual(len(ours), 1)
            command = ours[0]["hooks"][0]["command"]
            self.assertIn("/new/kit", command)
            self.assertNotIn("\\", command)

            first_mtime = settings_path.stat().st_mtime_ns
            self.assertFalse(seed_hooks(config_dir, new_root))
            self.assertEqual(settings_path.stat().st_mtime_ns, first_mtime)

    def test_seed_refuses_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / ".claude.json"
            state_path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Cannot read"):
                seed(Path(raw))
            self.assertEqual(state_path.read_text(encoding="utf-8"), "not json")


if __name__ == "__main__":
    unittest.main()

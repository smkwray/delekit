from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tools.seed_claude_context_cache import FAMILY, WINDOW, patch_document, seed


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

    def test_seed_refuses_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / ".claude.json"
            state_path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Cannot read"):
                seed(Path(raw))
            self.assertEqual(state_path.read_text(encoding="utf-8"), "not json")


if __name__ == "__main__":
    unittest.main()

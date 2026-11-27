"""Loading the add-on's settings: older ones are brought up to date."""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ADDON = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADDON))
HAVE_BUILD = (ADDON / "knightly_review" / "vendor" / "knightly").is_dir()
DEFAULTS = json.loads((ADDON / "knightly_review" / "config.json").read_text())


@unittest.skipUnless(HAVE_BUILD, "needs build.py --dev")
class LoadTest(unittest.TestCase):
    def load(self, stored: dict) -> dict:
        from knightly_review import settings

        # Anki's getConfig fills in the defaults for keys the stored config lacks.
        manager = SimpleNamespace(getConfig=lambda _name: {**DEFAULTS, **stored})
        with mock.patch.object(settings, "_manager", lambda: manager):
            return settings.load()

    def test_old_usernames_become_your_accounts(self):
        for version in ({}, {"config_version": 2}):  # a hand-made config may store no version
            cfg = self.load({"lichess_user": "a", "chesscom_user": "b", "stockfish_path": "x", **version})
            self.assertEqual(
                cfg["accounts"],
                [{"source": "lichess", "name": "a", "role": "me"}, {"source": "chesscom", "name": "b", "role": "me"}],
            )
            self.assertNotIn("lichess_user", cfg)
            self.assertEqual(cfg["config_version"], DEFAULTS["config_version"])

    def test_accounts_are_kept(self):
        accounts = [{"source": "lichess", "name": "x", "role": "other"}]
        cfg = self.load({"accounts": accounts, "lichess_user": "stale", "config_version": 3})
        self.assertEqual(cfg["accounts"], accounts)
        self.assertNotIn("lichess_user", cfg)

    def test_keep_running_becomes_keep_annotating(self):
        for mode, hours, keep in (("precalculate", 2.5, True), ("unlimited", 0, True), ("off", 3, False)):
            cfg = self.load({"keep_running": mode, "keep_running_hours": hours})
            self.assertNotIn("keep_running", cfg)
            self.assertNotIn("keep_running_hours", cfg)
            self.assertEqual(cfg["keep_annotating"], keep)
            if keep:
                self.assertEqual(cfg["keep_annotating_hours"], hours)
        self.assertFalse(self.load({})["keep_annotating"])

    def test_configured(self):
        from knightly_review import settings

        self.assertFalse(settings.configured({"accounts": [], "stockfish_path": "x"}))
        self.assertTrue(settings.configured({"pgn_paths": ["/g.pgn"], "stockfish_path": "x"}))
        self.assertTrue(settings.configured({"accounts": [{"source": "chesscom", "name": "b"}], "stockfish_path": "x"}))
        self.assertFalse(settings.configured({"accounts": [{"source": "chesscom", "name": "b"}]}))


if __name__ == "__main__":
    unittest.main()

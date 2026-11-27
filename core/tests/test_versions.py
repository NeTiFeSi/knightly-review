"""The release version lives in `knightly.__version__`; everything else must follow it."""

import json
import unittest
from pathlib import Path

import knightly

ROOT = Path(__file__).resolve().parents[2]


class VersionsAgree(unittest.TestCase):
    def test_card_package_json(self) -> None:
        pkg = json.loads((ROOT / "card" / "package.json").read_text())
        self.assertEqual(pkg["version"], knightly.__version__)

    def test_card_package_lock(self) -> None:
        lock = json.loads((ROOT / "card" / "package-lock.json").read_text())
        self.assertEqual(lock["version"], knightly.__version__)
        self.assertEqual(lock["packages"][""]["version"], knightly.__version__)


if __name__ == "__main__":
    unittest.main()

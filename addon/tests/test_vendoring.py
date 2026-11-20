"""Import rewriting for bundled packages, and that the bundle imports without a global `chess`."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from vendoring import rewrite_imports, vendor_tree  # noqa: E402


class RewriteTest(unittest.TestCase):
    def test_forms(self):
        src = textwrap.dedent("""\
            import chess
            import chess.pgn
            from chess import Board
            from chess.engine import Limit
            import chessboard
            import os
            from . import cache
            def f():
                import chess.svg  # lazy
            """)
        out = rewrite_imports(src, {"chess"}, depth=2)
        self.assertEqual(out.splitlines(), [
            "from ... import chess",
            "from ... import chess; from ...chess import pgn as _kr_pgn",
            "from ...chess import Board",
            "from ...chess.engine import Limit",
            "import chessboard",
            "import os",
            "from . import cache",
            "def f():",
            "    from ... import chess; from ...chess import svg as _kr_svg  # lazy",
        ])

    def test_bundle_imports_in_isolation(self):
        """A package named like an AnkiWeb add-on (digits) imports its own copy, never a global one."""
        d = pathlib.Path(tempfile.mkdtemp())
        pkg = d / "addon123"
        (pkg / "vendor" / "lib").mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "vendor" / "__init__.py").write_text("")
        src = d / "src" / "lib"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text("VALUE = 'ours'\n")
        (src / "sub.py").write_text("import lib\nGOT = lib.VALUE\n")
        (src / "data").mkdir()
        (src / "data" / "table.tsv").write_text("a\tb\n")
        (src / "notes.txt").write_text("not shipped\n")
        vendor_tree(src, pkg / "vendor" / "lib", {"lib"})
        self.assertTrue((pkg / "vendor" / "lib" / "data" / "table.tsv").is_file())
        self.assertFalse((pkg / "vendor" / "lib" / "notes.txt").exists())
        # A different global `lib` on sys.path must not be picked up.
        (d / "lib.py").write_text("VALUE = 'theirs'\n")
        code = "import addon123.vendor.lib.sub as s, sys; print(s.GOT, 'lib' in sys.modules)"
        out = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(d)!r}); {code}"],
                             capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out, ["ours", "False"])


if __name__ == "__main__":
    unittest.main()

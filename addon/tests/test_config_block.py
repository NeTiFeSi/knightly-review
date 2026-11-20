import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "knightly_review"))
from config_block import format_block, rewrite, validate  # noqa: E402

LOADER = pathlib.Path(__file__).resolve().parents[2] / "card" / "templates" / "loader.html"

DEFAULT = {
    "acceptNags": [1, 3, 5, 8],
    "strictMate": False,
    "autoReplyDelay": 400,
    "showEval": True,
    "evalOnPuzzleFront": False,
    "intro": "lastMove",
    "introMoveDelay": 900,
    "afterAnswer": "flip",
    "flipDelay": 1200,
    "gradeDelay": 2500,
    "gradeSolved": 3,
    "gradeHinted": 2,
    "gradeFailed": 1,
    "hints": True,
    "live": False,
    "goodMove": 2,
    "liveMaxMoves": 10,
}


class ConfigBlockTest(unittest.TestCase):
    def test_validate(self):
        self.assertIsNone(validate(DEFAULT))
        self.assertIn("intro", validate({**DEFAULT, "intro": "fullgame"}))
        self.assertIn("hints", validate({**DEFAULT, "hints": 1}))
        self.assertIn("gradeSolved", validate({**DEFAULT, "gradeSolved": 5}))
        self.assertIn("gradeSolved", validate({**DEFAULT, "gradeSolved": True}))
        self.assertIn("unknown", validate({**DEFAULT, "intr": "x"}))
        self.assertIn("missing", validate({k: v for k, v in DEFAULT.items() if k != "hints"}))
        self.assertIn("acceptNags", validate({**DEFAULT, "acceptNags": ["!"]}))

    def test_rewrite_template(self):
        tpl = LOADER.read_text()
        cfg = {**DEFAULT, "intro": "fullGame", "acceptNags": [1, 3]}
        out, n = rewrite(tpl, cfg)
        self.assertEqual(n, 1)
        self.assertIn("intro: 'fullGame'", out)
        self.assertIn("acceptNags: [1, 3]", out)
        self.assertIn("window.KR_CONFIG = null;", out)  # the reset line is left alone
        # The shipped template's block is exactly the formatted defaults.
        self.assertIn(format_block(DEFAULT), tpl)
        again, n2 = rewrite(out, cfg)
        self.assertEqual((again, n2), (out, 1))

    def test_floats_and_ints(self):
        self.assertIn("flipDelay: 1200,", format_block({**DEFAULT, "flipDelay": 1200.0}))


if __name__ == "__main__":
    unittest.main()

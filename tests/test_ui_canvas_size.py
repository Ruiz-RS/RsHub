"""A canvas backing store must be set with a rounded size: assigning width = css * dpr truncates (1455 * 1.1 = 1600.5 -> 1600),
and code that compares against Math.round() then sees a mismatch on every frame and reallocates the canvas each time."""
import re
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "rshub" / "ui" / "js"
FILES = ["shell.js", "component-sheet.js", "widget.js", "modules/tester.js"]


class CanvasSize(unittest.TestCase):
    def test_backing_store_is_rounded(self):
        bad = re.compile(r"\.(width|height) = (?!Math\.round)[\w.]+ \* (d|DPR)\b")
        for f in FILES:
            for n, line in enumerate((UI / f).read_text(encoding="utf-8").splitlines(), 1):
                if "Math.round" in line and bad.search(line.replace("Math.round(", "R(")) is None:
                    continue
                self.assertIsNone(bad.search(line), f"{f}:{n} sets a canvas size without rounding")


if __name__ == "__main__":
    unittest.main()

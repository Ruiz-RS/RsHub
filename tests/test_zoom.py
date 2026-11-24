"""Interface size: the Automatic rule (ui/js/zoom.js, run in Node) never gives a small or high-scaling screen less CSS
room than the layouts need when a smaller zoom can, and the main window's minimum size always fits the screen."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from rshub import winmgr

ZOOM_JS = Path(__file__).resolve().parent.parent / "rshub" / "ui" / "js" / "zoom.js"
TASKBAR = 48  # CSS px at 100 %


def window(w, h, scale):
    """The maximized window at 100 % (CSS px) on a w x h screen at that Windows scale, minus the taskbar."""
    return round(w / scale), round(h / scale) - TASKBAR


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class AutoZoom(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.z = cls.run_js("""
            const out = { auto: {}, capped: {}, compact: {} };
            for (const [w, h] of CASES) out.auto[w + 'x' + h] = Z.auto(w, h);
            for (const [want, w, h] of CAPS) out.capped[want + '@' + w + 'x' + h] = Z.capped(want, w, h);
            out.compact = [Z.compact(1280, 672), Z.compact(1100, 700), Z.compact(1099, 900), Z.compact(1920, 1080)];
            /* every window from 640 x 400 to 2600 x 1600 */
            const bad = [];
            for (let w = 640; w <= 2600; w += 8) for (let h = 400; h <= 1600; h += 8) {
              const z = Z.auto(w, h), vw = w / z, vh = h / z;
              if (Z.AUTO.indexOf(z) < 0) bad.push([w, h, z, 'not a step']);
              if ((w < 1100 || h < 700) && z > 1) bad.push([w, h, z, 'grew a small window']);
              if (vw + .5 < Math.min(w, 1100) || vh + .5 < Math.min(h, 700)) bad.push([w, h, z, 'viewport smaller than needed']);
              const fit = Z.AUTO.filter(s => s <= 1 && w / s >= 1099.5 && h / s >= 699.5);
              if (fit.length && (vw < 1099.5 || vh < 699.5)) bad.push([w, h, z, 'compact although a step fits']);
              if (fit.length && z < 1 && z !== Math.max(...fit)) bad.push([w, h, z, 'smaller than needed']);
            }
            out.bad = bad.slice(0, 10);
            console.log(JSON.stringify(out));""")

    CASES = [window(1920, 1080, 1.5), window(1280, 800, 1), window(1920, 1200, 1.5), window(1920, 1200, 1.75),
             window(2560, 1600, 2), window(1920, 1080, 1.25), window(1920, 1080, 1), window(2560, 1440, 1),
             window(1280, 720, 1.5), window(1366, 768, 1), (1280, 720), (1024, 576)]
    CAPS = [(1.25, 1100, 700), (1.25, 1920, 1032), (1, 1280, 672), (0.9, 1920, 1032), (1.4, 1280, 752)]

    @classmethod
    def run_js(cls, body):
        code = (f"const Z = require({json.dumps(str(ZOOM_JS))});\nconst CASES = {json.dumps(cls.CASES)};\n"
                f"const CAPS = {json.dumps(cls.CAPS)};\n{body}")
        r = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise AssertionError(r.stderr)
        return json.loads(r.stdout)

    def auto(self, w, h, scale):
        return self.z["auto"]["%dx%d" % window(w, h, scale)]

    def test_handhelds_and_laptops(self):
        self.assertEqual(self.auto(1920, 1080, 1.5), 0.9)   # ROG Ally: 1280 x 672 -> 1422 x 747
        self.assertEqual(self.auto(1280, 800, 1), 1)        # Steam Deck: 1280 x 752
        self.assertEqual(self.auto(1920, 1200, 1.5), 1)     # 1280 x 752
        self.assertEqual(self.auto(1920, 1200, 1.75), 0.9)  # 1097 x 638 -> 1219 x 709
        self.assertEqual(self.auto(2560, 1600, 2), 1)       # Legion Go: 1280 x 752
        self.assertEqual(self.auto(1920, 1080, 1.25), 1)    # 1536 x 816: no longer shrunk to 92 %
        self.assertEqual(self.auto(1366, 768, 1), 1)
        self.assertEqual(self.auto(1280, 720, 1.5), 0.8)    # 853 x 432: the smallest step, compact layout
        self.assertEqual(self.z["auto"]["1024x576"], 0.8)   # -> 1280 x 720

    def test_big_screens_still_grow(self):
        self.assertEqual(self.auto(1920, 1080, 1), 1.25)
        self.assertEqual(self.auto(2560, 1440, 1), 1.25)

    def test_every_window(self):
        self.assertEqual(self.z["bad"], [])

    def test_a_picked_size_is_capped_in_a_small_window(self):
        c = self.z["capped"]
        self.assertLess(c["1.25@1100x700"], 1.25)
        self.assertGreaterEqual(1100 / c["1.25@1100x700"], 1000)
        self.assertEqual(c["1.25@1920x1032"], 1.25)
        self.assertEqual(c["1@1280x672"], 1)
        self.assertEqual(c["0.9@1920x1032"], 0.9)   # a smaller size the user picked is never raised
        self.assertEqual(c["1.4@1280x752"], 1.15)

    def test_compact_threshold(self):
        self.assertEqual(self.z["compact"], [True, False, True, False])


class MainWindowSize(unittest.TestCase):
    def test_minimum_fits_a_handheld(self):
        # ROG Ally: 1920 x 1008 device px of work area at 150 % = 1280 x 672 logical
        size, mn = winmgr.main_window_sizes((1920, 1008), 1.5)
        self.assertLessEqual(mn[0] * 1.5, 1920)
        self.assertLessEqual(mn[1] * 1.5, 1008)
        self.assertLessEqual(size[1] * 1.5, 1008)

    def test_tiny_and_normal_screens(self):
        size, mn = winmgr.main_window_sizes((1280, 672), 1.5)   # 1280 x 720 at 150 %
        self.assertLessEqual(mn[0], 853)
        self.assertLessEqual(mn[1], 448)
        self.assertEqual(winmgr.main_window_sizes((2560, 1392), 1), (winmgr.MAIN_SIZE, winmgr.MAIN_MIN))
        self.assertEqual(winmgr.main_window_sizes(None, None)[1][0] <= 1024, True)

    def test_bad_input_falls_back(self):
        self.assertEqual(winmgr.main_window_sizes((0, 0), 1.5), (winmgr.MAIN_SIZE, winmgr.MAIN_MIN))


if __name__ == "__main__":
    unittest.main()

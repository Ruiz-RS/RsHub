/* RsHub · Interface size (Settings > Appearance > Interface size): which zoom the window gets. Pure rules, no
   DOM: shell.js measures the window and applies the result (Python's set_zoom, the real WebView2 zoom), and
   tests/test_zoom.py runs these same functions in Node.

   Sizes are the window's CSS pixels at 100 %, after Windows' own scaling (a 1920 x 1080 screen at 150 % is a
   1280 x 720 window, 1280 x 672 without the taskbar). The CSS viewport is that size divided by the zoom.

   Automatic, in order:
   1. Room to spare: the largest zoom from 100 % up (125 %, 115 %, 110 %) that still leaves the comfortable size
      (COMFORT, 1440 x 820), so big screens get bigger text.
   2. Otherwise 100 % when the layouts have the room they are designed for (FIT, 1100 x 700).
   3. Otherwise (small or high-scaling screens: a handheld's 7" 1080p screen at 150 %) the largest step below 100 %
      (90 %, 80 %) at which they have it, so Automatic never makes the CSS viewport smaller than it has to be: it
      never picks more than 100 % on a window smaller than FIT.
   4. Never below 80 %: in an even smaller window the interface keeps 80 % and switches to its compact layout
      (shell.js, html.compact: the page scrolls instead of every panel fitting the window).

   A size the user picked is kept unless it would leave fewer than MIN CSS pixels (125 % in a 1100 x 700 window
   would leave 880 x 560); then the largest smaller step that leaves them is used while the window is that small. */
(function (root) {
  'use strict';
  const STEPS = [1.4, 1.25, 1.15, 1.1, 1, 0.9, 0.8];
  const AUTO = [1.25, 1.15, 1.1, 1, 0.9, 0.8];
  const COMFORT = [1440, 820], FIT = [1100, 700], MIN = [1000, 640];
  const fits = (w, h, z, [W, H]) => w / z >= W - 0.5 && h / z >= H - 0.5;

  function auto(w, h) {
    for (const z of AUTO) if (z > 1 && fits(w, h, z, COMFORT)) return z;
    if (fits(w, h, 1, FIT)) return 1;
    for (const z of AUTO) if (z < 1 && fits(w, h, z, FIT)) return z;
    return AUTO[AUTO.length - 1];
  }
  /* the zoom used for a size the user picked (want): itself, or less while the window is too small for it */
  function capped(want, w, h) {
    const z = STEPS.find(x => x <= want && fits(w, h, x, MIN)) || STEPS[STEPS.length - 1];
    return Math.min(z, want);
  }
  /* the compact layout: the CSS viewport is smaller than the layouts are designed for */
  const compact = (vw, vh) => vw < FIT[0] || vh < FIT[1];

  const api = { auto, capped, compact, STEPS, AUTO, COMFORT, FIT, MIN };
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.SH) root.SH.zoomRule = api;
})(typeof window === 'object' ? window : null);

"""The start screen across processes (splash.py): never two cards on screen at once. The copy that gives way hands
its card over (its title carries the animation clock, place, scale, light and status), the next process takes it over
on the same clock, and a copy launched while another one is starting shows none of its own. Nothing here opens a
window: the Win32 side is replaced by fakes."""
import sys
import unittest
from unittest import mock

from rshub import elevation, splash, startup
from rshub.engine import selfupdate


class Title(unittest.TestCase):
    def test_round_trip(self):
        t = splash.handoff_title(1795360225.5022, 0.1748, (600, -20), 1.25, "Opening as administrator…")
        self.assertTrue(t.startswith("RsHub"))  # still a RsHub window for anything that looks by title
        info = splash.parse_handoff(t)
        self.assertAlmostEqual(info["e"], 1795360225.5022, places=3)
        self.assertAlmostEqual(info["l"], 0.1748, places=3)
        self.assertEqual((info["x"], info["y"]), (600, -20))
        self.assertAlmostEqual(info["s"], 1.25)
        self.assertIsNone(info["f"])
        self.assertEqual(info["status"], "Opening as administrator…")

    def test_no_light_yet_and_a_frozen_frame(self):
        info = splash.parse_handoff(splash.handoff_title(1e9, None, (0, 0), 1.0, "", frozen=2.25))
        self.assertIsNone(info["l"])
        self.assertAlmostEqual(info["f"], 2.25)
        self.assertEqual(info["status"], "")

    def test_a_status_with_a_bar_is_kept_whole(self):
        info = splash.parse_handoff(splash.handoff_title(1e9, 1.0, (0, 0), 1.0, "a | b"))
        self.assertEqual(info["status"], "a | b")

    def test_anything_else_is_not_a_handoff(self):
        for t in ("RsHub", "", None, "RsHub handoff v1 e=x l=1 x=0 y=0 s=1|",
                  "RsHub handoff v1 l=1 x=0 y=0 s=1|", "RsHub handoff v1 e=1e9 l=1 x=0 y=0 s=99|",
                  "RsHub handoff v2 e=1e9 l=1 x=0 y=0 s=1|"):
            self.assertIsNone(splash.parse_handoff(t), t)


class OneCardAtATime(unittest.TestCase):
    def cards(self, *titles):
        return mock.patch.object(splash, "_cards", return_value=[(100 + i, t, v) for i, (t, v) in enumerate(titles)])

    def test_a_starting_copy_is_not_a_handing_one(self):
        with self.cards(("RsHub", True)):
            self.assertTrue(splash.other_starting())
            self.assertTrue(splash.other_showing())
        with self.cards((splash.handoff_title(1e9, None, (0, 0), 1.0, ""), True)):
            self.assertFalse(splash.other_starting())
            self.assertTrue(splash.other_showing())
        with self.cards(("RsHub", False)):  # hidden (handed over): not on screen
            self.assertFalse(splash.other_starting())
            self.assertFalse(splash.other_showing())

    def test_only_a_visible_handing_card_of_another_process_is_taken_over(self):
        title = splash.handoff_title(1e9, None, (0, 0), 1.0, "")
        with self.cards(("RsHub", True), (title, False)):
            self.assertIsNone(splash._handing_card())
        with self.cards(("RsHub", True), (title, True)), \
                mock.patch.object(splash.u32, "GetWindowThreadProcessId", side_effect=lambda h, p: setattr(p._obj, "value", 1)):
            h, info = splash._handing_card()
            self.assertEqual(h, 101)

    def test_a_second_copy_waits_without_its_own_start_screen(self):
        launch = startup.Launch(["RsHub.exe"])
        launch.splash = True
        with mock.patch.object(splash, "other_starting", return_value=True), \
                mock.patch.object(splash, "show") as show:
            startup.show_splash(launch)
        show.assert_not_called()
        self.assertTrue(launch.splash and launch.splash_later)
        # it turns out this copy does start (the other one went away): its start screen comes now
        with mock.patch.object(startup.instance, "wait_for_previous"), \
                mock.patch.object(startup.instance, "mutex_exists", return_value=False), \
                mock.patch.object(startup.elevation, "elevate_on_start", return_value=False), \
                mock.patch.object(startup.instance, "claim_single_instance", return_value=True), \
                mock.patch.object(splash, "show") as show:
            self.assertTrue(startup.acquire(launch))
        show.assert_called_once()

    def test_a_copy_alone_shows_its_start_screen_at_once(self):
        launch = startup.Launch(["RsHub.exe"])
        launch.splash = True
        with mock.patch.object(splash, "other_starting", return_value=False), \
                mock.patch.object(splash, "show") as show, mock.patch.object(splash, "status"):
            startup.show_splash(launch)
        show.assert_called_once()
        self.assertFalse(launch.splash_later)


class Relaunches(unittest.TestCase):
    """Every path where one process gives way to another hands its card over before starting the next one."""

    def test_administrator_at_start_hands_over_before_the_task_runs(self):
        order = []
        sp = mock.Mock(showing=lambda: True, handed=lambda: True,
                       hand_over=mock.Mock(side_effect=lambda on=True: order.append(("hand_over", on))))
        run = mock.Mock(side_effect=lambda *a, **k: (order.append(("schtasks",)), mock.Mock(returncode=0))[1])
        with mock.patch.object(elevation, "wants_elevation", return_value=True), \
                mock.patch.object(elevation, "_hand_over_splash", side_effect=lambda: (sp.hand_over(), sp)[1]), \
                mock.patch.object(elevation, "task_opens_this_copy", return_value=True), \
                mock.patch.object(elevation, "uses_admin_task", return_value=True), \
                mock.patch.object(elevation.config, "PORTABLE", False), \
                mock.patch("subprocess.run", run), \
                mock.patch.object(elevation, "elevated_copy_started", return_value=True):
            self.assertTrue(elevation.elevate_on_start())
        self.assertEqual(order[0], ("hand_over", True))
        self.assertEqual(order[1], ("schtasks",))

    def test_a_slow_elevated_copy_is_waited_for_not_ended(self):
        """At a cold logon the copy the task started can take more than 8 s to reach the lock: it was ended and the
        Windows prompt came up. If it did start, it gets up to a minute."""
        waits = []
        with mock.patch.object(elevation, "wants_elevation", return_value=True), \
                mock.patch.object(elevation, "_hand_over_splash", return_value=None), \
                mock.patch.object(elevation, "task_opens_this_copy", return_value=True), \
                mock.patch.object(elevation, "uses_admin_task", return_value=True), \
                mock.patch.object(elevation.config, "PORTABLE", False), \
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(elevation, "_copy_started_since", return_value=True), \
                mock.patch.object(elevation, "elevated_copy_started",
                                  side_effect=lambda seconds=8: (waits.append(seconds), seconds > 8)[1]), \
                mock.patch("rshub.schtask.end") as end, \
                mock.patch.object(elevation.ctypes.windll.shell32, "ShellExecuteW") as uac:
            self.assertTrue(elevation.elevate_on_start())
        self.assertEqual(waits, [8, 52])
        end.assert_not_called()
        uac.assert_not_called()

    def test_declined_administrator_takes_the_card_back(self):
        sp = mock.Mock(showing=lambda: True, handed=lambda: False)
        with mock.patch.object(elevation, "wants_elevation", return_value=True), \
                mock.patch.object(elevation, "_hand_over_splash", return_value=sp), \
                mock.patch.object(elevation, "task_opens_this_copy", return_value=False), \
                mock.patch.object(elevation.ctypes.windll.shell32, "ShellExecuteW", return_value=5):  # declined
            self.assertFalse(elevation.elevate_on_start())
        sp.hand_over.assert_called_with(False)

    def test_the_exiting_copy_waits_until_its_card_is_taken_over(self):
        sp = mock.Mock(showing=lambda: True)
        sp.handed.side_effect = [False, False, True]
        with mock.patch.object(elevation.time, "sleep"):
            elevation._until_taken_over(sp)
        self.assertEqual(sp.handed.call_count, 3)

    def test_administrator_from_the_window_hands_over_before_asking_windows(self):
        from rshub.api import Api
        api = Api()
        order = []
        with mock.patch.object(splash, "show"), mock.patch.object(splash, "status"), \
                mock.patch.object(splash, "hand_over", side_effect=lambda on=True: order.append(("hand_over", on))), \
                mock.patch.object(splash, "close", side_effect=lambda: order.append(("close",))), \
                mock.patch.object(elevation, "relaunch_elevated", side_effect=lambda *a: (order.append(("uac",)), False)[1]), \
                mock.patch("rshub.api.main_window.config.is_admin", return_value=False):
            self.assertFalse(api.relaunch_admin())
        self.assertEqual(order, [("hand_over", True), ("uac",), ("hand_over", False), ("close",)])

    def test_a_staged_update_hands_the_card_to_the_updater(self):
        sp = mock.Mock(showing=lambda: True, handed=lambda: True)
        with mock.patch.object(selfupdate, "staged", return_value="9.0.0"), \
                mock.patch.object(selfupdate, "start", return_value={"ok": True, "at": 1}), \
                mock.patch.object(selfupdate, "_hand_over_splash", return_value=sp), \
                mock.patch.object(selfupdate, "_wait_staged", return_value=True):
            self.assertTrue(selfupdate.apply_staged_at_start(say=lambda t: None))
        sp.hand_over.assert_not_called()  # handed over: nothing to take back
        sp2 = mock.Mock(showing=lambda: True, handed=lambda: False)
        with mock.patch.object(selfupdate, "staged", return_value="9.0.0"), \
                mock.patch.object(selfupdate, "start", return_value={"ok": True, "at": 1}), \
                mock.patch.object(selfupdate, "_hand_over_splash", return_value=sp2), \
                mock.patch.object(selfupdate, "_wait_staged", return_value=False):
            self.assertFalse(selfupdate.apply_staged_at_start(say=lambda t: None))
        sp2.hand_over.assert_called_with(False)  # the update did not go ahead: the card is this process's again


class CardCache(unittest.TestCase):
    """The card's static scene and the ring's maps took 0.9 s (100 %) to over 3 s (150 %, a handheld) to draw before
    the start screen could appear, at every start; they are now kept once per version and scale."""

    SCALE = 0.75  # small: quick to draw

    def setUp(self):
        import os
        import tempfile
        self.dir = tempfile.mkdtemp()
        p = mock.patch.object(splash._CardCache, "path", staticmethod(lambda s: os.path.join(self.dir, "card.bin")))
        p.start()
        self.addCleanup(p.stop)
        self.file = os.path.join(self.dir, "card.bin")

    def drawn(self):
        card = splash._Card(self.SCALE)
        self.assertTrue(hasattr(card, "_drawn_whole"))
        card._ring_maps()  # (it saves the card)
        return card

    @staticmethod
    def same(a, b):
        from PIL import ImageChops
        return a.size == b.size and a.mode == b.mode and ImageChops.difference(a, b).getbbox() is None

    def test_the_next_card_is_read_back_pixel_for_pixel(self):
        a = self.drawn()
        b = splash._Card(self.SCALE)
        self.assertFalse(hasattr(b, "_drawn_whole"))  # read, not drawn
        self.assertTrue(hasattr(b, "_star"))          # the ring's light from the first frame
        self.assertTrue(self.same(a.base, b.base))
        for m in splash._CardCache.MAPS + ("_star",):
            self.assertTrue(self.same(getattr(a, m), getattr(b, m)), m)
        a._light_t0 = b._light_t0 = 0.0
        for t, st in ((0.05, ""), (1.3, "Opening as administrator…"), (2.9, "")):
            self.assertTrue(self.same(a.frame(t, st), b.frame(t, st)), t)

    def test_another_version_or_scale_draws_it_again(self):
        self.drawn()
        with mock.patch.object(splash._CardCache, "key", staticmethod(lambda s: "another version")):
            card = splash._Card(self.SCALE)
        self.assertTrue(hasattr(card, "_drawn_whole"))

    def test_a_damaged_or_foreign_file_is_never_trusted(self):
        self.drawn()
        with open(self.file, "rb") as fh:
            good = fh.read()
        magic = splash._CardCache.MAGIC
        hlen = int.from_bytes(good[len(magic):len(magic) + 4], "little")
        head = good[len(magic) + 4:len(magic) + 4 + hlen]
        import json
        h = json.loads(head)

        def with_head(new):
            b = json.dumps(new).encode()
            return magic + len(b).to_bytes(4, "little") + b + good[len(magic) + 4 + hlen:]
        bigger = dict(h, items=[[n, m, w * 2 if n == "base" else w, ht, sz] for n, m, w, ht, sz in h["items"]])
        lying = dict(h, items=[[n, m, w, ht, sz * 50] for n, m, w, ht, sz in h["items"]])
        moded = dict(h, items=[[n, "P" if n == "base" else m, w, ht, sz] for n, m, w, ht, sz in h["items"]])
        for bad in (b"", good[:len(good) // 2], b"junk" + good, good[:len(magic)] + bytes([255, 255, 255, 127]),
                    with_head(bigger), with_head(lying), with_head(moded), with_head([1, 2])):
            with open(self.file, "wb") as fh:
                fh.write(bad)
            with mock.patch("builtins.print"):
                card = splash._Card(self.SCALE)
            self.assertTrue(hasattr(card, "_drawn_whole"), bad[:40])

    def test_a_card_whose_logo_failed_is_not_kept(self):
        import os
        with mock.patch("rshub.tray.make_icon_image", side_effect=RuntimeError("no")), mock.patch("builtins.print"):
            card = splash._Card(self.SCALE)
            card._ring_maps()
        self.assertFalse(os.path.exists(self.file))


class ShowAgain(unittest.TestCase):
    def test_a_closed_start_screen_can_be_shown_again(self):
        """The administrator relaunch from the window shows the start screen a second time in a process: it used to
        be a no-op (the first card's finished thread was still recorded), so nothing covered the handover."""
        started = []

        class FakeThread:
            def __init__(self, target, args, **kw):
                self.args = args
                self.alive = False

            def start(self):
                started.append(self)
                self.alive = True
                self.args[0].set()

            def is_alive(self):
                return self.alive

            def join(self, t=None):
                self.alive = False
        old = dict(splash._state)
        try:
            splash._state.update(thread=None, closing=None)
            with mock.patch.object(splash.threading, "Thread", FakeThread):
                splash.show()
                splash.show()             # already up: nothing new
                self.assertEqual(len(started), 1)
                started[0].alive = False  # it closed
                splash.show()
                self.assertEqual(len(started), 2)
                self.assertIsNone(splash._state["closing"])
        finally:
            splash._state.clear()
            splash._state.update(old)


if __name__ == "__main__":
    unittest.main()

"""Start, single instance and log: the pieces that must never leave the user with nothing."""
import io
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ["RSHUB_NO_DIALOGS"] = "1"
os.environ["RSHUB_NO_SPLASH"] = "1"

from rshub import instance, log, paths  # noqa: E402


class LogOnce(unittest.TestCase):
    def test_repeated_lines_are_written_once_and_counted(self):
        fh = io.StringIO()
        out_log = log.LogOnce(fh)
        for _ in range(50):
            out_log.write("[telemetry] boom\n")
        out_log.write("something else\n")
        out = fh.getvalue()
        self.assertEqual(out.count("[telemetry] boom"), 1)
        self.assertIn("49 more times", out)
        self.assertIn("something else", out)

    def test_many_threads_printing_lose_and_repeat_nothing(self):
        buf = io.StringIO()
        lo = log.LogOnce(buf)
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)  # switch threads as often as possible: the unlocked version duplicated lines
        self.addCleanup(sys.setswitchinterval, old)

        def w(i):
            for k in range(1500):
                lo.write(f"thread {i} line {k}\n")
        ts = [threading.Thread(target=w, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 9000)
        self.assertEqual(len(set(lines)), 9000)

    def test_rotate_keeps_one_old_copy(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "rshub.log")
        with open(p, "wb") as fh:
            fh.write(b"x" * 3000)
        log.rotate_log(p, limit=2000)
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.exists(p + ".1"))
        with open(p, "wb") as fh:
            fh.write(b"y")
        log.rotate_log(p, limit=2000)  # small: left alone
        self.assertTrue(os.path.exists(p))


class Dialogs(unittest.TestCase):
    def test_tests_never_get_a_modal_box(self):
        self.assertFalse(paths.dialogs_allowed())
        with mock.patch.object(log.ctypes.windll.user32, "MessageBoxW") as box:
            log.fatal("test message")
        box.assert_not_called()


class SingleInstance(unittest.TestCase):
    def setUp(self):  # never the real note in the user's local data folder
        p = mock.patch.object(instance, "_port_file", return_value=os.path.join(tempfile.mkdtemp(), "instance.port"))
        p.start()
        self.addCleanup(p.stop)

    def serve(self, reply, hold=0.0):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)
        self.addCleanup(srv.close)

        def loop():
            try:
                conn, _ = srv.accept()
                with conn:
                    conn.recv(8)
                    time.sleep(hold)
                    if reply:
                        conn.sendall(reply)
            except OSError:
                pass
        threading.Thread(target=loop, daemon=True).start()
        return srv.getsockname()[1]

    def test_show_needs_an_answer(self):
        with mock.patch.object(instance, "SINGLE_PORT", self.serve(b"ok")):
            self.assertTrue(instance.ask_running_to_show(1.0))

    def test_a_deaf_copy_is_not_taken_for_an_answer(self):
        with mock.patch.object(instance, "SINGLE_PORT", self.serve(b"", hold=1.5)):
            t = time.time()
            self.assertFalse(instance.ask_running_to_show(0.4))
            self.assertLess(time.time() - t, 1.2)

    def test_nobody_listening_is_quick_and_false(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with mock.patch.object(instance, "SINGLE_PORT", port):
            self.assertFalse(instance.ask_running_to_show(0.4))

    def test_the_listener_survives_a_silent_client(self):
        class FakeApi:
            shown = 0

            def show_main(self):
                FakeApi.shown += 1

            def quit(self, force=False):
                pass
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with mock.patch.object(instance, "SINGLE_PORT", port):
            instance.listen_single(FakeApi())
            silent = socket.create_connection(("127.0.0.1", port), timeout=1)  # connects and says nothing
            self.addCleanup(silent.close)
            t = time.time()
            self.assertTrue(instance.ask_running_to_show(5))  # a second client still gets served
            self.assertLess(time.time() - t, 4)
        self.assertEqual(FakeApi.shown, 1)

    def test_a_quitting_copy_does_not_answer_show(self):
        """Quit, then open RsHub again within a second: the copy that is closing must not answer "ok" (the new
        one would exit thinking it was shown, and nothing would open); it says nothing and the new one waits."""
        class Quitting:
            _quitting = True
            shown = 0

            def show_main(self):
                Quitting.shown += 1
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with mock.patch.object(instance, "SINGLE_PORT", port):
            instance.listen_single(Quitting())
            self.assertFalse(instance.ask_running_to_show(0.6))
        self.assertEqual(Quitting.shown, 0)

    def test_a_copy_that_just_started_is_given_a_minute(self):
        young = mock.Mock(create_time=lambda: time.time() - 5)
        old = mock.Mock(create_time=lambda: time.time() - 600)
        self.assertLess(instance._age(young), 10)
        self.assertGreater(instance._age(old), 500)
        self.assertEqual(instance._age(mock.Mock(create_time=mock.Mock(side_effect=OSError))), 60.0)

    def test_another_users_copy_is_never_taken_for_this_sessions(self):
        """Only this session's RsHub can hold this session's lock: another user's (fast user switching, Remote
        Desktop) must not be waited for, nor ended as stuck. A process whose session Windows won't say is kept."""
        import types
        run_py = os.path.join(instance.config.base_dir(), "run.py")

        def proc(pid):
            return types.SimpleNamespace(info={"pid": pid, "name": "python.exe", "exe": sys.executable,
                                               "cmdline": [sys.executable, run_py]})
        procs = [proc(101), proc(202), proc(303)]
        sessions = {None: 2, 101: 2, 202: 3, 303: 0}
        with mock.patch.object(instance, "session_id", side_effect=lambda pid=None: sessions[pid]), \
                mock.patch("psutil.process_iter", return_value=procs), mock.patch.object(instance.config, "FROZEN", False):
            self.assertEqual([p.info["pid"] for p in instance.other_copies()], [101, 303])


    def test_a_taken_port_moves_the_listener_and_the_next_copy_finds_it(self):
        """Another program held the single-instance port: the running copy listened nowhere, so every later double
        click waited, took the healthy copy for stuck and ended it. Now it listens on a free port, writes it down,
        and the next copy (and the installer's --quit) try that one too."""
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        api = mock.Mock(_quitting=False)
        with mock.patch.object(instance, "SINGLE_PORT", blocker.getsockname()[1]):
            srv = instance.listen_single(api)
            self.assertIsNotNone(srv)
            self.addCleanup(srv.close)
            self.assertNotEqual(srv.getsockname()[1], instance.SINGLE_PORT)
            self.assertEqual(instance._ports(), [instance.SINGLE_PORT, srv.getsockname()[1]])
            self.assertTrue(instance.ask_running_to_show(1.0))
        api.show_main.assert_called_once()

    def test_a_note_that_is_junk_or_the_usual_port_adds_nothing(self):
        port = 47999
        with mock.patch.object(instance, "SINGLE_PORT", port):
            self.assertEqual(instance._ports(), [port])
            for junk in ("junk", "", "80", str(port), "70000"):
                with open(instance._port_file(), "w", encoding="ascii") as fh:
                    fh.write(junk)
                self.assertEqual(instance._ports(), [port], junk)

    def test_listening_on_the_usual_port_drops_an_old_note(self):
        with open(instance._port_file(), "w", encoding="ascii") as fh:
            fh.write("51234")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with mock.patch.object(instance, "SINGLE_PORT", port):
            srv = instance.listen_single(mock.Mock(_quitting=False))
            self.addCleanup(srv.close)
        self.assertFalse(os.path.exists(instance._port_file()))


class EarlyExit(unittest.TestCase):
    """A copy that has nothing more to do (the running copy showed itself, the elevated copy or the installer took
    over) ends its process at once: an interpreter exit while the prefetch thread is loading .NET crashed in
    pythonnet's shutdown (0xE0434352, a Windows Error Reporting crash at every second double click)."""

    def test_handed_over_copies_end_at_once(self):
        from rshub import app
        for step in ("acquire", "start_background_work", "webview2_present"):
            with mock.patch.object(app.startup, "run_command", return_value=False),                     mock.patch.object(app.startup, "prepare"), mock.patch.object(app.startup, "start_prefetch"),                     mock.patch.object(app.startup, "show_splash"), mock.patch.object(app.startup, "no_webview2"),                     mock.patch.object(app.startup, "acquire", return_value=step != "acquire"),                     mock.patch.object(app.startup, "start_background_work", return_value=step != "start_background_work"),                     mock.patch.object(app.startup, "webview2_present", return_value=False),                     mock.patch.object(app, "_leave", side_effect=SystemExit) as leave:
                with self.assertRaises(SystemExit):
                    app.main()
            leave.assert_called_once()

    def test_leave_terminates_instead_of_shutting_net_down(self):
        from rshub import app
        k32 = mock.Mock(**{"GetCurrentProcess.return_value": 12345})
        with mock.patch.object(app.ctypes, "windll", mock.Mock(kernel32=k32)),                 mock.patch.object(app.os, "_exit") as exit_:
            app._leave()
        k32.TerminateProcess.assert_called_once()
        self.assertEqual(k32.TerminateProcess.call_args[0][1], 0)
        exit_.assert_called_once_with(0)  # (only reached if TerminateProcess could not end it)


class Jobs(unittest.TestCase):
    def test_finished_jobs_need_no_timer_thread(self):
        from rshub.engine import jobs
        with mock.patch.object(jobs.threading, "Timer", side_effect=AssertionError("a Timer per job")):
            jid = jobs.start("t", lambda job: None)
            for _ in range(50):
                if jobs._jobs[jid].ended:
                    break
                time.sleep(0.05)
        self.assertTrue(jobs._jobs[jid].ended)



class StagedUpdateAtStart(unittest.TestCase):
    def test_settings_unreadable_for_a_moment_is_not_a_failed_update(self):
        """A settings file locked for a moment (antivirus, backup) said "The update could not be installed" at start,
        although nothing was tried. Now the start just goes on; the staged update waits for the next start."""
        from rshub import config, settings, startup
        from rshub.engine import selfupdate
        with mock.patch.object(config, "FROZEN", True), mock.patch.object(config, "ISOLATED", False), \
                mock.patch.object(selfupdate, "staged", return_value="9.9.9"), \
                mock.patch.object(settings, "load_strict", side_effect=OSError(32, "in use")), \
                mock.patch.object(selfupdate, "apply_staged_at_start") as apply, mock.patch.object(startup, "fatal") as fatal:
            self.assertFalse(startup.install_staged_update(True))
        fatal.assert_not_called()
        apply.assert_not_called()

    def test_a_skipped_version_is_not_installed_even_if_it_was_already_downloaded(self):
        from rshub import config, settings, startup
        from rshub.engine import selfupdate
        for prefs, installs in (({"skipVersion": "9.9.9"}, False), ({"skipVersion": "9.9.8"}, True), ({}, True),
                                ({"autoUpdateFeatures": False}, False)):
            with mock.patch.object(config, "FROZEN", True), mock.patch.object(config, "ISOLATED", False), \
                    mock.patch.object(selfupdate, "staged", return_value="9.9.9"), \
                    mock.patch.object(settings, "load_strict", return_value=prefs), \
                    mock.patch.object(selfupdate, "apply_staged_at_start", return_value=True) as apply:
                self.assertEqual(startup.install_staged_update(False), installs, prefs)
                self.assertEqual(apply.called, installs, prefs)


if __name__ == "__main__":
    unittest.main()

"""engine/jobs.py: the UI must always hear that a task is over, whatever the task did."""
import json
import threading
import unittest

from rshub.engine import jobs


class JobEndAlwaysDelivered(unittest.TestCase):
    def setUp(self):
        self.old = jobs._emit
        self.got = []
        self.done = threading.Event()

        def emit(ev, data):
            msg = json.loads(json.dumps(data))  # like the app's sender: a result that is not JSON raises here
            self.got.append(msg)
            if msg["kind"] == "end":
                self.done.set()
        jobs.set_emitter(emit)

    def tearDown(self):
        jobs.set_emitter(self.old)

    def end(self):
        self.assertTrue(self.done.wait(5), "no 'end' message reached the UI")
        return [m for m in self.got if m["kind"] == "end"][-1]

    def test_unserializable_result_still_ends(self):
        jobs.start("t", lambda job: {"x": object()})
        m = self.end()
        self.assertEqual(m["state"], "error")
        self.assertIn("error", m["result"])

    def test_system_exit_in_task_still_ends(self):
        def boom(job):
            raise SystemExit(3)
        jobs.start("t", boom)
        self.assertEqual(self.end()["state"], "error")

    def test_normal_result(self):
        jobs.start("t", lambda job: {"a": 1})
        m = self.end()
        self.assertEqual((m["state"], m["result"]), ("done", {"a": 1}))



class RunningWhileJobsStart(unittest.TestCase):
    def test_a_job_starting_meanwhile_does_not_break_the_list(self):
        class Busy:  # reading its state starts another job, as another thread can at any moment
            id = "busy"

            @property
            def state(self):
                jobs._jobs.setdefault("late", jobs.Job("late"))
                return "running"

            def info(self):
                return {"id": self.id}
        jobs._jobs["busy"] = Busy()
        try:
            self.assertIn({"id": "busy"}, jobs.running())
        finally:
            jobs._jobs.pop("busy", None)
            jobs._jobs.pop("late", None)


if __name__ == "__main__":
    unittest.main()

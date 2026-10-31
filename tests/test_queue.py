import time
import unittest
from pathlib import Path

from tests.helpers import Server, make_fixtures, tree_snapshot


class QueueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server().start()
        cls.fixtures = make_fixtures(cls.server.source, long_video=True)
        cls.before = tree_snapshot(cls.server.source)

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()

    def wait_job(self, job_id, statuses, timeout=120):
        return self.server.wait_for(
            lambda state: any(j["id"] == job_id and j["status"] in statuses for j in state["jobs"]),
            timeout=timeout,
        )

    def test_01_queue_pause_batch_undo_and_per_job_pause(self):
        self.server.ok("/api/settings", "PATCH", {"concurrency": 1})
        self.server.ok("/api/queue/pause", "POST", {})
        items = [
            {"source_path": str(self.fixtures["long"]), "target": "webm", "output_name": "queue-a"},
            {"source_path": str(self.fixtures["long"]), "target": "webm", "output_name": "queue-b"},
        ]
        created = self.server.create(items)
        ids = [j["id"] for j in created["jobs"]]
        # All jobs are still queued: batch undo must remove the whole batch.
        action = next(
            a for a in self.server.ok("/api/actions?limit=20")["actions"]
            if a["kind"] == "create_jobs" and a["payload"]["job_ids"] == ids
        )
        self.server.ok(f"/api/actions/{action['id']}/undo", "POST", {})
        self.assertFalse(set(ids) & {j["id"] for j in self.server.jobs()})

        created = self.server.create(items)
        ids = [j["id"] for j in created["jobs"]]
        self.server.ok("/api/queue/resume", "POST", {})
        state = self.server.wait_for(
            lambda st: any(j["id"] == ids[0] and j["status"] == "running" and j["progress"] > 1 for j in st["jobs"]),
            timeout=60,
        )
        jobs = {j["id"]: j for j in state["jobs"]}
        self.assertEqual(jobs[ids[1]]["status"], "queued")  # concurrency=1

        self.server.ok(f"/api/jobs/{ids[0]}/pause", "POST", {})
        state = self.wait_job(ids[0], {"paused"}, timeout=10)
        paused = next(j for j in state["jobs"] if j["id"] == ids[0])
        before = paused["progress"]
        time.sleep(1.3)
        paused2 = self.server.job(ids[0])
        self.assertEqual(paused2["status"], "paused")
        self.assertLessEqual(abs(paused2["progress"] - before), 0.6)

        self.server.ok(f"/api/jobs/{ids[0]}/resume", "POST", {})
        self.server.ok(f"/api/jobs/{ids[1]}/cancel", "POST", {})
        state = self.server.wait_terminal(ids, timeout=180)
        jobs = {j["id"]: j for j in state["jobs"]}
        self.assertEqual(jobs[ids[0]]["status"], "completed", jobs[ids[0]]["error"])
        self.assertEqual(jobs[ids[1]]["status"], "canceled")
        self.assertEqual(self.before, tree_snapshot(self.server.source))

    def test_02_pause_all_and_stop_all(self):
        self.server.ok("/api/settings", "PATCH", {"concurrency": 2})
        items = [
            {"source_path": str(self.fixtures["long"]), "target": "webm", "output_name": "stop-a"},
            {"source_path": str(self.fixtures["long"]), "target": "webm", "output_name": "stop-b"},
        ]
        self.server.ok("/api/queue/resume", "POST", {})
        created = self.server.create(items)
        ids = [j["id"] for j in created["jobs"]]
        state = self.server.wait_for(
            lambda st: all(
                any(j["id"] == job_id and j["status"] == "running" for j in st["jobs"])
                for job_id in ids
            ),
            timeout=60,
        )
        self.server.ok("/api/queue/pause", "POST", {})
        state = self.wait_job(ids[0], {"paused"}, timeout=10)
        jobs = {j["id"]: j for j in state["jobs"]}
        self.assertEqual(jobs[ids[1]]["status"], "paused")
        self.assertTrue(state["queue_paused"])
        time.sleep(0.4)  # allow the final progress write before SIGSTOP settles
        progress = {job_id: self.server.job(job_id)["progress"] for job_id in ids}
        time.sleep(1.2)
        state = self.server.state()
        for job_id in ids:
            self.assertLessEqual(abs(next(j for j in state["jobs"] if j["id"] == job_id)["progress"] - progress[job_id]), 1.5)
        self.server.ok("/api/queue/stop", "POST", {})
        state = self.server.wait_for(
            lambda st: all(
                any(j["id"] == job_id and j["status"] in ("canceled", "failed") for j in st["jobs"])
                for job_id in ids
            ),
            timeout=30,
        )
        self.assertTrue(state["queue_paused"])
        self.assertEqual({j["status"] for j in state["jobs"] if j["id"] in ids}, {"canceled"})
        self.assertEqual(self.before, tree_snapshot(self.server.source))

    def test_03_cancel_running_job(self):
        self.server.ok("/api/queue/resume", "POST", {})
        self.server.ok("/api/settings", "PATCH", {"concurrency": 1})
        created = self.server.create([{
            "source_path": str(self.fixtures["long"]), "target": "webm", "output_name": "cancel-run",
        }])
        job_id = created["jobs"][0]["id"]
        self.server.wait_for(
            lambda st: any(j["id"] == job_id and j["status"] == "running" for j in st["jobs"]),
            timeout=60,
        )
        self.server.ok(f"/api/jobs/{job_id}/cancel", "POST", {})
        state = self.server.wait_terminal([job_id], timeout=30)
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        self.assertEqual(job["status"], "canceled", job["error"])
        temp = self.server.output / ".transcoder-tmp" / job_id
        self.assertFalse(temp.exists())
        self.assertEqual(self.before, tree_snapshot(self.server.source))


if __name__ == "__main__":
    unittest.main()

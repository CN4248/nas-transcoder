import zipfile
import unittest
from pathlib import Path

from tests.helpers import Server, make_fixtures


class BulkQueueActionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server().start()
        cls.fixtures = make_fixtures(cls.server.source)

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()

    def test_clear_completed_with_undo_then_retry_failed(self):
        # Two real completed tasks.
        completed = self.server.create([
            {"source_path": str(self.fixtures["audio"]), "target": "m4a", "output_name": "bulk-a"},
            {"source_path": str(self.fixtures["image"]), "target": "webp", "output_name": "bulk-b"},
        ])["jobs"]
        completed_ids = [j["id"] for j in completed]
        state = self.server.wait_terminal(completed_ids, timeout=90)
        jobs = {j["id"]: j for j in state["jobs"]}
        outputs = [Path(jobs[job_id]["output_path"]) for job_id in completed_ids]
        for output in outputs:
            self.assertTrue(output.is_file())

        # One genuinely failing task (archive path traversal).
        evil = self.server.source / "bulk-evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../bulk-escape.txt", "pwn")
        failed = self.server.create([{
            "source_path": str(evil), "target": "zip", "output_name": "bulk-fail",
        }])["jobs"][0]
        self.server.wait_terminal([failed["id"]], timeout=60)
        self.assertEqual(self.server.job(failed["id"])["status"], "failed")

        # Clear completed records only; output files must remain on disk.
        status, data = self.server.api("/api/queue/clear-completed", "POST", {})
        self.assertEqual(status, 200, data)
        self.assertGreaterEqual(data["cleared"], 2)
        remaining = {j["id"] for j in self.server.jobs()}
        self.assertFalse(set(completed_ids) & remaining)
        for output in outputs:
            self.assertTrue(output.is_file(), output)

        # The bulk clear is one undoable action; restoring it must bring records back.
        action = next(a for a in self.server.ok("/api/actions?limit=20")["actions"]
                      if a["kind"] == "clear_completed")
        self.assertTrue(action["can_undo"])
        self.server.ok(f"/api/undo", "POST", {})
        restored = {j["id"]: j for j in self.server.jobs()}
        for job_id in completed_ids:
            self.assertIn(job_id, restored)
            self.assertEqual(restored[job_id]["status"], "completed")

        # Retry every failed task; pause the queue so the retried job stays queued.
        self.server.ok("/api/queue/pause", "POST", {})
        status, data = self.server.api("/api/queue/retry-failed", "POST", {})
        self.assertEqual(status, 200, data)
        self.assertGreaterEqual(data["retried"], 1)
        retried = self.server.job(failed["id"])
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["progress"], 0)
        self.assertIsNone(retried["error"])
        self.server.ok("/api/queue/resume", "POST", {})
        state = self.server.wait_terminal([failed["id"]], timeout=60)
        self.assertEqual(next(j for j in state["jobs"] if j["id"] == failed["id"])["status"], "failed")


if __name__ == "__main__":
    unittest.main()

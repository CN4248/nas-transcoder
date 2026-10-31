import os
import unittest
from pathlib import Path

from tests.helpers import Server, make_fixtures, tree_snapshot


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.server = Server().start()
        self.fixtures = make_fixtures(self.server.source)
        self.before = tree_snapshot(self.server.source)

    def tearDown(self):
        self.server.cleanup()

    def test_queue_settings_logs_and_tmp_cleanup_survive_restart(self):
        self.server.ok("/api/settings", "PATCH", {"concurrency": 4, "name_pattern": "{name}-{target}"})
        self.server.ok("/api/queue/pause", "POST", {})
        created = self.server.create([{
            "source_path": str(self.fixtures["audio"]), "target": "flac", "output_name": "persist-me",
        }])
        job_id = created["jobs"][0]["id"]
        # Simulate a stale temp dir left by a crash.
        stale = self.server.output / ".transcoder-tmp" / "stale-job"
        stale.mkdir(parents=True)
        (stale / "partial.tmp").write_text("partial", encoding="utf-8")
        boot = self.server.ok("/api/bootstrap")
        self.assertTrue(any(log["message"] == "加入队列：" + "audio.mp3 → FLAC" for log in boot["logs"]))

        self.server.restart()
        boot = self.server.ok("/api/bootstrap")
        self.assertEqual(boot["settings"]["concurrency"], 4)
        self.assertEqual(boot["settings"]["name_pattern"], "{name}-{target}")
        self.assertTrue(boot["queue_paused"])
        job = next(j for j in boot["jobs"] if j["id"] == job_id)
        self.assertEqual(job["status"], "queued")
        self.assertFalse(stale.exists())
        self.assertTrue(any(log["job_id"] == job_id for log in boot["logs"]))
        self.assertEqual(self.before, tree_snapshot(self.server.source))

        self.server.ok("/api/queue/resume", "POST", {})
        state = self.server.wait_terminal([job_id], timeout=90)
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertTrue(Path(job["output_path"]).is_file())
        # Same database survives a second restart with the completed output.
        self.server.restart()
        boot = self.server.ok("/api/bootstrap")
        job = next(j for j in boot["jobs"] if j["id"] == job_id)
        self.assertEqual(job["status"], "completed")
        self.assertTrue(Path(job["output_path"]).is_file())
        self.assertEqual(self.before, tree_snapshot(self.server.source))


if __name__ == "__main__":
    unittest.main()

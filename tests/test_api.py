import os
import time
import unittest
import urllib.request
from pathlib import Path

from tests.helpers import Server, make_fixtures, tree_snapshot


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server().start()
        cls.fixtures = make_fixtures(cls.server.source)

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()

    def test_01_bootstrap_tools_and_matrix(self):
        data = self.server.ok("/api/bootstrap")
        self.assertTrue(data["app"]["version"])
        self.assertTrue(data["capabilities"]["tools"])
        matrix = data["capabilities"]["matrix"]
        self.assertIn("m4a", [t["ext"] for t in matrix["mp3"]])
        self.assertIn("flac", [t["ext"] for t in matrix["mp3"]])
        targets = [t["ext"] for t in matrix["mp4"]]
        self.assertIn("mp4", targets)
        self.assertIn("mp3", targets)  # extract audio
        self.assertIn("gif", targets)
        self.assertIn("7z", [t["ext"] for t in matrix["zip"]])

    def test_01b_capabilities_refresh(self):
        before = self.server.ok("/api/capabilities")["capabilities"]
        data = self.server.ok("/api/capabilities/refresh", "POST", {})
        caps = data["capabilities"]
        self.assertTrue(caps["tools"])
        self.assertTrue(all("error" in tool for tool in caps["tools"]))
        self.assertGreaterEqual(caps["probed_at"], before["probed_at"])
        self.assertIn("mp3", caps["matrix"])

    def test_02_browse_and_filter(self):
        data = self.server.ok(f"/api/fs/list?path={self.server.source}")
        self.assertGreaterEqual(data["total"], 5)
        self.assertTrue(all(e["path"].startswith(str(self.server.source)) for e in data["entries"]))
        audio = self.server.ok(f"/api/fs/list?path={self.server.source}&kind=audio")
        self.assertTrue(audio["entries"])
        self.assertTrue(all(e["is_dir"] or e["family"] == "audio" for e in audio["entries"]))
        hidden = self.server.ok(f"/api/fs/list?path={self.server.source}&hidden=1")
        self.assertTrue(hidden["ok"])

    def test_03_output_mkdir_and_undo(self):
        data = self.server.ok("/api/output/mkdir", "POST", {"parent": str(self.server.output), "name": "exports"})
        folder = Path(data["path"])
        self.assertTrue(folder.is_dir())
        listing = self.server.ok(f"/api/output/list?path={self.server.output}")
        self.assertIn("exports", [d["name"] for d in listing["dirs"]])
        actions = self.server.ok("/api/actions?limit=20")["actions"]
        action = next(a for a in actions if a["kind"] == "mkdir" and a["payload"]["path"] == str(folder))
        self.server.ok(f"/api/actions/{action['id']}/undo", "POST", {})
        self.assertFalse(folder.exists())

    def test_04_settings_and_undo(self):
        before = self.server.ok("/api/settings")["settings"]
        target = 3 if before["concurrency"] != 3 else 4
        data = self.server.ok("/api/settings", "PATCH", {"concurrency": target, "name_pattern": "{name}-{target}"})
        self.assertEqual(data["settings"]["concurrency"], target)
        self.assertEqual(data["settings"]["name_pattern"], "{name}-{target}")
        action = next(a for a in self.server.ok("/api/actions?limit=20")["actions"] if a["kind"] == "settings")
        self.server.ok(f"/api/actions/{action['id']}/undo", "POST", {})
        after = self.server.ok("/api/settings")["settings"]
        self.assertEqual(after["concurrency"], before["concurrency"])
        self.assertEqual(after["name_pattern"], before["name_pattern"])

    def test_05_batch_create_undo_and_no_source_changes(self):
        before = tree_snapshot(self.server.source)
        self.server.ok("/api/queue/pause", "POST", {})
        try:
            items = [
                {"source_path": str(self.fixtures["audio"]), "target": "m4a", "output_name": "undo-a"},
                {"source_path": str(self.fixtures["image"]), "target": "webp", "output_name": "undo-b"},
            ]
            data = self.server.create(items)
            ids = [j["id"] for j in data["jobs"]]
            self.assertEqual(len(ids), 2)
            action = next(
                a for a in self.server.ok("/api/actions?limit=20")["actions"]
                if a["kind"] == "create_jobs" and a["payload"]["job_ids"] == ids
            )
            self.assertTrue(action["can_undo"])
            self.server.ok(f"/api/actions/{action['id']}/undo", "POST", {})
            remaining = {j["id"] for j in self.server.jobs()}
            self.assertFalse(set(ids) & remaining)
        finally:
            self.server.ok("/api/queue/resume", "POST", {})
        self.assertEqual(before, tree_snapshot(self.server.source))

    def test_06_unsafe_paths_are_rejected(self):
        status, _ = self.server.api("/api/fs/list?path=/etc")
        self.assertEqual(status, 403)
        status, _ = self.server.api("/api/output/mkdir", "POST", {"parent": str(self.server.source), "name": "x"})
        self.assertEqual(status, 403)
        status, _ = self.server.api("/api/download?path=" + urllib.parse.quote(str(self.fixtures["audio"])))
        self.assertEqual(status, 403)
        status, data = self.server.api("/api/jobs", "POST", {"items": [{
            "source_path": str(self.fixtures["audio"]), "target": "m4a",
            "output_dir": str(self.server.source), "output_name": "bad",
        }]})
        self.assertEqual(status, 400)
        self.assertIn("导出", data["error"]["message"])
        self.assertFalse(data.get("ok"))

    def test_07_source_symlink_escape_rejected(self):
        outside = self.server.tmp / "outside.txt"
        outside.write_text("do not read", encoding="utf-8")
        link = self.server.source / "escape.mp3"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlinks unavailable")
        status, data = self.server.api("/api/jobs", "POST", {"items": [{
            "source_path": str(link), "target": "m4a", "output_name": "escape",
        }]})
        self.assertEqual(status, 400)
        self.assertIn("符号链接", data["error"]["message"])

    def test_08_download_safe_output(self):
        target = self.server.output / "downloadable.txt"
        target.write_bytes(b"hello download\n")
        with urllib.request.urlopen(self.server.base + "/api/download?path=" + urllib.parse.quote(str(target)), timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"hello download\n")
            self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))

    def test_09_job_queue_and_state_persist_in_db(self):
        self.server.ok("/api/queue/pause", "POST", {})
        try:
            data = self.server.create([{
                "source_path": str(self.fixtures["audio"]), "target": "flac", "output_name": "persist-test",
            }])
            job = data["jobs"][0]
            state = self.server.state()
            self.assertTrue(state["queue_paused"])
            found = next(j for j in state["jobs"] if j["id"] == job["id"])
            self.assertEqual(found["status"], "queued")
            self.assertTrue(any(log["job_id"] == job["id"] for log in state["logs"]))
            self.server.ok(f"/api/jobs/{job['id']}", "DELETE")
        finally:
            self.server.ok("/api/queue/resume", "POST", {})


if __name__ == "__main__":
    unittest.main()

import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import sha256

from app.config import Config
from app.db import Database
from app.security import (
    ConflictError,
    ForbiddenError,
    ValidationError,
    candidate_paths,
    fingerprint,
    render_name,
    safe_output_path,
    source_path,
)


class SecurityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nts-core-")
        root = Path(self.tmp.name)
        self.src = root / "src"
        self.out = root / "out"
        self.data = root / "data"
        for path in (self.src, self.out, self.data):
            path.mkdir()
        self.cfg = Config.from_env(SOURCE_ROOT=str(self.src), OUTPUT_ROOT=str(self.out), DATA_DIR=str(self.data))
        self.cfg.prepare()

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_traversal_rejected(self):
        with self.assertRaises((ForbiddenError, ValidationError)):
            source_path(str(self.src / ".." / "etc" / "passwd"), self.cfg)
        with self.assertRaises(ForbiddenError):
            source_path("/etc/passwd", self.cfg)

    def test_source_symlink_escape_rejected(self):
        outside = Path(self.tmp.name) / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        os.symlink(outside, self.src / "escape.txt")
        with self.assertRaises((ForbiddenError, ValidationError)):
            source_path(str(self.src / "escape.txt"), self.cfg)

    def test_source_device_rejected(self):
        with self.assertRaises((ForbiddenError, ValidationError)):
            source_path("/dev/null", self.cfg)

    def test_output_outside_rejected(self):
        with self.assertRaises(ForbiddenError):
            safe_output_path(str(self.src / "result.mp4"), self.cfg)
        with self.assertRaises(ForbiddenError):
            safe_output_path("/tmp/elsewhere.mp4", self.cfg)

    def test_output_symlink_escape_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        os.symlink(outside, self.out / "link")
        with self.assertRaises(ForbiddenError):
            safe_output_path(str(self.out / "link" / "x.mp4"), self.cfg)

    def test_compound_suffix_collision_names(self):
        names = []
        for path in candidate_paths(str(self.out), "movie.tar.gz"):
            names.append(os.path.basename(path))
            if len(names) == 3:
                break
        self.assertEqual(names, ["movie.tar.gz", "movie (1).tar.gz", "movie (2).tar.gz"])

    def test_filename_extension_and_traversal(self):
        self.assertEqual(render_name("{name}", "song.mp3", "m4a", 1), "song.m4a")
        self.assertEqual(render_name("{name}", "song.mp3", "m4a", 1, "../../evil"), "evil.m4a")
        self.assertEqual(render_name("{name}", "movie.tar.gz", "tar.xz", 1), "movie.tar.xz")

    def test_source_fingerprint_detects_change(self):
        path = self.src / "sample.bin"
        path.write_bytes(b"a" * 100 + b"b" * 100)
        before = fingerprint(str(path))
        self.assertTrue(before["sample_sha256"])
        path.write_bytes(b"a" * 100 + b"c" * 100)
        after = fingerprint(str(path))
        self.assertNotEqual(before["sample_sha256"], after["sample_sha256"])


class DatabaseTest(unittest.TestCase):
    def test_jobs_settings_events_actions_survive_reopen(self):
        with tempfile.TemporaryDirectory(prefix="nts-db-") as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "out").mkdir()
            (root / "data").mkdir()
            cfg = Config.from_env(
                SOURCE_ROOT=str(root / "src"), OUTPUT_ROOT=str(root / "out"), DATA_DIR=str(root / "data"),
            )
            cfg.prepare()
            db = Database(cfg)
            db.update_settings({"concurrency": 3})
            job = {
                "id": "a" * 16, "batch_id": "b" * 16,
                "source_path": str(root / "src" / "x.mp3"), "source_name": "x.mp3",
                "source_ext": "mp3", "source_size": 10, "family": "audio",
                "target": "m4a", "target_family": "audio", "mode": "audio",
                "output_dir": str(root / "out"), "output_name": "x.m4a",
                "output_path": str(root / "out" / "x.m4a"), "status": "queued",
                "progress": 0, "created_at": 1, "updated_at": 1, "options": {"quality_preset": "balanced"},
                "sort_order": 1,
            }
            db.insert_jobs([job])
            event_id = db.add_event("info", "hello", job["id"])
            action = db.add_action("create_jobs", "batch", {"job_ids": [job["id"]]})
            db.close()

            db2 = Database(cfg)
            self.assertEqual(db2.get_setting("concurrency"), 3)
            self.assertEqual(db2.get_job(job["id"])["options"]["quality_preset"], "balanced")
            self.assertEqual(db2.events(0, 10)[0]["id"], event_id)
            self.assertEqual(db2.get_action(int(action["id"]))["summary"], "batch")
            db2.close()


class NamingTest(unittest.TestCase):
    def test_dangerous_names_are_sanitized(self):
        from app.security import safe_filename
        self.assertNotIn("/", safe_filename("../../x", "fallback", "mp4"))
        self.assertTrue(safe_filename("a" * 300, "fallback", "mp4").endswith(".mp4"))
        self.assertEqual(safe_filename("clip.MP4", "fallback", "mp4"), "clip.MP4")
        self.assertEqual(safe_filename("clip", "fallback", "tar.gz"), "clip.tar.gz")


if __name__ == "__main__":
    unittest.main()

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.helpers import Server, make_fixtures, tree_snapshot


class TranscodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server().start()
        cls.fixtures = make_fixtures(cls.server.source)
        cls.caps = cls.server.ok("/api/bootstrap")["capabilities"]
        cls.before = tree_snapshot(cls.server.source)

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()

    def targets(self, ext):
        return {t["ext"] for t in self.caps["matrix"].get(ext, [])}

    def test_01_capability_matrix_is_honest(self):
        self.assertIn("m4a", self.targets("mp3"))
        self.assertIn("flac", self.targets("mp3"))
        self.assertIn("mp4", self.targets("mp4"))
        self.assertIn("mp3", self.targets("mp4"))  # video -> audio
        self.assertIn("gif", self.targets("mp4"))
        self.assertIn("webp", self.targets("png"))
        self.assertIn("7z", self.targets("zip"))
        self.assertIn("vtt", self.targets("srt"))
        if "avif" in self.targets("png"):
            self.assertTrue(any(t["mode"] == "image_ffmpeg" for t in self.caps["matrix"]["png"] if t["ext"] == "avif"))

    def test_02_transcode_five_families_and_source_untouched(self):
        items = [
            {"source_path": str(self.fixtures["audio"]), "target": "m4a", "output_name": "x-audio"},
            {"source_path": str(self.fixtures["image"]), "target": "webp", "output_name": "x-image"},
            {"source_path": str(self.fixtures["video"]), "target": "mp4", "output_name": "x-video"},
            {"source_path": str(self.fixtures["archive"]), "target": "7z", "output_name": "x-archive"},
            {"source_path": str(self.fixtures["archive"]), "target": "tar.gz", "output_name": "x-archive-tgz"},
            {"source_path": str(self.fixtures["subtitle"]), "target": "vtt", "output_name": "x-subtitle"},
        ]
        if "avif" in self.targets("png"):
            items.append({"source_path": str(self.fixtures["image"]), "target": "avif", "output_name": "x-avif"})
        if "gif" in self.targets("mp4"):
            items.append({"source_path": str(self.fixtures["video"]), "target": "gif", "output_name": "x-gif"})
        if "mp3" in self.targets("mp4"):
            items.append({"source_path": str(self.fixtures["video"]), "target": "mp3", "output_name": "x-from-video"})
        if "zip" in self.targets("rar"):
            items.append({"source_path": str(self.fixtures["rar"]), "target": "zip", "output_name": "x-from-rar"})
        if self.fixtures.get("avif") and "jpg" in self.targets("avif"):
            items.append({"source_path": str(self.fixtures["avif"]), "target": "jpg", "output_name": "x-from-avif"})
        created = self.server.create(items, {"quality_preset": "balanced"})
        ids = [job["id"] for job in created["jobs"]]
        state = self.server.wait_terminal(ids, timeout=180)
        jobs = {job["id"]: job for job in state["jobs"]}
        for job_id in ids:
            job = jobs[job_id]
            self.assertEqual(job["status"], "completed", f"{job['source_name']} -> {job['target']}: {job['error']}")
            self.assertTrue(Path(job["output_path"]).is_file())
        by_name = {job["output_name"]: job for job in jobs.values()}
        # Validate generated media with the real tools, not just file size.
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            for name in ("x-audio.m4a", "x-video.mp4", "x-from-video.mp3", "x-gif.gif", "x-avif.avif", "x-from-avif.jpg"):
                if name not in by_name:
                    continue
                proc = subprocess.run(
                    [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                     "-of", "json", by_name[name]["output_path"]],
                    capture_output=True, text=True,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertTrue(json.loads(proc.stdout)["streams"], name)
        magick = shutil.which("magick") or shutil.which("convert")
        if magick:
            proc = subprocess.run([magick, "identify", by_name["x-image.webp"]["output_path"]], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        sevenzip = shutil.which("7z") or shutil.which("7zz")
        if sevenzip:
            proc = subprocess.run([sevenzip, "t", by_name["x-archive.7z"]["output_path"]], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        import tarfile
        with tarfile.open(by_name["x-archive-tgz.tar.gz"]["output_path"]) as tf:
            self.assertIn("hello.txt", tf.getnames())
        self.assertIn("WEBVTT", Path(by_name["x-subtitle.vtt"]["output_path"]).read_text(errors="replace"))
        if "x-from-rar.zip" in by_name:
            import zipfile
            with zipfile.ZipFile(by_name["x-from-rar.zip"]["output_path"]) as zf:
                self.assertTrue(zf.namelist())
        self.assertEqual(self.before, tree_snapshot(self.server.source))

    def test_03_never_overwrite_existing_output(self):
        existing = self.server.output / "x-overwrite.m4a"
        existing.write_bytes(b"KEEP-THIS-ORIGINAL")
        created = self.server.create([{
            "source_path": str(self.fixtures["audio"]), "target": "m4a", "output_name": "x-overwrite",
        }])
        job_id = created["jobs"][0]["id"]
        state = self.server.wait_terminal([job_id], timeout=90)
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(existing.read_bytes(), b"KEEP-THIS-ORIGINAL")
        self.assertNotEqual(Path(job["output_path"]).resolve(), existing.resolve())
        self.assertTrue(Path(job["output_path"]).is_file())
        self.assertIn(" (1)", Path(job["output_path"]).name)

    def test_04_undo_and_restore_output(self):
        created = self.server.create([{
            "source_path": str(self.fixtures["audio"]), "target": "flac", "output_name": "x-undo",
        }])
        job_id = created["jobs"][0]["id"]
        state = self.server.wait_terminal([job_id], timeout=90)
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        path = Path(job["output_path"])
        self.assertTrue(path.is_file())
        self.server.ok(f"/api/jobs/{job_id}/undo-output", "POST", {})
        job = self.server.job(job_id)
        self.assertEqual(job["status"], "undone")
        self.assertFalse(path.exists())
        self.assertTrue(job["trash_path"])
        self.assertTrue(Path(job["trash_path"]).is_file())
        self.assertEqual(self.before, tree_snapshot(self.server.source))
        self.server.ok(f"/api/jobs/{job_id}/restore", "POST", {})
        job = self.server.job(job_id)
        self.assertEqual(job["status"], "completed")
        self.assertTrue(Path(job["output_path"]).is_file())
        self.assertEqual(self.before, tree_snapshot(self.server.source))

    def test_05_archive_traversal_rejected(self):
        import zipfile
        evil = self.server.source / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../escaped.txt", "pwn")
        before = tree_snapshot(self.server.source)
        created = self.server.create([{
            "source_path": str(evil), "target": "zip", "output_name": "evil-output",
        }])
        job_id = created["jobs"][0]["id"]
        state = self.server.wait_terminal([job_id], timeout=60)
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("危险路径", job["error"] or "")
        self.assertFalse((self.server.output.parent / "escaped.txt").exists())
        self.assertEqual(before, tree_snapshot(self.server.source))

    def test_06_archive_symlink_rejected(self):
        import io
        import tarfile
        evil = self.server.source / "symlink.tar"
        with tarfile.open(evil, "w") as tf:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info, io.BytesIO(b""))
            data = b"hello"
            regular = tarfile.TarInfo("hello.txt")
            regular.size = len(data)
            tf.addfile(regular, io.BytesIO(data))
        before = tree_snapshot(self.server.source)
        created = self.server.create([{
            "source_path": str(evil), "target": "zip", "output_name": "symlink-output",
        }])
        job_id = created["jobs"][0]["id"]
        state = self.server.wait_terminal([job_id], timeout=60)
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        self.assertEqual(job["status"], "failed")
        self.assertTrue(any(word in (job["error"] or "") for word in ("符号链接", "特殊文件")))
        self.assertEqual(before, tree_snapshot(self.server.source))


if __name__ == "__main__":
    unittest.main()

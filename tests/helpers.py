"""Test helpers: isolated server process, API client and media fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

PROJECT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server:
    def __init__(self, *, source: Path | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="nts-test-"))
        self.source = source or (self.tmp / "source")
        self.output = self.tmp / "output"
        self.data = self.tmp / "data"
        for path in (self.source, self.output, self.data):
            path.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.log_path = self.tmp / "server.log"
        self._log = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 90) -> "Server":
        env = os.environ.copy()
        env.update({
            "SOURCE_ROOT": str(self.source),
            "OUTPUT_ROOT": str(self.output),
            "DATA_DIR": str(self.data),
            "PORT": str(self.port),
            "HOST": "127.0.0.1",
            "PYTHONPATH": str(PROJECT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LOG_LEVEL": "INFO",
        })
        self._log = open(self.log_path, "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "app.server"],
            cwd=str(PROJECT), env=env, stdout=self._log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early:\n{self.log_tail()}")
            try:
                status, _ = self.api("/api/health", timeout=2)
                if status == 200:
                    return self
            except Exception:
                pass
            time.sleep(0.15)
        raise RuntimeError(f"server did not become healthy:\n{self.log_tail()}")

    def stop(self, sig: int = 15, wait: float = 8) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(sig)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self._log:
            self._log.close()
            self._log = None

    def restart(self) -> "Server":
        self.stop()
        self.start()
        return self

    def cleanup(self) -> None:
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self) -> "Server":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.cleanup()

    def api(self, path: str, method: str = "GET", payload: Any = None, timeout: float = 60) -> tuple[int, Any]:
        raw = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=raw, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, {"raw": body}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"API {method} {path} failed: {exc}\n{self.log_tail()}") from exc

    def ok(self, path: str, method: str = "GET", payload: Any = None, timeout: float = 60) -> Any:
        status, data = self.api(path, method, payload, timeout)
        if status >= 300 or (isinstance(data, dict) and data.get("ok") is False):
            raise AssertionError(f"{method} {path} -> {status}: {data}")
        return data

    def create(self, items: list[dict[str, Any]], options: dict[str, Any] | None = None) -> Any:
        return self.ok("/api/jobs", "POST", {"items": items, "options": options or {}})

    def state(self) -> dict[str, Any]:
        return self.ok("/api/state?after_log=0")

    def jobs(self) -> list[dict[str, Any]]:
        return self.state().get("jobs") or []

    def job(self, job_id: str) -> dict[str, Any]:
        for job in self.jobs():
            if job["id"] == job_id:
                return job
        raise KeyError(job_id)

    def wait_for(self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 120) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.state()
            if predicate(last):
                return last
            time.sleep(0.2)
        raise AssertionError(f"condition not reached; state={json.dumps(last, ensure_ascii=False)[:3000]}\n{self.log_tail()}")

    def wait_terminal(self, job_ids: list[str], timeout: float = 180) -> dict[str, Any]:
        terminal = {"completed", "failed", "canceled", "undone"}
        return self.wait_for(
            lambda state: all(
                any(j["id"] == job_id and j["status"] in terminal for j in state["jobs"])
                for job_id in job_ids
            ),
            timeout=timeout,
        )

    def log_tail(self, lines: int = 80) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except FileNotFoundError:
            return ""


def make_fixtures(root: Path, *, long_video: bool = False) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    magick = shutil.which("magick") or shutil.which("convert") or "magick"
    env = os.environ.copy()
    env["MAGICK_TMPDIR"] = "/tmp"

    def run(argv: list[str]) -> None:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"fixture command failed: {argv}\n{proc.stdout}\n{proc.stderr}")

    audio = root / "audio.mp3"
    video = root / "video.mp4"
    image = root / "image.png"
    archive = root / "archive.zip"
    subtitle = root / "subs.srt"
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:a", "libmp3lame", str(audio), "-y"])
    run([ffmpeg, "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(video), "-y"])
    run([magick, "-size", "320x240", "gradient:teal-purple", str(image)])
    avif = root / "image.avif"
    avif_proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(image), "-frames:v", "1",
         "-c:v", "libaom-av1", "-still-picture", "1", "-crf", "30", str(avif), "-y"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    if avif_proc.returncode != 0:
        avif = None
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("hello.txt", "hello\n")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    rar = root / "sample.rar"
    shutil.copy2(Path(__file__).parent / "fixtures" / "rar5-times.rar", rar)
    result = {
        "audio": audio, "video": video, "image": image, "archive": archive,
        "subtitle": subtitle, "rar": rar, "avif": avif,
    }
    if long_video:
        long_path = root / "long.mp4"
        run([ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=960x540:rate=24:duration=15",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", str(long_path), "-y"])
        result["long"] = long_path
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            st = path.stat()
            result[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns, sha256(path))
    return result


def wait_until(fn: Callable[[], bool], timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.1)
    raise AssertionError("timed out")

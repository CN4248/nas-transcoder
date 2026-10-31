"""Runtime configuration for NAS Transcode Studio.

All values are resolved once at import/startup from environment variables so the
container image stays configuration-free.  Tests inject explicit paths via
``Config.from_env`` overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

APP_VERSION = "1.0.0"
APP_NAME = "NAS Transcode Studio"

DEFAULT_SETTINGS: dict[str, object] = {
    "concurrency": 2,
    "default_output_dir": "/output",
    "name_pattern": "{name}",
    "quality_preset": "balanced",
    "preserve_metadata": True,
    "show_hidden": False,
    "queue_paused": False,
    "verify_source": True,
    "image_quality": 88,
    "audio_bitrate": "192k",
    "video_crf": 23,
    "video_preset": "veryfast",
    "archive_level": 5,
}

SETTING_RANGES = {
    "concurrency": (1, 8),
    "image_quality": (1, 100),
    "audio_bitrate": {"96k", "128k", "160k", "192k", "256k", "320k"},
    "video_crf": (14, 40),
    "video_preset": {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "veryslow"},
    "archive_level": (0, 9),
    "quality_preset": {"balanced", "high", "compact", "lossless"},
}

QUALITY_PRESETS = {
    "balanced": {"image_quality": 88, "audio_bitrate": "192k", "video_crf": 23, "video_preset": "veryfast", "archive_level": 5},
    "high": {"image_quality": 95, "audio_bitrate": "320k", "video_crf": 19, "video_preset": "medium", "archive_level": 7},
    "compact": {"image_quality": 74, "audio_bitrate": "128k", "video_crf": 28, "video_preset": "veryfast", "archive_level": 9},
    "lossless": {"image_quality": 100, "audio_bitrate": "320k", "video_crf": 16, "video_preset": "slow", "archive_level": 9},
}


def _env_path(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    return os.path.abspath(os.path.expanduser(value))


@dataclass
class Config:
    source_root: str = "/host"
    output_root: str = "/output"
    data_dir: str = "/data"
    host: str = "0.0.0.0"
    port: int = 5124
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    magick: str = "magick"
    sevenzip: str = "7z"
    bsdtar: str = "bsdtar"
    max_archive_entries: int = 2_000_000
    max_archive_gib: int = 200
    max_archive_ratio: int = 200
    log_keep: int = 5000
    start_scheduler: bool = True
    static_dir: str = field(default_factory=lambda: str(Path(__file__).resolve().parent / "static"))

    @classmethod
    def from_env(cls, **overrides: object) -> "Config":
        cfg = cls(
            source_root=_env_path("SOURCE_ROOT", "/host"),
            output_root=_env_path("OUTPUT_ROOT", "/output"),
            data_dir=_env_path("DATA_DIR", "/data"),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "5124")),
            ffmpeg=os.environ.get("FFMPEG_BIN", "ffmpeg"),
            ffprobe=os.environ.get("FFPROBE_BIN", "ffprobe"),
            magick=os.environ.get("MAGICK_BIN", "magick"),
            sevenzip=os.environ.get("SEVENZIP_BIN", "7z"),
            bsdtar=os.environ.get("BSDTAR_BIN", "bsdtar"),
            max_archive_entries=int(os.environ.get("MAX_ARCHIVE_ENTRIES", "2000000")),
            max_archive_gib=int(os.environ.get("MAX_ARCHIVE_GIB", "200")),
            max_archive_ratio=int(os.environ.get("MAX_ARCHIVE_RATIO", "200")),
            log_keep=int(os.environ.get("LOG_KEEP", "5000")),
        )
        for key, value in overrides.items():
            field_name = key.lower()
            if not hasattr(cfg, field_name):
                raise TypeError(f"unknown config field: {key}")
            setattr(cfg, field_name, value)
        return cfg

    def prepare(self) -> None:
        """Create writable application directories and fail loudly if unsafe."""
        self.source_root = os.path.realpath(self.source_root)
        self.output_root = os.path.realpath(self.output_root)
        self.data_dir = os.path.realpath(self.data_dir)
        if not os.path.isdir(self.source_root):
            raise RuntimeError(f"source root does not exist: {self.source_root}")
        if _overlaps(self.source_root, self.output_root):
            raise RuntimeError("source root and output root must not overlap")
        if _overlaps(self.source_root, self.data_dir):
            raise RuntimeError("source root and data dir must not overlap")
        for path in (self.output_root, self.data_dir):
            os.makedirs(path, exist_ok=True)
        for path in (self.tmp_dir, self.trash_dir):
            if os.path.islink(path):
                raise RuntimeError(f"internal directory must not be a symlink: {path}")
            os.makedirs(path, exist_ok=True)
            if not os.path.realpath(path).startswith(self.output_root + os.sep):
                raise RuntimeError(f"internal directory escapes output root: {path}")
            if _overlaps(os.path.realpath(path), self.source_root):
                raise RuntimeError(f"internal directory overlaps source root: {path}")
        # Make sure the process can actually write output (clear error on NAS ACLs).
        probe = os.path.join(self.output_root, ".transcoder-write-probe")
        try:
            fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        finally:
            try:
                os.unlink(probe)
            except FileNotFoundError:
                pass

    @property
    def tmp_dir(self) -> str:
        return os.path.join(self.output_root, ".transcoder-tmp")

    @property
    def trash_dir(self) -> str:
        return os.path.join(self.output_root, ".transcoder-trash")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "app.db")

    def source_mount_readonly(self) -> bool | None:
        """Return True/False when source_root is a mountpoint, else None."""
        try:
            with open("/proc/self/mounts", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    mountpoint = parts[1].replace("\\040", " ")
                    if os.path.realpath(mountpoint) == self.source_root:
                        return "ro" in parts[3].split(",")
        except OSError:
            return None
        return None

    def as_public(self) -> dict[str, object]:
        return {
            "name": APP_NAME,
            "version": APP_VERSION,
            "source_root": self.source_root,
            "output_root": self.output_root,
            "host": self.host,
            "port": self.port,
            "max_archive_gib": self.max_archive_gib,
            "max_archive_entries": self.max_archive_entries,
        }


def _overlaps(a: str, b: str) -> bool:
    a = os.path.realpath(a).rstrip(os.sep) + os.sep
    b = os.path.realpath(b).rstrip(os.sep) + os.sep
    return a.startswith(b) or b.startswith(a)


def public_config_fields() -> list[str]:
    return [f.name for f in fields(Config) if not f.name.startswith("_")]

"""Dynamic format capability probing.

Nothing here claims a format is supported unless the tool inside the image
actually advertises the needed encoder/muxer/format at startup.  The resulting
matrix is served to the WebUI, so the format dropdown always reflects reality.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config

# Source extension -> family (canonical, lowercase, no dot).
SOURCE_EXTENSIONS: dict[str, str] = {
    # audio
    "mp3": "audio", "m4a": "audio", "aac": "audio", "flac": "audio", "wav": "audio",
    "ogg": "audio", "oga": "audio", "opus": "audio", "wma": "audio", "aiff": "audio",
    "aif": "audio", "ape": "audio", "ac3": "audio", "alac": "audio",
    # video
    "mp4": "video", "mkv": "video", "avi": "video", "mov": "video", "webm": "video",
    "m4v": "video", "flv": "video", "wmv": "video", "mpg": "video", "mpeg": "video",
    "ts": "video", "m2ts": "video", "rmvb": "video", "rm": "video", "3gp": "video",
    "ogv": "video",
    # image
    "jpg": "image", "jpeg": "image", "png": "image", "webp": "image", "avif": "image",
    "gif": "image", "bmp": "image", "tif": "image", "tiff": "image", "heic": "image",
    "heif": "image",
    # archive
    "zip": "archive", "rar": "archive", "7z": "archive", "tar": "archive", "gz": "archive",
    "tgz": "archive", "bz2": "archive", "tbz2": "archive", "xz": "archive", "txz": "archive",
    # subtitle
    "srt": "subtitle", "vtt": "subtitle", "ass": "subtitle", "ssa": "subtitle",
}

FAMILY_LABELS = {
    "audio": "音频",
    "video": "视频",
    "image": "图片",
    "archive": "压缩包",
    "subtitle": "字幕",
}

EXT_LABELS = {
    "jpg": "JPG", "jpeg": "JPEG", "png": "PNG", "webp": "WebP", "avif": "AVIF",
    "gif": "GIF", "bmp": "BMP", "tif": "TIFF", "tiff": "TIFF", "heic": "HEIC",
    "heif": "HEIF", "mp3": "MP3", "m4a": "M4A", "aac": "AAC", "flac": "FLAC",
    "wav": "WAV", "ogg": "OGG", "oga": "OGG", "opus": "OPUS", "wma": "WMA",
    "aiff": "AIFF", "aif": "AIFF", "ape": "APE", "ac3": "AC3", "alac": "ALAC",
    "mp4": "MP4", "mkv": "MKV", "avi": "AVI", "mov": "MOV", "webm": "WebM",
    "m4v": "M4V", "flv": "FLV", "wmv": "WMV", "mpg": "MPG", "mpeg": "MPEG",
    "ts": "TS", "m2ts": "M2TS", "rmvb": "RMVB", "rm": "RM", "3gp": "3GP",
    "ogv": "OGV", "zip": "ZIP", "rar": "RAR", "7z": "7Z", "tar": "TAR",
    "gz": "GZ", "tgz": "TAR.GZ", "bz2": "BZ2", "tbz2": "TAR.BZ2", "xz": "XZ",
    "txz": "TAR.XZ", "srt": "SRT", "vtt": "VTT", "ass": "ASS", "ssa": "SSA",
}

# Audio encoder/muxer pair per target extension.  Bitrate is controlled via
# settings; lossless codecs ignore it.
AUDIO_TARGETS: list[dict[str, str]] = [
    {"ext": "mp3", "label": "MP3", "codec": "libmp3lame", "muxer": "mp3", "codec_label": "MP3 (LAME)"},
    {"ext": "m4a", "label": "M4A", "codec": "aac", "muxer": "ipod", "codec_label": "AAC"},
    {"ext": "aac", "label": "AAC", "codec": "aac", "muxer": "adts", "codec_label": "AAC (ADTS)"},
    {"ext": "flac", "label": "FLAC", "codec": "flac", "muxer": "flac", "codec_label": "FLAC 无损"},
    {"ext": "wav", "label": "WAV", "codec": "pcm_s16le", "muxer": "wav", "codec_label": "PCM 16-bit"},
    {"ext": "ogg", "label": "OGG", "codec": "libvorbis", "muxer": "ogg", "codec_label": "Vorbis"},
    {"ext": "opus", "label": "OPUS", "codec": "libopus", "muxer": "ogg", "codec_label": "Opus"},
    {"ext": "wma", "label": "WMA", "codec": "wmav2", "muxer": "asf", "codec_label": "WMA v2"},
    {"ext": "aiff", "label": "AIFF", "codec": "pcm_s16be", "muxer": "aiff", "codec_label": "PCM 16-bit BE"},
]

VIDEO_TARGETS: list[dict[str, Any]] = [
    {"ext": "mp4", "label": "MP4", "vcodec": "libx264", "acodec": "aac", "muxer": "mp4",
     "codec_label": "H.264 + AAC", "extra": ["-movflags", "+faststart", "-pix_fmt", "yuv420p"]},
    {"ext": "mkv", "label": "MKV", "vcodec": "libx264", "acodec": "aac", "muxer": "matroska",
     "codec_label": "H.264 + AAC"},
    {"ext": "webm", "label": "WebM", "vcodec": "libvpx-vp9", "acodec": "libopus", "muxer": "webm",
     "codec_label": "VP9 + Opus", "extra": ["-b:v", "0", "-crf", "32"]},
    {"ext": "avi", "label": "AVI", "vcodec": "mpeg4", "acodec": "libmp3lame", "muxer": "avi",
     "codec_label": "MPEG-4 + MP3", "extra": ["-q:v", "5"]},
    {"ext": "mov", "label": "MOV", "vcodec": "libx264", "acodec": "aac", "muxer": "mov",
     "codec_label": "H.264 + AAC", "extra": ["-pix_fmt", "yuv420p"]},
    {"ext": "m4v", "label": "M4V", "vcodec": "libx264", "acodec": "aac", "muxer": "mp4",
     "codec_label": "H.264 + AAC", "extra": ["-pix_fmt", "yuv420p"]},
    {"ext": "flv", "label": "FLV", "vcodec": "libx264", "acodec": "aac", "muxer": "flv",
     "codec_label": "H.264 + AAC", "extra": ["-pix_fmt", "yuv420p"]},
    {"ext": "wmv", "label": "WMV", "vcodec": "wmv2", "acodec": "wmav2", "muxer": "asf",
     "codec_label": "WMV2 + WMA", "extra": ["-q:v", "5"]},
    {"ext": "ts", "label": "TS", "vcodec": "libx264", "acodec": "aac", "muxer": "mpegts",
     "codec_label": "H.264 + AAC", "extra": ["-pix_fmt", "yuv420p"]},
]

IMAGE_TARGETS: list[dict[str, Any]] = [
    {"ext": "jpg", "label": "JPG", "format": "JPEG", "codec_label": "JPEG"},
    {"ext": "png", "label": "PNG", "format": "PNG", "codec_label": "PNG 无损"},
    {"ext": "webp", "label": "WebP", "format": "WEBP", "codec_label": "WebP"},
    {"ext": "avif", "label": "AVIF", "format": "AVIF", "codec_label": "AV1 图片", "ffmpeg_muxer": "avif"},
    {"ext": "gif", "label": "GIF", "format": "GIF", "codec_label": "GIF"},
    {"ext": "bmp", "label": "BMP", "format": "BMP", "codec_label": "BMP"},
    {"ext": "tif", "label": "TIFF", "format": "TIFF", "codec_label": "TIFF"},
]

ARCHIVE_TARGETS: list[dict[str, Any]] = [
    {"ext": "zip", "label": "ZIP", "format": "zip", "codec_label": "ZIP (Deflate)"},
    {"ext": "7z", "label": "7Z", "format": "7z", "codec_label": "7-Zip (LZMA2)"},
    {"ext": "tar", "label": "TAR", "format": "tar", "codec_label": "TAR 未压缩"},
    {"ext": "tar.gz", "label": "TAR.GZ", "format": "tar.gz", "codec_label": "TAR + Gzip"},
    {"ext": "tar.bz2", "label": "TAR.BZ2", "format": "tar.bz2", "codec_label": "TAR + Bzip2"},
    {"ext": "tar.xz", "label": "TAR.XZ", "format": "tar.xz", "codec_label": "TAR + XZ"},
]

IMAGE_FFMPEG_CODECS = {
    "jpg": "mjpeg", "png": "png", "webp": "libwebp", "avif": "libaom-av1",
    "gif": "gif", "bmp": "bmp", "tif": "tiff",
}

SUBTITLE_TARGETS: list[dict[str, Any]] = [
    {"ext": "srt", "label": "SRT", "muxer": "srt", "codec": "subrip", "codec_label": "SubRip"},
    {"ext": "vtt", "label": "VTT", "muxer": "webvtt", "codec": "webvtt", "codec_label": "WebVTT"},
    {"ext": "ass", "label": "ASS", "muxer": "ass", "codec": "ass", "codec_label": "ASS"},
]

# ffmpeg -encoders output parser. Example:
#  A....D aac                  AAC (Advanced Audio Coding)
ENCODER_RE = re.compile(r"^\s*[VASDT][.A-Z]{5}\s+(\S+)", re.MULTILINE)
MUXER_RE = re.compile(r"^\s*[DE]\s+(\S+)", re.MULTILINE)


@dataclass
class Tool:
    name: str
    binary: str
    available: bool = False
    version: str = ""
    error: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "binary": self.binary,
            "available": self.available,
            "version": self.version,
            "error": self.error,
        }


class Capabilities:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ffmpeg = Tool("ffmpeg", cfg.ffmpeg)
        self.ffprobe = Tool("ffprobe", cfg.ffprobe)
        self.magick = Tool("ImageMagick", cfg.magick)
        self.sevenzip = Tool("7-Zip", cfg.sevenzip)
        self.bsdtar = Tool("bsdtar", cfg.bsdtar)
        self.encoders: set[str] = set()
        self.muxers: set[str] = set()
        self.magick_formats: dict[str, str] = {}
        self.magick_cmd = cfg.magick
        self.sevenzip_formats: set[str] = set()
        self.warnings: list[str] = []
        self.probed_at = 0.0
        self.matrix: dict[str, list[dict[str, Any]]] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self.families: dict[str, dict[str, Any]] = {}
        self._magick_is7 = True
        self.refresh()

    # ------------------------------------------------------------------ probes
    def _run(self, argv: list[str], timeout: float = 8.0) -> tuple[int, str, bool]:
        """Run a probe.

        Returns (returncode, combined_output, retryable).  retryable is true
        only for a timeout or a transient OS error; a clean non-zero exit code
        (for example a missing shared library) is not retried.
        """
        try:
            proc = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=timeout, check=False, text=True, errors="replace",
            )
            return proc.returncode, proc.stdout or "", False
        except subprocess.TimeoutExpired as exc:
            return 127, str(exc), True
        except OSError as exc:
            return 127, str(exc), True

    @staticmethod
    def _error_text(rc: int, out: str) -> str:
        text = " ".join((out or "").strip().split())
        return (f"rc={rc}" + (f": {text}" if text else ""))[:600]

    def refresh(self) -> None:
        self.warnings = []
        self.probed_at = time.time()
        for tool in self.tool_list():
            tool.available = False
            tool.version = ""
            tool.error = ""
        self.encoders = set()
        self.muxers = set()
        self.magick_formats = {}
        self.sevenzip_formats = set()
        self._probe_ffmpeg()
        self._probe_magick()
        self._probe_sevenzip()
        self._probe_bsdtar()
        self.matrix = {}
        self.sources = {}
        self.families = {}
        self._build_audio()
        self._build_video()
        self._build_image()
        self._build_archive()
        self._build_subtitle()
        self._finish_sources()

    def _probe_ffmpeg(self) -> None:
        rc, out, retryable = 0, "", False
        for attempt in range(2):
            # The second attempt gets a much longer timeout.  After a NAS
            # reboot the page cache is cold and the first 8s can expire while
            # the disk is still spinning up.
            rc, out, _ = self._run([self.cfg.ffmpeg, "-version"], timeout=8 if attempt == 0 else 20)
            if rc == 0 and "ffmpeg version" in out:
                break
            if attempt == 0:
                time.sleep(0.8)
        self.ffmpeg.available = rc == 0 and "ffmpeg version" in out
        if self.ffmpeg.available:
            first = out.splitlines()[0] if out else "ffmpeg"
            match = re.search(r"ffmpeg version (\S+)", first)
            self.ffmpeg.version = match.group(1) if match else first[:80]
        else:
            self.ffmpeg.error = self._error_text(rc, out)
            self.warnings.append("未找到可用的 ffmpeg：音视频/字幕转码不可用")
            self.ffprobe.error = "ffmpeg 不可用，未执行 ffprobe 探测"
            return
        self.encoders = self._probe_ffmpeg_list("-encoders", ENCODER_RE)
        self.muxers = self._probe_ffmpeg_list("-muxers", MUXER_RE)
        if not self.encoders:
            self.warnings.append("ffmpeg 未返回编码器列表：音频/视频目标可能为空")
        self._probe_ffprobe()

    def _probe_ffprobe(self) -> None:
        rc, out, retryable = 0, "", False
        for attempt in range(2):
            rc, out, _ = self._run([self.cfg.ffprobe, "-version"], timeout=8 if attempt == 0 else 20)
            if rc == 0 and "ffprobe version" in out:
                break
            if attempt == 0:
                time.sleep(0.8)
        self.ffprobe.available = rc == 0 and "ffprobe version" in out
        if self.ffprobe.available:
            match = re.search(r"ffprobe version (\S+)", out)
            self.ffprobe.version = match.group(1) if match else "ffprobe"
        else:
            self.ffprobe.error = self._error_text(rc, out)
            self.warnings.append("未找到 ffprobe：无法读取媒体时长/进度")

    def _probe_ffmpeg_list(self, flag: str, pattern: re.Pattern[str]) -> set[str]:
        pool: set[str] = set()
        for attempt in range(2):
            rc, out, _ = self._run([self.cfg.ffmpeg, "-hide_banner", flag], timeout=25)
            if rc == 0:
                pool = set(pattern.findall(out))
                if pool:
                    return pool
            if attempt == 0:
                time.sleep(0.3)
        return pool

    def _probe_magick(self) -> None:
        binary = shutil.which(self.cfg.magick)
        if not binary:
            binary = shutil.which("magick") or shutil.which("convert")
        if not binary:
            self.warnings.append("未找到 ImageMagick：图片转码不可用")
            return
        self.magick_cmd = binary
        self.magick.binary = binary
        rc, out, retryable = 0, "", False
        for attempt in range(2):
            rc, out, _ = self._run([binary, "-version"], timeout=8 if attempt == 0 else 20)
            if rc == 0:
                break
            if attempt == 0:
                time.sleep(0.8)
        self.magick.available = rc == 0
        if self.magick.available:
            line = out.splitlines()[0] if out else "ImageMagick"
            self.magick.version = line[:100]
            self._magick_is7 = "ImageMagick 7" in line or os.path.basename(binary) == "magick"
        else:
            self.magick.error = self._error_text(rc, out)
            self.warnings.append("ImageMagick 无法运行：图片转码不可用")
            return
        rc, out, _ = self._run([binary, "-list", "format"], timeout=15)
        if rc == 0:
            for line in out.splitlines():
                match = re.match(r"^\s*([A-Za-z0-9]+)\*?\s+(\S+)\s+([rw+-]{1,4})", line)
                if not match:
                    continue
                name = match.group(1).upper()
                mode = match.group(3)
                self.magick_formats[name] = mode

    def _probe_sevenzip(self) -> None:
        binary = shutil.which(self.cfg.sevenzip) or shutil.which("7z") or shutil.which("7zz")
        if not binary:
            self.warnings.append("未找到 7-Zip：压缩包转码不可用")
            return
        self.sevenzip.binary = binary
        rc, out, _ = self._run([binary, "i"])
        self.sevenzip.available = rc == 0
        if not self.sevenzip.available:
            self.warnings.append("7-Zip 无法运行：压缩包转码不可用")
            return
        match = re.search(r"7-Zip(?:\s+\([^)]*\))?\s+([0-9.]+)", out)
        self.sevenzip.version = match.group(1) if match else "unknown"
        for line in out.splitlines():
            # Format table rows: " 0 C...F....  Rar   rar r00 ..."
            match = re.match(r"^\s*\d+\s+[A-Za-z.+]+\s+([A-Za-z0-9]+)\s+", line)
            if match:
                self.sevenzip_formats.add(match.group(1).upper())
        # Ensure the formats we need are present (7z i table can be locale-specific).
        for fmt in ("ZIP", "7Z", "TAR", "GZIP", "BZIP2", "XZ", "RAR", "RAR5"):
            if fmt in out.upper():
                self.sevenzip_formats.add(fmt)

    def _probe_bsdtar(self) -> None:
        binary = shutil.which(self.cfg.bsdtar) or shutil.which("bsdtar")
        if not binary:
            return
        self.bsdtar.binary = binary
        rc, out, _ = self._run([binary, "--version"])
        self.bsdtar.available = rc == 0
        if self.bsdtar.available:
            line = out.splitlines()[0] if out else "bsdtar"
            match = re.search(r"bsdtar\s+([0-9.]+)", line)
            self.bsdtar.version = match.group(1) if match else line[:60]

    # --------------------------------------------------------------- builders
    def _can_ffmpeg(self, kind: str, name: str, muxer: str | None = None) -> bool:
        if not self.ffmpeg.available:
            return False
        pool = self.encoders if kind == "encoder" else self.muxers
        if name not in pool:
            return False
        if muxer and muxer not in self.muxers:
            # ffmpeg accepts "ipod" as an alias; also allow mp4.
            if not (muxer == "ipod" and "mp4" in self.muxers):
                return False
        return True

    def _can_magick_write(self, fmt: str) -> bool:
        mode = self.magick_formats.get(fmt.upper(), "")
        return "w" in mode

    def _can_magick_read(self, fmt: str) -> bool:
        mode = self.magick_formats.get(fmt.upper(), "")
        return "r" in mode or "w" in mode

    def _avif_ffmpeg(self) -> bool:
        return (
            self.ffmpeg.available
            and "libaom-av1" in self.encoders
            and "avif" in self.muxers
        )

    def _add_source(self, ext: str, family: str, targets: list[dict[str, Any]]) -> None:
        ext = ext.lower()
        if not targets:
            return
        # Deduplicate by target extension + mode.
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, Any]] = []
        for target in targets:
            key = (target["ext"], target.get("mode", ""))
            if key in seen:
                continue
            seen.add(key)
            unique.append(target)
        self.matrix[ext] = unique
        self.sources[ext] = {
            "ext": ext,
            "family": family,
            "label": EXT_LABELS.get(ext, ext.upper()),
            "targets": len(unique),
        }

    def _build_audio(self) -> None:
        targets: list[dict[str, Any]] = []
        for item in AUDIO_TARGETS:
            if self._can_ffmpeg("encoder", item["codec"], item["muxer"]):
                targets.append({
                    "ext": item["ext"],
                    "label": item["label"],
                    "family": "audio",
                    "mode": "audio",
                    "codec": item["codec_label"],
                    "note": "同格式重编码" if item["ext"] in ("mp3",) else "",
                })
        self.families["audio"] = {
            "label": FAMILY_LABELS["audio"],
            "extensions": [ext for ext, fam in SOURCE_EXTENSIONS.items() if fam == "audio"],
            "targets": targets,
        }
        for ext, family in SOURCE_EXTENSIONS.items():
            if family == "audio":
                self._add_source(ext, "audio", [dict(t, mode="audio") for t in targets])

    def _build_video(self) -> None:
        video_targets: list[dict[str, Any]] = []
        for item in VIDEO_TARGETS:
            if not self._can_ffmpeg("encoder", item["vcodec"], item["muxer"]):
                continue
            if not self._can_ffmpeg("encoder", item["acodec"]):
                continue
            video_targets.append({
                "ext": item["ext"],
                "label": item["label"],
                "family": "video",
                "mode": "video",
                "codec": item["codec_label"],
                "vcodec": item["vcodec"],
                "acodec": item["acodec"],
                "note": "",
            })
        audio_targets: list[dict[str, Any]] = []
        for item in AUDIO_TARGETS:
            if self._can_ffmpeg("encoder", item["codec"], item["muxer"]):
                audio_targets.append({
                    "ext": item["ext"],
                    "label": item["label"],
                    "family": "audio",
                    "mode": "extract_audio",
                    "codec": item["codec_label"],
                    "note": "从视频提取音频",
                })
        gif_target = None
        if self.ffmpeg.available and "gif" in self.muxers:
            gif_target = {
                "ext": "gif", "label": "GIF 动图", "family": "image", "mode": "video_gif",
                "codec": "GIF (10fps)", "note": "无声动图",
            }
        all_targets = video_targets + audio_targets + ([gif_target] if gif_target else [])
        self.families["video"] = {
            "label": FAMILY_LABELS["video"],
            "extensions": [ext for ext, fam in SOURCE_EXTENSIONS.items() if fam == "video"],
            "targets": all_targets,
        }
        for ext, family in SOURCE_EXTENSIONS.items():
            if family == "video":
                self._add_source(ext, "video", [dict(t) for t in all_targets])

    def _build_image(self) -> None:
        targets: list[dict[str, Any]] = []
        for item in IMAGE_TARGETS:
            fmt = item["format"]
            if self._can_magick_write(fmt):
                targets.append({
                    "ext": item["ext"],
                    "label": item["label"],
                    "family": "image",
                    "mode": "image_magick",
                    "codec": item["codec_label"],
                    "note": "",
                })
            elif item.get("ffmpeg_muxer") and self._avif_ffmpeg():
                targets.append({
                    "ext": item["ext"],
                    "label": item["label"],
                    "family": "image",
                    "mode": "image_ffmpeg",
                    "codec": item["codec_label"],
                    "note": "ffmpeg AV1 编码",
                })
        self.families["image"] = {
            "label": FAMILY_LABELS["image"],
            "extensions": [ext for ext, fam in SOURCE_EXTENSIONS.items() if fam == "image"],
            "targets": targets,
        }
        image_read_map = {
            "jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "avif": "AVIF",
            "gif": "GIF", "bmp": "BMP", "tif": "TIFF", "tiff": "TIFF", "heic": "HEIC",
            "heif": "HEIF",
        }
        for ext, family in SOURCE_EXTENSIONS.items():
            if family != "image":
                continue
            fmt = image_read_map.get(ext, ext.upper())
            magick_readable = self._can_magick_read(fmt)
            ffmpeg_readable = ext == "avif" and self.ffmpeg.available
            if not magick_readable and not ffmpeg_readable:
                continue
            source_targets = [dict(t) for t in targets]
            if not magick_readable:
                # AVIF input in Alpine: ImageMagick has no decoder, but ffmpeg
                # does.  Re-encode through ffmpeg for every image target.
                source_targets = []
                for item in IMAGE_TARGETS:
                    codec = IMAGE_FFMPEG_CODECS.get(item["ext"])
                    if not codec:
                        continue
                    source_targets.append({
                        "ext": item["ext"], "label": item["label"], "family": "image",
                        "mode": "image_ffmpeg", "codec": item["codec_label"],
                        "ffmpeg_codec": codec,
                        "note": "ffmpeg 解码 AVIF",
                    })
            self._add_source(ext, "image", source_targets)

    def _build_archive(self) -> None:
        if not self.sevenzip.available:
            self.families["archive"] = {
                "label": FAMILY_LABELS["archive"],
                "extensions": [ext for ext, fam in SOURCE_EXTENSIONS.items() if fam == "archive"],
                "targets": [],
                "note": "7-Zip 不可用",
            }
            return
        targets = [dict(t, mode="archive", family="archive", codec=t.get("codec_label")) for t in ARCHIVE_TARGETS]
        self.families["archive"] = {
            "label": FAMILY_LABELS["archive"],
            "extensions": [ext for ext, fam in SOURCE_EXTENSIONS.items() if fam == "archive"],
            "targets": targets,
            "note": "RAR 仅作为输入（7-Zip/bsdtar 无 RAR 编码器）；可转为 ZIP/7Z/TAR 系列",
        }
        has_rar = any(fmt in self.sevenzip_formats for fmt in ("RAR", "RAR5")) or self.bsdtar.available
        if not has_rar:
            self.warnings.append("RAR 解压不可用：当前 7-Zip/bsdtar 均不支持 RAR")
        for ext, family in SOURCE_EXTENSIONS.items():
            if family != "archive":
                continue
            if ext == "rar" and not has_rar:
                continue
            self._add_source(ext, "archive", [dict(t) for t in targets])

    def _build_subtitle(self) -> None:
        if not self.ffmpeg.available:
            self.families["subtitle"] = {
                "label": FAMILY_LABELS["subtitle"],
                "extensions": [ext for ext, fam in SOURCE_EXTENSIONS.items() if fam == "subtitle"],
                "targets": [],
            }
            return
        targets: list[dict[str, Any]] = []
        for item in SUBTITLE_TARGETS:
            if item["muxer"] in self.muxers:
                targets.append({
                    "ext": item["ext"],
                    "label": item["label"],
                    "family": "subtitle",
                    "mode": "subtitle",
                    "codec": item["codec"],
                    "codec_label": item["codec_label"],
                    "note": "",
                })
        self.families["subtitle"] = {
            "label": FAMILY_LABELS["subtitle"],
            "extensions": [ext for ext, fam in SOURCE_EXTENSIONS.items() if fam == "subtitle"],
            "targets": targets,
        }
        for ext, family in SOURCE_EXTENSIONS.items():
            if family == "subtitle":
                self._add_source(ext, "subtitle", [dict(t) for t in targets])

    def _finish_sources(self) -> None:
        # Ensure families expose only sources with at least one target.
        for family, info in self.families.items():
            available = [
                ext for ext in info["extensions"]
                if ext in self.matrix and self.matrix[ext]
            ]
            info["available_extensions"] = available
            info["sources"] = len(available)

    # ------------------------------------------------------------------ public
    def family_for(self, ext: str) -> str | None:
        return SOURCE_EXTENSIONS.get((ext or "").lower().lstrip("."))

    def ext_label(self, ext: str) -> str:
        return EXT_LABELS.get((ext or "").lower(), (ext or "").upper())

    def target(self, source_ext: str, target_ext: str) -> dict[str, Any] | None:
        for item in self.matrix.get(source_ext.lower(), []):
            if item["ext"] == target_ext.lower():
                return item
        return None

    def tool_list(self) -> list[Tool]:
        return [self.ffmpeg, self.ffprobe, self.magick, self.sevenzip, self.bsdtar]

    def public(self) -> dict[str, Any]:
        tools = [tool.public() for tool in self.tool_list()]
        return {
            "probed_at": self.probed_at,
            "tools": tools,
            "families": self.families,
            "sources": self.sources,
            "matrix": self.matrix,
            "warnings": self.warnings,
            "extensions": SOURCE_EXTENSIONS,
            "extension_labels": EXT_LABELS,
            "magick_formats": self.magick_formats,
        }

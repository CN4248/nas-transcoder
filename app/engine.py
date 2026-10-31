"""Transcoding engine: build safe command lines and run external tools.

The engine never publishes the final file.  It writes only into a private temp
directory under ``/output/.transcoder-tmp`` and returns the temp path to the job
manager, which atomically publishes it without overwriting anything.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from .capabilities import Capabilities
from .config import QUALITY_PRESETS, Config
from .security import (
    AppError,
    ValidationError,
    fingerprint,
    fingerprints_equal,
    safe_source_path,
)


class EngineError(AppError):
    status = 500
    code = "engine_error"


class CanceledError(EngineError):
    code = "canceled"


@dataclass
class Hooks:
    on_progress: Callable[[float, str | None, int | None], None] = lambda p, s, e: None
    on_log: Callable[[str], None] = lambda m: None
    on_process: Callable[[subprocess.Popen | None], None] = lambda p: None
    cancelled: Callable[[], bool] = lambda: False
    paused: Callable[[], bool] = lambda: False


@dataclass
class RunResult:
    temp_path: str
    source_sig: dict[str, Any]
    source_size: int
    duration: float | None = None
    width: int | None = None
    height: int | None = None


DURATION_RE = re.compile(r"out_time_(?:us|ms)=(\d+)")
SPEED_RE = re.compile(r"speed=\s*([0-9.]+x)")
PROGRESS_RE = re.compile(r"progress=(\w+)")


class Engine:
    def __init__(self, cfg: Config, caps: Capabilities):
        self.cfg = cfg
        self.caps = caps

    # ------------------------------------------------------------------ probes
    def probe(self, path: str, family: str) -> dict[str, Any]:
        """Read-only ffprobe inspection used at enqueue time and for progress."""
        if family not in ("audio", "video", "subtitle"):
            return {"duration": None, "has_video": family == "video", "has_audio": family == "audio"}
        if not self.caps.ffprobe.available:
            return {"duration": None, "has_video": True, "has_audio": True, "warning": "ffprobe 不可用"}
        argv = [
            self.cfg.ffprobe, "-v", "error", "-print_format", "json",
            "-show_entries", "format=duration,format_name:stream=codec_type,codec_name",
            "-i", path,
        ]
        rc, out = self._run_capture(argv, timeout=30)
        if rc != 0:
            raise ValidationError("无法解析该媒体文件（格式损坏或不受支持）")
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise ValidationError("ffprobe 返回异常") from exc
        streams = data.get("streams") or []
        has_video = any(s.get("codec_type") == "video" for s in streams)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        duration = None
        raw_duration = (data.get("format") or {}).get("duration")
        try:
            duration = float(raw_duration) if raw_duration not in (None, "N/A") else None
        except (TypeError, ValueError):
            duration = None
        if family == "audio" and not has_audio:
            raise ValidationError("文件中没有音频流")
        if family == "video" and not has_video:
            raise ValidationError("文件中没有视频流")
        return {
            "duration": duration,
            "has_video": has_video,
            "has_audio": has_audio,
            "format_name": (data.get("format") or {}).get("format_name", ""),
            "streams": streams,
        }

    def _run_capture(self, argv: list[str], timeout: float = 30) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, check=False, text=True, errors="replace",
            )
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)

    # ------------------------------------------------------------------- entry
    def run(self, job: dict[str, Any], hooks: Hooks) -> RunResult:
        source = safe_source_path(job["source_path"], self.cfg)
        target = self.caps.target(job["source_ext"], job["target"])
        if not target:
            raise ValidationError(f"当前镜像不支持 {job['source_ext']} → {job['target']}")
        source_sig = fingerprint(source)
        free = shutil.disk_usage(self.cfg.output_root).free
        if free < 256 * 1024 * 1024:
            raise EngineError("导出磁盘剩余空间不足 256 MB，已拒绝开始")
        job_tmp = os.path.join(self.cfg.tmp_dir, job["id"])
        os.makedirs(job_tmp, mode=0o700, exist_ok=True)
        temp_name = "output." + job["target"]
        temp_path = os.path.join(job_tmp, temp_name)
        options = self._options(job)
        mode = job.get("mode") or target.get("mode")
        started = time.monotonic()
        duration: float | None = None
        try:
            if mode in ("audio", "extract_audio"):
                duration = self.probe(source, "audio").get("duration")
                hooks.on_progress(1.0, None, None)
                self._run_ffmpeg_media(source, temp_path, target, "audio", options, duration, hooks)
            elif mode == "video":
                duration = self.probe(source, "video").get("duration")
                hooks.on_progress(1.0, None, None)
                self._run_ffmpeg_media(source, temp_path, target, "video", options, duration, hooks)
            elif mode == "video_gif":
                duration = self.probe(source, "video").get("duration")
                hooks.on_progress(1.0, None, None)
                self._run_video_gif(source, temp_path, options, duration, hooks)
            elif mode == "image_magick":
                self._run_image_magick(source, temp_path, options, hooks)
            elif mode == "image_ffmpeg":
                self._run_image_ffmpeg(source, temp_path, target, options, hooks)
            elif mode == "archive":
                self._run_archive(source, temp_path, target["format"], options, hooks, job["source_ext"])
            elif mode == "subtitle":
                self._run_subtitle(source, temp_path, target, hooks)
            else:
                raise ValidationError(f"未知转码模式：{mode}")
            if not os.path.isfile(temp_path) or os.path.getsize(temp_path) <= 0:
                raise EngineError("转码结束但未生成有效文件")
            after = fingerprint(source)
            if not fingerprints_equal(source_sig, after):
                raise EngineError("检测到源文件在转码期间发生变化，已停止发布输出")
            hooks.on_progress(100.0, None, 0)
            hooks.on_log(f"完成 {job['source_name']} → {os.path.basename(temp_path)}（{_fmt_duration(time.monotonic() - started)}）")
            return RunResult(
                temp_path=temp_path,
                source_sig=source_sig,
                source_size=source_sig["size"],
                duration=duration,
            )
        except CanceledError:
            raise
        except Exception:
            # Keep the temp dir for the manager to remove uniformly.
            raise

    def _options(self, job: dict[str, Any]) -> dict[str, Any]:
        options = dict(job.get("options") or {})
        preset = options.get("quality_preset")
        if preset in QUALITY_PRESETS:
            merged = dict(QUALITY_PRESETS[preset])
            merged.update({k: v for k, v in options.items() if v not in (None, "")})
            options = merged
        options.setdefault("image_quality", 88)
        options.setdefault("audio_bitrate", "192k")
        options.setdefault("video_crf", 23)
        options.setdefault("video_preset", "veryfast")
        options.setdefault("archive_level", 5)
        options.setdefault("preserve_metadata", True)
        return options

    # ------------------------------------------------------------- ffmpeg core
    def _run_ffmpeg_media(
        self,
        source: str,
        output: str,
        target: dict[str, Any],
        kind: str,
        options: dict[str, Any],
        duration: float | None,
        hooks: Hooks,
    ) -> None:
        argv = [
            self.cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-progress", "pipe:1", "-i", source,
        ]
        if kind == "audio":
            argv += ["-vn", "-map", "0:a:0"]
            argv += self._audio_codec_args(target["ext"], options)
            if options.get("preserve_metadata", True):
                argv += ["-map_metadata", "0"]
            argv += [output]
        else:
            argv += ["-map", "0:v:0", "-map", "0:a:0?"]
            argv += self._video_codec_args(target, options)
            argv += ["-sn", "-dn"]
            if options.get("preserve_metadata", True):
                argv += ["-map_metadata", "0"]
            argv += [output]
        self._run_ffmpeg(argv, duration, hooks)

    def _audio_codec_args(self, ext: str, options: dict[str, Any]) -> list[str]:
        bitrate = str(options.get("audio_bitrate", "192k"))
        if ext == "mp3":
            return ["-c:a", "libmp3lame", "-b:a", bitrate]
        if ext in ("m4a", "aac"):
            return ["-c:a", "aac", "-b:a", bitrate]
        if ext == "flac":
            return ["-c:a", "flac"]
        if ext == "wav":
            return ["-c:a", "pcm_s16le"]
        if ext == "ogg":
            return ["-c:a", "libvorbis", "-q:a", "5"]
        if ext == "opus":
            return ["-c:a", "libopus", "-b:a", bitrate]
        if ext == "wma":
            return ["-c:a", "wmav2", "-b:a", bitrate]
        if ext == "aiff":
            return ["-c:a", "pcm_s16be"]
        raise ValidationError(f"不支持的音频目标：{ext}")

    def _video_codec_args(self, target: dict[str, Any], options: dict[str, Any]) -> list[str]:
        vcodec = target.get("vcodec") or "libx264"
        acodec = target.get("acodec") or "aac"
        crf = str(options.get("video_crf", 23))
        preset = str(options.get("video_preset", "veryfast"))
        bitrate = str(options.get("audio_bitrate", "192k"))
        args = ["-c:v", vcodec]
        extra = target.get("extra") or []
        if target["ext"] in ("mp4", "mkv", "mov", "m4v", "flv", "ts"):
            args += ["-preset", preset, "-crf", crf]
        if target["ext"] == "webm":
            args += ["-crf", "32", "-b:v", "0"]
        args += list(extra)
        if acodec == "aac":
            args += ["-c:a", "aac", "-b:a", bitrate]
        elif acodec == "libopus":
            args += ["-c:a", "libopus", "-b:a", "128k"]
        elif acodec == "libmp3lame":
            args += ["-c:a", "libmp3lame", "-b:a", bitrate]
        elif acodec == "wmav2":
            args += ["-c:a", "wmav2", "-b:a", bitrate]
        else:
            args += ["-c:a", acodec]
        return args

    def _run_video_gif(
        self,
        source: str,
        output: str,
        options: dict[str, Any],
        duration: float | None,
        hooks: Hooks,
    ) -> None:
        vf = (
            "fps=10,scale=640:-2:flags=lanczos,split[s0][s1];"
            "[s0]palettegen=max_colors=192[p];[s1][p]paletteuse=dither=bayer"
        )
        argv = [
            self.cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-progress", "pipe:1", "-i", source,
            "-an", "-vf", vf, "-loop", "0", output,
        ]
        self._run_ffmpeg(argv, duration, hooks)

    def _run_image_magick(self, source: str, output: str, options: dict[str, Any], hooks: Hooks) -> None:
        argv = [self.caps.magick_cmd, source, "-auto-orient", "-quality", str(options.get("image_quality", 88))]
        if not options.get("preserve_metadata", True):
            argv.append("-strip")
        argv.append(output)
        hooks.on_progress(10.0, None, None)
        self._run_simple(argv, hooks, label="ImageMagick")
        hooks.on_progress(95.0, None, None)

    def _run_image_ffmpeg(self, source: str, output: str, target: dict[str, Any], options: dict[str, Any], hooks: Hooks) -> None:
        codec = target.get("ffmpeg_codec") or "libaom-av1"
        argv = [
            self.cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", source, "-frames:v", "1", "-c:v", codec,
        ]
        if codec == "libaom-av1":
            argv += ["-still-picture", "1", "-crf", str(min(50, max(1, int(options.get("video_crf", 23)))))]
        elif codec == "mjpeg":
            argv += ["-q:v", str(max(2, 100 - int(options.get("image_quality", 88))))]
        elif codec == "libwebp":
            argv += ["-quality", str(int(options.get("image_quality", 88)))]
        argv.append(output)
        hooks.on_progress(10.0, None, None)
        self._run_simple(argv, hooks, label="ffmpeg (AVIF)")
        hooks.on_progress(95.0, None, None)

    def _run_subtitle(self, source: str, output: str, target: dict[str, Any], hooks: Hooks) -> None:
        argv = [
            self.cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", source, "-map", "0:s:0", "-c:s", target.get("codec", "subrip"), output,
        ]
        hooks.on_progress(5.0, None, None)
        self._run_simple(argv, hooks, label="ffmpeg (subtitle)")
        hooks.on_progress(95.0, None, None)

    def _run_ffmpeg(self, argv: list[str], duration: float | None, hooks: Hooks) -> None:
        proc = self._spawn(argv)
        hooks.on_process(proc)
        if proc.stdout is None or proc.stderr is None:
            raise EngineError("无法创建转码管道")
        errors: deque[str] = deque(maxlen=20)
        stderr_thread = threading.Thread(target=_drain_stderr, args=(proc.stderr, errors), daemon=True)
        stderr_thread.start()
        last_speed: float | None = None
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("speed="):
                    last_speed = _speed_from_line(line)
                elif line.startswith("out_time_"):
                    match = DURATION_RE.search(line)
                    if match:
                        # ffmpeg's historical "out_time_ms" value is in
                        # microseconds despite the name; out_time_us is explicit.
                        micro = int(match.group(1))
                        seconds = micro / 1_000_000
                        if duration and duration > 0:
                            percent = max(0.5, min(99.4, seconds / duration * 100))
                            eta = int(max(0, duration - seconds) / max(0.05, last_speed)) if last_speed else None
                            hooks.on_progress(percent, f"{last_speed:.2f}x" if last_speed else None, eta)
        finally:
            proc.wait()
            stderr_thread.join(timeout=2)
            hooks.on_process(None)
        if proc.returncode != 0:
            if hooks.cancelled():
                raise CanceledError("任务已取消")
            detail = " | ".join(list(errors)[-6:]) or f"退出码 {proc.returncode}"
            raise EngineError(f"ffmpeg 失败：{detail[:800]}")
        if hooks.cancelled():
            raise CanceledError("任务已取消")

    def _run_simple(self, argv: list[str], hooks: Hooks, label: str = "tool") -> None:
        proc = self._spawn(argv)
        hooks.on_process(proc)
        if proc.stdout is None or proc.stderr is None:
            raise EngineError("无法创建进程管道")
        errors: deque[str] = deque(maxlen=20)
        stderr_thread = threading.Thread(target=_drain_stderr, args=(proc.stderr, errors), daemon=True)
        stderr_thread.start()
        try:
            for _ in proc.stdout:
                pass
            proc.wait()
        finally:
            stderr_thread.join(timeout=2)
            hooks.on_process(None)
        if proc.returncode != 0:
            if hooks.cancelled():
                raise CanceledError("任务已取消")
            detail = " | ".join(list(errors)[-6:]) or f"退出码 {proc.returncode}"
            raise EngineError(f"{label} 失败：{detail[:800]}")
        if hooks.cancelled():
            raise CanceledError("任务已取消")

    def _spawn(self, argv: list[str]) -> subprocess.Popen:
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError as exc:
            raise EngineError(f"无法启动外部工具：{exc}") from exc
        # on_process is called by the manager through a wrapper closure; the
        # caller passes the current process with hooks.on_process in _run_*.
        return proc

    # --------------------------------------------------------------- archives
    def _run_archive(self, source: str, output: str, target_format: str, options: dict[str, Any], hooks: Hooks, source_ext: str = "") -> None:
        if not self.caps.sevenzip.available:
            raise EngineError("7-Zip 不可用，无法处理压缩包")
        extract_dir = os.path.join(os.path.dirname(output), "extract")
        os.makedirs(extract_dir, mode=0o700, exist_ok=True)
        hooks.on_log("校验归档目录结构")
        use_bsdtar = source_ext == "rar" and self.caps.bsdtar.available
        entries, total_size = self._list_archive_bsdtar(source) if use_bsdtar else self._list_archive(source)
        archive_size = max(1, os.path.getsize(source))
        if entries > self.cfg.max_archive_entries:
            raise ValidationError(f"压缩包条目过多（{entries} > {self.cfg.max_archive_entries}）")
        if total_size > self.cfg.max_archive_gib * 1024**3:
            raise ValidationError(f"解压后体积超过 {self.cfg.max_archive_gib} GiB 上限")
        if total_size > 1024**3 and total_size / archive_size > self.cfg.max_archive_ratio:
            raise ValidationError("压缩比异常，疑似解压炸弹，已拒绝")
        free = shutil.disk_usage(self.cfg.output_root).free
        if free < total_size + 256 * 1024 * 1024:
            raise EngineError("导出磁盘空间不足以安全解压此压缩包")
        hooks.on_progress(4.0, None, None)
        if use_bsdtar:
            argv = [
                self.caps.bsdtar.binary, "-xf", source, "-C", extract_dir,
                "--no-same-owner",
            ]
            self._run_tool_with_progress(argv, hooks, label="bsdtar 解压 RAR", cap=35.0)
        else:
            argv = [
                self.caps.sevenzip.binary, "x", "-y", "-bd", "-bso0", "-bsp0",
                f"-o{extract_dir}", source,
            ]
            self._run_tool_with_progress(argv, hooks, label="7-Zip 解压", cap=35.0)
        hooks.on_progress(40.0, None, None)
        extracted_size = self._verify_extracted(extract_dir, hooks)
        if extracted_size > 1024**3 and extracted_size / archive_size > self.cfg.max_archive_ratio:
            raise ValidationError("解压后膨胀比异常，疑似解压炸弹，已拒绝")
        entries_list = sorted(os.listdir(extract_dir))
        if not entries_list:
            raise ValidationError("压缩包为空，无法生成目标归档")
        hooks.on_log(f"重新打包为 {target_format}（{entries} 个条目）")
        if target_format == "zip":
            self._zip_directory(extract_dir, output, int(options.get("archive_level", 5)), hooks)
        elif target_format.startswith("tar"):
            self._tar_directory(extract_dir, output, target_format, hooks)
        elif target_format == "7z":
            argv = [
                self.caps.sevenzip.binary, "a", "-t7z", f"-mx={int(options.get('archive_level', 5))}",
                "-bso0", "-bsp0", output, *entries_list,
            ]
            self._run_tool_with_progress(argv, hooks, label="7-Zip 打包", cap=95.0, cwd=extract_dir)
        else:
            raise ValidationError(f"不支持的归档目标：{target_format}")
        hooks.on_progress(98.0, None, None)

    def _list_archive(self, source: str) -> tuple[int, int]:
        argv = [self.caps.sevenzip.binary, "l", "-slt", "-ba", source]
        rc, out = self._run_capture(argv, timeout=120)
        if rc != 0:
            raise ValidationError("无法读取压缩包（损坏、加密或缺少解码器）")
        # 7z -slt emits blocks separated by blank lines, each with key = value.
        entries = 0
        total = 0
        current: dict[str, str] = {}
        blocks: list[dict[str, str]] = []
        for line in out.splitlines():
            if not line.strip():
                if current:
                    blocks.append(current)
                    current = {}
                continue
            if " = " in line:
                key, value = line.split(" = ", 1)
                current[key.strip()] = value.strip()
        if current:
            blocks.append(current)
        for block in blocks:
            if "Path" not in block:
                continue
            rel = _normalise_archive_path(block["Path"])
            if rel is None:
                raise ValidationError(f"压缩包内存在危险路径：{block.get('Path', '')[:160]}")
            if block.get("Symbolic Link") or block.get("Hard Link"):
                raise ValidationError("压缩包包含符号链接/硬链接，出于安全考虑已拒绝")
            attrs = block.get("Attributes", "")
            if "L" in attrs or attrs.lower().startswith("l"):
                raise ValidationError("压缩包包含符号链接，出于安全考虑已拒绝")
            if block.get("Encrypted", "") in ("+", "1"):
                raise ValidationError("加密压缩包不支持自动解压")
            if block.get("Folder") in ("-", ""):
                entries += 1
                try:
                    total += int(block.get("Size", "0") or 0)
                except ValueError:
                    pass
        if entries == 0:
            raise ValidationError("压缩包为空或没有可读取的文件")
        return entries, total

    def _list_archive_bsdtar(self, source: str) -> tuple[int, int]:
        argv = [self.caps.bsdtar.binary, "-tf", source]
        rc, out = self._run_capture(argv, timeout=120)
        if rc != 0:
            raise ValidationError("无法读取压缩包（损坏、加密或缺少解码器）")
        for name in out.splitlines():
            if not name.strip():
                continue
            if _normalise_archive_path(name) is None:
                raise ValidationError(f"压缩包内存在危险路径：{name[:160]}")
        argv = [self.caps.bsdtar.binary, "-tvf", source]
        rc, out = self._run_capture(argv, timeout=120)
        if rc != 0:
            raise ValidationError("无法读取压缩包详情")
        entries = 0
        total = 0
        for line in out.splitlines():
            if not line.strip():
                continue
            first = line[:1]
            if first in ("l", "h", "p", "c", "b"):
                raise ValidationError("压缩包包含符号链接或特殊文件，出于安全考虑已拒绝")
            match = re.match(r"^[-d][rwxStTs-]{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+", line)
            if not match:
                # Some bsdtar builds omit owner/group columns; fall back to
                # counting the entry without trusting its size.
                entries += 1
                continue
            entries += 1
            total += int(match.group(1) or 0)
        if entries == 0:
            raise ValidationError("压缩包为空或没有可读取的文件")
        return entries, total

    def _wait_if_paused(self, hooks: Hooks) -> None:
        while hooks.paused():
            if hooks.cancelled():
                raise CanceledError("任务已取消")
            time.sleep(0.2)
        if hooks.cancelled():
            raise CanceledError("任务已取消")

    def _verify_extracted(self, root: str, hooks: Hooks | None = None) -> int:
        count = 0
        total = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in dirnames + filenames:
                if hooks:
                    self._wait_if_paused(hooks)
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                if os.path.islink(full):
                    raise ValidationError("归档解压产生符号链接，已拒绝")
                if not (os.path.isfile(full) or os.path.isdir(full)):
                    raise ValidationError("归档解压产生特殊文件，已拒绝")
                if os.path.isfile(full):
                    count += 1
                    total += st.st_size
                if count > self.cfg.max_archive_entries:
                    raise ValidationError("归档解压文件数超限")
        if count == 0:
            raise ValidationError("归档解压后没有文件")
        if total > self.cfg.max_archive_gib * 1024**3:
            raise ValidationError("归档解压后体积超限")
        return total

    def _zip_directory(self, root: str, output: str, level: int, hooks: Hooks) -> None:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=max(0, min(9, level))) as zf:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                for name in sorted(filenames):
                    self._wait_if_paused(hooks)
                    full = os.path.join(dirpath, name)
                    if os.path.islink(full):
                        continue
                    rel = os.path.relpath(full, root)
                    info = zipfile.ZipInfo(rel)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    st = os.stat(full)
                    info.date_time = time.localtime(st.st_mtime)[:6]
                    info.external_attr = (st.st_mode & 0xFFFF) << 16
                    with open(full, "rb") as src, zf.open(info, "w") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)

    def _tar_directory(self, root: str, output: str, target_format: str, hooks: Hooks) -> None:
        mode = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}.get(target_format)
        if not mode:
            raise ValidationError(f"不支持的 TAR 目标：{target_format}")
        with tarfile.open(output, mode, format=tarfile.PAX_FORMAT) as tf:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                for name in sorted(dirnames + filenames):
                    self._wait_if_paused(hooks)
                    full = os.path.join(dirpath, name)
                    if os.path.islink(full):
                        continue
                    rel = os.path.relpath(full, root)
                    tf.add(full, arcname=rel, recursive=False, filter=_tar_filter)

    def _run_tool_with_progress(
        self, argv: list[str], hooks: Hooks, label: str, cap: float = 90.0, cwd: str | None = None
    ) -> None:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            start_new_session=True, cwd=cwd,
        )
        if proc.stdout is None or proc.stderr is None:
            raise EngineError("无法创建进程管道")
        hooks.on_process(proc)
        errors: deque[str] = deque(maxlen=20)
        stderr_thread = threading.Thread(target=_drain_stderr, args=(proc.stderr, errors), daemon=True)
        stderr_thread.start()
        while proc.poll() is None:
            if hooks.cancelled():
                _terminate(proc)
                break
            time.sleep(0.25)
            hooks.on_progress(min(cap, 4.0 + (cap - 4.0) * 0.45), None, None)
        proc.wait()
        stderr_thread.join(timeout=2)
        hooks.on_process(None)
        if proc.returncode != 0:
            if hooks.cancelled():
                raise CanceledError("任务已取消")
            detail = " | ".join(list(errors)[-6:]) or f"退出码 {proc.returncode}"
            raise EngineError(f"{label} 失败：{detail[:800]}")

    # ------------------------------------------------------------- subprocess
def _drain_stderr(stream, errors: deque[str]) -> None:
    try:
        for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                errors.append(line)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _speed_from_line(line: str) -> float | None:
    match = SPEED_RE.search(line)
    if not match:
        return None
    try:
        return float(match.group(1).rstrip("x"))
    except ValueError:
        return None


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _normalise_archive_path(path: str) -> str | None:
    path = path.replace("\\", "/").strip()
    if not path:
        return None
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return None
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if info.issym() or info.islnk() or info.isdev():
        return None
    return info


def _terminate(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, 15)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass


# The manager needs a handle to the process for pause/cancel.  Rather than
# duplicating process management, the engine exposes a small adapter that tools
# can use in tests and the manager can monkey-patch.
def make_hooks(
    on_progress: Callable[[float, str | None, int | None], None],
    on_log: Callable[[str], None],
    cancelled: Callable[[], bool],
    process_sink: Callable[[subprocess.Popen | None], None],
    paused: Callable[[], bool] = lambda: False,
) -> Hooks:
    return Hooks(
        on_progress=on_progress, on_log=on_log, on_process=process_sink,
        cancelled=cancelled, paused=paused,
    )

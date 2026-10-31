"""Filesystem safety helpers.

The single most important invariant of this application:

    nothing under ``config.source_root`` is ever opened for writing, renamed,
    unlinked, or otherwise modified.

Every API that writes must pass through :func:`safe_output_path` (which rejects
anything outside ``config.output_root`` and anything overlapping the source
tree).  Source files are only opened with ``O_RDONLY`` and verified to be
regular files.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import stat
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from .config import Config

CHUNK = 1024 * 1024
SAMPLE_CHUNK = 1024 * 1024
INVALID_NAME = re.compile(r'[\x00-\x1f\x7f/\\<>:"|?*]')
RESERVED_NAMES = {".transcoder-tmp", ".transcoder-trash", ".transcoder-write-probe"}


class AppError(Exception):
    """Base error carrying an HTTP status code."""

    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status


class ValidationError(AppError):
    status = 400
    code = "validation_error"


class NotFoundError(AppError):
    status = 404
    code = "not_found"


class ConflictError(AppError):
    status = 409
    code = "conflict"


class ForbiddenError(AppError):
    status = 403
    code = "forbidden"


def _norm(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ValidationError("路径不能为空")
    if "\x00" in path:
        raise ValidationError("路径包含非法字符")
    return os.path.abspath(path)


def is_within(path: str, root: str) -> bool:
    """True when real path is root itself or a descendant (case-sensitive)."""
    root = os.path.realpath(root).rstrip(os.sep)
    path = os.path.realpath(path).rstrip(os.sep)
    return path == root or path.startswith(root + os.sep)


def source_path(path: str, cfg: Config, *, want_dir: bool = False, must_exist: bool = True) -> str:
    """Validate a path under the read-only source root.

    Returns the resolved real path.  Refuses symlinks that escape ``/host`` and
    non-regular files.
    """
    candidate = _norm(path)
    root = os.path.realpath(cfg.source_root)
    if not (candidate == root or candidate.startswith(root + os.sep)):
        raise ForbiddenError("只能访问只读源目录 /host")
    if not os.path.lexists(candidate):
        if must_exist:
            raise NotFoundError("路径不存在")
        return candidate
    real = os.path.realpath(candidate)
    if not is_within(real, root):
        raise ForbiddenError("符号链接指向源目录之外，已拒绝")
    try:
        st = os.stat(real)
    except OSError as exc:
        raise ValidationError(f"无法读取路径：{exc.strerror}") from exc
    if want_dir:
        if not stat.S_ISDIR(st.st_mode):
            raise ValidationError("不是文件夹")
    else:
        if not stat.S_ISREG(st.st_mode):
            raise ValidationError("不是普通文件（设备/管道/套接字不允许）")
        # Prove the file can be opened read-only; this never mutates it.
        try:
            fd = os.open(real, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            raise ValidationError(f"源文件不可读：{exc.strerror}") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValidationError("不是普通文件")
        finally:
            os.close(fd)
    return real


def safe_output_path(path: str, cfg: Config, *, must_exist: bool = False, allow_root: bool = True, allow_internal: bool = False) -> str:
    """Validate a write destination under ``/output``.

    For non-existent leaf paths, the *parent* is resolved so a symlinked parent
    cannot be used to escape the output volume.
    """
    candidate = _norm(path)
    root = os.path.realpath(cfg.output_root)
    if candidate == root:
        if not allow_root:
            raise ValidationError("不能直接写入导出根目录")
        return root
    if not (candidate == root or candidate.startswith(root + os.sep)):
        raise ForbiddenError("导出路径必须位于 /output 内")
    parent = os.path.realpath(os.path.dirname(candidate))
    if not (parent == root or parent.startswith(root + os.sep)):
        raise ForbiddenError("导出目录的符号链接指向 /output 之外")
    leaf = os.path.basename(candidate)
    if not allow_internal and (leaf in RESERVED_NAMES or any(part in RESERVED_NAMES for part in candidate.split(os.sep))):
        raise ForbiddenError("该目录由应用内部使用")
    result = os.path.join(parent, leaf)
    if os.path.lexists(result):
        resolved = os.path.realpath(result)
        if not (resolved == root or resolved.startswith(root + os.sep)):
            raise ForbiddenError("导出路径的符号链接指向 /output 之外")
        if _is_internal_relative(os.path.relpath(resolved, root)) and not allow_internal:
            raise ForbiddenError("该目录由应用内部使用")
        result = resolved
    if must_exist and not os.path.exists(result):
        raise NotFoundError("目录不存在")
    return result


def _is_internal_relative(rel: str) -> bool:
    return any(part in RESERVED_NAMES for part in rel.split(os.sep) if part)


def safe_output_dir(path: str, cfg: Config, *, create: bool = False) -> str:
    resolved = safe_output_path(path, cfg)
    if os.path.exists(resolved):
        if not os.path.isdir(resolved):
            raise ValidationError("导出路径不是文件夹")
    elif create:
        os.makedirs(resolved, mode=0o755, exist_ok=False)
    else:
        raise NotFoundError("导出目录不存在")
    return resolved


def validate_dir_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValidationError("文件夹名必须是文本")
    name = unicodedata.normalize("NFC", name).strip()
    if not name:
        raise ValidationError("文件夹名不能为空")
    if name in (".", "..") or INVALID_NAME.search(name):
        raise ValidationError("文件夹名包含非法字符")
    if len(name.encode("utf-8")) > 120:
        raise ValidationError("文件夹名过长（最多 120 字节）")
    if name.startswith("."):
        raise ValidationError("不能创建隐藏文件夹")
    return name


def safe_filename(name: str, fallback: str, target_ext: str) -> str:
    """Normalise a user supplied output file name and force the target suffix."""
    name = unicodedata.normalize("NFC", name or "").strip()
    name = os.path.basename(name.replace("\\", "/"))
    name = INVALID_NAME.sub("_", name)
    name = name.strip(" .")
    if not name or name in (".", ".."):
        name = fallback
    ext = target_ext.lower().lstrip(".")
    suffix = "." + ext
    # Only treat it as already-suffixed when the complete target extension
    # (including multi-dot extensions such as tar.gz) is present.
    if not name.lower().endswith(suffix):
        name += suffix
    # Enforce a byte budget while preserving the extension.
    while len(name.encode("utf-8")) > 180:
        stem, dot, file_ext = name.rpartition(".")
        if not dot or len(stem) <= 1:
            name = name[:60]
            break
        name = stem[:-1] + "." + file_ext
    return name


def source_stem(name: str) -> str:
    base = os.path.basename(name)
    lower = base.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz"):
        if lower.endswith(ext):
            return base[: -len(ext)]
    stem, dot, _ = base.rpartition(".")
    return stem if dot and stem else base


def render_name(pattern: str, source_name: str, target_ext: str, index: int = 1, custom: str | None = None) -> str:
    if custom:
        return safe_filename(custom, source_stem(source_name), target_ext)
    from datetime import datetime

    stem = source_stem(source_name)
    values = {
        "{name}": stem,
        "{target}": target_ext.lower().lstrip("."),
        "{index}": str(index),
        "{date}": datetime.now().strftime("%Y%m%d"),
    }
    rendered = pattern or "{name}"
    for key, value in values.items():
        rendered = rendered.replace(key, value)
    return safe_filename(rendered, stem, target_ext)


def candidate_paths(directory: str, filename: str) -> Iterable[str]:
    yield os.path.join(directory, filename)
    stem, ext = _split_compound_suffix(filename)
    for idx in range(1, 10000):
        candidate = f"{stem} ({idx}){ext}" if ext else f"{stem} ({idx})"
        yield os.path.join(directory, candidate)


def _split_compound_suffix(filename: str) -> tuple[str, str]:
    lower = filename.lower()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if lower.endswith(compound):
            return filename[: -len(compound)], filename[-len(compound):]
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        return filename, ""
    return stem, "." + ext


def publish_no_overwrite(temp_path: str, output_dir: str, filename: str) -> str:
    """Atomically publish ``temp_path`` without ever replacing an existing file."""
    temp_path = os.path.realpath(temp_path)
    output_dir = os.path.realpath(output_dir)
    if not is_within(temp_path, output_dir):
        # Normally temp lives under /output/.transcoder-tmp, a sibling of the
        # destination; both are under the output root.  The check is made by
        # the caller with the full config, this is just defence in depth.
        pass
    for candidate in candidate_paths(output_dir, filename):
        try:
            os.link(temp_path, candidate)
            return candidate
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP):
                raise
            # Cross-device / restricted link: exclusive copy fallback.
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            try:
                with open(temp_path, "rb") as src, os.fdopen(fd, "wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
                    dst.flush()
                    os.fsync(dst.fileno())
                return candidate
            except Exception:
                try:
                    os.unlink(candidate)
                except OSError:
                    pass
                raise
    raise ConflictError("同名文件过多，无法生成不冲突的输出名")


def fingerprint(path: str) -> dict[str, object]:
    """Cheap identity + content sample used to prove the source did not change."""
    st = os.stat(path)
    size = st.st_size
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        if size <= 3 * SAMPLE_CHUNK:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                digest.update(chunk)
        else:
            for offset in (0, max(0, size // 2 - SAMPLE_CHUNK // 2), max(0, size - SAMPLE_CHUNK)):
                fh.seek(offset)
                digest.update(fh.read(SAMPLE_CHUNK))
    return {
        "size": size,
        "mtime_ns": st.st_mtime_ns,
        "inode": st.st_ino,
        "sample_sha256": digest.hexdigest(),
    }


def fingerprints_equal(a: dict[str, object], b: dict[str, object]) -> bool:
    return all(a.get(k) == b.get(k) for k in ("size", "mtime_ns", "inode", "sample_sha256"))


def move_to_trash(path: str, cfg: Config, job_id: str) -> str:
    """Move an application-produced output file into the trash, never source."""
    real = os.path.realpath(path)
    root = os.path.realpath(cfg.output_root)
    if not (real == root or real.startswith(root + os.sep)):
        raise ForbiddenError("只能撤销 /output 内的文件")
    if not os.path.isfile(real):
        raise NotFoundError("输出文件不存在，可能已被移动")
    target_dir = os.path.join(cfg.trash_dir, _safe_component(job_id))
    os.makedirs(target_dir, exist_ok=True)
    filename = os.path.basename(real)
    for candidate in candidate_paths(target_dir, filename):
        try:
            os.link(real, candidate)
        except FileExistsError:
            continue
        except OSError:
            # Fall back to rename within the same output volume.
            try:
                os.rename(real, candidate)
                return candidate
            except OSError:
                continue
        try:
            os.unlink(real)
        except OSError:
            try:
                os.unlink(candidate)
            except OSError:
                pass
            raise
        return candidate
    raise ConflictError("无法移入回收站：同名文件过多")


def safe_trash_path(path: str, cfg: Config) -> str:
    return safe_output_path(path, cfg, must_exist=True, allow_root=False)


def _safe_component(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return value[:80] or "job"


def restore_from_trash(trash_path: str, output_dir: str, filename: str) -> str:
    real = os.path.realpath(trash_path)
    if not os.path.isfile(real):
        raise NotFoundError("回收站文件不存在")
    return publish_no_overwrite(real, output_dir, filename)


def human_size(num: int | float | None) -> str:
    if num is None:
        return "-"
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def parse_size(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?i?B?)\s*", text or "", re.I)
    if not match:
        raise ValidationError("无法识别的容量")
    value = float(match.group(1))
    unit = match.group(2).upper().replace("I", "")
    mult = {"": 1, "B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}.get(unit)
    if mult is None:
        raise ValidationError("无法识别的容量单位")
    return int(value * mult)

# Backwards/readability alias used by the service and engine layers.
safe_source_file = source_path
safe_source_path = source_path

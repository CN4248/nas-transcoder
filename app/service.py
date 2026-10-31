"""Application service: validation, file browsing, job creation, undo."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from typing import Any

from .capabilities import Capabilities
from .config import APP_NAME, APP_VERSION, DEFAULT_SETTINGS, QUALITY_PRESETS, Config
from .db import Database, JOB_FIELDS, new_id, row_to_job, validate_setting
from .engine import Engine
from .jobs import JobManager
from .security import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
    candidate_paths,
    human_size,
    is_within,
    move_to_trash,
    publish_no_overwrite,
    render_name,
    restore_from_trash,
    safe_filename,
    safe_output_dir,
    safe_output_path,
    safe_source_path,
    source_stem,
    validate_dir_name,
)

QUALITY_KEYS = {
    "quality_preset", "image_quality", "audio_bitrate", "video_crf",
    "video_preset", "archive_level", "preserve_metadata",
}
READ_ONLY_FILTERS = {"audio", "video", "image", "archive", "subtitle"}
MAX_BATCH = 500


class Service:
    def __init__(self, cfg: Config, db: Database, caps: Capabilities, engine: Engine, jobs: JobManager):
        self.cfg = cfg
        self.db = db
        self.caps = caps
        self.engine = engine
        self.jobs = jobs
        self.started_at = time.time()

    # ------------------------------------------------------------------ startup
    def bootstrap(self) -> dict[str, Any]:
        events = self.db.recent_events(300)
        latest = self.db.latest_action()
        latest_public = self._action_public(latest) if latest else None
        return {
            "ok": True,
            "app": {
                "name": APP_NAME,
                "version": APP_VERSION,
                "source_root": self.cfg.source_root,
                "output_root": self.cfg.output_root,
                "started_at": self.started_at,
            },
            "settings": self.db.settings(),
            "jobs": self._public_jobs(self.db.list_jobs()),
            "counts": self.db.count_jobs(),
            "queue_paused": self.jobs.queue_paused,
            "capabilities": self.caps.public(),
            "logs": events,
            "last_log_id": events[-1]["id"] if events else 0,
            "latest_action": latest_public,
            "actions": [self._action_public(a) for a in self.db.list_actions(15)],
            "disk": self._disk(),
            "limits": {"max_batch": MAX_BATCH},
        }

    def state(self, after_log: int = 0) -> dict[str, Any]:
        if after_log:
            events = self.db.events(after_log, 500)
        else:
            events = self.db.recent_events(200)
        latest = self.db.latest_action()
        return {
            "ok": True,
            "jobs": self._public_jobs(self.db.list_jobs()),
            "counts": self.db.count_jobs(),
            "queue_paused": self.jobs.queue_paused,
            "settings": self.db.settings(),
            "logs": events,
            "last_log_id": events[-1]["id"] if events else after_log,
            "latest_action": self._action_public(latest) if latest else None,
            "disk": self._disk(),
        }

    def _disk(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(self.cfg.output_root)
            return {"total": usage.total, "used": usage.used, "free": usage.free}
        except OSError:
            return {"total": 0, "used": 0, "free": 0}

    def _public_jobs(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for job in jobs:
            item = dict(job)
            item["target_label"] = self.caps.ext_label(job.get("target", ""))
            item["source_label"] = self.caps.ext_label(job.get("source_ext", ""))
            item["output_size_human"] = human_size(job.get("output_size"))
            item["source_size_human"] = human_size(job.get("source_size"))
            result.append(item)
        return result

    # -------------------------------------------------------------------- logs
    def logs(self, after: int = 0, limit: int = 300) -> list[dict[str, Any]]:
        if after:
            return self.db.events(after, limit)
        return self.db.recent_events(limit)

    # ---------------------------------------------------------------- browsing
    def list_source(
        self,
        path: str,
        *,
        q: str = "",
        kind: str = "all",
        show_hidden: bool = False,
        offset: int = 0,
        limit: int = 300,
    ) -> dict[str, Any]:
        requested = os.path.abspath(path or self.cfg.source_root)
        real = safe_source_path(requested, self.cfg, want_dir=True)
        if not is_within(real, self.cfg.source_root):
            raise ForbiddenError("目录不在源根目录内")
        show_hidden = bool(show_hidden)
        q = (q or "").casefold().strip()[:120]
        kind = kind if kind in READ_ONLY_FILTERS else "all"
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 300), 1000))
        entries: list[dict[str, Any]] = []
        try:
            iterator = os.scandir(real)
        except OSError as exc:
            raise ValidationError(f"无法读取目录：{exc.strerror}") from exc
        with iterator:
            for entry in iterator:
                name = entry.name
                if not show_hidden and name.startswith("."):
                    continue
                if q and q not in name.casefold():
                    continue
                full = os.path.join(requested, name)
                info: dict[str, Any] = {
                    "name": name,
                    "path": full,
                    "is_dir": False,
                    "is_file": False,
                    "is_symlink": entry.is_symlink(),
                    "size": 0,
                    "mtime": None,
                    "family": None,
                    "ext": "",
                    "label": "",
                    "supported": False,
                    "targets": 0,
                    "error": None,
                }
                try:
                    if entry.is_symlink():
                        try:
                            target = os.path.realpath(full)
                            if not is_within(target, self.cfg.source_root):
                                info["error"] = "符号链接指向源目录之外"
                                entries.append(info)
                                continue
                        except OSError:
                            info["error"] = "符号链接已损坏"
                            entries.append(info)
                            continue
                    is_dir = entry.is_dir(follow_symlinks=True)
                    is_file = entry.is_file(follow_symlinks=True)
                    info["is_dir"] = is_dir
                    info["is_file"] = is_file
                    if is_file:
                        st = entry.stat(follow_symlinks=True)
                        info["size"] = st.st_size
                        info["mtime"] = st.st_mtime
                        ext = _file_ext(name)
                        family = self.caps.family_for(ext)
                        info["ext"] = ext
                        info["family"] = family
                        info["label"] = self.caps.ext_label(ext) if ext else ""
                        info["targets"] = len(self.caps.matrix.get(ext, []))
                        info["supported"] = family is not None and info["targets"] > 0
                        if kind != "all" and family != kind:
                            continue
                    elif is_dir:
                        pass
                    else:
                        info["error"] = "不是普通文件或目录"
                except OSError as exc:
                    info["error"] = exc.strerror or "无法读取"
                entries.append(info)
        entries.sort(key=lambda item: (0 if item["is_dir"] else 1, item["name"].casefold()))
        total = len(entries)
        page = entries[offset: offset + limit]
        return {
            "ok": True,
            "path": requested,
            "real_path": real,
            "parent": None if requested == self.cfg.source_root else os.path.dirname(requested),
            "breadcrumbs": _breadcrumbs(requested, self.cfg.source_root),
            "entries": page,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(page) < total,
            "show_hidden": show_hidden,
            "kind": kind,
        }

    def list_output(self, path: str = "") -> dict[str, Any]:
        requested = os.path.abspath(path or self.cfg.output_root)
        real = safe_output_dir(requested, self.cfg, create=False)
        dirs: list[dict[str, Any]] = []
        try:
            with os.scandir(real) as iterator:
                for entry in iterator:
                    if entry.name.startswith(".transcoder-"):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append({
                            "name": entry.name,
                            "path": os.path.join(requested, entry.name),
                            "mtime": entry.stat(follow_symlinks=False).st_mtime,
                        })
        except OSError as exc:
            raise ValidationError(f"无法读取导出目录：{exc.strerror}") from exc
        dirs.sort(key=lambda item: item["name"].casefold())
        return {
            "ok": True,
            "path": requested,
            "parent": None if requested == self.cfg.output_root else os.path.dirname(requested),
            "breadcrumbs": _breadcrumbs(requested, self.cfg.output_root),
            "dirs": dirs,
            "free": self._disk()["free"],
        }

    def mkdir(self, parent: str, name: str) -> dict[str, Any]:
        parent_real = safe_output_dir(parent or self.cfg.output_root, self.cfg, create=False)
        clean = validate_dir_name(name)
        target = os.path.join(parent_real, clean)
        target = safe_output_path(target, self.cfg)
        if os.path.exists(target):
            raise ConflictError("同名文件夹已存在")
        try:
            os.mkdir(target, 0o755)
        except OSError as exc:
            raise ValidationError(f"创建文件夹失败：{exc.strerror}") from exc
        self.db.add_action("mkdir", f"新建导出文件夹 {clean}", {"path": target})
        self.jobs.log("info", f"新建导出文件夹 {target}")
        return {"ok": True, "path": target}

    # ------------------------------------------------------------- job creation
    def create_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError("请选择至少一个文件")
        if len(items) > MAX_BATCH:
            raise ValidationError(f"单次最多 {MAX_BATCH} 个文件")
        raw_options = payload.get("options") or {}
        if not isinstance(raw_options, dict):
            raise ValidationError("options 必须是对象")
        options = self._job_options(raw_options)
        name_pattern = str(payload.get("name_pattern") or self.db.get_setting("name_pattern", "{name}"))
        if len(name_pattern) > 120:
            raise ValidationError("命名模板过长")
        default_out = str(self.db.get_setting("default_output_dir", self.cfg.output_root))
        settings = self.db.settings()
        batch_id = new_id()
        created: list[dict[str, Any]] = []
        reserved: dict[str, set[str]] = {}
        for existing in self.db.list_jobs(("queued", "running", "paused")):
            if existing.get("output_path"):
                reserved.setdefault(existing["output_dir"], set()).add(
                    os.path.basename(existing["output_path"])
                )
        errors: list[str] = []
        for idx, raw in enumerate(items, start=1):
            try:
                created.append(self._build_job(raw, idx, batch_id, options, name_pattern, default_out, settings, reserved))
            except AppError as exc:
                label = raw.get("source_path", f"第 {idx} 个文件") if isinstance(raw, dict) else f"第 {idx} 个文件"
                errors.append(f"{os.path.basename(str(label))}: {exc.message}")
        if errors:
            raise ValidationError("；".join(errors[:8]) + ("…" if len(errors) > 8 else ""))
        if not created:
            raise ValidationError("没有可加入队列的任务")
        self.db.insert_jobs(created)
        self.db.add_action(
            "create_jobs",
            f"批量加入 {len(created)} 个转码任务",
            {"job_ids": [job["id"] for job in created]},
        )
        for job in created:
            self.jobs.log("info", f"加入队列：{job['source_name']} → {job['target'].upper()}", job["id"])
        self.jobs.wake()
        return {"ok": True, "jobs": self._public_jobs(created), "batch_id": batch_id}

    def _job_options(self, raw: dict[str, Any]) -> dict[str, Any]:
        options: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in QUALITY_KEYS:
                raise ValidationError(f"未知转码选项：{key}")
            options[key] = validate_setting(key, value)
        return options

    def _build_job(
        self,
        raw: dict[str, Any],
        index: int,
        batch_id: str,
        options: dict[str, Any],
        name_pattern: str,
        default_out: str,
        settings: dict[str, Any],
        reserved: dict[str, set[str]],
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("任务条目必须是对象")
        source_raw = raw.get("source_path")
        target_ext = str(raw.get("target") or "").lower().lstrip(".")
        if not source_raw:
            raise ValidationError("缺少 source_path")
        if not target_ext:
            raise ValidationError("缺少目标格式")
        source_real = safe_source_path(str(source_raw), self.cfg)
        source_name = os.path.basename(os.path.normpath(str(source_raw)))
        source_ext = _file_ext(source_name)
        family = self.caps.family_for(source_ext)
        if not family:
            raise ValidationError(f"不支持的源格式：{source_ext or '未知'}")
        if source_ext not in self.caps.matrix:
            raise ValidationError(f"当前工具无法读取 {source_ext.upper()} 文件")
        target = self.caps.target(source_ext, target_ext)
        if not target:
            raise ValidationError(f"不支持 {source_ext.upper()} → {target_ext.upper()}")
        output_dir_raw = str(raw.get("output_dir") or default_out)
        output_dir = safe_output_dir(output_dir_raw, self.cfg, create=False)
        custom_name = raw.get("output_name")
        if custom_name is not None and not isinstance(custom_name, str):
            raise ValidationError("output_name 必须是文本")
        if custom_name and len(custom_name) > 200:
            raise ValidationError("输出文件名过长")
        filename = render_name(name_pattern, source_name, target_ext, index, custom_name or None)
        reserved_dir = reserved.setdefault(output_dir, set())
        filename = _available_name(output_dir, filename, reserved_dir)
        reserved_dir.add(filename)
        output_path = os.path.join(output_dir, filename)
        output_path = safe_output_path(output_path, self.cfg)
        source_size = os.path.getsize(source_real)
        duration = None
        if family in ("audio", "video"):
            probe = self.engine.probe(source_real, family)
            duration = probe.get("duration")
        preset = options.get("quality_preset") or settings.get("quality_preset", "balanced")
        preset_values = dict(QUALITY_PRESETS.get(preset, QUALITY_PRESETS["balanced"]))
        merged_options = {
            "quality_preset": preset,
            "image_quality": options.get("image_quality", preset_values["image_quality"]),
            "audio_bitrate": options.get("audio_bitrate", preset_values["audio_bitrate"]),
            "video_crf": options.get("video_crf", preset_values["video_crf"]),
            "video_preset": options.get("video_preset", preset_values["video_preset"]),
            "archive_level": options.get("archive_level", preset_values["archive_level"]),
            "preserve_metadata": options.get("preserve_metadata", settings.get("preserve_metadata", True)),
        }
        return {
            "id": new_id(),
            "batch_id": batch_id,
            "source_path": source_real,
            "source_name": source_name,
            "source_ext": source_ext,
            "source_size": source_size,
            "family": family,
            "target": target_ext,
            "target_family": target["family"],
            "mode": target["mode"],
            "output_dir": output_dir,
            "output_name": filename,
            "output_path": output_path,
            "status": "queued",
            "progress": 0,
            "speed": None,
            "eta": None,
            "message": None,
            "error": None,
            "created_at": time.time() + index / 10000.0,
            "started_at": None,
            "finished_at": None,
            "updated_at": time.time(),
            "pid": None,
            "pause_reason": None,
            "options": merged_options,
            "output_size": None,
            "source_sig": None,
            "trash_path": None,
            "sort_order": self.db.next_sort_order() + index,
        }

    def update_job(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise NotFoundError("任务不存在")
        if job_id in self.jobs.active or job["status"] not in ("queued", "paused"):
            raise ConflictError("只有尚未运行的任务可以编辑")
        values: dict[str, Any] = {}
        target_ext = str(payload.get("target") or job["target"]).lower().lstrip(".")
        target = self.caps.target(job["source_ext"], target_ext)
        if not target:
            raise ValidationError(f"不支持 {job['source_ext'].upper()} → {target_ext.upper()}")
        values.update({"target": target_ext, "target_family": target["family"], "mode": target["mode"]})
        output_dir = safe_output_dir(str(payload.get("output_dir") or job["output_dir"]), self.cfg, create=False)
        values["output_dir"] = output_dir
        custom = payload.get("output_name")
        if custom is not None:
            filename = safe_filename(str(custom), source_stem(job["source_name"]), target_ext)
        else:
            filename = safe_filename(job["output_name"], source_stem(job["source_name"]), target_ext)
        filename = _available_name(output_dir, filename, set())
        values["output_name"] = filename
        values["output_path"] = os.path.join(output_dir, filename)
        if payload.get("options"):
            values["options"] = {**job.get("options", {}), **self._job_options(payload["options"])}
        old = job.copy()
        updated = self.jobs.update_job_fields(job_id, values)
        self.db.add_action(
            "edit_job",
            f"修改任务 {job['source_name']}",
            {"job_id": job_id, "before": _job_snapshot(old)},
        )
        return {"ok": True, "job": self._public_jobs([updated])[0]}

    def clear_completed(self) -> dict[str, Any]:
        completed = self.db.list_jobs(("completed",), limit=10000)
        if not completed:
            return {"cleared": 0}
        ids = [job["id"] for job in completed]
        snapshots = [_job_snapshot(job) for job in completed]
        deleted = self.db.delete_jobs(ids)
        self.db.add_action(
            "clear_completed",
            f"清除 {deleted} 条已完成任务记录（输出文件保留）",
            {"jobs": snapshots},
        )
        self.jobs.log("info", f"清除 {deleted} 条已完成任务记录，输出文件保留")
        return {"cleared": deleted, "job_ids": ids}

    def retry_failed(self) -> dict[str, Any]:
        return self.jobs.retry_all_failed()

    def delete_job(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise NotFoundError("任务不存在")
        if job_id in self.jobs.active:
            raise ConflictError("任务仍在运行，请先取消")
        deleted = self.jobs.delete_job(job_id)
        self.db.add_action(
            "delete_job",
            f"删除任务记录 {deleted['source_name']}（输出文件保留）",
            {"job": _job_snapshot(deleted)},
        )
        return {"ok": True, "deleted": job_id}

    # ------------------------------------------------------------------ settings
    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValidationError("设置必须是对象")
        allowed = set(DEFAULT_SETTINGS) - {"queue_paused"}
        before = self.db.settings()
        updates: dict[str, Any] = {}
        for key, value in payload.items():
            if key not in allowed:
                continue
            clean = validate_setting(key, value)
            if key == "default_output_dir":
                clean = safe_output_dir(clean, self.cfg, create=False)
            if before.get(key) != clean:
                updates[key] = clean
        if not updates:
            return {"ok": True, "settings": before}
        changed_before = {key: before.get(key) for key in updates}
        if all(changed_before[key] == value for key, value in updates.items()):
            return {"ok": True, "settings": before}
        self.db.update_settings(updates)
        self.db.add_action(
            "settings",
            "修改设置：" + "、".join(sorted(updates.keys())),
            {"before": changed_before, "after": updates},
        )
        self.jobs.log("info", "设置已更新：" + "、".join(f"{k}={v}" for k, v in sorted(updates.items())))
        self.jobs.wake()
        return {"ok": True, "settings": self.db.settings()}

    # -------------------------------------------------------------------- actions
    def actions(self, limit: int = 30) -> list[dict[str, Any]]:
        return [self._action_public(item) for item in self.db.list_actions(limit)]

    def _action_public(self, action: dict[str, Any]) -> dict[str, Any]:
        data = dict(action)
        can_undo, reason = self._can_undo(data)
        data["can_undo"] = can_undo
        data["reason"] = reason
        data["can_restore"] = self._can_restore(data)
        return data

    def _can_undo(self, action: dict[str, Any]) -> tuple[bool, str | None]:
        if action.get("undone_at"):
            return False, "该操作已撤销"
        kind = action.get("kind")
        payload = action.get("payload") or {}
        if kind == "settings":
            return True, None
        if kind == "create_jobs":
            ids = payload.get("job_ids") or []
            if not ids:
                return False, "没有可撤销的任务"
            for job_id in ids:
                job = self.db.get_job(job_id)
                if not job:
                    return False, "任务记录已被删除"
                if job["status"] != "queued":
                    return False, "任务已开始，无法整批撤销；可在队列中单独取消"
            return True, None
        if kind == "mkdir":
            path = payload.get("path")
            if not path or not is_within(path, self.cfg.output_root):
                return False, "目录不在导出区"
            if not os.path.isdir(path):
                return False, "目录已不存在"
            try:
                if os.listdir(path):
                    return False, "目录非空，不能撤销"
            except OSError:
                return False, "目录无法读取"
            return True, None
        if kind == "complete":
            job = self.db.get_job(payload.get("job_id", ""))
            if not job:
                return False, "任务记录已删除"
            if job["status"] != "completed":
                return False, "任务已不在完成状态"
            path = payload.get("output_path")
            if not path or not os.path.isfile(path):
                return False, "输出文件不存在（可能已移动）"
            return True, None
        if kind == "delete_job":
            job = payload.get("job") or {}
            if self.db.get_job(job.get("id", "")):
                return False, "任务记录已存在"
            return True, None
        if kind == "clear_completed":
            snapshots = payload.get("jobs") or []
            if not snapshots:
                return False, "没有可恢复的任务记录"
            for item in snapshots:
                if self.db.get_job(item.get("id", "")):
                    return False, "任务记录已存在"
            return True, None
        if kind == "edit_job":
            job = self.db.get_job(payload.get("job_id", ""))
            if not job or job["status"] not in ("queued", "paused"):
                return False, "任务已开始，不能撤销编辑"
            return True, None
        return False, "该操作不支持撤销"

    def _can_restore(self, action: dict[str, Any]) -> bool:
        if action.get("kind") != "complete" or not action.get("undone_at"):
            return False
        payload = action.get("payload") or {}
        job = self.db.get_job(payload.get("job_id", ""))
        if not job or job["status"] != "undone":
            return False
        path = job.get("trash_path")
        return bool(path and os.path.isfile(path))

    def undo_latest(self) -> dict[str, Any]:
        action = self.db.latest_action()
        if not action:
            raise NotFoundError("没有可撤销的操作")
        return self.undo_action(int(action["id"]))

    def undo_action(self, action_id: int) -> dict[str, Any]:
        action = self.db.get_action(action_id)
        if not action:
            raise NotFoundError("操作记录不存在")
        can, reason = self._can_undo(action)
        if not can:
            raise ConflictError(reason or "该操作不能撤销")
        kind = action["kind"]
        payload = action["payload"]
        message = action["summary"]
        if kind == "settings":
            before = payload.get("before") or {}
            self.db.update_settings({k: v for k, v in before.items() if k in DEFAULT_SETTINGS})
            self.jobs.wake()
        elif kind == "create_jobs":
            for job_id in payload.get("job_ids", []):
                self.db.delete_job(job_id)
        elif kind == "mkdir":
            path = safe_output_path(payload["path"], self.cfg)
            try:
                os.rmdir(path)
            except OSError as exc:
                raise ConflictError(f"无法撤销新建目录：{exc.strerror}") from exc
        elif kind == "complete":
            job = self.db.get_job(payload["job_id"])
            if not job:
                raise NotFoundError("任务不存在")
            trash = move_to_trash(payload["output_path"], self.cfg, job["id"])
            self.db.update_job(
                job["id"], status="undone", trash_path=trash, message="输出已撤销",
                finished_at=time.time(), speed=None, eta=None,
            )
        elif kind == "delete_job":
            snapshot = payload.get("job") or {}
            if not snapshot.get("id"):
                raise ConflictError("缺少任务快照")
            if self.db.get_job(snapshot["id"]):
                raise ConflictError("任务记录已存在")
            job = {field: snapshot.get(field) for field in JOB_FIELDS}
            if isinstance(job.get("options"), (dict, list)):
                job["options"] = json.dumps(job["options"], ensure_ascii=False)
            if isinstance(job.get("source_sig"), (dict, list)):
                job["source_sig"] = json.dumps(job["source_sig"], ensure_ascii=False)
            self.db.insert_jobs([job])
        elif kind == "clear_completed":
            snapshots = payload.get("jobs") or []
            jobs_to_restore = []
            for snapshot in snapshots:
                if not snapshot.get("id"):
                    raise ConflictError("任务快照损坏")
                if self.db.get_job(snapshot["id"]):
                    raise ConflictError("任务记录已存在，无法恢复")
                job = {field: snapshot.get(field) for field in JOB_FIELDS}
                if isinstance(job.get("options"), (dict, list)):
                    job["options"] = json.dumps(job["options"], ensure_ascii=False)
                if isinstance(job.get("source_sig"), (dict, list)):
                    job["source_sig"] = json.dumps(job["source_sig"], ensure_ascii=False)
                jobs_to_restore.append(job)
            self.db.insert_jobs(jobs_to_restore)
        elif kind == "edit_job":
            job = self.db.get_job(payload["job_id"])
            if not job:
                raise NotFoundError("任务不存在")
            before = payload.get("before") or {}
            allowed = {"target", "target_family", "mode", "output_dir", "output_name", "output_path", "options"}
            self.db.update_job(job["id"], **{k: v for k, v in before.items() if k in allowed})
        self.db.mark_action_undone(action_id)
        self.jobs.log("info", f"已撤销：{message}")
        return {"ok": True, "action": self._action_public(self.db.get_action(action_id)), "message": message}

    def undo_output(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise NotFoundError("任务不存在")
        if job["status"] != "completed":
            raise ConflictError("只有已完成任务可以撤销输出")
        for action in self.db.list_actions(200):
            if (
                action["kind"] == "complete"
                and action["payload"].get("job_id") == job_id
                and not action["undone_at"]
            ):
                return self.undo_action(int(action["id"]))
        raise ConflictError("找不到该任务的完成记录，无法撤销")

    def restore_job(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise NotFoundError("任务不存在")
        if job["status"] != "undone":
            raise ConflictError("只有已撤销任务可以恢复输出")
        trash = job.get("trash_path")
        if not trash:
            raise ConflictError("回收站中的输出文件已不存在")
        trash = safe_output_path(trash, self.cfg, must_exist=True, allow_root=False, allow_internal=True)
        if not os.path.isfile(trash):
            raise ConflictError("回收站中的输出文件已不存在")
        output_dir = safe_output_dir(job["output_dir"], self.cfg, create=True)
        final_path = restore_from_trash(trash, output_dir, job["output_name"])
        if os.path.realpath(trash) != os.path.realpath(final_path):
            try:
                os.unlink(trash)
            except OSError:
                pass
        self.db.update_job(
            job_id, status="completed", output_path=final_path, trash_path=None,
            output_size=os.path.getsize(final_path), finished_at=time.time(), message="输出已恢复",
        )
        # Re-arm the latest complete action for this job so it can be undone again.
        restored_action = None
        for action in self.db.list_actions(100):
            if action["kind"] == "complete" and action["payload"].get("job_id") == job_id and action["undone_at"]:
                restored_action = action
                break
        if restored_action:
            self.db.mark_action_restored(int(restored_action["id"]))
        else:
            self.db.add_action(
                "complete", f"恢复输出 {job['source_name']}", {
                    "job_id": job_id, "output_path": final_path,
                    "output_name": os.path.basename(final_path), "size": os.path.getsize(final_path),
                },
            )
        self.jobs.log("info", f"输出已恢复：{os.path.basename(final_path)}", job_id)
        return {"ok": True, "job": self._public_jobs([self.db.get_job(job_id)])[0]}

    # ------------------------------------------------------------------ download
    def download_path(self, path: str) -> tuple[str, str, int]:
        real = safe_output_path(path, self.cfg, must_exist=True, allow_root=False)
        real = os.path.realpath(real)
        if not is_within(real, self.cfg.output_root):
            raise ForbiddenError("文件不在导出目录内")
        if not os.path.isfile(real):
            raise NotFoundError("文件不存在")
        return real, os.path.basename(real), os.path.getsize(real)

    # ------------------------------------------------------------------ misc
    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "healthy",
            "version": APP_VERSION,
            "counts": self.db.count_jobs(),
            "queue_paused": self.jobs.queue_paused,
            "source_readonly": self.cfg.source_mount_readonly(),
            "tools": {
                "ffmpeg": self.caps.ffmpeg.available,
                "ffprobe": self.caps.ffprobe.available,
                "magick": self.caps.magick.available,
                "sevenzip": self.caps.sevenzip.available,
                "bsdtar": self.caps.bsdtar.available,
            },
        }


def _file_ext(name: str) -> str:
    base = os.path.basename(name).lower()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if base.endswith(compound):
            return compound[1:].split(".")[-1]  # archive family uses inner compression ext
    return base.rsplit(".", 1)[-1] if "." in base else ""


def _available_name(directory: str, filename: str, reserved: set[str]) -> str:
    for candidate in candidate_paths(directory, filename):
        name = os.path.basename(candidate)
        if name not in reserved and not os.path.exists(candidate):
            return name
    raise ConflictError("无法生成不冲突的文件名")


def _breadcrumbs(path: str, root: str) -> list[dict[str, str]]:
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    crumbs = [{"name": os.path.basename(root) or root, "path": root}]
    rel = os.path.relpath(path, root)
    if rel == ".":
        return crumbs
    current = root
    for part in rel.split(os.sep):
        current = os.path.join(current, part)
        crumbs.append({"name": part, "path": current})
    return crumbs


def _job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    snapshot = {field: job.get(field) for field in JOB_FIELDS}
    return snapshot

"""Persistent job queue, worker scheduler, pause/resume and process control."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .capabilities import Capabilities
from .config import Config
from .db import Database, new_id
from .engine import CanceledError, Engine, Hooks
from .security import AppError, ConflictError, NotFoundError, ValidationError, publish_no_overwrite, safe_output_dir

log = logging.getLogger("transcoder.jobs")


@dataclass
class ActiveJob:
    job_id: str
    proc: subprocess.Popen | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    paused_by: str | None = None
    pause_requested: bool = False
    thread: threading.Thread | None = None
    publish_done: bool = False


class JobManager:
    def __init__(self, cfg: Config, db: Database, caps: Capabilities, engine: Engine):
        self.cfg = cfg
        self.db = db
        self.caps = caps
        self.engine = engine
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self.active: dict[str, ActiveJob] = {}
        self.queue_paused = bool(db.get_setting("queue_paused", False))
        self.shutdown = False
        self._dispatcher: threading.Thread | None = None
        self._last_log: dict[str, float] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._cleanup_tmp()
        interrupted = self.db.recover_interrupted()
        if interrupted:
            self.log("warn", f"检测到 {interrupted} 个上次未结束的任务，已标记失败（服务重启中断）")
        self.queue_paused = bool(self.db.get_setting("queue_paused", False))
        self.log("info", f"任务引擎启动，并发上限 {self.concurrency()}，队列{'已暂停' if self.queue_paused else '运行中'}")
        self._dispatcher = threading.Thread(target=self._dispatch_loop, name="dispatcher", daemon=True)
        self._dispatcher.start()

    def _cleanup_tmp(self) -> None:
        try:
            for name in os.listdir(self.cfg.tmp_dir):
                path = os.path.join(self.cfg.tmp_dir, name)
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.log("warn", f"清理临时目录失败：{exc}")

    def stop(self) -> None:
        with self._cond:
            self.shutdown = True
            active = list(self.active.values())
            self._cond.notify_all()
        for item in active:
            item.cancel.set()
            self._signal(item, signal.SIGCONT)
            self._signal(item, signal.SIGTERM)
        deadline = time.monotonic() + 5
        for item in active:
            if item.thread:
                item.thread.join(timeout=max(0, deadline - time.monotonic()))
        for item in active:
            if item.thread and item.thread.is_alive():
                self._signal(item, signal.SIGKILL)

    # -------------------------------------------------------------------- helpers
    def concurrency(self) -> int:
        try:
            value = int(self.db.get_setting("concurrency", 2))
        except (TypeError, ValueError):
            value = 2
        return max(1, min(8, value))

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def log(self, level: str, message: str, job_id: str | None = None) -> None:
        try:
            self.db.add_event(level, message, job_id)
        except Exception:
            log.exception("failed to persist log")
        log.log({"debug": 10, "info": 20, "warn": 30, "error": 40}.get(level, 20), message)

    def _job(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise NotFoundError("任务不存在")
        return job

    def _update(self, job_id: str, **values: Any) -> None:
        self.db.update_job(job_id, **values)

    # ------------------------------------------------------------------- queue ops
    def pause_all(self) -> dict[str, Any]:
        with self._cond:
            self.queue_paused = True
            self.db.set_setting("queue_paused", True)
            paused = 0
            for item in self.active.values():
                if item.paused_by is None:
                    item.paused_by = "queue"
                    self._signal(item, signal.SIGSTOP)
                    paused += 1
                    self._update(item.job_id, status="paused", pause_reason="queue", message="队列暂停")
            self._cond.notify_all()
        self.log("info", f"队列已暂停，暂停运行中任务 {paused} 个")
        return {"queue_paused": True, "paused": paused}

    def resume_all(self) -> dict[str, Any]:
        with self._cond:
            self.queue_paused = False
            self.db.set_setting("queue_paused", False)
            resumed = 0
            for item in self.active.values():
                if item.paused_by == "queue":
                    item.paused_by = None
                    self._signal(item, signal.SIGCONT)
                    resumed += 1
                    self._update(item.job_id, status="running", pause_reason=None, message=None)
            self._cond.notify_all()
        self.log("info", f"队列已继续，恢复运行中任务 {resumed} 个")
        return {"queue_paused": False, "resumed": resumed}

    def stop_all(self) -> dict[str, Any]:
        canceled = 0
        with self._cond:
            for job in self.db.list_jobs(("queued", "paused")):
                if job["id"] in self.active:
                    continue
                self._update(job["id"], status="canceled", finished_at=time.time(), error="队列被终止", message="已终止")
                canceled += 1
            self.queue_paused = True
            self.db.set_setting("queue_paused", True)
            active = list(self.active.values())
            self._cond.notify_all()
        for item in active:
            item.cancel.set()
            self._signal(item, signal.SIGCONT)
            self._signal(item, signal.SIGTERM)
            self._schedule_kill(item)
        self.log("warn", f"已终止队列：取消排队 {canceled} 个，终止运行 {len(active)} 个")
        return {"canceled": canceled, "active": len(active), "queue_paused": True}

    def pause_job(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        with self._cond:
            if job["status"] == "queued":
                self._update(job_id, status="paused", pause_reason="user", message="已暂停（未开始）")
                self.log("info", "任务已暂停", job_id)
                return self._job(job_id)
            if job["status"] == "running":
                item = self.active.get(job_id)
                if item:
                    item.paused_by = "user"
                    item.pause_requested = True
                    self._signal(item, signal.SIGSTOP)
                self._update(job_id, status="paused", pause_reason="user", message="已暂停")
                self.log("info", "任务已暂停", job_id)
                return self._job(job_id)
            if job["status"] == "paused":
                raise ConflictError("任务已经是暂停状态")
            raise ConflictError("只有排队中或运行中的任务可以暂停")

    def resume_job(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        with self._cond:
            if job["status"] == "paused" and not self.active.get(job_id):
                self._update(job_id, status="queued", pause_reason=None, message=None)
                self._cond.notify_all()
                self.log("info", "任务重新排队", job_id)
                return self._job(job_id)
            if job["status"] != "paused":
                raise ConflictError("任务不是暂停状态")
            item = self.active.get(job_id)
            if not item:
                self._update(job_id, status="queued", pause_reason=None, message=None)
                self._cond.notify_all()
                return self._job(job_id)
            if item.paused_by == "queue" and self.queue_paused:
                raise ConflictError("队列处于全局暂停，请先继续队列")
            item.paused_by = None
            item.pause_requested = False
            self._signal(item, signal.SIGCONT)
            self._update(job_id, status="running", pause_reason=None, message=None)
            self.log("info", "任务已继续", job_id)
            return self._job(job_id)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        if job["status"] in ("completed", "failed", "canceled", "undone"):
            raise ConflictError("终态任务不能取消；可重试或删除")
        with self._cond:
            item = self.active.get(job_id)
            if not item:
                self._update(job_id, status="canceled", finished_at=time.time(), error="用户取消", message="已取消")
                self.log("warn", "排队任务已取消", job_id)
                return self._job(job_id)
            item.cancel.set()
            self._signal(item, signal.SIGCONT)
            self._signal(item, signal.SIGTERM)
            self._schedule_kill(item)
            self._update(job_id, message="正在终止…")
            self.log("warn", "正在终止任务", job_id)
        return self._job(job_id)

    def retry_job(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        if self.active.get(job_id):
            raise ConflictError("任务仍在运行")
        if job["status"] not in ("failed", "canceled", "undone"):
            raise ConflictError("只有失败/取消/已撤销任务可以重试")
        self._update(
            job_id, status="queued", progress=0, speed=None, eta=None, error=None,
            message="重新排队", output_path=None, output_size=None, trash_path=None,
            started_at=None, finished_at=None, pause_reason=None, source_sig=None,
        )
        self.log("info", "任务重试", job_id)
        self.wake()
        return self._job(job_id)

    def retry_all_failed(self) -> dict[str, Any]:
        failed = self.db.list_jobs(("failed",), limit=10000)
        queued_ids: list[str] = []
        with self._cond:
            for job in failed:
                if job["id"] in self.active:
                    continue
                self._update(
                    job["id"], status="queued", progress=0, speed=None, eta=None, error=None,
                    message="批量重试，重新排队", output_path=None, output_size=None,
                    trash_path=None, started_at=None, finished_at=None, pause_reason=None,
                    pid=None, source_sig=None,
                )
                queued_ids.append(job["id"])
            self._cond.notify_all()
        if queued_ids:
            self.log("info", f"批量重试失败任务：{len(queued_ids)} 个重新排队")
        return {"retried": len(queued_ids), "job_ids": queued_ids}

    def delete_job(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        if job_id in self.active:
            raise ConflictError("任务仍在运行，请先取消")
        if job["status"] == "running":
            raise ConflictError("任务仍在运行，请先取消")
        self.db.delete_job(job_id)
        self.log("info", "任务记录已删除（输出文件保留）", job_id)
        return job

    # ------------------------------------------------------------------ scheduling
    def _dispatch_loop(self) -> None:
        while True:
            with self._cond:
                if self.shutdown:
                    return
                try:
                    capacity = self.concurrency() - len(self.active)
                    jobs = self.db.queued_jobs() if not self.queue_paused and capacity > 0 else []
                    for job in jobs[:capacity]:
                        self._start_job_locked(job)
                except Exception:
                    self.log("error", "调度器异常，1 秒后重试")
                    log.exception("dispatcher error")
                self._cond.wait(timeout=0.8)

    def _start_job_locked(self, job: dict[str, Any]) -> None:
        if job["id"] in self.active:
            return
        item = ActiveJob(job_id=job["id"])
        self.active[job["id"]] = item
        self._update(
            job["id"], status="running", started_at=time.time(), updated_at=time.time(),
            progress=0, speed=None, eta=None, error=None, message=None, pid=None,
        )
        self.log("info", f"开始转码 {job['source_name']} → {job['target'].upper()}", job["id"])
        item.thread = threading.Thread(target=self._worker, args=(dict(job), item), name=f"job-{job['id']}", daemon=True)
        item.thread.start()

    def _worker(self, job: dict[str, Any], item: ActiveJob) -> None:
        job_id = job["id"]
        last_progress_at = 0.0

        def on_progress(progress: float, speed: str | None, eta: int | None) -> None:
            nonlocal last_progress_at
            now = time.monotonic()
            if progress < 99.5 and now - last_progress_at < 0.7:
                return
            last_progress_at = now
            try:
                self._update(job_id, progress=round(max(0.0, min(100.0, progress)), 2), speed=speed, eta=eta)
            except Exception:
                log.exception("progress update failed")

        def on_process(proc: subprocess.Popen | None) -> None:
            with self._cond:
                item.proc = proc
                if proc is not None:
                    try:
                        self._update(job_id, pid=proc.pid)
                    except Exception:
                        log.exception("pid update failed")
                    if item.cancel.is_set():
                        self._signal(item, signal.SIGKILL)
                    elif item.paused_by or item.pause_requested:
                        self._signal(item, signal.SIGSTOP)

        def on_log(message: str) -> None:
            self.log("info", message, job_id)

        hooks = Hooks(
            on_progress=on_progress,
            on_log=on_log,
            on_process=on_process,
            cancelled=lambda: item.cancel.is_set() or self.shutdown,
            paused=lambda: item.paused_by is not None,
        )
        try:
            result = self.engine.run(job, hooks)
            if item.cancel.is_set():
                raise CanceledError("任务已取消")
            # Re-load to pick up output_dir/output_name edited before start.
            fresh = self._job(job_id)
            output_dir = safe_output_dir(fresh["output_dir"], self.cfg, create=True)
            if item.cancel.is_set():
                raise CanceledError("任务已取消")
            final_path = publish_no_overwrite(result.temp_path, output_dir, fresh["output_name"])
            item.publish_done = True
            if item.cancel.is_set():
                from .security import move_to_trash
                try:
                    move_to_trash(final_path, self.cfg, job_id)
                except Exception:
                    log.exception("failed to move canceled output to trash")
                raise CanceledError("任务已取消")
            try:
                os.unlink(result.temp_path)
            except OSError:
                pass
            size = os.path.getsize(final_path)
            self._update(
                job_id, status="completed", progress=100, speed=None, eta=0,
                finished_at=time.time(), output_path=final_path, output_size=size,
                source_size=result.source_size, source_sig=result.source_sig,
                message="已完成", error=None, pid=None, pause_reason=None,
            )
            output_name = os.path.basename(final_path)
            try:
                self.db.add_action(
                    "complete",
                    f"转码完成 {fresh['source_name']} → {output_name}",
                    {"job_id": job_id, "output_path": final_path, "output_name": output_name, "size": size},
                )
            except Exception:
                self.log("warn", "完成记录写入撤销历史失败", job_id)
            self.log("info", f"任务完成：{output_name}（{_human(size)}）", job_id)
        except CanceledError:
            self._update(
                job_id, status="canceled", finished_at=time.time(), speed=None, eta=None,
                error="用户取消", message="已取消", pid=None, pause_reason=None,
            )
            self.log("warn", "任务已取消，临时文件已清理", job_id)
        except Exception as exc:
            message = _safe_error(exc)
            self._update(
                job_id, status="failed", finished_at=time.time(), speed=None, eta=None,
                error=message, message="失败", pid=None, pause_reason=None,
            )
            self.log("error", f"任务失败：{message}", job_id)
        finally:
            job_tmp = os.path.join(self.cfg.tmp_dir, job_id)
            shutil.rmtree(job_tmp, ignore_errors=True)
            with self._cond:
                self.active.pop(job_id, None)
                self._cond.notify_all()

    # ------------------------------------------------------------------ signalling
    def _signal(self, item: ActiveJob, sig: int) -> None:
        proc = item.proc
        if not proc or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass
        except OSError:
            pass

    def _schedule_kill(self, item: ActiveJob) -> None:
        def killer() -> None:
            time.sleep(3)
            if item.proc and item.proc.poll() is None:
                self._signal(item, signal.SIGKILL)
        threading.Thread(target=killer, name=f"kill-{item.job_id}", daemon=True).start()

    # ------------------------------------------------------------------ edit queue
    def update_job_fields(self, job_id: str, values: dict[str, Any]) -> dict[str, Any]:
        job = self._job(job_id)
        if job_id in self.active or job["status"] not in ("queued", "paused"):
            raise ConflictError("只有尚未开始的任务可编辑")
        if job["status"] == "paused" and job_id in self.active:
            raise ConflictError("运行中的任务不能编辑")
        allowed = {"target", "target_family", "mode", "output_dir", "output_name", "output_path", "options"}
        clean = {k: v for k, v in values.items() if k in allowed}
        if clean:
            self._update(job_id, **clean)
            self.log("info", "排队任务配置已更新", job_id)
        return self._job(job_id)


def _safe_error(exc: Exception) -> str:
    text = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
    return text.replace("\x00", "").strip()[:1200]


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"

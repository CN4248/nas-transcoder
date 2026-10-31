"""SQLite persistence for settings, job queue, logs and undo history."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

from .config import DEFAULT_SETTINGS, Config
from .security import ConflictError, NotFoundError, ValidationError

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_ext TEXT NOT NULL,
    source_size INTEGER NOT NULL DEFAULT 0,
    family TEXT NOT NULL,
    target TEXT NOT NULL,
    target_family TEXT NOT NULL,
    mode TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    output_name TEXT NOT NULL,
    output_path TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    speed TEXT,
    eta INTEGER,
    message TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    updated_at REAL NOT NULL,
    pid INTEGER,
    pause_reason TEXT,
    options TEXT NOT NULL DEFAULT '{}',
    output_size INTEGER,
    source_sig TEXT,
    trash_path TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    job_id TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL,
    undone_at REAL,
    undone_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

JOB_FIELDS = [
    "id", "batch_id", "source_path", "source_name", "source_ext", "source_size",
    "family", "target", "target_family", "mode", "output_dir", "output_name",
    "output_path", "status", "progress", "speed", "eta", "message", "error",
    "created_at", "started_at", "finished_at", "updated_at", "pid", "pause_reason",
    "options", "output_size", "source_sig", "trash_path", "sort_order",
]


def now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def row_to_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = {key: row[key] for key in row.keys()}
    try:
        data["options"] = json.loads(data.get("options") or "{}")
    except json.JSONDecodeError:
        data["options"] = {}
    try:
        data["source_sig"] = json.loads(data.get("source_sig") or "null")
    except json.JSONDecodeError:
        data["source_sig"] = None
    return data


class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = cfg.db_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            self._seed_settings()

    # ------------------------------------------------------------------ utils
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # --------------------------------------------------------------- settings
    def _seed_settings(self) -> None:
        defaults = dict(DEFAULT_SETTINGS)
        if defaults["default_output_dir"] == "/output":
            defaults["default_output_dir"] = self.cfg.output_root
        for key, value in defaults.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        self.conn.commit()
        # Keep test/custom deployments coherent when /output is not the mount.
        if self.cfg.output_root != "/output":
            current = self.conn.execute("SELECT value FROM settings WHERE key='default_output_dir'").fetchone()
            if current and json.loads(current["value"]) == "/output":
                self.conn.execute(
                    "UPDATE settings SET value=? WHERE key='default_output_dir'",
                    (json.dumps(self.cfg.output_root),),
                )
                self.conn.commit()

    def settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        result = dict(DEFAULT_SETTINGS)
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                result[row["key"]] = row["value"]
        return result

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return DEFAULT_SETTINGS.get(key, default)
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for key, value in values.items():
                self.conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
            self.conn.commit()
        return self.settings()

    # ------------------------------------------------------------------- jobs
    def insert_jobs(self, jobs: list[dict[str, Any]]) -> None:
        if not jobs:
            return
        fields = ",".join(JOB_FIELDS)
        placeholders = ",".join("?" for _ in JOB_FIELDS)
        rows = []
        for job in jobs:
            row = dict(job)
            for key in ("options", "source_sig"):
                if key in row and not isinstance(row[key], (str, type(None))):
                    row[key] = json.dumps(row[key], ensure_ascii=False)
            rows.append(tuple(row.get(field) for field in JOB_FIELDS))
        with self._lock:
            self.conn.executemany(
                f"INSERT INTO jobs({fields}) VALUES({placeholders})",
                rows,
            )
            self.conn.commit()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row_to_job(row)

    def list_jobs(self, statuses: Iterable[str] | None = None, limit: int = 500) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs"
        params: list[Any] = []
        if statuses:
            values = list(statuses)
            sql += f" WHERE status IN ({','.join('?' for _ in values)})"
            params.extend(values)
        sql += " ORDER BY sort_order ASC, created_at ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [row_to_job(row) for row in rows if row is not None]  # type: ignore[misc]

    def queued_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY sort_order ASC, created_at ASC"
            ).fetchall()
        return [row_to_job(row) for row in rows if row is not None]  # type: ignore[misc]

    def update_job(self, job_id: str, **values: Any) -> dict[str, Any] | None:
        if not values:
            return self.get_job(job_id)
        values["updated_at"] = values.get("updated_at", now())
        for key in ("options", "source_sig"):
            if key in values and not isinstance(values[key], str):
                values[key] = json.dumps(values[key], ensure_ascii=False)
        allowed = set(JOB_FIELDS)
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown job fields: {sorted(unknown)}")
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._lock:
            self.conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?",
                tuple(values.values()) + (job_id,),
            )
            self.conn.commit()
        return self.get_job(job_id)

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self.conn.commit()
        return cur.rowcount > 0

    def delete_jobs(self, job_ids: Iterable[str]) -> int:
        values = list(job_ids)
        if not values:
            return 0
        deleted = 0
        with self._lock:
            for start in range(0, len(values), 500):
                chunk = values[start:start + 500]
                cur = self.conn.execute(
                    f"DELETE FROM jobs WHERE id IN ({','.join('?' for _ in chunk)})", tuple(chunk)
                )
                deleted += cur.rowcount
            self.conn.commit()
        return deleted

    def count_jobs(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        counts = {row["status"]: row["n"] for row in rows}
        total = sum(counts.values())
        return {
            "total": total,
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "paused": counts.get("paused", 0),
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
            "canceled": counts.get("canceled", 0),
            "undone": counts.get("undone", 0),
        }

    def recover_interrupted(self) -> int:
        """Mark in-flight jobs as failed after a restart (child process is gone)."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE jobs SET status='failed', finished_at=?, updated_at=?, "
                "error='服务重启，任务中断。临时文件已清理，可点击重试。', "
                "progress=0, speed=NULL, eta=NULL, pid=NULL, pause_reason=NULL "
                "WHERE status='running' OR (status='paused' AND pid IS NOT NULL)",
                (now(), now()),
            )
            self.conn.commit()
        return cur.rowcount

    def next_sort_order(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM jobs").fetchone()
        return int(row["n"])

    def queued_until(self, before: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE status='queued' AND created_at<=? ORDER BY sort_order ASC, created_at ASC",
                (before,),
            ).fetchall()
        return [row_to_job(row) for row in rows if row is not None]  # type: ignore[misc]

    # ------------------------------------------------------------------ events
    def add_event(self, level: str, message: str, job_id: str | None = None) -> int:
        ts = now()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO events(ts, level, job_id, message) VALUES(?,?,?,?)",
                (ts, level, job_id, message),
            )
            event_id = int(cur.lastrowid)
            self.conn.execute(
                "DELETE FROM events WHERE id <= (SELECT MAX(id) - ? FROM events)",
                (self.cfg.log_keep,),
            )
            self.conn.commit()
        return event_id

    def events(self, after: int = 0, limit: int = 300) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, ts, level, job_id, message FROM events WHERE id>? ORDER BY id ASC LIMIT ?",
                (max(0, after), max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_events(self, limit: int = 300) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, ts, level, job_id, message FROM events ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def latest_event_id(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM events").fetchone()
        return int(row["n"])

    # ----------------------------------------------------------------- actions
    def add_action(self, kind: str, summary: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO actions(ts, kind, summary, payload) VALUES(?,?,?,?)",
                (now(), kind, summary, json.dumps(payload, ensure_ascii=False)),
            )
            self.conn.commit()
            action_id = int(cur.lastrowid)
        return self.get_action(action_id)  # type: ignore[return-value]

    def get_action(self, action_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        return self._action_row(row)

    def list_actions(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)
            ).fetchall()
        return [self._action_row(row) for row in rows if row is not None]  # type: ignore[misc]

    def latest_action(self, *, undone: bool = False) -> dict[str, Any] | None:
        with self._lock:
            if undone:
                row = self.conn.execute(
                    "SELECT * FROM actions WHERE undone_at IS NOT NULL ORDER BY id DESC LIMIT 1"
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT * FROM actions WHERE undone_at IS NULL ORDER BY id DESC LIMIT 1"
                ).fetchone()
        return self._action_row(row)

    def mark_action_undone(self, action_id: int, by: str = "undo") -> None:
        self._exec(
            "UPDATE actions SET undone_at=?, undone_by=? WHERE id=? AND undone_at IS NULL",
            (now(), by, action_id),
        )

    def mark_action_restored(self, action_id: int) -> None:
        self._exec("UPDATE actions SET undone_at=NULL, undone_by=NULL WHERE id=?", (action_id,))

    @staticmethod
    def _action_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        try:
            data["payload"] = json.loads(data["payload"] or "{}")
        except json.JSONDecodeError:
            data["payload"] = {}
        return data

    # -------------------------------------------------------------------- meta
    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_meta(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


def validate_setting(key: str, value: Any) -> Any:
    from .config import SETTING_RANGES

    if key not in DEFAULT_SETTINGS:
        raise ValidationError(f"未知设置项：{key}")
    if key in ("show_hidden", "preserve_metadata", "verify_source", "queue_paused"):
        if not isinstance(value, bool):
            raise ValidationError(f"{key} 必须是布尔值")
        return value
    if key in ("concurrency", "image_quality", "video_crf", "archive_level"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{key} 必须是整数")
        low, high = SETTING_RANGES[key]  # type: ignore[misc]
        if not low <= value <= high:
            raise ValidationError(f"{key} 必须在 {low}–{high} 之间")
        return value
    if key == "audio_bitrate":
        if value not in SETTING_RANGES[key]:
            raise ValidationError("audio_bitrate 取值不合法")
        return value
    if key == "video_preset":
        if value not in SETTING_RANGES[key]:
            raise ValidationError("video_preset 取值不合法")
        return value
    if key == "quality_preset":
        if value not in SETTING_RANGES[key]:
            raise ValidationError("quality_preset 取值不合法")
        return value
    if key == "name_pattern":
        if not isinstance(value, str) or not value.strip() or len(value) > 120:
            raise ValidationError("命名模板不能为空且不超过 120 字符")
        return value.strip()
    if key == "default_output_dir":
        if not isinstance(value, str):
            raise ValidationError("默认导出目录必须是文本")
        return value
    return value

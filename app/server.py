"""HTTP server for NAS Transcode Studio (Python standard library only)."""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import shutil
import signal
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .capabilities import Capabilities
from .config import APP_NAME, APP_VERSION, Config
from .db import Database
from .engine import Engine
from .jobs import JobManager
from .security import AppError, ConflictError, NotFoundError, ValidationError
from .service import Service

log = logging.getLogger("transcoder.http")
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
MAX_BODY = 4 * 1024 * 1024


class AppContext:
    def __init__(self, cfg: Config):
        cfg.prepare()
        self.cfg = cfg
        self.db = Database(cfg)
        self.caps = Capabilities(cfg)
        for tool in self.caps.tool_list():
            if not tool.available:
                log.warning("转码工具不可用：%s (%s)", tool.name, tool.error or "未探测")
        for warning in self.caps.warnings:
            log.warning("能力探测：%s", warning)
        self.source_readonly = cfg.source_mount_readonly()
        if self.source_readonly is False:
            self.caps.warnings.append("警告：/host 未以只读方式挂载；应用仍拒绝写入源目录")
            log.error("source mount is not read-only; application write guard remains active")
        self.engine = Engine(cfg, self.caps)
        self.jobs = JobManager(cfg, self.db, self.caps, self.engine)
        self.service = Service(cfg, self.db, self.caps, self.engine, self.jobs)
        self.started_at = time.time()

    def close(self) -> None:
        try:
            self.jobs.stop()
        finally:
            self.db.close()


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], ctx: AppContext):
        super().__init__(address, Handler)
        self.ctx = ctx


class Handler(BaseHTTPRequestHandler):
    server_version = "NAS-Transcode-Studio/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def ctx(self) -> AppContext:
        return self.server.ctx  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ helpers
    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/api/") and self.command not in ("GET", "HEAD"):
            log.info("%s %s", self.command, self.path)

    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._common_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def error(self, exc: Exception) -> None:
        if isinstance(exc, AppError):
            self.send_json(
                {"ok": False, "error": {"code": exc.code, "message": exc.message}},
                status=exc.status,
            )
        else:
            log.error("unhandled API error on %s: %s\n%s", self.path, exc, traceback.format_exc())
            self.send_json(
                {"ok": False, "error": {"code": "internal_error", "message": "服务器内部错误，请查看日志"}},
                status=500,
            )

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValidationError("Content-Length 非法")
        if length < 0 or length > MAX_BODY:
            raise ValidationError("请求体过大")
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("请求体不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _route_path(self) -> str:
        return unquote(urlparse(self.path).path)

    # -------------------------------------------------------------------- verbs
    def do_GET(self) -> None:
        try:
            path = self._route_path()
            if path.startswith("/api/"):
                self._api_get(path)
            else:
                self._serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - API boundary
            self.error(exc)

    def do_HEAD(self) -> None:
        try:
            path = self._route_path()
            if path == "/api/health":
                payload = self.ctx.service.health()
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._common_headers()
                self.end_headers()
                return
            self._serve_static(path, head=True)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self.error(exc)

    def do_POST(self) -> None:
        try:
            self._api_mutation("POST")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self.error(exc)

    def do_PATCH(self) -> None:
        try:
            self._api_mutation("PATCH")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self.error(exc)

    def do_DELETE(self) -> None:
        try:
            self._api_mutation("DELETE")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self.error(exc)

    # ------------------------------------------------------------------- static
    def _serve_static(self, path: str, head: bool = False) -> None:
        name = "index.html" if path in ("", "/") else path
        if name.startswith("/static/"):
            name = name[len("/static/"):]
        else:
            name = name.lstrip("/")
        allowed = {"index.html", "app.js", "style.css", "favicon.svg"}
        if name not in allowed:
            raise NotFoundError("页面不存在")
        full = os.path.join(self.ctx.cfg.static_dir, name)
        if not os.path.isfile(full):
            raise NotFoundError("静态资源不存在")
        content = open(full, "rb").read()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(name)[1], "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self._common_headers()
        self.end_headers()
        if not head:
            self.wfile.write(content)

    # ---------------------------------------------------------------------- GET
    def _api_get(self, path: str) -> None:
        query = self._query()
        svc = self.ctx.service
        if path == "/api/health":
            self.send_json(svc.health())
        elif path == "/api/bootstrap":
            self.send_json(svc.bootstrap())
        elif path == "/api/state":
            self.send_json(svc.state(_int(query.get("after_log"), 0)))
        elif path == "/api/logs":
            self.send_json({"ok": True, "logs": svc.logs(_int(query.get("after"), 0), _int(query.get("limit"), 300))})
        elif path == "/api/fs/list":
            self.send_json(svc.list_source(
                query.get("path") or self.ctx.cfg.source_root,
                q=query.get("q", ""),
                kind=query.get("kind", "all"),
                show_hidden=_bool(query.get("hidden"), False),
                offset=_int(query.get("offset"), 0),
                limit=_int(query.get("limit"), 300),
            ))
        elif path == "/api/output/list":
            self.send_json(svc.list_output(query.get("path") or self.ctx.cfg.output_root))
        elif path == "/api/actions":
            self.send_json({"ok": True, "actions": svc.actions(_int(query.get("limit"), 30))})
        elif path == "/api/settings":
            self.send_json({"ok": True, "settings": self.ctx.db.settings()})
        elif path == "/api/capabilities":
            self.send_json({"ok": True, "capabilities": self.ctx.caps.public()})
        elif path == "/api/download":
            self._download(query.get("path", ""))
        else:
            raise NotFoundError("接口不存在")

    def _download(self, path: str) -> None:
        real, filename, size = self.ctx.service.download_path(path)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "download"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}")
        self.send_header("Cache-Control", "no-store")
        self._common_headers()
        self.end_headers()
        with open(real, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1024 * 1024)

    # ----------------------------------------------------------------- mutations
    def _api_mutation(self, method: str) -> None:
        path = self._route_path()
        if not path.startswith("/api/"):
            raise NotFoundError("接口不存在")
        body = self._read_json()
        svc = self.ctx.service
        jobs = self.ctx.jobs
        if path == "/api/jobs" and method == "POST":
            self.send_json(svc.create_jobs(body), status=201)
            return
        if path == "/api/settings" and method in ("POST", "PATCH"):
            self.send_json(svc.update_settings(body))
            return
        if path == "/api/capabilities/refresh" and method == "POST":
            if self.ctx.jobs.active:
                raise ConflictError("有任务正在运行，暂不能重新探测引擎；请等任务结束或先终止")
            self.ctx.caps.refresh()
            self.ctx.jobs.log("info", "已重新探测转码引擎")
            self.send_json({"ok": True, "capabilities": self.ctx.caps.public()})
            return
        if path == "/api/output/mkdir" and method == "POST":
            self.send_json(svc.mkdir(str(body.get("parent") or self.ctx.cfg.output_root), str(body.get("name") or "")))
            return
        if path == "/api/undo" and method == "POST":
            self.send_json(svc.undo_latest())
            return
        if path == "/api/queue/pause" and method == "POST":
            self.send_json({"ok": True, "queue": self.ctx.jobs.pause_all()})
            return
        if path == "/api/queue/resume" and method == "POST":
            self.send_json({"ok": True, "queue": self.ctx.jobs.resume_all()})
            return
        if path == "/api/queue/stop" and method == "POST":
            self.send_json({"ok": True, "queue": self.ctx.jobs.stop_all()})
            return
        if path == "/api/queue/retry-failed" and method == "POST":
            self.send_json({"ok": True, **svc.retry_failed()})
            return
        if path == "/api/queue/clear-completed" and method == "POST":
            self.send_json({"ok": True, **svc.clear_completed()})
            return
        match = re.fullmatch(r"/api/actions/(\d+)/undo", path)
        if match and method == "POST":
            self.send_json(svc.undo_action(int(match.group(1))))
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9]+)(?:/(pause|resume|cancel|retry|restore|undo-output))?", path)
        if match:
            job_id, action = match.group(1), match.group(2)
            if method == "PATCH" and not action:
                self.send_json(svc.update_job(job_id, body))
                return
            if method == "DELETE" and not action:
                self.send_json(svc.delete_job(job_id))
                return
            if method == "POST" and action:
                if action == "pause":
                    jobs.pause_job(job_id)
                elif action == "resume":
                    jobs.resume_job(job_id)
                elif action == "cancel":
                    jobs.cancel_job(job_id)
                elif action == "retry":
                    jobs.retry_job(job_id)
                elif action == "restore":
                    self.send_json(svc.restore_job(job_id))
                    return
                elif action == "undo-output":
                    self.send_json(svc.undo_output(job_id))
                    return
                self.send_json({"ok": True, "job": self._job_public(svc.db.get_job(job_id))})
                return
        raise NotFoundError("接口不存在")

    def _job_public(self, job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        return self.ctx.service._public_jobs([job])[0]  # noqa: SLF001


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("transcoder.http").setLevel(logging.INFO)


def build_context(cfg: Config | None = None) -> AppContext:
    return AppContext(cfg or Config.from_env())


def serve(cfg: Config | None = None) -> None:
    _setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    ctx = build_context(cfg)
    server = AppServer((ctx.cfg.host, ctx.cfg.port), ctx)
    stop = threading.Event()

    def handle_signal(signum: int, _frame: Any) -> None:
        if stop.is_set():
            return
        stop.set()
        log.info("received signal %s, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    ctx.jobs.start()
    log.info("%s %s listening on http://%s:%s", APP_NAME, APP_VERSION, ctx.cfg.host, ctx.cfg.port)
    log.info("source=%s (mount ro=%s) output=%s data=%s", ctx.cfg.source_root, ctx.source_readonly, ctx.cfg.output_root, ctx.cfg.data_dir)
    for warning in ctx.caps.warnings:
        log.warning("capability: %s", warning)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        ctx.close()
        log.info("stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NAS Transcode Studio")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--check", action="store_true", help="probe tools and exit")
    args = parser.parse_args(argv)
    cfg = Config.from_env()
    if args.port:
        cfg.port = args.port
    if args.host:
        cfg.host = args.host
    if args.check:
        _setup_logging()
        cfg.prepare()
        caps = Capabilities(cfg)
        print(json.dumps({
            "version": APP_VERSION,
            "tools": [t.public() for t in (caps.ffmpeg, caps.ffprobe, caps.magick, caps.sevenzip, caps.bsdtar)],
            "warnings": caps.warnings,
            "sources": len(caps.matrix),
        }, ensure_ascii=False, indent=2))
        return 0
    try:
        serve(cfg)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - startup boundary
        log.error("startup failed: %s\n%s", exc, traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

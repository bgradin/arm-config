from __future__ import annotations

import json
import re
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .domain import HIGH_ORDER_CONFIDENCE
from .importer import import_manifest
from .planner import build_plan, commit_plan
from .tmdb import TMDBClient, TMDBError
from .util import ensure_within
from .web import render_template, static_asset
from .worker import Worker
from .workflow import approval_errors, approval_fingerprint, approve_job


class Application:
    def __init__(self, config: Config, database: Database):
        self.config = config
        self.database = database
        self.tmdb = TMDBClient(config.tmdb_token, database)

    def job_detail(self, job_id: str) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        errors = approval_errors(self.config, self.database, job_id)
        plan = self.database.latest_plan(job_id)
        if not errors:
            if not plan or plan["status"] != "draft":
                errors.append("Create a dry-run plan before approval.")
            elif plan["plan"].get("approval_fingerprint") != approval_fingerprint(
                self.database, job_id
            ):
                errors.append("Review decisions changed; create a new dry-run plan.")
        return {
            "job": job,
            "sources": self.database.list_sources(job_id),
            "assets": self.database.list_assets(job_id),
            "assignments": self.database.list_assignments(job_id),
            "suggestions": self.database.list_suggestions(job_id),
            "audit": self.database.list_audit(job_id),
            "tasks": self.database.list_tasks(job_id),
            "approval_errors": errors,
            "plan": plan,
        }

    def dashboard(self) -> bytes:
        return render_template(
            "dashboard.jinja",
            title="TV Organizer",
            jobs=self.database.list_jobs(),
        )

    def _tmdb_results(self, query: str) -> tuple[list[dict[str, Any]], str | None]:
        if not query:
            return [], None
        try:
            result = self.tmdb.search_tv(query)
        except TMDBError as exc:
            return [], str(exc)
        return result.get("results", [])[:10], None

    def job_page(self, job_id: str, query: dict[str, list[str]]) -> bytes:
        detail = self.job_detail(job_id)
        job = detail["job"]
        assignments_by_asset = {
            item["asset_id"]: item for item in detail["assignments"]
        }
        tmdb_query = query.get("q", [""])[0]
        tmdb_results, tmdb_error = self._tmdb_results(tmdb_query)
        return render_template(
            "job.jinja",
            title=job.get("show_name") or "Review disc",
            base_path=f"/jobs/{job_id}",
            assignments_by_asset=assignments_by_asset,
            tmdb_query=tmdb_query,
            tmdb_results=tmdb_results,
            tmdb_error=tmdb_error,
            dispositions=("unresolved", "episode", "extra", "duplicate", "ignore"),
            high_order_confidence=HIGH_ORDER_CONFIDENCE,
            **detail,
        )


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ArmTvOrganizer/0.1"

    @property
    def app(self) -> Application:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _authorized(self) -> bool:
        if urllib.parse.urlparse(self.path).path == "/health":
            return True
        expected = self.app.config.auth_token
        if not expected:
            return True
        value = self.headers.get("Authorization", "")
        return value == f"Bearer {expected}"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'; script-src 'self'; style-src 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        self._send(
            status,
            json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _redirect(self, path: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            raise ValueError("Request body is too large")
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            return json.loads(body or b"{}")
        parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _handle_error(self, exc: Exception) -> None:
        status = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
        if isinstance(exc, TMDBError):
            status = HTTPStatus.BAD_GATEWAY
        message = str(exc) or exc.__class__.__name__
        if self.path.startswith("/api/"):
            self._json(status, {"error": message})
        else:
            self._send(
                status,
                render_template("error.jinja", title="Error", message=message),
                "text/html; charset=utf-8",
            )

    def do_GET(self) -> None:
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self._json(HTTPStatus.OK, {"status": "ok", "version": "0.1.0"})
            elif match := re.fullmatch(r"/static/([a-z0-9.-]+)", parsed.path):
                body, content_type = static_asset(match.group(1))
                self._send(HTTPStatus.OK, body, content_type)
            elif parsed.path == "/":
                self._send(HTTPStatus.OK, self.app.dashboard(), "text/html; charset=utf-8")
            elif match := re.fullmatch(r"/jobs/([0-9a-f-]+)", parsed.path):
                self._send(
                    HTTPStatus.OK,
                    self.app.job_page(match.group(1), query),
                    "text/html; charset=utf-8",
                )
            elif parsed.path == "/api/jobs":
                self._json(HTTPStatus.OK, self.app.database.list_jobs())
            elif match := re.fullmatch(r"/api/jobs/([0-9a-f-]+)", parsed.path):
                self._json(HTTPStatus.OK, self.app.job_detail(match.group(1)))
            elif parsed.path == "/api/tmdb/search":
                value = self.app.tmdb.search_tv(
                    query.get("q", [""])[0],
                    year=int(query["year"][0]) if query.get("year") else None,
                )
                self._json(HTTPStatus.OK, value)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except Exception as exc:
            self._handle_error(exc)

    @staticmethod
    def _integer(value: Any) -> int | None:
        return int(value) if value not in (None, "") else None

    def _apply_order(self, job_id: str, data: dict[str, Any]) -> None:
        suggestion = self.app.database.get_suggestion(data["suggestion_id"])
        if not suggestion or suggestion["job_id"] != job_id:
            raise KeyError(data["suggestion_id"])
        if suggestion["kind"] != "episode_order":
            raise ValueError("Suggestion is not an episode order")
        if suggestion["confidence"] < HIGH_ORDER_CONFIDENCE:
            raise ValueError(
                "Order evidence is below the high-confidence application threshold"
            )
        start = int(data["start"])
        job = self.app.database.get_job(job_id)
        assert job is not None
        assets = self.app.database.list_assets(job_id)
        source_assets: dict[str, list[dict[str, Any]]] = {}
        for asset in assets:
            if asset.get("source_title_id"):
                source_assets.setdefault(asset["source_title_id"], []).append(asset)
        episode = start
        for source_id in suggestion["value"].get("source_ids", []):
            mapped = source_assets.get(source_id, [])
            if len(mapped) != 1:
                raise ValueError(
                    f"Source {source_id} does not map to exactly one asset"
                )
            asset = mapped[0]
            self.app.database.assign_asset(
                job_id,
                asset["id"],
                disposition="episode",
                season=int(job["season"]) if job.get("season") is not None else None,
                episode_start=episode,
            )
            episode += 1

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            data = self._body()
            if parsed.path in {"/import", "/api/import"}:
                manifest = ensure_within(
                    Path(data["manifest_path"]), self.app.config.data_root
                )
                rip_root = ensure_within(
                    Path(data["rip_root"]), self.app.config.inbox_root
                )
                job_id = import_manifest(
                    self.app.config,
                    self.app.database,
                    manifest,
                    rip_root=rip_root,
                )
                if parsed.path.startswith("/api/"):
                    self._json(HTTPStatus.CREATED, {"job_id": job_id})
                else:
                    self._redirect(f"/jobs/{job_id}")
                return
            match = re.fullmatch(r"/jobs/([0-9a-f-]+)/(.*)", parsed.path)
            if not match:
                raise KeyError(parsed.path)
            job_id, action = match.groups()
            if action == "resolve":
                values = {}
                for key in (
                    "resolved_media_type",
                    "show_provider",
                    "show_id",
                    "show_name",
                ):
                    if key in data:
                        values[key] = data.get(key) or None
                for key in ("show_year", "season"):
                    if key in data:
                        values[key] = self._integer(data.get(key))
                self.app.database.resolve_job(job_id, values)
            elif action == "analyze":
                job = self.app.database.get_job(job_id)
                if not job:
                    raise KeyError(job_id)
                if job["state"] != "analyzing":
                    self.app.database.transition(job_id, "analyzing", actor="user")
                self.app.database.enqueue("analyze", job_id)
            elif action == "approve":
                approve_job(self.app.config, self.app.database, job_id)
            elif action == "retry":
                self.app.database.retry_failed_tasks(job_id)
            elif action == "plan":
                build_plan(self.app.config, self.app.database, job_id)
            elif action == "commit":
                commit_plan(self.app.config, self.app.database, job_id)
            elif action == "apply-order":
                self._apply_order(job_id, data)
            elif suggestion_match := re.fullmatch(
                r"suggestions/([0-9a-f-]+)", action
            ):
                suggestion_id = suggestion_match.group(1)
                suggestion = self.app.database.get_suggestion(suggestion_id)
                if not suggestion:
                    raise KeyError(suggestion_id)
                decision = data.get("action")
                self.app.database.decide_suggestion(
                    job_id, suggestion_id, decision
                )
                if decision == "accepted":
                    if suggestion["kind"] == "media_type":
                        self.app.database.resolve_job(
                            job_id,
                            {
                                "resolved_media_type": suggestion["value"].get(
                                    "media_type"
                                )
                            },
                        )
                    elif suggestion["kind"] == "season":
                        self.app.database.resolve_job(
                            job_id,
                            {"season": suggestion["value"].get("season")},
                        )
            elif asset_match := re.fullmatch(r"assets/([0-9a-f-]+)", action):
                self.app.database.assign_asset(
                    job_id,
                    asset_match.group(1),
                    disposition=data["disposition"],
                    season=self._integer(data.get("season")),
                    episode_start=self._integer(data.get("episode_start")),
                    episode_end=self._integer(data.get("episode_end")),
                    part=self._integer(data.get("part")),
                    episode_title=data.get("episode_title") or None,
                    edition_name=data.get("edition_name") or None,
                    preferred=data.get("preferred") == "1",
                )
            else:
                raise KeyError(action)
            self._redirect(f"/jobs/{job_id}")
        except Exception as exc:
            traceback.print_exc()
            self._handle_error(exc)


class OrganizerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: Application):
        super().__init__(address, RequestHandler)
        self.app = app


def serve(config: Config, database: Database) -> None:
    config.ensure_directories()
    database.initialize()
    worker = Worker(config, database)
    if config.worker_enabled:
        worker.start_thread()
    server = OrganizerHTTPServer((config.bind, config.port), Application(config, database))
    try:
        print(f"ARM TV Organizer listening on http://{config.bind}:{config.port}")
        server.serve_forever()
    finally:
        worker.stop()
        server.server_close()

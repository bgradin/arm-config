from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .domain import validate_transition
from .util import canonical_json, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    arm_job_id TEXT,
    disc_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    disc_type TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    rip_root TEXT,
    resolved_media_type TEXT,
    show_provider TEXT,
    show_id TEXT,
    show_name TEXT,
    show_year INTEGER,
    season INTEGER,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint
    ON jobs(disc_fingerprint, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, updated_at);

CREATE TABLE IF NOT EXISTS source_titles (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    source_type TEXT NOT NULL,
    label TEXT,
    duration_seconds REAL,
    topology_hash TEXT,
    chapter_fingerprint TEXT,
    stream_fingerprint TEXT,
    payload_json TEXT NOT NULL,
    UNIQUE(job_id, source_key)
);

CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_title_id TEXT REFERENCES source_titles(id) ON DELETE SET NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT,
    quick_fingerprint TEXT NOT NULL,
    duration_seconds REAL,
    chapter_fingerprint TEXT,
    stream_fingerprint TEXT,
    metadata_json TEXT NOT NULL,
    disposition TEXT NOT NULL DEFAULT 'unresolved',
    edition_name TEXT,
    preferred INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, path)
);

CREATE TABLE IF NOT EXISTS suggestions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL,
    contradictions_json TEXT NOT NULL,
    analyzer TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    input_manifest_hash TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_suggestions_job
    ON suggestions(job_id, kind, revision);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    action TEXT NOT NULL,
    value_json TEXT NOT NULL,
    suggestion_id TEXT REFERENCES suggestions(id) ON DELETE SET NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    season INTEGER,
    episode_start INTEGER,
    episode_end INTEGER,
    part INTEGER,
    episode_title TEXT,
    edition_name TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(asset_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_queue
    ON tasks(status, created_at);

CREATE TABLE IF NOT EXISTS organization_moves (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(job_id, asset_id, target_path)
);

CREATE INDEX IF NOT EXISTS idx_organization_moves_job
    ON organization_moves(job_id, status, created_at);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    committed_at TEXT
);

CREATE TABLE IF NOT EXISTS tmdb_cache (
    cache_key TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

PRAGMA user_version = 1;
"""


def _decoded(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def audit(
        self,
        event_type: str,
        payload: Any,
        job_id: str | None = None,
        actor: str = "system",
        connection: sqlite3.Connection | None = None,
    ) -> None:
        values = (job_id, event_type, actor, canonical_json(payload), utc_now())
        if connection is not None:
            connection.execute(
                "INSERT INTO audit_events "
                "(job_id, event_type, actor, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                values,
            )
            return
        with self.connect() as own:
            own.execute(
                "INSERT INTO audit_events "
                "(job_id, event_type, actor, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                values,
            )

    def create_job(
        self,
        *,
        disc_fingerprint: str,
        disc_type: str,
        manifest_path: Path,
        manifest_hash: str,
        state: str = "awaiting_assets",
        arm_job_id: str | None = None,
        rip_root: Path | None = None,
    ) -> str:
        job_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, arm_job_id, disc_fingerprint, state, disc_type,
                    manifest_path, manifest_hash, rip_root, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    arm_job_id,
                    disc_fingerprint,
                    state,
                    disc_type,
                    str(manifest_path),
                    manifest_hash,
                    str(rip_root) if rip_root else None,
                    now,
                    now,
                ),
            )
            self.audit(
                "job.created",
                {"state": state, "disc_fingerprint": disc_fingerprint},
                job_id,
                connection=connection,
            )
        return job_id

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return _decoded(
                connection.execute(
                    "SELECT * FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
            )

    def find_existing_job(
        self,
        *,
        arm_job_id: str | None,
        manifest_path: Path,
        manifest_hash: str,
        rip_root: Path | None,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            if arm_job_id:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE arm_job_id = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (arm_job_id,),
                ).fetchone()
                if row:
                    return dict(row)
            row = connection.execute(
                "SELECT * FROM jobs WHERE manifest_path = ? AND manifest_hash = ? "
                "AND rip_root IS ? ORDER BY created_at DESC LIMIT 1",
                (
                    str(manifest_path),
                    manifest_hash,
                    str(rip_root.resolve()) if rip_root else None,
                ),
            ).fetchone()
            return dict(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def transition(
        self,
        job_id: str,
        target: str,
        *,
        error: str | None = None,
        actor: str = "system",
    ) -> None:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT state FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = row["state"]
            validate_transition(current, target)
            connection.execute(
                "UPDATE jobs SET state = ?, error = ?, updated_at = ? "
                "WHERE id = ?",
                (target, error, utc_now(), job_id),
            )
            self.audit(
                "job.transition",
                {"from": current, "to": target, "error": error},
                job_id,
                actor,
                connection,
            )

    def resolve_job(
        self,
        job_id: str,
        values: dict[str, Any],
        actor: str = "user",
    ) -> None:
        allowed = {
            "resolved_media_type",
            "show_provider",
            "show_id",
            "show_name",
            "show_year",
            "season",
        }
        update = {key: value for key, value in values.items() if key in allowed}
        if not update:
            return
        columns = ", ".join(f"{key} = ?" for key in update)
        parameters = list(update.values()) + [utc_now(), job_id]
        with self.transaction() as connection:
            connection.execute(
                f"UPDATE jobs SET {columns}, updated_at = ? WHERE id = ?",
                parameters,
            )
            for field, value in update.items():
                connection.execute(
                    "INSERT INTO decisions "
                    "(id, job_id, field, action, value_json, actor, created_at) "
                    "VALUES (?, ?, ?, 'set', ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        job_id,
                        field,
                        canonical_json(value),
                        actor,
                        utc_now(),
                    ),
                )
            self.audit(
                "job.resolved",
                update,
                job_id,
                actor,
                connection,
            )
            resolved = connection.execute(
                "SELECT resolved_media_type, show_provider, show_id, show_name, season "
                "FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if resolved and resolved["resolved_media_type"] == "tv" and all(
                resolved[field] not in (None, "")
                for field in ("show_provider", "show_id", "show_name", "season")
            ):
                self._enqueue_unique_connection(
                    connection, "organize", job_id, {}, requeue_complete=True
                )

    def set_rip_root(self, job_id: str, rip_root: Path) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET rip_root = ?, updated_at = ? WHERE id = ?",
                (str(rip_root), utc_now(), job_id),
            )

    def replace_sources(self, job_id: str, sources: list[dict[str, Any]]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM source_titles WHERE job_id = ?", (job_id,)
            )
            for source in sources:
                connection.execute(
                    """
                    INSERT INTO source_titles (
                        id, job_id, source_key, source_type, label,
                        duration_seconds, topology_hash, chapter_fingerprint,
                        stream_fingerprint, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source["id"],
                        job_id,
                        source["source_key"],
                        source["source_type"],
                        source.get("label"),
                        source.get("duration_seconds"),
                        source.get("topology_hash"),
                        source.get("chapter_fingerprint"),
                        source.get("stream_fingerprint"),
                        canonical_json(source.get("payload", {})),
                    ),
                )

    def list_sources(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM source_titles WHERE job_id = ? "
                "ORDER BY source_key",
                (job_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                result.append(item)
            return result

    def replace_assets(self, job_id: str, assets: list[dict[str, Any]]) -> None:
        with self.transaction() as connection:
            old = {
                row["path"]: dict(row)
                for row in connection.execute(
                    "SELECT * FROM assets WHERE job_id = ?", (job_id,)
                )
            }
            old_assignments = {
                row["asset_id"]: dict(row)
                for row in connection.execute(
                    "SELECT * FROM assignments WHERE job_id = ?", (job_id,)
                )
            }
            connection.execute("DELETE FROM assets WHERE job_id = ?", (job_id,))
            for asset in assets:
                previous = old.get(asset["path"], {})
                connection.execute(
                    """
                    INSERT INTO assets (
                        id, job_id, source_title_id, path, size_bytes, sha256,
                        quick_fingerprint, duration_seconds,
                        chapter_fingerprint, stream_fingerprint, metadata_json,
                        disposition, edition_name, preferred, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset["id"],
                        job_id,
                        asset.get("source_title_id"),
                        asset["path"],
                        asset["size_bytes"],
                        asset.get("sha256"),
                        asset["quick_fingerprint"],
                        asset.get("duration_seconds"),
                        asset.get("chapter_fingerprint"),
                        asset.get("stream_fingerprint"),
                        canonical_json(asset.get("metadata", {})),
                        previous.get("disposition", "unresolved"),
                        previous.get("edition_name"),
                        previous.get("preferred", 0),
                        previous.get("created_at", utc_now()),
                    ),
                )
                assignment = old_assignments.get(asset["id"])
                if assignment:
                    connection.execute(
                        """
                        INSERT INTO assignments (
                            id, job_id, asset_id, season, episode_start,
                            episode_end, part, episode_title, edition_name,
                            updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            assignment["id"],
                            job_id,
                            asset["id"],
                            assignment["season"],
                            assignment["episode_start"],
                            assignment["episode_end"],
                            assignment["part"],
                            assignment["episode_title"],
                            assignment["edition_name"],
                            assignment["updated_at"],
                        ),
                    )

    def list_assets(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*, s.source_key
                FROM assets a
                LEFT JOIN source_titles s ON s.id = a.source_title_id
                WHERE a.job_id = ?
                ORDER BY a.path
                """,
                (job_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["metadata"] = json.loads(item.pop("metadata_json"))
                result.append(item)
            return result

    def assign_asset(
        self,
        job_id: str,
        asset_id: str,
        *,
        disposition: str,
        season: int | None = None,
        episode_start: int | None = None,
        episode_end: int | None = None,
        part: int | None = None,
        episode_title: str | None = None,
        edition_name: str | None = None,
        preferred: bool = False,
        actor: str = "user",
    ) -> None:
        from .domain import ASSET_DISPOSITIONS

        if disposition not in ASSET_DISPOSITIONS:
            raise ValueError(f"Unknown disposition: {disposition}")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM assets WHERE id = ? AND job_id = ?",
                (asset_id, job_id),
            ).fetchone()
            if row is None:
                raise KeyError(asset_id)
            connection.execute(
                "UPDATE assets SET disposition = ?, edition_name = ?, "
                "preferred = ? WHERE id = ?",
                (disposition, edition_name, int(preferred), asset_id),
            )
            if disposition == "episode":
                connection.execute(
                    """
                    INSERT INTO assignments (
                        id, job_id, asset_id, season, episode_start,
                        episode_end, part, episode_title, edition_name,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(asset_id) DO UPDATE SET
                        season = excluded.season,
                        episode_start = excluded.episode_start,
                        episode_end = excluded.episode_end,
                        part = excluded.part,
                        episode_title = excluded.episode_title,
                        edition_name = excluded.edition_name,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(uuid.uuid4()),
                        job_id,
                        asset_id,
                        season,
                        episode_start,
                        episode_end,
                        part,
                        episode_title,
                        edition_name,
                        utc_now(),
                    ),
                )
            else:
                connection.execute(
                    "DELETE FROM assignments WHERE asset_id = ?", (asset_id,)
                )
            self.audit(
                "asset.assigned",
                {
                    "asset_id": asset_id,
                    "disposition": disposition,
                    "season": season,
                    "episode_start": episode_start,
                    "episode_end": episode_end,
                    "part": part,
                    "edition_name": edition_name,
                    "preferred": preferred,
                },
                job_id,
                actor,
                connection,
            )
            if disposition == "episode":
                self._enqueue_unique_connection(
                    connection, "organize", job_id, {}, requeue_complete=True
                )

    def list_assignments(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM assignments WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
            ]

    def add_suggestions(
        self,
        job_id: str,
        manifest_hash: str,
        suggestions: list[dict[str, Any]],
    ) -> None:
        with self.transaction() as connection:
            revision = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM suggestions "
                "WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE suggestions SET status = 'superseded' "
                "WHERE job_id = ? AND status = 'pending'",
                (job_id,),
            )
            for suggestion in suggestions:
                connection.execute(
                    """
                    INSERT INTO suggestions (
                        id, job_id, kind, value_json, confidence,
                        evidence_json, contradictions_json, analyzer,
                        analyzer_version, input_manifest_hash, revision,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        job_id,
                        suggestion["kind"],
                        canonical_json(suggestion["value"]),
                        suggestion["confidence"],
                        canonical_json(suggestion.get("evidence", [])),
                        canonical_json(suggestion.get("contradictions", [])),
                        suggestion["analyzer"],
                        suggestion["analyzer_version"],
                        manifest_hash,
                        revision,
                        utc_now(),
                    ),
                )
            self.audit(
                "analysis.completed",
                {"revision": revision, "suggestion_count": len(suggestions)},
                job_id,
                connection=connection,
            )

    def list_suggestions(
        self, job_id: str, include_superseded: bool = False
    ) -> list[dict[str, Any]]:
        where = "job_id = ?" if include_superseded else "job_id = ? AND status != 'superseded'"
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM suggestions WHERE {where} "
                "ORDER BY revision DESC, confidence DESC, kind",
                (job_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                for key in ("value_json", "evidence_json", "contradictions_json"):
                    item[key.removesuffix("_json")] = json.loads(item.pop(key))
                result.append(item)
            return result

    def get_suggestion(self, suggestion_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            for key in ("value_json", "evidence_json", "contradictions_json"):
                item[key.removesuffix("_json")] = json.loads(item.pop(key))
            return item

    def decide_suggestion(
        self,
        job_id: str,
        suggestion_id: str,
        action: str,
        actor: str = "user",
    ) -> None:
        if action not in {"accepted", "rejected"}:
            raise ValueError(f"Invalid suggestion action: {action}")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT kind, value_json FROM suggestions "
                "WHERE id = ? AND job_id = ?",
                (suggestion_id, job_id),
            ).fetchone()
            if row is None:
                raise KeyError(suggestion_id)
            connection.execute(
                "UPDATE suggestions SET status = ? WHERE id = ?",
                (action, suggestion_id),
            )
            connection.execute(
                "INSERT INTO decisions "
                "(id, job_id, field, action, value_json, suggestion_id, "
                "actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    job_id,
                    row["kind"],
                    action,
                    row["value_json"],
                    suggestion_id,
                    actor,
                    utc_now(),
                ),
            )
            self.audit(
                "suggestion.decided",
                {"suggestion_id": suggestion_id, "action": action},
                job_id,
                actor,
                connection,
            )

    def list_audit(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE job_id = ? ORDER BY id DESC",
                (job_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                result.append(item)
            return result

    @staticmethod
    def _enqueue_unique_connection(
        connection: sqlite3.Connection,
        kind: str,
        job_id: str | None,
        payload: Any = None,
        *,
        requeue_complete: bool = False,
    ) -> str:
        now = utc_now()
        existing = connection.execute(
            "SELECT id, status FROM tasks WHERE kind = ? AND job_id IS ? "
            "ORDER BY created_at DESC LIMIT 1",
            (kind, job_id),
        ).fetchone()
        if existing and existing["status"] != "failed":
            if existing["status"] == "complete" and requeue_complete:
                connection.execute(
                    "UPDATE tasks SET status = 'queued', attempts = 0, "
                    "error = NULL, payload_json = ?, updated_at = ? WHERE id = ?",
                    (canonical_json(payload or {}), now, existing["id"]),
                )
            else:
                return str(existing["id"])
            return str(existing["id"])
        if existing:
            connection.execute(
                "UPDATE tasks SET status = 'queued', attempts = 0, "
                "error = NULL, payload_json = ?, updated_at = ? WHERE id = ?",
                (canonical_json(payload or {}), now, existing["id"]),
            )
            return str(existing["id"])
        task_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO tasks "
            "(id, job_id, kind, payload_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, job_id, kind, canonical_json(payload or {}), now, now),
        )
        return task_id

    def enqueue(self, kind: str, job_id: str | None, payload: Any = None) -> str:
        task_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks "
                "(id, job_id, kind, payload_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, job_id, kind, canonical_json(payload or {}), now, now),
            )
        return task_id

    def enqueue_unique(
        self,
        kind: str,
        job_id: str | None,
        payload: Any = None,
        *,
        requeue_complete: bool = False,
    ) -> str:
        """Idempotently enqueue a lifecycle event for one job and task kind."""

        with self.transaction(immediate=True) as connection:
            return self._enqueue_unique_connection(
                connection,
                kind,
                job_id,
                payload,
                requeue_complete=requeue_complete,
            )

    def list_tasks(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE job_id = ? ORDER BY created_at DESC",
                (job_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                result.append(item)
            return result

    def list_all_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                result.append(item)
            return result

    def list_active_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status IN ('queued', 'running') "
                "ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                result.append(item)
            return result

    def list_moves(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM organization_moves WHERE job_id = ? "
                    "ORDER BY created_at DESC",
                    (job_id,),
                ).fetchall()
            ]

    def prepare_move(
        self,
        job_id: str,
        asset_id: str,
        source_path: str,
        target_path: str,
        size_bytes: int,
        sha256: str | None,
    ) -> dict[str, Any]:
        """Durably record an intended move before touching the filesystem."""

        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM organization_moves WHERE job_id = ? "
                "AND asset_id = ? AND target_path = ?",
                (job_id, asset_id, target_path),
            ).fetchone()
            if row is not None:
                return dict(row)
            move_id = str(uuid.uuid4())
            now = utc_now()
            connection.execute(
                "INSERT INTO organization_moves "
                "(id, job_id, asset_id, source_path, target_path, size_bytes, "
                "sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    move_id,
                    job_id,
                    asset_id,
                    source_path,
                    target_path,
                    size_bytes,
                    sha256,
                    now,
                ),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM organization_moves WHERE id = ?", (move_id,)
                ).fetchone()
            )

    def start_move(self, move_id: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE organization_moves SET status = 'moving', started_at = ?, "
                "error = NULL WHERE id = ? AND status != 'moved'",
                (utc_now(), move_id),
            )

    def complete_move(self, move_id: str) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM organization_moves WHERE id = ?", (move_id,)
            ).fetchone()
            if row is None:
                raise KeyError(move_id)
            if row["status"] != "moved":
                connection.execute(
                    "UPDATE organization_moves SET status = 'moved', error = NULL, "
                    "completed_at = ? WHERE id = ?",
                    (utc_now(), move_id),
                )
                connection.execute(
                    "UPDATE assets SET path = ? WHERE id = ? AND job_id = ?",
                    (row["target_path"], row["asset_id"], row["job_id"]),
                )
            self.audit(
                "asset.moved",
                {
                    "move_id": move_id,
                    "asset_id": row["asset_id"],
                    "source": row["source_path"],
                    "target": row["target_path"],
                },
                row["job_id"],
                "worker",
                connection,
            )
            return dict(
                connection.execute(
                    "SELECT * FROM organization_moves WHERE id = ?", (move_id,)
                ).fetchone()
            )

    def fail_move(self, move_id: str, error: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE organization_moves SET status = 'failed', error = ? "
                "WHERE id = ?",
                (error, move_id),
            )

    def retry_failed_tasks(self, job_id: str) -> int:
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status = 'queued', attempts = 0, error = NULL, "
                "updated_at = ? WHERE job_id = ? AND status = 'failed'",
                (utc_now(), job_id),
            )
            count = cursor.rowcount
            self.audit(
                "tasks.retried",
                {"count": count},
                job_id,
                "user",
                connection,
            )
            return count

    def claim_task(self) -> dict[str, Any] | None:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE status = 'queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE tasks SET status = 'running', attempts = attempts + 1, "
                "updated_at = ? WHERE id = ?",
                (utc_now(), row["id"]),
            )
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            return item

    def recover_running_tasks(self) -> int:
        """Requeue tasks abandoned by the previous single worker process."""

        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status = 'queued', error = ?, updated_at = ? "
                "WHERE status = 'running'",
                ("worker process restarted before completion", utc_now()),
            )
            return cursor.rowcount

    def finish_task(self, task_id: str, error: str | None = None) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT job_id, kind, attempts FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            connection.execute(
                "UPDATE tasks SET status = ?, error = ?, updated_at = ? "
                "WHERE id = ?",
                ("failed" if error else "complete", error, utc_now(), task_id),
            )
            self.audit(
                "task.failed" if error else "task.completed",
                {
                    "task_id": task_id,
                    "kind": row["kind"],
                    "attempts": row["attempts"],
                    "error": error,
                },
                row["job_id"],
                "worker",
                connection,
            )

    def save_plan(self, job_id: str, plan: Any) -> str:
        plan_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO plans (id, job_id, plan_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (plan_id, job_id, canonical_json(plan), utc_now()),
            )
        return plan_id

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["plan"] = json.loads(item.pop("plan_json"))
            return item

    def latest_plan(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM plans WHERE job_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["plan"] = json.loads(item.pop("plan_json"))
            return item

    def mark_plan_committed(self, plan_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE plans SET status = 'committed', committed_at = ? "
                "WHERE id = ?",
                (utc_now(), plan_id),
            )

    def complete_plan(
        self,
        plan_id: str,
        job_id: str,
        operations: list[dict[str, Any]],
        actor: str = "user",
    ) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            plan = connection.execute(
                "SELECT status FROM plans WHERE id = ? AND job_id = ?",
                (plan_id, job_id),
            ).fetchone()
            job = connection.execute(
                "SELECT state FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if plan is None or job is None:
                raise KeyError(plan_id)
            if plan["status"] != "draft" or job["state"] != "organizing":
                raise ValueError("Plan or job is no longer commit-ready")
            connection.execute(
                "UPDATE plans SET status = 'committed', committed_at = ? "
                "WHERE id = ?",
                (now, plan_id),
            )
            connection.execute(
                "UPDATE jobs SET state = 'complete', error = NULL, "
                "updated_at = ? WHERE id = ?",
                (now, job_id),
            )
            self.audit(
                "plan.committed",
                {"plan_id": plan_id, "operations": operations},
                job_id,
                actor,
                connection,
            )
            self.audit(
                "job.transition",
                {"from": "organizing", "to": "complete", "error": None},
                job_id,
                actor,
                connection,
            )

    def cache_get(self, key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT response_json FROM tmdb_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def cache_put(self, key: str, value: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO tmdb_cache (cache_key, response_json, fetched_at) "
                "VALUES (?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "response_json = excluded.response_json, "
                "fetched_at = excluded.fetched_at",
                (key, canonical_json(value), utc_now()),
            )

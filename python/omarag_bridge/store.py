from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models.domain import JobSnapshot, JobStatus, RunSnapshot, WorkspaceManifest
from .models.errors import IdempotencyConflictError, NotFoundError
from .models.events import DomainEvent

DOCUMENT_FILTER_KEYS = frozenset(
    {
        "document_id",
        "logical_document_id",
        "document_ids",
        "logical_document_ids",
        "work_id",
        "title",
        "edition",
        "edition_number",
        "publication_year",
        "document_status",
        "language",
        "author",
        "authors",
        "isbn",
        "tags",
    }
)
DOCUMENT_POLICIES = frozenset({"current-only", "all-editions"})


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class StateStore:
    """Small synchronous SQLite store guarded for FastAPI's worker threads.

    SQLite is deliberately the operational store only. Haiku remains the sole
    owner of LanceDB data.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT 'queued',
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    checkpoint TEXT,
                    progress_detail_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_event_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS jobs_workspace_idx
                    ON jobs(workspace_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    status TEXT NOT NULL,
                    question TEXT NOT NULL,
                    evidence_mode TEXT NOT NULL,
                    answer TEXT NOT NULL DEFAULT '',
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    error_json TEXT,
                    request_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_event_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    type TEXT NOT NULL,
                    workspace_id TEXT,
                    job_id TEXT,
                    run_id TEXT,
                    correlation_id TEXT NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_workspace_idx
                    ON events(workspace_id, event_id);
                CREATE INDEX IF NOT EXISTS events_job_idx ON events(job_id, event_id);
                CREATE INDEX IF NOT EXISTS events_run_idx ON events(run_id, event_id);
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    result_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(scope, key)
                );
                CREATE TABLE IF NOT EXISTS job_checkpoints (
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    name TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, name)
                );
                CREATE TABLE IF NOT EXISTS ingest_segments (
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    source_index INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    segment_index INTEGER NOT NULL,
                    page_start INTEGER NOT NULL,
                    page_end INTEGER NOT NULL,
                    document_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'committed',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, source_index, page_start, page_end)
                );
                CREATE INDEX IF NOT EXISTS ingest_segments_generation_idx
                    ON ingest_segments(generation_id, segment_index);
                CREATE TABLE IF NOT EXISTS document_index (
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    logical_document_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, logical_document_id),
                    UNIQUE(workspace_id, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS document_index_source_idx
                    ON document_index(workspace_id, source_path);
                CREATE TABLE IF NOT EXISTS conversion_cache (
                    cache_key TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    last_used_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS import_preflights (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS book_records (
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    logical_document_id TEXT NOT NULL,
                    original_source TEXT NOT NULL,
                    managed_source TEXT,
                    fingerprint TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    quality_json TEXT NOT NULL DEFAULT '{}',
                    pipeline_version TEXT NOT NULL DEFAULT 'textbook-v1',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, logical_document_id)
                );
                CREATE INDEX IF NOT EXISTS book_records_work_idx
                    ON book_records(workspace_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS document_segments (
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    logical_document_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    segment_document_id TEXT NOT NULL,
                    page_start INTEGER NOT NULL,
                    page_end INTEGER NOT NULL,
                    PRIMARY KEY(workspace_id, segment_document_id)
                );
                CREATE INDEX IF NOT EXISTS document_segments_book_idx
                    ON document_segments(workspace_id, logical_document_id, page_start);
                CREATE TABLE IF NOT EXISTS chunk_manifest (
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    logical_document_id TEXT NOT NULL,
                    segment_document_id TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    chunk_order INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    pages_json TEXT NOT NULL DEFAULT '[]',
                    headings_json TEXT NOT NULL DEFAULT '[]',
                    labels_json TEXT NOT NULL DEFAULT '[]',
                    refs_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(workspace_id, chunk_id)
                );
                CREATE INDEX IF NOT EXISTS chunk_manifest_book_idx
                    ON chunk_manifest(workspace_id, logical_document_id, chunk_order);
                CREATE TABLE IF NOT EXISTS evaluations (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS evaluations_workspace_idx
                    ON evaluations(workspace_id, created_at DESC);
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (1, datetime('now'));
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (2, datetime('now'));
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (3, datetime('now'));
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (4, datetime('now'));
                """
            )
            job_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "progress_detail_json" not in job_columns:
                self._db.execute("ALTER TABLE jobs ADD COLUMN progress_detail_json TEXT")
            # A daemon crash must never leave an operation looking active.
            self._db.execute(
                """UPDATE jobs SET status = ?, phase = 'interrupted', updated_at = ?
                   WHERE status IN (?, ?, ?)""",
                (
                    JobStatus.PAUSED,
                    now_iso(),
                    JobStatus.RUNNING,
                    JobStatus.QUEUED,
                    JobStatus.PAUSE_REQUESTED,
                ),
            )
            self._db.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE status IN (?, ?)",
                (JobStatus.FAILED, now_iso(), JobStatus.RUNNING, JobStatus.QUEUED),
            )

    def add_workspace(self, manifest: WorkspaceManifest) -> None:
        raw = manifest.model_dump_json()
        with self._lock:
            self._db.execute(
                "INSERT INTO workspaces VALUES (?, ?, ?, ?)",
                (
                    manifest.id,
                    raw,
                    manifest.created_at.isoformat(),
                    manifest.updated_at.isoformat(),
                ),
            )

    def update_workspace(self, manifest: WorkspaceManifest) -> None:
        with self._lock:
            cursor = self._db.execute(
                "UPDATE workspaces SET manifest_json = ?, updated_at = ? WHERE id = ?",
                (manifest.model_dump_json(), manifest.updated_at.isoformat(), manifest.id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"Workspace {manifest.id} wurde nicht gefunden")

    def get_workspace(self, workspace_id: str) -> WorkspaceManifest:
        with self._lock:
            row = self._db.execute(
                "SELECT manifest_json FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Workspace {workspace_id} wurde nicht gefunden")
        return WorkspaceManifest.model_validate_json(row["manifest_json"])

    def list_workspaces(self) -> list[WorkspaceManifest]:
        with self._lock:
            rows = self._db.execute(
                "SELECT manifest_json FROM workspaces ORDER BY updated_at DESC"
            ).fetchall()
        return [WorkspaceManifest.model_validate_json(row["manifest_json"]) for row in rows]

    def remove_workspace(self, workspace_id: str) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                active = self._db.execute(
                    """SELECT 1 FROM jobs WHERE workspace_id = ?
                       AND status IN (?, ?, ?, ?) LIMIT 1""",
                    (
                        workspace_id,
                        JobStatus.QUEUED,
                        JobStatus.RUNNING,
                        JobStatus.PAUSE_REQUESTED,
                        JobStatus.PAUSED,
                    ),
                ).fetchone()
                active_run = self._db.execute(
                    """SELECT 1 FROM runs WHERE workspace_id = ?
                       AND status IN (?, ?) LIMIT 1""",
                    (workspace_id, JobStatus.QUEUED, JobStatus.RUNNING),
                ).fetchone()
                if active or active_run:
                    from .models.errors import ConflictError

                    raise ConflictError("Workspace besitzt einen aktiven oder pausierten Job")
                self._db.execute("DELETE FROM events WHERE workspace_id = ?", (workspace_id,))
                self._db.execute("DELETE FROM runs WHERE workspace_id = ?", (workspace_id,))
                self._db.execute(
                    "DELETE FROM job_checkpoints WHERE job_id IN "
                    "(SELECT id FROM jobs WHERE workspace_id = ?)",
                    (workspace_id,),
                )
                self._db.execute(
                    "DELETE FROM ingest_segments WHERE job_id IN "
                    "(SELECT id FROM jobs WHERE workspace_id = ?)",
                    (workspace_id,),
                )
                self._db.execute(
                    "DELETE FROM document_index WHERE workspace_id = ?", (workspace_id,)
                )
                self._db.execute("DELETE FROM evaluations WHERE workspace_id = ?", (workspace_id,))
                self._db.execute(
                    "DELETE FROM chunk_manifest WHERE workspace_id = ?", (workspace_id,)
                )
                self._db.execute(
                    "DELETE FROM document_segments WHERE workspace_id = ?", (workspace_id,)
                )
                self._db.execute("DELETE FROM book_records WHERE workspace_id = ?", (workspace_id,))
                self._db.execute(
                    "DELETE FROM import_preflights WHERE workspace_id = ?", (workspace_id,)
                )
                self._db.execute(
                    "DELETE FROM idempotency_keys WHERE scope LIKE ?",
                    (f"job:{workspace_id}:%",),
                )
                self._db.execute("DELETE FROM jobs WHERE workspace_id = ?", (workspace_id,))
                cursor = self._db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
                if cursor.rowcount != 1:
                    raise NotFoundError(f"Workspace {workspace_id} wurde nicht gefunden")
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def create_job_idempotent(
        self,
        *,
        job_id: str,
        workspace_id: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> tuple[JobSnapshot, bool]:
        digest = request_hash(payload)
        scope = f"job:{workspace_id}:{kind}"
        timestamp = now_iso()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT request_hash, result_id FROM idempotency_keys WHERE scope=? AND key=?",
                    (scope, idempotency_key),
                ).fetchone()
                if existing:
                    if existing["request_hash"] != digest:
                        raise IdempotencyConflictError(
                            "Idempotency-Key wurde bereits fuer einen anderen Request verwendet"
                        )
                    self._db.execute("COMMIT")
                    return self.get_job(existing["result_id"]), True
                self._db.execute(
                    """INSERT INTO jobs(
                        id, workspace_id, kind, status, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        job_id,
                        workspace_id,
                        kind,
                        JobStatus.QUEUED,
                        json.dumps(payload),
                        timestamp,
                        timestamp,
                    ),
                )
                self._db.execute(
                    "INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, ?)",
                    (scope, idempotency_key, digest, job_id, timestamp),
                )
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        return self.get_job(job_id), False

    def get_job(self, job_id: str) -> JobSnapshot:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Job {job_id} wurde nicht gefunden")
        return self._job_from_row(row)

    def list_jobs(self, workspace_id: str | None = None) -> list[JobSnapshot]:
        sql = "SELECT * FROM jobs"
        args: tuple[str, ...] = ()
        if workspace_id:
            sql += " WHERE workspace_id = ?"
            args = (workspace_id,)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._job_from_row(row) for row in rows]

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobSnapshot:
        return JobSnapshot(
            id=row["id"],
            workspace_id=row["workspace_id"],
            kind=row["kind"],
            status=row["status"],
            progress=row["progress"],
            phase=row["phase"],
            payload=json.loads(row["payload_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            checkpoint=row["checkpoint"],
            progress_detail=(
                json.loads(row["progress_detail_json"]) if row["progress_detail_json"] else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_event_id=row["last_event_id"],
        )

    def update_job(self, job_id: str, **changes: Any) -> JobSnapshot:
        allowed = {
            "status",
            "progress",
            "phase",
            "result",
            "error",
            "checkpoint",
            "last_event_id",
            "progress_detail",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported job fields: {unknown}")
        columns: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            column = f"{key}_json" if key in {"result", "error", "progress_detail"} else key
            if key in {"result", "error", "progress_detail"} and value is not None:
                if hasattr(value, "model_dump"):
                    value = value.model_dump(mode="json")
                value = json.dumps(value)
            columns.append(f"{column} = ?")
            values.append(value)
        columns.append("updated_at = ?")
        values.append(now_iso())
        values.append(job_id)
        with self._lock:
            cursor = self._db.execute(
                f"UPDATE jobs SET {', '.join(columns)} WHERE id = ?",
                values,  # noqa: S608
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"Job {job_id} wurde nicht gefunden")
        return self.get_job(job_id)

    def create_run(self, run_id: str, workspace_id: str, request: dict[str, Any]) -> RunSnapshot:
        timestamp = now_iso()
        with self._lock:
            self._db.execute(
                """INSERT INTO runs(
                    id, workspace_id, status, question, evidence_mode, request_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    workspace_id,
                    JobStatus.QUEUED,
                    request["question"],
                    request["evidence_mode"],
                    json.dumps(request),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_run(run_id)

    def get_run_request(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT request_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Run {run_id} wurde nicht gefunden")
        return json.loads(row["request_json"])

    def get_run(self, run_id: str) -> RunSnapshot:
        with self._lock:
            row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Run {run_id} wurde nicht gefunden")
        return RunSnapshot(
            id=row["id"],
            workspace_id=row["workspace_id"],
            status=row["status"],
            question=row["question"],
            evidence_mode=row["evidence_mode"],
            answer=row["answer"],
            citations=json.loads(row["citations_json"]),
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_event_id=row["last_event_id"],
        )

    def update_run(self, run_id: str, **changes: Any) -> RunSnapshot:
        allowed = {"status", "answer", "citations", "error", "last_event_id"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported run fields: {unknown}")
        columns: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            column = f"{key}_json" if key in {"citations", "error"} else key
            if key in {"citations", "error"} and value is not None:
                value = json.dumps(value)
            columns.append(f"{column} = ?")
            values.append(value)
        columns.append("updated_at = ?")
        values.extend([now_iso(), run_id])
        with self._lock:
            cursor = self._db.execute(
                f"UPDATE runs SET {', '.join(columns)} WHERE id = ?",
                values,  # noqa: S608
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"Run {run_id} wurde nicht gefunden")
        return self.get_run(run_id)

    def append_event(
        self,
        *,
        event_type: str,
        correlation_id: str,
        payload: dict[str, Any],
        workspace_id: str | None = None,
        job_id: str | None = None,
        run_id: str | None = None,
    ) -> DomainEvent:
        with self._lock:
            row = self._db.execute(
                """SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM events
                   WHERE (job_id = ? AND ? IS NOT NULL) OR (run_id = ? AND ? IS NOT NULL)
                      OR (job_id IS NULL AND run_id IS NULL AND workspace_id IS ?)""",
                (job_id, job_id, run_id, run_id, workspace_id),
            ).fetchone()
            sequence = int(row["next_sequence"])
            timestamp = now_iso()
            cursor = self._db.execute(
                """INSERT INTO events(
                    sequence, timestamp, type, workspace_id, job_id, run_id,
                    correlation_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sequence,
                    timestamp,
                    event_type,
                    workspace_id,
                    job_id,
                    run_id,
                    correlation_id,
                    json.dumps(payload),
                ),
            )
            event_id = int(cursor.lastrowid)
            if job_id:
                self._db.execute(
                    "UPDATE jobs SET last_event_id = ? WHERE id = ?", (event_id, job_id)
                )
            if run_id:
                self._db.execute(
                    "UPDATE runs SET last_event_id = ? WHERE id = ?", (event_id, run_id)
                )
        return DomainEvent(
            event_id=event_id,
            sequence=sequence,
            timestamp=timestamp,
            type=event_type,
            workspace_id=workspace_id,
            job_id=job_id,
            run_id=run_id,
            correlation_id=correlation_id,
            payload=payload,
        )

    def events_after(
        self,
        after_id: int,
        *,
        workspace_id: str | None = None,
        job_id: str | None = None,
        run_id: str | None = None,
        limit: int = 500,
    ) -> list[DomainEvent]:
        clauses = ["event_id > ?"]
        args: list[Any] = [after_id]
        for column, value in (
            ("workspace_id", workspace_id),
            ("job_id", job_id),
            ("run_id", run_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                args.append(value)
        args.append(limit)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM events WHERE {' AND '.join(clauses)} "  # noqa: S608
                "ORDER BY event_id LIMIT ?",
                args,
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> DomainEvent:
        return DomainEvent(
            event_id=row["event_id"],
            sequence=row["sequence"],
            timestamp=row["timestamp"],
            type=row["type"],
            workspace_id=row["workspace_id"],
            job_id=row["job_id"],
            run_id=row["run_id"],
            correlation_id=row["correlation_id"],
            schema_version=row["schema_version"],
            payload=json.loads(row["payload_json"]),
        )

    def checkpoint(self, job_id: str, name: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO job_checkpoints VALUES (?, ?, ?, ?)
                   ON CONFLICT(job_id, name) DO UPDATE SET data_json=excluded.data_json,
                   created_at=excluded.created_at""",
                (job_id, name, json.dumps(data), now_iso()),
            )

    def checkpoint_data(self, job_id: str, name: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data_json FROM job_checkpoints WHERE job_id = ? AND name = ?",
                (job_id, name),
            ).fetchone()
        return json.loads(row["data_json"]) if row else None

    def record_segment(self, job_id: str, source_index: int, segment: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO ingest_segments(
                       job_id, source_index, fingerprint, generation_id, segment_index,
                       page_start, page_end, document_id, status, metadata_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, source_index, page_start, page_end) DO UPDATE SET
                       document_id=excluded.document_id, status=excluded.status,
                       metadata_json=excluded.metadata_json""",
                (
                    job_id,
                    source_index,
                    segment["fingerprint"],
                    segment["generation_id"],
                    segment["segment_index"],
                    segment["page_start"],
                    segment["page_end"],
                    segment["document_id"],
                    segment.get("status", "committed"),
                    json.dumps(segment.get("metadata", {})),
                    now_iso(),
                ),
            )

    def list_segments(self, job_id: str, source_index: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM ingest_segments WHERE job_id = ? AND source_index = ?
                   ORDER BY segment_index""",
                (job_id, source_index),
            ).fetchall()
        return [
            {
                "fingerprint": row["fingerprint"],
                "generation_id": row["generation_id"],
                "segment_index": row["segment_index"],
                "page_start": row["page_start"],
                "page_end": row["page_end"],
                "document_id": row["document_id"],
                "status": row["status"],
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    def clear_segments(self, job_id: str, source_index: int | None = None) -> None:
        with self._lock:
            if source_index is None:
                self._db.execute("DELETE FROM ingest_segments WHERE job_id = ?", (job_id,))
            else:
                self._db.execute(
                    "DELETE FROM ingest_segments WHERE job_id = ? AND source_index = ?",
                    (job_id, source_index),
                )

    def document_by_fingerprint(self, workspace_id: str, fingerprint: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM document_index WHERE workspace_id = ? AND fingerprint = ?",
                (workspace_id, fingerprint),
            ).fetchone()
        return self._document_index_row(row) if row else None

    def document_by_source(self, workspace_id: str, source_path: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM document_index WHERE workspace_id = ? AND source_path = ?",
                (workspace_id, source_path),
            ).fetchone()
        return self._document_index_row(row) if row else None

    @staticmethod
    def _document_index_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "workspace_id": row["workspace_id"],
            "logical_document_id": row["logical_document_id"],
            "source_path": row["source_path"],
            "fingerprint": row["fingerprint"],
            "generation_id": row["generation_id"],
            "result": json.loads(row["result_json"]),
            "updated_at": row["updated_at"],
        }

    def upsert_document(
        self, workspace_id: str, source_path: str, fingerprint: str, result: dict[str, Any]
    ) -> None:
        logical_id = str(result.get("logical_document_id") or result.get("document_id"))
        generation_id = str(result.get("generation_id", ""))
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "DELETE FROM document_index WHERE workspace_id = ? AND source_path = ?",
                    (workspace_id, source_path),
                )
                self._db.execute(
                    """INSERT INTO document_index VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(workspace_id, logical_document_id) DO UPDATE SET
                         source_path=excluded.source_path, fingerprint=excluded.fingerprint,
                         generation_id=excluded.generation_id, result_json=excluded.result_json,
                         updated_at=excluded.updated_at""",
                    (
                        workspace_id,
                        logical_id,
                        source_path,
                        fingerprint,
                        generation_id,
                        json.dumps(result),
                        now_iso(),
                    ),
                )
                metadata = result.get("book_metadata") or {}
                quality = result.get("quality") or {}
                managed_source = result.get("managed_source") or result.get("source")
                self._db.execute(
                    """INSERT INTO book_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(workspace_id, logical_document_id) DO UPDATE SET
                         original_source=excluded.original_source,
                         managed_source=excluded.managed_source,
                         fingerprint=excluded.fingerprint,
                         generation_id=excluded.generation_id,
                         metadata_json=excluded.metadata_json,
                         quality_json=excluded.quality_json,
                         pipeline_version=excluded.pipeline_version,
                         updated_at=excluded.updated_at""",
                    (
                        workspace_id,
                        logical_id,
                        str(result.get("original_source") or source_path),
                        str(managed_source) if managed_source else None,
                        fingerprint,
                        generation_id,
                        json.dumps(metadata),
                        json.dumps(quality),
                        str(result.get("pipeline_version", "textbook-v1")),
                        now_iso(),
                    ),
                )
                self._db.execute(
                    """DELETE FROM document_segments
                       WHERE workspace_id = ? AND logical_document_id = ?""",
                    (workspace_id, logical_id),
                )
                self._db.execute(
                    "DELETE FROM chunk_manifest WHERE workspace_id = ? AND logical_document_id = ?",
                    (workspace_id, logical_id),
                )
                segment_ids: dict[int, str] = {}
                for segment in result.get("segments", []):
                    segment_id = str(segment.get("document_id", ""))
                    if not segment_id:
                        continue
                    self._db.execute(
                        """INSERT OR REPLACE INTO document_segments
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            workspace_id,
                            logical_id,
                            generation_id,
                            segment_id,
                            int(segment.get("page_start", 1)),
                            int(segment.get("page_end", 1)),
                        ),
                    )
                    segment_ids[int(segment.get("segment_index", 0))] = segment_id
                for chunk in result.get("chunk_manifest", []):
                    chunk_id = str(chunk.get("chunk_id", ""))
                    segment_id = segment_ids.get(int(chunk.get("segment_index", 0)))
                    if not chunk_id or not segment_id:
                        continue
                    self._db.execute(
                        """INSERT OR REPLACE INTO chunk_manifest
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            workspace_id,
                            logical_id,
                            segment_id,
                            chunk_id,
                            int(chunk.get("chunk_order", 0)),
                            str(chunk.get("content_hash", "")),
                            json.dumps(chunk.get("pages", [])),
                            json.dumps(chunk.get("headings", [])),
                            json.dumps(chunk.get("labels", [])),
                            json.dumps(chunk.get("doc_item_refs", [])),
                        ),
                    )
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def save_import_preflight(
        self, preflight_id: str, workspace_id: str, payload: dict[str, Any]
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO import_preflights VALUES (?, ?, ?, ?)",
                (preflight_id, workspace_id, json.dumps(payload), now_iso()),
            )

    def get_import_preflight(self, preflight_id: str, workspace_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT payload_json FROM import_preflights WHERE id = ? AND workspace_id = ?",
                (preflight_id, workspace_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Import preflight {preflight_id} was not found")
        return json.loads(row["payload_json"])

    def book_records(self, workspace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM book_records WHERE workspace_id = ? ORDER BY updated_at DESC",
                (workspace_id,),
            ).fetchall()
            segments = self._db.execute(
                """SELECT logical_document_id, segment_document_id, page_start, page_end
                   FROM document_segments WHERE workspace_id = ? ORDER BY page_start""",
                (workspace_id,),
            ).fetchall()
        by_book: dict[str, list[dict[str, Any]]] = {}
        for segment in segments:
            by_book.setdefault(segment["logical_document_id"], []).append(dict(segment))
        return [
            {
                **dict(row),
                "metadata": json.loads(row["metadata_json"]),
                "quality": json.loads(row["quality_json"]),
                "segments": by_book.get(row["logical_document_id"], []),
            }
            for row in rows
        ]

    def book_record(self, workspace_id: str, logical_document_id: str) -> dict[str, Any]:
        for record in self.book_records(workspace_id):
            if record["logical_document_id"] == logical_document_id:
                return record
        raise NotFoundError(f"Document {logical_document_id} was not found")

    def update_book_metadata(
        self, workspace_id: str, logical_document_id: str, metadata: dict[str, Any]
    ) -> None:
        with self._lock:
            cursor = self._db.execute(
                """UPDATE book_records SET metadata_json = ?, updated_at = ?
                   WHERE workspace_id = ? AND logical_document_id = ?""",
                (json.dumps(metadata), now_iso(), workspace_id, logical_document_id),
            )
        if cursor.rowcount != 1:
            raise NotFoundError(f"Document {logical_document_id} was not found")

    def chunk_manifest(self, workspace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM chunk_manifest WHERE workspace_id = ?
                   ORDER BY logical_document_id, chunk_order""",
                (workspace_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "pages": json.loads(row["pages_json"]),
                "headings": json.loads(row["headings_json"]),
                "labels": json.loads(row["labels_json"]),
                "refs": json.loads(row["refs_json"]),
            }
            for row in rows
        ]

    def resolve_segment_ids(
        self, workspace_id: str, filters: dict[str, Any], document_policy: str
    ) -> list[str] | None:
        unknown_filters = filters.keys() - DOCUMENT_FILTER_KEYS
        if unknown_filters:
            raise ValueError(f"Unsupported document filters: {sorted(unknown_filters)}")
        if document_policy not in DOCUMENT_POLICIES:
            raise ValueError(f"Unsupported document policy: {document_policy}")
        records = self.book_records(workspace_id)
        if not records:
            return None

        def values(value: Any) -> set[str]:
            if value is None:
                return set()
            if isinstance(value, (list, tuple, set)):
                return {str(item).casefold() for item in value}
            return {str(value).casefold()}

        def matches(record: dict[str, Any]) -> bool:
            meta = record["metadata"]
            aliases = {
                "document_id": record["logical_document_id"],
                "logical_document_id": record["logical_document_id"],
                "work_id": meta.get("work_id"),
                "title": meta.get("title"),
                "edition": meta.get("edition_label"),
                "edition_number": meta.get("edition_number"),
                "publication_year": meta.get("publication_year"),
                "document_status": meta.get("document_status", "active"),
                "language": meta.get("language"),
            }
            for key, expected in filters.items():
                if key in {"authors", "author"}:
                    actual = values(meta.get("authors"))
                elif key == "isbn":
                    actual = values(meta.get("isbn"))
                elif key == "tags":
                    actual = values(meta.get("tags"))
                elif key in {"document_ids", "logical_document_ids"}:
                    actual = values(record["logical_document_id"])
                elif key in aliases:
                    actual = values(aliases[key])
                wanted = values(expected)
                if wanted and actual.isdisjoint(wanted):
                    return False
            return True

        selected = [record for record in records if matches(record)]
        explicit_version = bool(
            {
                "document_id",
                "logical_document_id",
                "document_ids",
                "logical_document_ids",
                "edition",
                "edition_number",
                "publication_year",
                "document_status",
            }
            & filters.keys()
        )
        if document_policy == "current-only" and not explicit_version:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for record in selected:
                meta = record["metadata"]
                if meta.get("document_status", "active") == "superseded":
                    continue
                grouped.setdefault(meta.get("work_id") or record["logical_document_id"], []).append(
                    record
                )
            selected = []
            for group in grouped.values():
                selected.append(
                    max(
                        group,
                        key=lambda item: (
                            item["metadata"].get("edition_number") or 0,
                            item["metadata"].get("publication_year") or 0,
                            item["updated_at"],
                        ),
                    )
                )
        return [
            str(segment["segment_document_id"])
            for record in selected
            for segment in record["segments"]
        ]

    def save_evaluation(
        self, evaluation_id: str, workspace_id: str, report: dict[str, Any]
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO evaluations VALUES (?, ?, ?, ?)",
                (evaluation_id, workspace_id, json.dumps(report), now_iso()),
            )

    def evaluation(self, workspace_id: str, evaluation_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT report_json FROM evaluations WHERE workspace_id = ? AND id = ?",
                (workspace_id, evaluation_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Evaluation {evaluation_id} was not found")
        return json.loads(row["report_json"])

    def latest_evaluation(self, workspace_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                """SELECT report_json FROM evaluations WHERE workspace_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (workspace_id,),
            ).fetchone()
        return json.loads(row["report_json"]) if row else None

    def touch_cache(
        self, cache_key: str, path: str, size_bytes: int, metadata: dict[str, Any]
    ) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO conversion_cache VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET path=excluded.path,
                     size_bytes=excluded.size_bytes, last_used_at=excluded.last_used_at,
                     metadata_json=excluded.metadata_json""",
                (cache_key, path, size_bytes, now_iso(), json.dumps(metadata)),
            )

    def cache_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM conversion_cache ORDER BY last_used_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_cache_entries(self, keys: list[str]) -> None:
        if not keys:
            return
        with self._lock:
            self._db.executemany(
                "DELETE FROM conversion_cache WHERE cache_key = ?", ((key,) for key in keys)
            )

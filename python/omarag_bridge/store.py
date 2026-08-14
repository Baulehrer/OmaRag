from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models.book import BookKnowledgeSnapshot
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
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    question TEXT NOT NULL,
                    evidence_mode TEXT NOT NULL,
                    answer TEXT NOT NULL DEFAULT '',
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    receipt_json TEXT,
                    error_json TEXT,
                    request_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_event_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS answer_cache (
                    cache_key TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    index_fingerprint TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    citations_json TEXT NOT NULL,
                    claims_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    hits INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS answer_cache_workspace_idx
                    ON answer_cache(workspace_id, last_used_at DESC);
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
                CREATE TABLE IF NOT EXISTS workspace_index_generations (
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    generation_id TEXT NOT NULL,
                    pipeline_version TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'maintenance',
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error_json TEXT,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(workspace_id, generation_id)
                );
                CREATE INDEX IF NOT EXISTS workspace_index_generations_current_idx
                    ON workspace_index_generations(workspace_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS book_structures (
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    logical_document_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    total_pages INTEGER NOT NULL,
                    page_labels_json TEXT NOT NULL DEFAULT '{}',
                    regions_json TEXT NOT NULL DEFAULT '[]',
                    stats_json TEXT NOT NULL DEFAULT '{}',
                    snapshot_hash TEXT,
                    PRIMARY KEY(workspace_id, logical_document_id)
                );
                CREATE INDEX IF NOT EXISTS book_structures_generation_idx
                    ON book_structures(workspace_id, generation_id);
                CREATE TABLE IF NOT EXISTS book_structure_nodes (
                    workspace_id TEXT NOT NULL,
                    logical_document_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    parent_id TEXT,
                    kind TEXT NOT NULL,
                    depth INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    page_start INTEGER NOT NULL,
                    page_end INTEGER NOT NULL,
                    source_kind TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    source_refs_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(workspace_id, logical_document_id, node_id),
                    FOREIGN KEY(workspace_id, logical_document_id)
                        REFERENCES book_structures(workspace_id, logical_document_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS book_structure_nodes_page_idx
                    ON book_structure_nodes(
                        workspace_id, logical_document_id, page_start, page_end
                    );
                CREATE TABLE IF NOT EXISTS book_terms (
                    workspace_id TEXT NOT NULL,
                    logical_document_id TEXT NOT NULL,
                    term_id TEXT NOT NULL,
                    canonical TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_page INTEGER,
                    source_ref TEXT,
                    confidence REAL NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(workspace_id, logical_document_id, term_id),
                    FOREIGN KEY(workspace_id, logical_document_id)
                        REFERENCES book_structures(workspace_id, logical_document_id)
                        ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS book_terms_normalized_idx
                    ON book_terms(workspace_id, logical_document_id, normalized);
                CREATE TABLE IF NOT EXISTS book_term_aliases (
                    workspace_id TEXT NOT NULL,
                    logical_document_id TEXT NOT NULL,
                    term_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    PRIMARY KEY(
                        workspace_id, logical_document_id, term_id, normalized_alias, relation
                    ),
                    FOREIGN KEY(workspace_id, logical_document_id, term_id)
                        REFERENCES book_terms(workspace_id, logical_document_id, term_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS book_term_targets (
                    workspace_id TEXT NOT NULL,
                    logical_document_id TEXT NOT NULL,
                    term_id TEXT NOT NULL,
                    node_id TEXT,
                    page_start INTEGER,
                    page_end INTEGER,
                    evidence_id TEXT,
                    relation TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    target_key TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, logical_document_id, term_id, target_key),
                    FOREIGN KEY(workspace_id, logical_document_id, term_id)
                        REFERENCES book_terms(workspace_id, logical_document_id, term_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS book_graph_edges (
                    workspace_id TEXT NOT NULL,
                    logical_document_id TEXT NOT NULL,
                    edge_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    weight REAL NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(workspace_id, logical_document_id, edge_id),
                    FOREIGN KEY(workspace_id, logical_document_id)
                        REFERENCES book_structures(workspace_id, logical_document_id)
                        ON DELETE CASCADE
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS book_term_router USING fts5(
                    workspace_id UNINDEXED,
                    logical_document_id UNINDEXED,
                    term_id UNINDEXED,
                    canonical,
                    aliases
                );
                CREATE TABLE IF NOT EXISTS book_knowledge_snapshots (
                    workspace_id TEXT NOT NULL,
                    logical_document_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, logical_document_id),
                    FOREIGN KEY(workspace_id, logical_document_id)
                        REFERENCES book_structures(workspace_id, logical_document_id)
                        ON DELETE CASCADE
                );
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (1, datetime('now'));
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (2, datetime('now'));
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (3, datetime('now'));
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (4, datetime('now'));
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (5, datetime('now'));
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (6, datetime('now'));
                """
            )
            job_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "progress_detail_json" not in job_columns:
                self._db.execute("ALTER TABLE jobs ADD COLUMN progress_detail_json TEXT")
            run_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "session_id" not in run_columns:
                self._db.execute("ALTER TABLE runs ADD COLUMN session_id TEXT")
                self._db.execute("UPDATE runs SET session_id = 'legacy-' || id")
            if "receipt_json" not in run_columns:
                self._db.execute("ALTER TABLE runs ADD COLUMN receipt_json TEXT")
            if "claims_json" not in run_columns:
                self._db.execute(
                    "ALTER TABLE runs ADD COLUMN claims_json TEXT NOT NULL DEFAULT '[]'"
                )
            self._ensure_columns(
                "answer_cache",
                {
                    "claims_json": "TEXT NOT NULL DEFAULT '[]'",
                    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                },
            )
            self._ensure_columns(
                "workspace_index_generations",
                {"config_json": "TEXT NOT NULL DEFAULT '{}'"},
            )
            self._ensure_columns(
                "document_segments",
                {
                    "core_start": "INTEGER",
                    "core_end": "INTEGER",
                    "conversion_start": "INTEGER",
                    "conversion_end": "INTEGER",
                    "page_number_mode": "TEXT NOT NULL DEFAULT 'legacy-offset'",
                    "role": "TEXT NOT NULL DEFAULT 'body'",
                },
            )
            self._ensure_columns(
                "chunk_manifest",
                {
                    "generation_id": "TEXT",
                    "evidence_id": "TEXT",
                    "global_order": "INTEGER",
                    "anchor_page": "INTEGER",
                    "page_labels_json": "TEXT NOT NULL DEFAULT '[]'",
                    "section_node_id": "TEXT",
                    "raw_tokens": "INTEGER",
                    "context_hash": "TEXT",
                    "previous_evidence_id": "TEXT",
                    "next_evidence_id": "TEXT",
                    "quality_flags_json": "TEXT NOT NULL DEFAULT '[]'",
                },
            )
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS chunk_manifest_evidence_idx "
                "ON chunk_manifest(workspace_id, evidence_id) WHERE evidence_id IS NOT NULL"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS runs_session_idx "
                "ON runs(workspace_id, session_id, created_at DESC)"
            )
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

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {
            row["name"] for row in self._db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

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
                self._db.execute("DELETE FROM answer_cache WHERE workspace_id = ?", (workspace_id,))
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
                    "DELETE FROM book_term_router WHERE workspace_id = ?", (workspace_id,)
                )
                self._db.execute(
                    "DELETE FROM book_structures WHERE workspace_id = ?", (workspace_id,)
                )
                self._db.execute(
                    "DELETE FROM workspace_index_generations WHERE workspace_id = ?",
                    (workspace_id,),
                )
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
                    id, workspace_id, session_id, status, question, evidence_mode, request_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    workspace_id,
                    request["session_id"],
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
        return self._run_from_row(row)

    def update_run(self, run_id: str, **changes: Any) -> RunSnapshot:
        allowed = {
            "status",
            "answer",
            "claims",
            "citations",
            "receipt",
            "error",
            "last_event_id",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported run fields: {unknown}")
        columns: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            column = f"{key}_json" if key in {"claims", "citations", "receipt", "error"} else key
            if key in {"claims", "citations", "receipt", "error"} and value is not None:
                if hasattr(value, "model_dump"):
                    value = value.model_dump(mode="json")
                elif isinstance(value, list):
                    value = [
                        item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                        for item in value
                    ]
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

    def session_turn(self, workspace_id: str, session_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE workspace_id = ? AND session_id = ?",
                (workspace_id, session_id),
            ).fetchone()
        return int(row["count"])

    def previous_completed_session_run(
        self, workspace_id: str, session_id: str, current_run_id: str
    ) -> RunSnapshot | None:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM runs
                   WHERE workspace_id = ? AND session_id = ? AND id != ? AND status = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (workspace_id, session_id, current_run_id, JobStatus.COMPLETED),
            ).fetchone()
        return self._run_from_row(row) if row else None

    def recent_completed_session_runs(
        self,
        workspace_id: str,
        session_id: str,
        current_run_id: str,
        *,
        limit: int = 4,
        max_age_hours: float = 24.0,
    ) -> list[RunSnapshot]:
        """Return a bounded, newest-first session history for query planning."""

        if limit < 1 or limit > 20:
            raise ValueError("limit must be between 1 and 20")
        if max_age_hours <= 0 or max_age_hours > 24 * 30:
            raise ValueError("max_age_hours must be between 0 and 720")
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM runs
                   WHERE workspace_id = ? AND session_id = ? AND id != ? AND status = ?
                     AND julianday(created_at) >= julianday('now', ?)
                   ORDER BY created_at DESC LIMIT ?""",
                (
                    workspace_id,
                    session_id,
                    current_run_id,
                    JobStatus.COMPLETED,
                    f"-{max_age_hours:g} hours",
                    limit,
                ),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def workspace_index_fingerprint(self, workspace_id: str) -> str:
        with self._lock:
            rows = self._db.execute(
                """SELECT logical_document_id, fingerprint, generation_id
                   FROM document_index WHERE workspace_id = ?
                   ORDER BY logical_document_id""",
                (workspace_id,),
            ).fetchall()
        material = [
            [row["logical_document_id"], row["fingerprint"], row["generation_id"]] for row in rows
        ]
        return request_hash({"documents": material})

    def cached_answer(self, cache_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                """SELECT answer, citations_json, claims_json, metadata_json
                   FROM answer_cache WHERE cache_key = ?""",
                (cache_key,),
            ).fetchone()
            if row is not None:
                self._db.execute(
                    "UPDATE answer_cache SET hits = hits + 1, last_used_at = ? WHERE cache_key = ?",
                    (now_iso(), cache_key),
                )
        if row is None:
            return None
        return {
            "answer": row["answer"],
            "citations": json.loads(row["citations_json"]),
            "claims": json.loads(row["claims_json"]),
            "metadata": json.loads(row["metadata_json"]),
        }

    def cache_answer(
        self,
        *,
        cache_key: str,
        workspace_id: str,
        index_fingerprint: str,
        config_fingerprint: str,
        request: dict[str, Any],
        answer: str,
        citations: list[dict[str, Any]],
        claims: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        max_entries: int,
    ) -> None:
        timestamp = now_iso()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    """INSERT INTO answer_cache(
                           cache_key, workspace_id, index_fingerprint, config_fingerprint,
                           request_json, answer, citations_json, claims_json, metadata_json,
                           created_at, last_used_at, hits
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                       ON CONFLICT(cache_key) DO UPDATE SET
                           answer=excluded.answer, citations_json=excluded.citations_json,
                           claims_json=excluded.claims_json,
                           metadata_json=excluded.metadata_json,
                           last_used_at=excluded.last_used_at""",
                    (
                        cache_key,
                        workspace_id,
                        index_fingerprint,
                        config_fingerprint,
                        json.dumps(request, sort_keys=True),
                        answer,
                        json.dumps(citations),
                        json.dumps(claims or []),
                        json.dumps(metadata or {}),
                        timestamp,
                        timestamp,
                    ),
                )
                self._db.execute(
                    """DELETE FROM answer_cache
                       WHERE workspace_id = ? AND cache_key IN (
                           SELECT cache_key FROM answer_cache WHERE workspace_id = ?
                           ORDER BY last_used_at DESC LIMIT -1 OFFSET ?
                       )""",
                    (workspace_id, workspace_id, max_entries),
                )
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def clear_answer_cache(self, workspace_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM answer_cache WHERE workspace_id = ?", (workspace_id,))

    def answer_cache_size(self, workspace_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS count FROM answer_cache WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        return int(row["count"])

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunSnapshot:
        return RunSnapshot(
            id=row["id"],
            workspace_id=row["workspace_id"],
            session_id=row["session_id"],
            status=row["status"],
            question=row["question"],
            evidence_mode=row["evidence_mode"],
            answer=row["answer"],
            claims=json.loads(row["claims_json"]) if row["claims_json"] else [],
            citations=json.loads(row["citations_json"]),
            receipt=json.loads(row["receipt_json"]) if row["receipt_json"] else None,
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_event_id=row["last_event_id"],
        )

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
        knowledge_snapshot = result.get("book_knowledge_snapshot")
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
                    page_start = int(segment.get("page_start", 1))
                    page_end = int(segment.get("page_end", 1))
                    self._db.execute(
                        """INSERT OR REPLACE INTO document_segments(
                               workspace_id, logical_document_id, generation_id,
                               segment_document_id, page_start, page_end, core_start, core_end,
                               conversion_start, conversion_end, page_number_mode, role
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            workspace_id,
                            logical_id,
                            generation_id,
                            segment_id,
                            page_start,
                            page_end,
                            int(segment.get("core_start", page_start)),
                            int(segment.get("core_end", page_end)),
                            int(segment.get("conversion_start", page_start)),
                            int(segment.get("conversion_end", page_end)),
                            str(segment.get("page_number_mode", "absolute")),
                            str(segment.get("role", "body")),
                        ),
                    )
                    segment_ids[int(segment.get("segment_index", 0))] = segment_id
                for chunk in result.get("chunk_manifest", []):
                    chunk_id = str(chunk.get("chunk_id", ""))
                    segment_id = segment_ids.get(int(chunk.get("segment_index", 0)))
                    if not chunk_id or not segment_id:
                        continue
                    pages = chunk.get("pages", [])
                    self._db.execute(
                        """INSERT OR REPLACE INTO chunk_manifest(
                               workspace_id, logical_document_id, segment_document_id, chunk_id,
                               chunk_order, content_hash, pages_json, headings_json, labels_json,
                               refs_json, generation_id, evidence_id, global_order, anchor_page,
                               page_labels_json, section_node_id, raw_tokens, context_hash,
                               previous_evidence_id, next_evidence_id, quality_flags_json
                           ) VALUES (
                               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                           )""",
                        (
                            workspace_id,
                            logical_id,
                            segment_id,
                            chunk_id,
                            int(chunk.get("chunk_order", 0)),
                            str(chunk.get("content_hash", "")),
                            json.dumps(pages),
                            json.dumps(chunk.get("headings", [])),
                            json.dumps(chunk.get("labels", [])),
                            json.dumps(chunk.get("doc_item_refs", [])),
                            str(chunk.get("generation_id", generation_id)),
                            chunk.get("evidence_id"),
                            int(chunk.get("global_order", chunk.get("chunk_order", 0))),
                            chunk.get("anchor_page", pages[0] if pages else None),
                            json.dumps(chunk.get("page_labels", [])),
                            chunk.get("section_node_id"),
                            chunk.get("raw_tokens"),
                            chunk.get("context_hash"),
                            chunk.get("previous_evidence_id"),
                            chunk.get("next_evidence_id"),
                            json.dumps(chunk.get("quality_flags", [])),
                        ),
                    )
                if knowledge_snapshot is not None:
                    self._write_book_knowledge_snapshot(
                        workspace_id, knowledge_snapshot, transactional=False
                    )
                self._db.execute("DELETE FROM answer_cache WHERE workspace_id = ?", (workspace_id,))
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

    def begin_index_generation(
        self,
        workspace_id: str,
        generation_id: str,
        pipeline_version: str,
        config_hash: str,
        status: str = "maintenance",
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"maintenance", "maintenance_failed", "ready"}:
            raise ValueError(f"Unsupported index-generation status: {status}")
        with self._lock:
            self._db.execute(
                """INSERT INTO workspace_index_generations(
                       workspace_id, generation_id, pipeline_version, config_hash,
                       status, started_at, completed_at, error_json, config_json
                   ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                   ON CONFLICT(workspace_id, generation_id) DO UPDATE SET
                       pipeline_version=excluded.pipeline_version,
                       config_hash=excluded.config_hash,
                       config_json=excluded.config_json,
                       status=excluded.status,
                       started_at=excluded.started_at,
                       completed_at=NULL,
                       error_json=NULL""",
                (
                    workspace_id,
                    generation_id,
                    pipeline_version,
                    config_hash,
                    status,
                    now_iso(),
                    json.dumps(config or {}, sort_keys=True),
                ),
            )
        result = self.index_generation(workspace_id, generation_id)
        assert result is not None
        return result

    def update_index_generation(
        self,
        workspace_id: str,
        generation_id: str,
        *,
        status: str,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"maintenance", "maintenance_failed", "ready"}:
            raise ValueError(f"Unsupported index-generation status: {status}")
        with self._lock:
            cursor = self._db.execute(
                """UPDATE workspace_index_generations
                   SET status = ?,
                       started_at = CASE
                           WHEN ? = 'maintenance' THEN ? ELSE started_at
                       END,
                       completed_at = ?, error_json = ?
                   WHERE workspace_id = ? AND generation_id = ?""",
                (
                    status,
                    status,
                    now_iso(),
                    now_iso() if status in {"maintenance_failed", "ready"} else None,
                    json.dumps(error) if error is not None else None,
                    workspace_id,
                    generation_id,
                ),
            )
        if cursor.rowcount != 1:
            raise NotFoundError(f"Index generation {generation_id} was not found")
        result = self.index_generation(workspace_id, generation_id)
        assert result is not None
        return result

    def index_generation(self, workspace_id: str, generation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM workspace_index_generations
                   WHERE workspace_id = ? AND generation_id = ?""",
                (workspace_id, generation_id),
            ).fetchone()
        return self._generation_row(row)

    def workspace_index_generation(self, workspace_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM workspace_index_generations
                   WHERE workspace_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 1""",
                (workspace_id,),
            ).fetchone()
        return self._generation_row(row)

    @staticmethod
    def _generation_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            **dict(row),
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "config": json.loads(row["config_json"] or "{}"),
        }

    def clear_workspace_index(self, workspace_id: str, preserve_books: bool = False) -> None:
        """Clear derived v1/v2 index state; callers own Haiku document deletion."""

        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM answer_cache WHERE workspace_id = ?", (workspace_id,))
                self._db.execute("DELETE FROM evaluations WHERE workspace_id = ?", (workspace_id,))
                self._db.execute(
                    "DELETE FROM chunk_manifest WHERE workspace_id = ?", (workspace_id,)
                )
                self._db.execute(
                    "DELETE FROM document_segments WHERE workspace_id = ?", (workspace_id,)
                )
                self._db.execute(
                    "DELETE FROM document_index WHERE workspace_id = ?", (workspace_id,)
                )
                self._db.execute(
                    "DELETE FROM book_term_router WHERE workspace_id = ?", (workspace_id,)
                )
                self._db.execute(
                    "DELETE FROM book_structures WHERE workspace_id = ?", (workspace_id,)
                )
                if not preserve_books:
                    self._db.execute(
                        "DELETE FROM book_records WHERE workspace_id = ?", (workspace_id,)
                    )
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def save_book_knowledge_snapshot(
        self, workspace_id: str, snapshot: BookKnowledgeSnapshot | dict[str, Any]
    ) -> None:
        with self._lock:
            self._write_book_knowledge_snapshot(workspace_id, snapshot, transactional=True)

    def _write_book_knowledge_snapshot(
        self,
        workspace_id: str,
        snapshot: BookKnowledgeSnapshot | dict[str, Any],
        *,
        transactional: bool,
    ) -> None:
        from .models.book import BookKnowledgeSnapshot as SnapshotModel

        value = (
            snapshot
            if isinstance(snapshot, SnapshotModel)
            else SnapshotModel.model_validate(snapshot)
        )
        logical_id = value.logical_document_id
        structure = value.structure
        if transactional:
            self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                """DELETE FROM book_term_router
                       WHERE workspace_id = ? AND logical_document_id = ?""",
                (workspace_id, logical_id),
            )
            self._db.execute(
                """DELETE FROM book_structures
                       WHERE workspace_id = ? AND logical_document_id = ?""",
                (workspace_id, logical_id),
            )
            self._db.execute(
                """INSERT INTO book_structures VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    workspace_id,
                    logical_id,
                    value.generation_id,
                    structure.mode,
                    structure.confidence,
                    structure.total_pages,
                    json.dumps(structure.page_labels),
                    json.dumps([item.model_dump(mode="json") for item in structure.regions]),
                    json.dumps(structure.stats),
                    value.content_hash,
                ),
            )
            for node in structure.nodes:
                self._db.execute(
                    """INSERT INTO book_structure_nodes VALUES (
                               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                           )""",
                    (
                        workspace_id,
                        logical_id,
                        node.node_id,
                        node.parent_id,
                        node.kind,
                        node.depth,
                        node.ordinal,
                        node.title,
                        node.normalized_title,
                        node.page_start,
                        node.page_end,
                        node.source_kind,
                        node.confidence,
                        json.dumps(node.source_refs),
                    ),
                )
                self._db.execute(
                    "INSERT INTO book_term_router VALUES (?, ?, ?, ?, '')",
                    (workspace_id, logical_id, node.node_id, node.title),
                )
            alias_by_term: dict[str, list[str]] = {}
            for alias in value.graph.aliases:
                alias_by_term.setdefault(alias.term_id, []).append(alias.alias)
            for term in value.graph.terms:
                self._db.execute(
                    """INSERT INTO book_terms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        workspace_id,
                        logical_id,
                        term.term_id,
                        term.canonical,
                        term.normalized,
                        term.kind,
                        term.source_page,
                        term.source_ref,
                        term.confidence,
                        json.dumps(term.metadata),
                    ),
                )
                self._db.execute(
                    "INSERT INTO book_term_router VALUES (?, ?, ?, ?, ?)",
                    (
                        workspace_id,
                        logical_id,
                        term.term_id,
                        term.canonical,
                        " ".join(alias_by_term.get(term.term_id, [])),
                    ),
                )
            for alias in value.graph.aliases:
                self._db.execute(
                    "INSERT INTO book_term_aliases VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        workspace_id,
                        logical_id,
                        alias.term_id,
                        alias.alias,
                        alias.normalized_alias,
                        alias.relation,
                    ),
                )
            for target in value.graph.targets:
                target_payload = target.model_dump(mode="json")
                target_key = request_hash(target_payload)
                self._db.execute(
                    """INSERT INTO book_term_targets VALUES (
                               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                           )""",
                    (
                        workspace_id,
                        logical_id,
                        target.term_id,
                        target.node_id,
                        target.page_start,
                        target.page_end,
                        target.evidence_id,
                        target.relation,
                        target.confidence,
                        target_key,
                    ),
                )
            for edge in value.graph.edges:
                self._db.execute(
                    "INSERT INTO book_graph_edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace_id,
                        logical_id,
                        edge.edge_id,
                        edge.source_id,
                        edge.target_id,
                        edge.relation,
                        edge.weight,
                        json.dumps(edge.evidence_ids),
                    ),
                )
            self._db.execute(
                "INSERT INTO book_knowledge_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    logical_id,
                    value.generation_id,
                    value.schema_version,
                    value.content_hash,
                    value.model_dump_json(),
                    now_iso(),
                ),
            )
            if transactional:
                self._db.execute("COMMIT")
        except Exception:
            if transactional and self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def book_structure(self, workspace_id: str, logical_document_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM book_structures
                   WHERE workspace_id = ? AND logical_document_id = ?""",
                (workspace_id, logical_document_id),
            ).fetchone()
            nodes = self._db.execute(
                """SELECT * FROM book_structure_nodes
                   WHERE workspace_id = ? AND logical_document_id = ? ORDER BY ordinal""",
                (workspace_id, logical_document_id),
            ).fetchall()
        if row is None:
            return None
        return {
            "logical_document_id": logical_document_id,
            "generation_id": row["generation_id"],
            "mode": row["mode"],
            "confidence": row["confidence"],
            "total_pages": row["total_pages"],
            "page_labels": json.loads(row["page_labels_json"]),
            "regions": json.loads(row["regions_json"]),
            "stats": json.loads(row["stats_json"]),
            "snapshot_hash": row["snapshot_hash"],
            "nodes": [
                {
                    **dict(node),
                    "source_refs": json.loads(node["source_refs_json"]),
                }
                for node in nodes
            ],
        }

    def book_knowledge_snapshot(
        self, workspace_id: str, logical_document_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                """SELECT snapshot_json FROM book_knowledge_snapshots
                   WHERE workspace_id = ? AND logical_document_id = ?""",
                (workspace_id, logical_document_id),
            ).fetchone()
        return json.loads(row["snapshot_json"]) if row is not None else None

    def validate_index_generation(self, workspace_id: str, generation_id: str) -> dict[str, Any]:
        """Validate invariants SQLite can prove before the caller marks READY."""

        errors: list[str] = []
        with self._lock:
            generation = self._db.execute(
                """SELECT * FROM workspace_index_generations
                   WHERE workspace_id = ? AND generation_id = ?""",
                (workspace_id, generation_id),
            ).fetchone()
            books = self._db.execute(
                """SELECT logical_document_id, generation_id, pipeline_version
                   FROM book_records WHERE workspace_id = ?""",
                (workspace_id,),
            ).fetchall()
            chunks = self._db.execute(
                "SELECT * FROM chunk_manifest WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchall()
            segments = self._db.execute(
                "SELECT * FROM document_segments WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchall()
            structures = self._db.execute(
                "SELECT * FROM book_structures WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchall()
            snapshots = self._db.execute(
                "SELECT logical_document_id, generation_id, content_hash, snapshot_json "
                "FROM book_knowledge_snapshots WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchall()
            nodes = self._db.execute(
                """SELECT logical_document_id, node_id, parent_id
                   FROM book_structure_nodes WHERE workspace_id = ?""",
                (workspace_id,),
            ).fetchall()
            terms = self._db.execute(
                "SELECT logical_document_id, term_id FROM book_terms WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchall()
            edges = self._db.execute(
                "SELECT * FROM book_graph_edges WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchall()
            targets = self._db.execute(
                "SELECT * FROM book_term_targets WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchall()
        if generation is None:
            errors.append("generation_missing")
            pipeline_version = None
        else:
            pipeline_version = generation["pipeline_version"]
        if not books:
            errors.append("books_missing")
        book_ids = {book["logical_document_id"] for book in books}
        structure_by_book = {row["logical_document_id"]: row for row in structures}
        snapshot_by_book = {row["logical_document_id"]: row for row in snapshots}
        for book in books:
            logical_id = book["logical_document_id"]
            if book["generation_id"] != generation_id:
                errors.append(f"mixed_generation:{logical_id}")
            if pipeline_version is not None and book["pipeline_version"] != pipeline_version:
                errors.append(f"mixed_pipeline:{logical_id}")
            structure = structure_by_book.get(logical_id)
            if structure is None:
                errors.append(f"structure_missing:{logical_id}")
            elif structure["generation_id"] != generation_id:
                errors.append(f"structure_generation:{logical_id}")
            snapshot = snapshot_by_book.get(logical_id)
            if snapshot is None:
                errors.append(f"snapshot_missing:{logical_id}")
            elif snapshot["generation_id"] != generation_id:
                errors.append(f"snapshot_generation:{logical_id}")
        for logical_id in structure_by_book.keys() - book_ids:
            errors.append(f"orphan_structure:{logical_id}")

        node_ids = {(row["logical_document_id"], row["node_id"]) for row in nodes}
        node_parent = {
            (row["logical_document_id"], row["node_id"]): row["parent_id"] for row in nodes
        }
        for node_key, parent_id in node_parent.items():
            if parent_id is not None and (node_key[0], parent_id) not in node_ids:
                errors.append(f"parent_missing:{node_key[1]}")
                continue
            seen_nodes: set[str] = set()
            current = node_key[1]
            while current is not None:
                if current in seen_nodes:
                    errors.append(f"parent_cycle:{node_key[1]}")
                    break
                seen_nodes.add(current)
                current = node_parent.get((node_key[0], current))

        segment_by_id = {
            (row["logical_document_id"], row["segment_document_id"]): row for row in segments
        }
        segments_by_book: dict[str, list[sqlite3.Row]] = {}
        for segment in segments:
            logical_id = segment["logical_document_id"]
            segments_by_book.setdefault(logical_id, []).append(segment)
            marker = segment["segment_document_id"]
            if segment["generation_id"] != generation_id:
                errors.append(f"segment_generation:{marker}")
            if segment["page_number_mode"] != "absolute":
                errors.append(f"segment_pages_not_absolute:{marker}")
            boundaries = (
                segment["conversion_start"],
                segment["core_start"],
                segment["core_end"],
                segment["conversion_end"],
            )
            if any(value is None for value in boundaries) or list(boundaries) != sorted(boundaries):
                errors.append(f"segment_boundaries:{marker}")
        for logical_id in book_ids:
            ordered = sorted(
                segments_by_book.get(logical_id, []), key=lambda row: row["core_start"] or 0
            )
            if not ordered:
                errors.append(f"segments_missing:{logical_id}")
                continue
            expected_page = 1
            for segment in ordered:
                if segment["core_start"] != expected_page:
                    errors.append(f"segment_core_gap_or_overlap:{logical_id}:{expected_page}")
                expected_page = int(segment["core_end"] or 0) + 1
            structure = structure_by_book.get(logical_id)
            if structure is not None and expected_page - 1 != structure["total_pages"]:
                errors.append(f"segment_core_coverage:{logical_id}")

        evidence_ids: set[str] = set()
        evidence_by_book: set[tuple[str, str]] = set()
        chunk_orders: dict[str, set[int]] = {}
        exact_chunks: set[tuple[str, str, str, str]] = set()
        chunks_by_book: dict[str, int] = {}
        for chunk in chunks:
            prefix = chunk["chunk_id"]
            logical_id = chunk["logical_document_id"]
            chunks_by_book[logical_id] = chunks_by_book.get(logical_id, 0) + 1
            if chunk["generation_id"] != generation_id:
                errors.append(f"chunk_generation:{prefix}")
            refs = json.loads(chunk["refs_json"])
            pages = json.loads(chunk["pages_json"])
            if not refs:
                errors.append(f"refs_missing:{prefix}")
            if not pages:
                errors.append(f"pages_missing:{prefix}")
            if (
                not chunk["section_node_id"]
                or (logical_id, chunk["section_node_id"]) not in node_ids
            ):
                errors.append(f"section_missing:{prefix}")
            segment = segment_by_id.get((logical_id, chunk["segment_document_id"]))
            if segment is None:
                errors.append(f"chunk_segment_missing:{prefix}")
            elif pages and any(
                page < segment["core_start"] or page > segment["core_end"] for page in pages
            ):
                errors.append(f"chunk_outside_core:{prefix}")
            total_pages = (
                structure_by_book[logical_id]["total_pages"]
                if logical_id in structure_by_book
                else 0
            )
            if pages and any(
                not isinstance(page, int) or page < 1 or page > total_pages for page in pages
            ):
                errors.append(f"chunk_page_invalid:{prefix}")
            global_order = chunk["global_order"]
            if global_order is None or global_order in chunk_orders.setdefault(logical_id, set()):
                errors.append(f"chunk_order_duplicate:{logical_id}:{global_order}")
            else:
                chunk_orders[logical_id].add(global_order)
            exact_key = (logical_id, chunk["content_hash"], chunk["pages_json"], chunk["refs_json"])
            if exact_key in exact_chunks:
                errors.append(f"chunk_overlap_duplicate:{prefix}")
            exact_chunks.add(exact_key)
            evidence_id = chunk["evidence_id"]
            if not evidence_id:
                errors.append(f"evidence_missing:{prefix}")
            elif evidence_id in evidence_ids:
                errors.append(f"evidence_duplicate:{evidence_id}")
            else:
                evidence_ids.add(evidence_id)
                evidence_by_book.add((logical_id, evidence_id))
        for logical_id in book_ids:
            if not chunks_by_book.get(logical_id):
                errors.append(f"chunks_missing:{logical_id}")
            orders = chunk_orders.get(logical_id, set())
            if orders and orders != set(range(len(orders))):
                errors.append(f"chunk_order_gap:{logical_id}")
        chain_rows: dict[str, dict[str, sqlite3.Row]] = {}
        for chunk in chunks:
            if chunk["evidence_id"]:
                chain_rows.setdefault(chunk["logical_document_id"], {})[chunk["evidence_id"]] = (
                    chunk
                )
        for logical_id, by_evidence in chain_rows.items():
            ordered = sorted(by_evidence.values(), key=lambda row: row["global_order"])
            for index, chunk in enumerate(ordered):
                previous = ordered[index - 1]["evidence_id"] if index else None
                following = ordered[index + 1]["evidence_id"] if index + 1 < len(ordered) else None
                if chunk["previous_evidence_id"] != previous:
                    errors.append(f"previous_chain:{logical_id}:{chunk['evidence_id']}")
                if chunk["next_evidence_id"] != following:
                    errors.append(f"next_chain:{logical_id}:{chunk['evidence_id']}")
        for logical_id, snapshot_row in snapshot_by_book.items():
            try:
                snapshot_payload = json.loads(snapshot_row["snapshot_json"])
                if snapshot_payload.get("schema_version") != "2":
                    errors.append(f"snapshot_schema:{logical_id}")
                if snapshot_payload.get("content_hash") != snapshot_row["content_hash"]:
                    errors.append(f"snapshot_hash:{logical_id}")
                snapshot_evidence = {
                    (logical_id, str(item["evidence_id"]))
                    for item in snapshot_payload.get("evidence", [])
                }
                manifest_evidence = {item for item in evidence_by_book if item[0] == logical_id}
                if snapshot_evidence != manifest_evidence:
                    errors.append(f"snapshot_evidence:{logical_id}")
            except (KeyError, TypeError, ValueError):
                errors.append(f"snapshot_invalid:{logical_id}")
        term_ids = {(row["logical_document_id"], row["term_id"]) for row in terms}
        graph_ids = node_ids | term_ids
        graph_parent: dict[tuple[str, str], str] = {}
        for edge in edges:
            logical_id = edge["logical_document_id"]
            if (logical_id, edge["source_id"]) not in graph_ids:
                errors.append(f"edge_source_missing:{edge['edge_id']}")
            if (logical_id, edge["target_id"]) not in graph_ids:
                errors.append(f"edge_target_missing:{edge['edge_id']}")
            if any(
                (logical_id, item) not in evidence_by_book
                for item in json.loads(edge["evidence_json"])
            ):
                errors.append(f"edge_evidence_missing:{edge['edge_id']}")
            expected_ids = (
                node_ids if edge["relation"] in {"parent_of", "next_section"} else term_ids
            )
            if (logical_id, edge["source_id"]) not in expected_ids or (
                logical_id,
                edge["target_id"],
            ) not in expected_ids:
                errors.append(f"edge_endpoint_kind:{edge['edge_id']}")
            if edge["relation"] == "parent_of":
                graph_parent[(logical_id, edge["target_id"])] = edge["source_id"]
                if node_parent.get((logical_id, edge["target_id"])) != edge["source_id"]:
                    errors.append(f"edge_parent_mismatch:{edge['edge_id']}")
        for node_key in graph_parent:
            seen_graph_nodes: set[str] = set()
            current: str | None = node_key[1]
            while current is not None:
                if current in seen_graph_nodes:
                    errors.append(f"edge_parent_cycle:{node_key[1]}")
                    break
                seen_graph_nodes.add(current)
                current = graph_parent.get((node_key[0], current))
        for target in targets:
            logical_id = target["logical_document_id"]
            if target["node_id"] is not None and (logical_id, target["node_id"]) not in node_ids:
                errors.append(f"target_node_missing:{target['term_id']}")
            if (
                target["evidence_id"] is not None
                and (
                    logical_id,
                    target["evidence_id"],
                )
                not in evidence_by_book
            ):
                errors.append(f"target_evidence_missing:{target['term_id']}")
        report = {
            "valid": not errors,
            "workspace_id": workspace_id,
            "generation_id": generation_id,
            "book_count": len(books),
            "chunk_count": len(chunks),
            "segment_count": len(segments),
            "structure_node_count": len(nodes),
            "term_count": len(terms),
            "edge_count": len(edges),
            "errors": sorted(set(errors)),
        }
        if errors:
            raise ValueError(f"Index generation validation failed: {report['errors']}")
        return report

    def route_book_knowledge(
        self, workspace_id: str, query: str, limit: int = 12
    ) -> list[dict[str, Any]]:
        """Route lexical terms/aliases to sections and evidence without a vector side index."""

        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        tokens = re.findall(r"[\w]+", query.casefold(), flags=re.UNICODE)
        if not tokens:
            return []
        fts_query = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        with self._lock:
            routed = self._db.execute(
                """SELECT r.logical_document_id, r.term_id, r.canonical,
                          bm25(book_term_router) AS lexical_rank
                   FROM book_term_router AS r
                   WHERE book_term_router MATCH ? AND r.workspace_id = ?
                   ORDER BY lexical_rank LIMIT ?""",
                (fts_query, workspace_id, limit * 3),
            ).fetchall()
            results: list[dict[str, Any]] = []
            seen: set[tuple[object, ...]] = set()
            for route in routed:
                targets = self._db.execute(
                    """SELECT t.*, COALESCE(c.chunk_id, section_chunk.chunk_id) AS chunk_id
                       FROM book_term_targets AS t
                       LEFT JOIN chunk_manifest AS c
                         ON c.workspace_id = t.workspace_id
                        AND c.logical_document_id = t.logical_document_id
                        AND c.evidence_id = t.evidence_id
                       LEFT JOIN chunk_manifest AS section_chunk
                         ON section_chunk.rowid = (
                            SELECT c2.rowid FROM chunk_manifest AS c2
                            WHERE c2.workspace_id = t.workspace_id
                              AND c2.logical_document_id = t.logical_document_id
                              AND c2.section_node_id = t.node_id
                            ORDER BY c2.global_order, c2.chunk_order LIMIT 1
                         )
                       WHERE t.workspace_id = ? AND t.logical_document_id = ? AND t.term_id = ?
                       ORDER BY t.confidence DESC, t.page_start LIMIT 4""",
                    (workspace_id, route["logical_document_id"], route["term_id"]),
                ).fetchall()
                if not targets:
                    node = self._db.execute(
                        """SELECT node_id, page_start, page_end, confidence
                           FROM book_structure_nodes
                           WHERE workspace_id = ? AND logical_document_id = ? AND node_id = ?""",
                        (workspace_id, route["logical_document_id"], route["term_id"]),
                    ).fetchone()
                    if node is not None:
                        representative = self._db.execute(
                            """SELECT chunk_id, evidence_id FROM chunk_manifest
                               WHERE workspace_id = ? AND logical_document_id = ?
                                 AND section_node_id = ?
                               ORDER BY global_order, chunk_order LIMIT 1""",
                            (workspace_id, route["logical_document_id"], node["node_id"]),
                        ).fetchone()
                        results.append(
                            {
                                "logical_document_id": route["logical_document_id"],
                                "term_id": route["term_id"],
                                "term": route["canonical"],
                                "section_node_id": node["node_id"],
                                "page_start": node["page_start"],
                                "page_end": node["page_end"],
                                "evidence_id": (
                                    representative["evidence_id"] if representative else None
                                ),
                                "chunk_id": (
                                    representative["chunk_id"] if representative else None
                                ),
                                "confidence": node["confidence"],
                                "retrieval_path": "book-section",
                                "lexical_rank": route["lexical_rank"],
                            }
                        )
                        if len(results) >= limit:
                            return results
                        continue
                    targets = [None]
                for target in targets:
                    key = (
                        route["logical_document_id"],
                        route["term_id"],
                        target["node_id"] if target else None,
                        target["evidence_id"] if target else None,
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append(
                        {
                            "logical_document_id": route["logical_document_id"],
                            "term_id": route["term_id"],
                            "term": route["canonical"],
                            "section_node_id": target["node_id"] if target else None,
                            "page_start": target["page_start"] if target else None,
                            "page_end": target["page_end"] if target else None,
                            "evidence_id": target["evidence_id"] if target else None,
                            "chunk_id": target["chunk_id"] if target else None,
                            "confidence": target["confidence"] if target else 0.0,
                            "retrieval_path": (
                                f"book-{target['relation']}" if target else "book-term"
                            ),
                            "lexical_rank": route["lexical_rank"],
                        }
                    )
                    if len(results) >= limit:
                        return results
        return results

    def update_book_metadata(
        self, workspace_id: str, logical_document_id: str, metadata: dict[str, Any]
    ) -> None:
        with self._lock:
            cursor = self._db.execute(
                """UPDATE book_records SET metadata_json = ?, updated_at = ?
                   WHERE workspace_id = ? AND logical_document_id = ?""",
                (json.dumps(metadata), now_iso(), workspace_id, logical_document_id),
            )
            if cursor.rowcount == 1:
                self._db.execute("DELETE FROM answer_cache WHERE workspace_id = ?", (workspace_id,))
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
                "page_labels": json.loads(row["page_labels_json"]),
                "quality_flags": json.loads(row["quality_flags_json"]),
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
            # A legacy workspace with no managed catalogue still needs Haiku's
            # unfiltered compatibility path. A managed import with staged
            # segments, however, must never leak its unpublished partial book.
            with self._lock:
                staged = self._db.execute(
                    """SELECT 1 FROM ingest_segments AS s
                       JOIN jobs AS j ON j.id = s.job_id
                       WHERE j.workspace_id = ? LIMIT 1""",
                    (workspace_id,),
                ).fetchone()
                active_managed_import = self._db.execute(
                    """SELECT 1 FROM jobs WHERE workspace_id = ? AND kind IN ('ingest','reindex')
                       AND status IN (?, ?, ?, ?) LIMIT 1""",
                    (
                        workspace_id,
                        JobStatus.QUEUED,
                        JobStatus.RUNNING,
                        JobStatus.PAUSE_REQUESTED,
                        JobStatus.PAUSED,
                    ),
                ).fetchone()
            if staged is not None or active_managed_import is not None:
                return []
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

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
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (1, datetime('now'));
                """
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
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_event_id=row["last_event_id"],
        )

    def update_job(self, job_id: str, **changes: Any) -> JobSnapshot:
        allowed = {"status", "progress", "phase", "result", "error", "checkpoint", "last_event_id"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported job fields: {unknown}")
        columns: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            column = f"{key}_json" if key in {"result", "error"} else key
            if key in {"result", "error"} and value is not None:
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

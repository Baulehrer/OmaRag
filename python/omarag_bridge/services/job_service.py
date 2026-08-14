from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from importlib import metadata as package_metadata
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from ..adapters.base import HaikuAdapter
from ..compat_probe import SUPPORTED_DOCLING, SUPPORTED_HAIKU
from ..models.api import IngestRequest, ReindexRequest
from ..models.domain import BookMetadata, JobSnapshot, JobStatus, ReindexPreflight
from ..models.errors import ConflictError, OmaRagError, ReadOnlyError
from ..store import StateStore
from .event_service import EventService
from .resource_coordinator import ResourceCoordinator
from .source_fetcher import download_url_source
from .textbook_service import archive_source, file_sha256
from .workspace_service import WorkspaceService

TERMINAL_STATUSES = {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}
_ACTIVE_IMPORT_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.RUNNING,
    JobStatus.PAUSE_REQUESTED,
    JobStatus.PAUSED,
}
_URL_IMPORT_JOB_ID = re.compile(r"^job-[0-9a-f]{12}$")
_URL_IMPORT_STALE_SECONDS = 3600.0


def _index_pipeline(indexing: dict[str, Any] | None) -> str:
    return (
        "book-index-v3"
        if str((indexing or {}).get("pipeline") or "book-v2") == "book-v3"
        else "book-index-v2"
    )


def _same_model_name(left: str, right: str) -> bool:
    return left == right or left == f"{right}:latest" or right == f"{left}:latest"


def _rebuild_runtime_lock(
    workspace: Path,
    ollama_url: str,
    pipeline: str = "book-index-v2",
    *,
    config_bytes: bytes | None = None,
) -> dict[str, str]:
    """Resolve every versioned indexing dependency without pulling a model."""
    haiku = next(
        (
            package_metadata.version(name)
            for name in ("haiku-rag", "haiku-rag-slim")
            if _distribution_exists(name)
        ),
        "",
    )
    docling = package_metadata.version("docling") if _distribution_exists("docling") else ""
    if haiku != SUPPORTED_HAIKU or docling != SUPPORTED_DOCLING:
        raise RuntimeError(
            f"Book-v2 requires Haiku {SUPPORTED_HAIKU} and Docling {SUPPORTED_DOCLING}; "
            f"found Haiku {haiku or 'missing'}, Docling {docling or 'missing'}"
        )
    if config_bytes is None:
        config_path = workspace / "haiku.rag.yaml"
        config_bytes = config_path.read_bytes()
    import yaml

    config = yaml.safe_load(config_bytes) or {}
    embedding_config = (config.get("embeddings") or {}).get("model") or {}
    embedding = str(embedding_config.get("name") or "")
    embedding_provider = str(embedding_config.get("provider") or "ollama").casefold()
    reranker = str((((config.get("reranking") or {}).get("model") or {}).get("name")) or "")
    if not embedding:
        raise RuntimeError("Workspace has no configured embedding model")
    embedding_digest = ""
    if embedding_provider == "ollama":
        try:
            with httpx.Client(
                timeout=httpx.Timeout(3.0),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.get(f"{ollama_url.rstrip('/')}/api/tags")
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Ollama model inventory is unavailable: {exc}") from exc
        models = payload.get("models", []) if isinstance(payload, dict) else []
        matches = [
            item
            for item in models
            if isinstance(item, dict)
            and _same_model_name(str(item.get("name") or item.get("model") or ""), embedding)
        ]
        if len(matches) != 1 or not matches[0].get("digest"):
            raise RuntimeError(
                f"Configured embedding model is not installed unambiguously: {embedding}"
            )
        embedding_digest = str(matches[0]["digest"])
    return {
        "pipeline": pipeline,
        "haiku": haiku,
        "docling": docling,
        "workspace_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "embedding_provider": embedding_provider,
        "embedding_model": embedding,
        "embedding_digest": embedding_digest,
        "reranker_model": reranker,
    }


class _WriterLease:
    """Unforgeable marker for calls already inside ``JobService.writer``."""

    def __init__(self, owner: JobService) -> None:
        self.owner = owner
        self.active = True


def _distribution_exists(name: str) -> bool:
    try:
        package_metadata.version(name)
    except package_metadata.PackageNotFoundError:
        return False
    return True


def _source_fingerprint(source: str) -> str:
    path = Path(source).expanduser()
    if path.is_file():
        return file_sha256(path)
    import hashlib

    return hashlib.sha256(source.encode()).hexdigest()


def _catalog_epoch(records: list[dict[str, Any]]) -> str:
    """Fingerprint source metadata and the currently published segment mapping."""
    canonical = [
        {
            "logical_document_id": str(record["logical_document_id"]),
            "fingerprint": str(record["fingerprint"]),
            "managed_source": str(record.get("managed_source") or ""),
            "original_source": str(record.get("original_source") or ""),
            "metadata": record.get("metadata") or {},
            "segments": sorted(
                (
                    {
                        "segment_document_id": str(segment["segment_document_id"]),
                        "page_start": int(segment["page_start"]),
                        "page_end": int(segment["page_end"]),
                    }
                    for segment in record.get("segments", [])
                ),
                key=lambda item: (
                    item["page_start"],
                    item["page_end"],
                    item["segment_document_id"],
                ),
            ),
        }
        for record in records
    ]
    canonical.sort(key=lambda item: item["logical_document_id"])
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def _validate_reindex_sources(sources: list[dict[str, Any]]) -> None:
    """Revalidate archived originals immediately before destructive work."""
    for source in sources:
        path = Path(str(source["path"]))
        if not path.is_file():
            raise ConflictError(
                "Archived original disappeared after reindex preflight",
                details={"path": str(path)},
            )
        size = path.stat().st_size
        expected_size = int(source.get("size_bytes") or size)
        if size != expected_size:
            raise ConflictError(
                "Archived original size changed after reindex preflight",
                details={"path": str(path), "expected": expected_size, "actual": size},
            )
        actual = file_sha256(path)
        expected = str(source["fingerprint"])
        if actual != expected:
            raise ConflictError(
                "Archived original fingerprint changed after reindex preflight",
                details={"path": str(path), "expected": expected, "actual": actual},
            )


class JobService:
    def __init__(
        self,
        store: StateStore,
        workspaces: WorkspaceService,
        events: EventService,
        adapter: HaikuAdapter,
        resources: ResourceCoordinator,
        profile_config_activator: Callable[[str, str, str, str, str], Any] | None = None,
    ) -> None:
        self.store = store
        self.workspaces = workspaces
        self.events = events
        self.adapter = adapter
        self.resources = resources
        self.profile_config_activator = profile_config_activator
        self.content_egress_guard: Callable[[str, str], None] | None = None
        self.url_source_guard: Callable[[str, str], None] | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._admission_lock = asyncio.Lock()
        self._writer_lock = asyncio.Lock()
        self._segment_samples: dict[tuple[str, int], list[tuple[float, int]]] = {}

    def _authorize_model_content(self, workspace_id: str) -> None:
        """Re-evaluate the workspace policy inside the corpus writer lease."""

        if self.content_egress_guard is not None:
            self.content_egress_guard(workspace_id, self.workspaces.ollama_url)

    @property
    def active(self) -> bool:
        return bool(self._tasks)

    @asynccontextmanager
    async def writer(self, *, fail_if_active: bool = False):
        """Serialize corpus/config mutations with complete import jobs.

        ``fail_if_active`` checks admission before waiting for the writer lock.
        Pausing is checkpoint based: the job task unwinds and releases this lock,
        while callers that require a quiescent corpus still reject the persisted
        paused job through the active-job admission policy.
        """

        async with self._admission_lock:
            persisted_active = False
            list_jobs = getattr(getattr(self, "store", None), "list_jobs", None)
            if fail_if_active and callable(list_jobs):
                persisted_active = any(
                    job.kind in {"ingest", "reindex"}
                    and job.status
                    in {
                        JobStatus.QUEUED,
                        JobStatus.RUNNING,
                        JobStatus.PAUSE_REQUESTED,
                        JobStatus.PAUSED,
                    }
                    for job in list_jobs()
                )
            if fail_if_active and (self.active or persisted_active):
                raise ConflictError("An import or rebuild is queued, running, or paused")
            async with self._writer_lock:
                lease = _WriterLease(self)
                try:
                    yield lease
                finally:
                    lease.active = False

    async def _verify_runtime_lock(self, workspace_id: str, expected: dict[str, str]) -> None:
        if not expected:
            return
        current = await asyncio.to_thread(
            _rebuild_runtime_lock,
            Path(self.workspaces.get(workspace_id).path),
            self.workspaces.ollama_url,
            str(expected.get("pipeline") or "book-index-v2"),
        )
        if current != expected:
            raise ConflictError(
                "Indexing model identity changed while the job was running",
                details={"expected": expected, "actual": current},
            )

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def _url_import_root(self, workspace_id: str, *, create: bool) -> Path | None:
        workspace_path = Path(self.workspaces.get(workspace_id).path)
        workspace_root = workspace_path.resolve()
        if (
            workspace_path.is_symlink()
            or workspace_root.parent != self.workspaces.root
            or workspace_root.suffix != ".omarag"
            or not workspace_root.is_dir()
        ):
            raise ConflictError("Managed URL-import storage is not trustworthy")
        metadata_root = workspace_root / ".omarag"
        if metadata_root.is_symlink() or (
            metadata_root.exists() and metadata_root.resolve().parent != workspace_root
        ):
            raise ConflictError("Managed URL-import storage is not trustworthy")
        if create:
            metadata_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata_root.chmod(0o700)
        elif not metadata_root.is_dir():
            return None

        import_root = metadata_root / "url-imports"
        if import_root.is_symlink() or (
            import_root.exists() and import_root.resolve().parent != metadata_root.resolve()
        ):
            raise ConflictError("Managed URL-import storage is not trustworthy")
        if create:
            import_root.mkdir(mode=0o700, exist_ok=True)
            import_root.chmod(0o700)
        elif not import_root.is_dir():
            return None
        return import_root

    @staticmethod
    def _remove_url_import_path(path: Path) -> bool:
        """Remove one already parent-gated entry without following symlinks."""

        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
        return True

    def _prepare_url_import_directory(self, workspace_id: str, job_id: str) -> Path:
        if _URL_IMPORT_JOB_ID.fullmatch(job_id) is None:
            raise ConflictError("URL-import job identity is invalid")
        root = self._url_import_root(workspace_id, create=True)
        assert root is not None
        target = root / job_id
        if target.parent != root:
            raise ConflictError("Managed URL-import storage is not trustworthy")
        self._remove_url_import_path(target)
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        if target.is_symlink() or target.resolve().parent != root.resolve():
            raise ConflictError("Managed URL-import storage is not trustworthy")
        return target

    def _cleanup_url_import_directory(self, workspace_id: str, job_id: str) -> None:
        if _URL_IMPORT_JOB_ID.fullmatch(job_id) is None:
            return
        root = self._url_import_root(workspace_id, create=False)
        if root is None:
            return
        target = root / job_id
        if target.parent == root:
            self._remove_url_import_path(target)

    def sweep_url_import_orphans(
        self,
        *,
        now: float | None = None,
        stale_after_seconds: float = _URL_IMPORT_STALE_SECONDS,
    ) -> int:
        """Delete only stale URL-import work paths not owned by an active job.

        The sweep is intentionally conservative: registered workspace roots,
        direct children, lstat age, active job ownership, and symlink handling
        are checked before any removal. Legacy V1.2 root-level temp directories
        are removed only when that workspace has no active ingest at all.
        """

        if stale_after_seconds < 1:
            raise ValueError("stale_after_seconds must be at least one second")
        cutoff = (time.time() if now is None else now) - stale_after_seconds
        removed = 0
        for workspace in self.workspaces.list():
            jobs = self.store.list_jobs(workspace.id)
            active = {
                job.id
                for job in jobs
                if job.kind == "ingest" and job.status in _ACTIVE_IMPORT_STATUSES
            }
            try:
                root = self._url_import_root(workspace.id, create=False)
            except (ConflictError, OSError):
                root = None
            if root is not None:
                try:
                    entries = list(root.iterdir())
                except OSError:
                    entries = []
                for entry in entries:
                    if entry.parent != root or entry.name in active:
                        continue
                    try:
                        if entry.lstat().st_mtime > cutoff:
                            continue
                        removed += int(self._remove_url_import_path(entry))
                    except OSError:
                        continue

            if active:
                continue
            workspace_root = Path(workspace.path).resolve()
            try:
                legacy_entries = [
                    entry
                    for entry in workspace_root.iterdir()
                    if entry.name.startswith(".omarag-url-import-")
                ]
            except OSError:
                legacy_entries = []
            for entry in legacy_entries:
                if entry.parent != workspace_root:
                    continue
                try:
                    if entry.lstat().st_mtime > cutoff:
                        continue
                    removed += int(self._remove_url_import_path(entry))
                except OSError:
                    continue
        return removed

    async def start_ingest(
        self, workspace_id: str, request: IngestRequest, idempotency_key: str
    ) -> tuple[JobSnapshot, bool]:
        async with self._admission_lock:
            return await self._start_ingest_admitted(workspace_id, request, idempotency_key)

    async def _start_ingest_admitted(
        self, workspace_id: str, request: IngestRequest, idempotency_key: str
    ) -> tuple[JobSnapshot, bool]:
        workspace = self.workspaces.get(workspace_id)
        if workspace.read_only:
            raise ReadOnlyError("A read-only workspace cannot accept imports")
        job_id = f"job-{uuid4().hex[:12]}"
        payload = request.model_dump(mode="json")
        job, reused = self.store.create_job_idempotent(
            job_id=job_id,
            workspace_id=workspace_id,
            kind="ingest",
            payload=payload,
            idempotency_key=idempotency_key,
        )
        if not reused:
            await self.events.emit(
                "job.queued",
                correlation_id=job.id,
                workspace_id=workspace_id,
                job_id=job.id,
                payload={"kind": "ingest"},
            )
            self._spawn(job.id)
        return self.store.get_job(job.id), reused

    def preflight_reindex(
        self,
        workspace_id: str,
        indexing: dict[str, Any],
        *,
        target_config_content: str | None = None,
    ) -> ReindexPreflight:
        workspace = self.workspaces.get(workspace_id)
        if workspace.read_only:
            raise ReadOnlyError("A read-only workspace cannot be rebuilt")
        records = self.store.book_records(workspace_id)
        issues: list[str] = []
        sources: list[dict[str, Any]] = []
        source_bytes = 0
        for record in records:
            source = Path(str(record.get("managed_source") or record["original_source"]))
            if not source.is_file():
                issues.append(f"Archived original is missing: {source}")
                continue
            actual = file_sha256(source)
            expected = str(record["fingerprint"])
            if actual != expected:
                issues.append(f"Archived original fingerprint changed: {source}")
                continue
            size = source.stat().st_size
            source_bytes += size
            sources.append(
                {
                    "path": str(source),
                    "original_source": str(record["original_source"]),
                    "fingerprint": expected,
                    "metadata": record.get("metadata") or {},
                    "logical_document_id": str(record["logical_document_id"]),
                    "size_bytes": size,
                }
            )
        available = shutil.disk_usage(Path(workspace.path)).free
        minimum_free = max(2 * 1024**3, source_bytes // 2)
        if available < minimum_free:
            issues.append(
                f"Insufficient free space: need {minimum_free} bytes, have {available} bytes"
            )
        if not self.adapter.available:
            issues.append("Haiku/Docling compatibility probe is not ready")
        if not records:
            issues.append("Workspace has no indexed archived originals to rebuild")
        cache_writable = False
        try:
            probe_dir = Path(workspace.path) / ".omarag"
            probe_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix="reindex-probe-", dir=probe_dir):
                cache_writable = True
        except OSError as exc:
            issues.append(f"Workspace cache is not writable: {exc}")
        runtime_lock: dict[str, str] = {}
        if records and self.adapter.available and cache_writable:
            try:
                runtime_lock = _rebuild_runtime_lock(
                    Path(workspace.path),
                    self.workspaces.ollama_url,
                    _index_pipeline(indexing),
                    config_bytes=(
                        target_config_content.encode()
                        if target_config_content is not None
                        else None
                    ),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                issues.append(str(exc))
        preflight_id = f"reindex-preflight-{uuid4().hex[:16]}"
        payload = {
            "kind": "reindex",
            "mode": "full",
            "ready": not issues,
            "sources": sources,
            "indexing": indexing,
            "issues": issues,
            "estimated_source_bytes": source_bytes,
            "available_bytes": available,
            "runtime_lock": runtime_lock,
            "catalog_epoch": _catalog_epoch(records),
        }
        self.store.save_import_preflight(preflight_id, workspace_id, payload)
        return ReindexPreflight(
            id=preflight_id,
            workspace_id=workspace_id,
            ready=not issues,
            documents=len(sources),
            estimated_source_bytes=source_bytes,
            available_bytes=available,
            checks={
                "archived_originals": len(sources) == len(records),
                "fingerprints": len(sources) == len(records),
                "adapter_compatible": self.adapter.available,
                "cache_writable": cache_writable,
                "runtime_locked": bool(runtime_lock),
                "disk_reserve": available >= minimum_free,
                "destructive_in_place": True,
                "live_rollback": False,
            },
            issues=issues,
        )

    async def start_reindex(
        self,
        workspace_id: str,
        request: ReindexRequest,
        idempotency_key: str,
    ) -> tuple[JobSnapshot, bool]:
        async with self._admission_lock:
            return await self._start_reindex_admitted(workspace_id, request, idempotency_key)

    async def _start_reindex_admitted(
        self,
        workspace_id: str,
        request: ReindexRequest,
        idempotency_key: str,
    ) -> tuple[JobSnapshot, bool]:
        self.workspaces.get(workspace_id)
        preflight = self.store.get_import_preflight(request.preflight_id, workspace_id)
        if preflight.get("kind") != "reindex" or preflight.get("mode") != "full":
            raise ConflictError("Preflight is not a full reindex preflight")
        if not preflight.get("ready"):
            raise ConflictError(
                "Reindex preflight did not pass", details={"issues": preflight.get("issues", [])}
            )
        if dict(preflight.get("indexing") or {}) != request.indexing.model_dump(mode="json"):
            raise ConflictError("Reindex options changed after preflight")
        payload = {
            "preflight_id": request.preflight_id,
            "mode": "full",
            "confirm": request.confirm,
            "sources": preflight["sources"],
            "indexing": preflight["indexing"],
            "runtime_lock": preflight.get("runtime_lock") or {},
            "catalog_epoch": str(preflight.get("catalog_epoch") or ""),
        }
        job_id = f"job-{uuid4().hex[:12]}"
        job, reused = self.store.create_job_idempotent(
            job_id=job_id,
            workspace_id=workspace_id,
            kind="reindex",
            payload=payload,
            idempotency_key=idempotency_key,
        )
        if not reused:
            await self.events.emit(
                "index.rebuild.queued",
                correlation_id=job.id,
                workspace_id=workspace_id,
                job_id=job.id,
                payload={"mode": "full", "documents": len(preflight["sources"])},
            )
            self._spawn(job.id)
        return self.store.get_job(job.id), reused

    def _profile_stage_path(self, workspace_id: str, target_etag: str) -> Path:
        if len(target_etag) != 64 or any(
            character not in "0123456789abcdef" for character in target_etag
        ):
            raise ConflictError("Target model configuration has an invalid digest")
        workspace_root = Path(self.workspaces.get(workspace_id).path).resolve()
        stage_dir = workspace_root / ".omarag" / "profile-reindex"
        stage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stage_dir.is_symlink() or stage_dir.resolve().parent.parent != workspace_root:
            raise ConflictError("Workspace profile staging directory is not trustworthy")
        stage_dir.chmod(0o700)
        target = stage_dir / f"{target_etag}.yaml"
        if target.is_symlink():
            raise ConflictError("Workspace profile staging file is not trustworthy")
        return target

    def _write_profile_stage(
        self,
        workspace_id: str,
        target_etag: str,
        content: str,
    ) -> str:
        target = self._profile_stage_path(workspace_id, target_etag)
        if hashlib.sha256(content.encode()).hexdigest() != target_etag:
            raise ConflictError("Target model configuration failed its integrity check")
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            if hashlib.sha256(existing.encode()).hexdigest() != target_etag:
                raise ConflictError("Existing staged model configuration is corrupt")
            target.chmod(0o600)
        else:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".profile-",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as stream:
                stream.write(content)
                temporary = Path(stream.name)
            try:
                temporary.chmod(0o600)
                temporary.replace(target)
                target.chmod(0o600)
            finally:
                temporary.unlink(missing_ok=True)
        workspace_root = Path(self.workspaces.get(workspace_id).path).resolve()
        return str(target.relative_to(workspace_root))

    def _read_profile_stage(
        self,
        workspace_id: str,
        transition: dict[str, Any],
    ) -> str:
        target_etag = str(transition.get("target_config_etag") or "")
        expected = self._profile_stage_path(workspace_id, target_etag)
        relative = Path(str(transition.get("staged_config") or ""))
        workspace_root = Path(self.workspaces.get(workspace_id).path).resolve()
        candidate = workspace_root / relative
        if candidate.is_symlink() or candidate.resolve() != expected.resolve():
            raise ConflictError("Staged model configuration path failed validation")
        try:
            content = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConflictError(
                "Staged model configuration is unavailable; rebuild cannot resume"
            ) from exc
        if hashlib.sha256(content.encode()).hexdigest() != target_etag:
            raise ConflictError("Staged model configuration failed its integrity check")
        return content

    @staticmethod
    def _idempotency_fingerprint(idempotency_key: str) -> str:
        return hashlib.sha256(idempotency_key.encode()).hexdigest()

    def profile_reindex_replay(
        self,
        workspace_id: str,
        *,
        profile_preflight_id: str,
        indexing: dict[str, Any],
        idempotency_key: str,
    ) -> JobSnapshot | None:
        """Resolve an exact profile-reindex replay before current-config checks."""

        fingerprint = self._idempotency_fingerprint(idempotency_key)
        for job in self.store.list_jobs(workspace_id):
            transition = dict(job.payload.get("profile_transition") or {})
            if transition.get("idempotency_fingerprint") != fingerprint:
                continue
            if (
                transition.get("profile_preflight_id") != profile_preflight_id
                or dict(job.payload.get("indexing") or {}) != indexing
            ):
                raise ConflictError("Idempotency-Key was already used for another profile rebuild")
            return job
        return None

    async def start_profile_reindex_under_writer(
        self,
        workspace_id: str,
        *,
        writer_lease: _WriterLease,
        profile_preflight_id: str,
        target_config_content: str,
        expected_current_etag: str,
        target_config_etag: str,
        expected_embedding_model: str,
        expected_embedding_digest: str,
        recommendation_id: str,
        indexing: dict[str, Any],
        idempotency_key: str,
    ) -> tuple[JobSnapshot, bool]:
        """Queue a staged embedding rebuild without re-entering writer locks.

        The explicit lease removes the old nested-lock/deadlock path.  Downloads,
        digest refresh, target rendering, and this admission all happen inside
        one caller-owned writer transaction; the active config is untouched.
        """

        if (
            not isinstance(writer_lease, _WriterLease)
            or writer_lease.owner is not self
            or not writer_lease.active
        ):
            raise ConflictError("Profile rebuild admission requires an active writer lease")
        if self.profile_config_activator is None:
            raise ConflictError("Staged profile activation is unavailable")
        current_path = Path(self.workspaces.get(workspace_id).path) / "haiku.rag.yaml"
        current_etag = hashlib.sha256(current_path.read_bytes()).hexdigest()
        if current_etag != expected_current_etag:
            raise ConflictError("Workspace configuration changed after model preflight")
        if hashlib.sha256(target_config_content.encode()).hexdigest() != target_config_etag:
            raise ConflictError("Target model configuration failed its integrity check")
        preflight_view = self.preflight_reindex(
            workspace_id,
            indexing,
            target_config_content=target_config_content,
        )
        preflight = self.store.get_import_preflight(preflight_view.id, workspace_id)
        if not preflight_view.ready:
            raise ConflictError(
                "Profile rebuild preflight did not pass",
                details={"issues": preflight_view.issues},
            )
        runtime_lock = dict(preflight.get("runtime_lock") or {})
        actual_model = str(runtime_lock.get("embedding_model") or "").removesuffix(":latest")
        expected_model = expected_embedding_model.removesuffix(":latest")
        actual_digest = str(runtime_lock.get("embedding_digest") or "").casefold()
        expected_digest = expected_embedding_digest.casefold()
        if actual_model != expected_model or actual_digest != expected_digest:
            raise ConflictError(
                "Installed embedding artifact does not match the consented model profile",
                details={
                    "expected_model": expected_embedding_model,
                    "actual_model": runtime_lock.get("embedding_model"),
                    "expected_digest": expected_embedding_digest,
                    "actual_digest": runtime_lock.get("embedding_digest"),
                },
            )
        staged_config = self._write_profile_stage(
            workspace_id,
            target_config_etag,
            target_config_content,
        )
        payload = {
            "preflight_id": preflight_view.id,
            "mode": "full",
            "confirm": "APPLY_AND_REINDEX",
            "sources": preflight["sources"],
            "indexing": preflight["indexing"],
            "runtime_lock": runtime_lock,
            "catalog_epoch": str(preflight.get("catalog_epoch") or ""),
            "profile_transition": {
                "profile_preflight_id": profile_preflight_id,
                "recommendation_id": recommendation_id,
                "expected_current_config_etag": expected_current_etag,
                "target_config_etag": target_config_etag,
                "staged_config": staged_config,
                "embedding_model": expected_embedding_model,
                "embedding_digest": expected_embedding_digest,
                "idempotency_fingerprint": self._idempotency_fingerprint(idempotency_key),
            },
        }
        job_id = f"job-{uuid4().hex[:12]}"
        job, reused = self.store.create_job_idempotent(
            job_id=job_id,
            workspace_id=workspace_id,
            kind="reindex",
            payload=payload,
            idempotency_key=idempotency_key,
        )
        if not reused:
            await self.events.emit(
                "model-profile.rebuild.queued",
                correlation_id=job.id,
                workspace_id=workspace_id,
                job_id=job.id,
                payload={
                    "mode": "apply-and-reindex",
                    "documents": len(preflight["sources"]),
                    "recommendation_id": recommendation_id,
                },
            )
        return self.store.get_job(job.id), reused

    def spawn_profile_reindex(self, job_id: str) -> None:
        """Start a previously admitted profile job after releasing resources."""

        job = self.store.get_job(job_id)
        if job.kind != "reindex" or not job.payload.get("profile_transition"):
            raise ConflictError("Job is not an admitted profile rebuild")
        if job.status == JobStatus.QUEUED:
            self._spawn(job_id)

    def _spawn(self, job_id: str) -> None:
        previous = self._tasks.get(job_id)
        if previous is not None and not previous.done():
            return
        job = self.store.get_job(job_id)
        runner = self._run_reindex if job.kind == "reindex" else self._run_ingest
        task = asyncio.create_task(runner(job_id), name=job_id)
        self._tasks[job_id] = task

        def forget(completed: asyncio.Task[None]) -> None:
            if self._tasks.get(job_id) is completed:
                self._tasks.pop(job_id, None)

        task.add_done_callback(forget)

    async def _run_reindex(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        generation_checkpoint = self.store.checkpoint_data(job_id, "reindex-generation")
        generation_id = str(
            (generation_checkpoint or {}).get("generation_id") or f"index-{uuid4().hex[:16]}"
        )
        config_hash = hashlib.sha256(
            json.dumps(
                {
                    "indexing": job.payload.get("indexing") or {},
                    "runtime_lock": job.payload.get("runtime_lock") or {},
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        generation_started = generation_checkpoint is not None
        pipeline_version = _index_pipeline(dict(job.payload.get("indexing") or {}))
        profile_transition = dict(job.payload.get("profile_transition") or {})
        staged_profile_content: str | None = None
        async with self._writer_lock:
            imported: list[dict[str, Any]] = []
            try:
                if not await self._continue(job_id):
                    return
                self._authorize_model_content(job.workspace_id)
                sources = list(job.payload.get("sources") or [])
                cleared = self.store.checkpoint_data(job_id, "legacy-index-cleared") is not None
                # Fail while the old index is still intact whenever the frozen
                # preflight catalogue or an archived source has changed.
                await asyncio.to_thread(_validate_reindex_sources, sources)
                if not cleared:
                    expected_catalog = str(job.payload.get("catalog_epoch") or "")
                    current_catalog = _catalog_epoch(self.store.book_records(job.workspace_id))
                    if expected_catalog and current_catalog != expected_catalog:
                        raise ConflictError(
                            "Workspace catalogue changed after reindex preflight",
                            details={
                                "expected_catalog_epoch": expected_catalog,
                                "actual_catalog_epoch": current_catalog,
                            },
                        )
                expected_lock = dict(job.payload.get("runtime_lock") or {})
                if profile_transition:
                    staged_profile_content = await asyncio.to_thread(
                        self._read_profile_stage,
                        job.workspace_id,
                        profile_transition,
                    )
                    target_lock = await asyncio.to_thread(
                        _rebuild_runtime_lock,
                        Path(self.workspaces.get(job.workspace_id).path),
                        self.workspaces.ollama_url,
                        pipeline_version,
                        config_bytes=staged_profile_content.encode(),
                    )
                    if target_lock != expected_lock:
                        raise ConflictError(
                            "Target indexing dependencies changed after profile admission",
                            details={"expected": expected_lock, "actual": target_lock},
                        )
                    current_config = Path(
                        self.workspaces.get(job.workspace_id).path,
                        "haiku.rag.yaml",
                    ).read_bytes()
                    current_etag = hashlib.sha256(current_config).hexdigest()
                    allowed_etags = {
                        str(profile_transition["expected_current_config_etag"]),
                        str(profile_transition["target_config_etag"]),
                    }
                    if current_etag not in allowed_etags:
                        raise ConflictError(
                            "Workspace configuration changed after profile admission",
                            details={"actual_config_etag": current_etag},
                        )
                    if generation_checkpoint is None and current_etag != str(
                        profile_transition["expected_current_config_etag"]
                    ):
                        raise ConflictError(
                            "Target profile was activated without a maintenance generation"
                        )
                else:
                    current_lock = await asyncio.to_thread(
                        _rebuild_runtime_lock,
                        Path(self.workspaces.get(job.workspace_id).path),
                        self.workspaces.ollama_url,
                        pipeline_version,
                    )
                    if current_lock != expected_lock:
                        raise ConflictError(
                            "Indexing dependencies changed after reindex preflight",
                            details={"expected": expected_lock, "actual": current_lock},
                        )
                if generation_checkpoint is None:
                    self.store.begin_index_generation(
                        job.workspace_id,
                        generation_id,
                        pipeline_version,
                        config_hash,
                        status="maintenance",
                        config=expected_lock,
                    )
                    generation_started = True
                    self.store.checkpoint(
                        job_id,
                        "reindex-generation",
                        {"generation_id": generation_id, "config_hash": config_hash},
                    )
                else:
                    self.store.update_index_generation(
                        job.workspace_id, generation_id, status="maintenance"
                    )
                self.store.update_job(job_id, status=JobStatus.RUNNING, phase="maintenance")
                await self.events.emit(
                    "index.rebuild.started",
                    correlation_id=job_id,
                    workspace_id=job.workspace_id,
                    job_id=job_id,
                    payload={"generation_id": generation_id},
                )
                # Maintenance is visible before admission. The exclusive lease
                # then drains already-admitted chats before stopping the query
                # worker or deleting a single published document.
                async with self.resources.indexing():
                    if profile_transition:
                        assert staged_profile_content is not None
                        target_lock = await asyncio.to_thread(
                            _rebuild_runtime_lock,
                            Path(self.workspaces.get(job.workspace_id).path),
                            self.workspaces.ollama_url,
                            pipeline_version,
                            config_bytes=staged_profile_content.encode(),
                        )
                        if target_lock != expected_lock:
                            raise ConflictError(
                                "Target dependencies changed at the rebuild boundary",
                                details={"expected": expected_lock, "actual": target_lock},
                            )
                    await asyncio.to_thread(_validate_reindex_sources, sources)
                    if not cleared:
                        expected_catalog = str(job.payload.get("catalog_epoch") or "")
                        current_catalog = _catalog_epoch(self.store.book_records(job.workspace_id))
                        if expected_catalog and current_catalog != expected_catalog:
                            raise ConflictError(
                                "Workspace catalogue changed at the rebuild boundary",
                                details={
                                    "expected_catalog_epoch": expected_catalog,
                                    "actual_catalog_epoch": current_catalog,
                                },
                            )
                    if profile_transition:
                        if not await self._continue(job_id):
                            raise asyncio.CancelledError
                        assert self.profile_config_activator is not None
                        await asyncio.to_thread(
                            self.profile_config_activator,
                            job.workspace_id,
                            staged_profile_content,
                            str(profile_transition["expected_current_config_etag"]),
                            str(profile_transition["target_config_etag"]),
                            generation_id,
                        )
                        self.store.checkpoint(
                            job_id,
                            "profile-config-activated",
                            {"target_config_etag": profile_transition["target_config_etag"]},
                        )
                    current_lock = await asyncio.to_thread(
                        _rebuild_runtime_lock,
                        Path(self.workspaces.get(job.workspace_id).path),
                        self.workspaces.ollama_url,
                        pipeline_version,
                    )
                    if current_lock != expected_lock:
                        raise ConflictError(
                            "Indexing dependencies changed at the rebuild boundary",
                            details={"expected": expected_lock, "actual": current_lock},
                        )
                    prepare = getattr(self.adapter, "prepare_rebuild", None)
                    if prepare is not None:
                        await prepare(self.workspaces.database_path(job.workspace_id))

                    if not cleared:
                        records = self.store.book_records(job.workspace_id)
                        targets = [
                            str(segment["segment_document_id"])
                            for record in records
                            for segment in record.get("segments", [])
                        ]
                        for target in targets:
                            if not await self._continue(job_id):
                                raise asyncio.CancelledError
                            await self.adapter.delete_document(
                                self.workspaces.database_path(job.workspace_id), target
                            )
                        # Originals and their catalogue metadata are source-of-truth,
                        # not derived index state. Keep them so an interrupted
                        # in-place rebuild is always resumable.
                        self.store.clear_workspace_index(job.workspace_id, preserve_books=True)
                        self.store.checkpoint(
                            job_id, "legacy-index-cleared", {"document_ids": targets}
                        )
                self.store.update_index_generation(
                    job.workspace_id, generation_id, status="maintenance"
                )

                for index, source in enumerate(sources):
                    if not await self._continue(job_id):
                        raise asyncio.CancelledError
                    completed = self.store.checkpoint_data(job_id, f"book-{index}-complete")
                    if completed is not None:
                        imported.append(completed)
                        continue
                    self.store.update_job(
                        job_id,
                        phase="rebuilding",
                        progress=index / max(len(sources), 1),
                        checkpoint=f"book-{index}",
                    )
                    metadata = BookMetadata.model_validate(source.get("metadata") or {})
                    await self._verify_runtime_lock(job.workspace_id, expected_lock)
                    indexing_options = dict(job.payload.get("indexing") or {})
                    indexing_options["_runtime_lock"] = expected_lock
                    result = await self.adapter.ingest(
                        self.workspaces.database_path(job.workspace_id),
                        str(source["path"]),
                        parser_id="docling",
                        processing_profile="quality",
                        segment_guard=self.resources.indexing,
                        generation_id=generation_id,
                        document_fingerprint=str(source["fingerprint"]),
                        resume_segments=self.store.list_segments(job_id, index),
                        on_segment=lambda segment, source_index=index: self._segment_committed(
                            job_id, source_index, segment
                        ),
                        on_phase=lambda phase, start, end, pages, source_index=index: self._phase(
                            job_id, source_index, len(sources), phase, start, end, pages
                        ),
                        segment_sizer=self.resources.segment_pages,
                        metadata=metadata,
                        original_source=str(source["original_source"]),
                        indexing_options=indexing_options,
                        llm_url=self.workspaces.ollama_url,
                    )
                    await self._verify_runtime_lock(job.workspace_id, expected_lock)
                    result.setdefault("fingerprint", source["fingerprint"])
                    result.setdefault("generation_id", generation_id)
                    result.setdefault("original_source", source["original_source"])
                    result.setdefault("managed_source", source["path"])
                    result.setdefault("runtime_lock", expected_lock)
                    if (
                        Path(str(source["path"])).suffix.casefold() == ".pdf"
                        and str(result.get("pipeline_version")) != pipeline_version
                    ):
                        raise ConflictError(
                            f"A PDF did not complete the homogeneous {pipeline_version} path"
                        )
                    self.store.upsert_document(
                        job.workspace_id,
                        str(source["original_source"]),
                        str(source["fingerprint"]),
                        result,
                    )
                    self.store.checkpoint(job_id, f"book-{index}-complete", result)
                    imported.append(result)

                await self._verify_runtime_lock(job.workspace_id, expected_lock)
                self.store.validate_index_generation(job.workspace_id, generation_id)
                self.store.update_index_generation(job.workspace_id, generation_id, status="ready")
                self.store.clear_answer_cache(job.workspace_id)
                self.store.update_job(
                    job_id,
                    status=JobStatus.COMPLETED,
                    phase="completed",
                    progress=1.0,
                    checkpoint="completed",
                    result={
                        "generation_id": generation_id,
                        "documents": imported,
                        **(
                            {
                                "config_etag": profile_transition["target_config_etag"],
                                "recommendation_id": profile_transition["recommendation_id"],
                            }
                            if profile_transition
                            else {}
                        ),
                    },
                )
                await self.events.emit(
                    "index.generation.published",
                    correlation_id=job_id,
                    workspace_id=job.workspace_id,
                    job_id=job_id,
                    payload={"generation_id": generation_id, "documents": len(imported)},
                )
                if profile_transition:
                    with suppress(OSError):
                        self._profile_stage_path(
                            job.workspace_id,
                            str(profile_transition["target_config_etag"]),
                        ).unlink(missing_ok=True)
            except asyncio.CancelledError:
                if generation_started:
                    self.store.update_index_generation(
                        job.workspace_id,
                        generation_id,
                        status="maintenance_failed",
                        error={
                            "code": "REINDEX_INTERRUPTED",
                            "message": "Rebuild interrupted",
                        },
                    )
                if self.store.get_job(job_id).status != JobStatus.CANCELLED:
                    self.store.update_job(job_id, status=JobStatus.PAUSED, phase="interrupted")
                raise
            except OmaRagError as exc:
                if generation_started:
                    self.store.update_index_generation(
                        job.workspace_id,
                        generation_id,
                        status="maintenance_failed",
                        error={
                            "code": exc.code,
                            "message": exc.message,
                            "details": exc.details,
                        },
                    )
                await self._fail(job, exc.code, exc.message, exc.details, exc.retryable)
            except Exception as exc:
                if generation_started:
                    self.store.update_index_generation(
                        job.workspace_id,
                        generation_id,
                        status="maintenance_failed",
                        error={"code": "REINDEX_FAILED", "message": str(exc)},
                    )
                await self._fail(job, "REINDEX_FAILED", str(exc), {}, True)

    def _published_source_result(
        self,
        job: JobSnapshot,
        source_index: int,
        fingerprint: str,
        generation_id: str,
    ) -> dict[str, Any] | None:
        """Resolve a generation durably published before a runner interruption."""

        publication = self.store.checkpoint_data(job.id, f"source-published-{source_index}")
        if publication is None:
            return None
        indexed = self.store.document_by_fingerprint(job.workspace_id, fingerprint)
        if (
            indexed is None
            or str(indexed.get("generation_id") or "") != generation_id
            or str(publication.get("generation_id") or "") != generation_id
        ):
            raise ConflictError(
                "Published document generation no longer matches its recovery checkpoint",
                details={
                    "source_index": source_index,
                    "generation_id": generation_id,
                    "published_generation_id": (
                        str(indexed.get("generation_id") or "") if indexed else None
                    ),
                },
            )
        return dict(indexed["result"])

    async def _retire_superseded_segments(
        self,
        job: JobSnapshot,
        source_index: int,
        result: dict[str, Any],
    ) -> None:
        """Idempotently retire provider rows hidden by the published Store mapping."""

        generation_id = str(result.get("generation_id") or "")
        current_ids = {str(item) for item in result.get("segment_document_ids", []) if str(item)}
        pending = list(
            dict.fromkeys(
                str(item)
                for item in result.get("superseded_segment_document_ids", [])
                if str(item) and str(item) not in current_ids
            )
        )
        checkpoint_name = f"source-retirement-{source_index}"
        checkpoint = self.store.checkpoint_data(job.id, checkpoint_name) or {}
        if checkpoint and str(checkpoint.get("generation_id") or "") != generation_id:
            raise ConflictError(
                "Retirement checkpoint belongs to another document generation",
                details={"source_index": source_index, "generation_id": generation_id},
            )
        retired = {str(item) for item in checkpoint.get("retired_document_ids", []) if str(item)}
        database = self.workspaces.database_path(job.workspace_id)
        for document_id in pending:
            if document_id in retired:
                continue
            # Haiku's documented delete operation is idempotent for our
            # purposes: False means the already-hidden row no longer exists.
            await self.adapter.delete_document(database, document_id)
            retired.add(document_id)
            self.store.checkpoint(
                job.id,
                checkpoint_name,
                {
                    "generation_id": generation_id,
                    "retired_document_ids": sorted(retired),
                    "pending_document_ids": [item for item in pending if item not in retired],
                },
            )
        if not pending:
            self.store.checkpoint(
                job.id,
                checkpoint_name,
                {
                    "generation_id": generation_id,
                    "retired_document_ids": [],
                    "pending_document_ids": [],
                },
            )

    async def _publish_ingest_result(
        self,
        job: JobSnapshot,
        source_index: int,
        source_path: str,
        fingerprint: str,
        result: dict[str, Any],
        expected_lock: dict[str, Any],
        *,
        already_published: bool = False,
    ) -> None:
        """Publish one complete generation and retire its predecessor atomically to readers."""

        async with self.resources.indexing():
            await self._verify_runtime_lock(job.workspace_id, expected_lock)
            if not already_published:
                # The catalogue swap and recovery marker share one SQLite
                # transaction. Queries obtain their segment IDs under the same
                # resource lease and therefore see either the old mapping or
                # this complete one.
                self.store.upsert_document(
                    job.workspace_id,
                    source_path,
                    fingerprint,
                    result,
                    publication_checkpoint=(job.id, source_index),
                )
            await self._retire_superseded_segments(job, source_index, result)

    async def _run_ingest(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        async with self._writer_lock:
            current = self.store.get_job(job_id)
            if not await self._continue(job_id):
                return
            current = self.store.get_job(job_id)
            self.store.update_job(job_id, status=JobStatus.RUNNING, phase="preflight")
            await self.events.emit(
                "job.started",
                correlation_id=job_id,
                workspace_id=job.workspace_id,
                job_id=job_id,
                payload={"phase": "preflight"},
            )
            sources = current.payload["sources"]
            imported: list[dict[str, Any]] = []
            url_import_directory: Path | None = None
            try:
                self._authorize_model_content(job.workspace_id)
                runtime_checkpoint = self.store.checkpoint_data(job_id, "runtime-lock")
                expected_lock = dict((runtime_checkpoint or {}).get("runtime_lock") or {})
                if getattr(self.adapter.capabilities, "book_index_v2", False):
                    pipeline_version = _index_pipeline(dict(current.payload.get("indexing") or {}))
                    current_lock = await asyncio.to_thread(
                        _rebuild_runtime_lock,
                        Path(self.workspaces.get(job.workspace_id).path),
                        self.workspaces.ollama_url,
                        pipeline_version,
                    )
                    if expected_lock and current_lock != expected_lock:
                        raise ConflictError(
                            "Indexing dependencies changed since this job was started",
                            details={"expected": expected_lock, "actual": current_lock},
                        )
                    if not expected_lock:
                        expected_lock = current_lock
                        self.store.checkpoint(
                            job_id, "runtime-lock", {"runtime_lock": expected_lock}
                        )
                total = len(sources)
                for index, source in enumerate(sources):
                    if not await self._continue(job_id):
                        return
                    completed = self.store.checkpoint_data(job_id, f"source-result-{index}")
                    if completed is not None:
                        imported.append(completed)
                        continue
                    url_recovery = self.store.checkpoint_data(job_id, f"url-source-{index}")
                    declared_type = str(source.get("type") or "file")
                    raw_source = str(source["path"])
                    provided_fingerprint = source.get("fingerprint")
                    source_reference = str(
                        (url_recovery or {}).get("original_source") or raw_source
                    )
                    if url_recovery is not None:
                        candidate_path = Path(str(url_recovery.get("managed_source") or ""))
                        provided_fingerprint = str(
                            url_recovery.get("fingerprint") or provided_fingerprint or ""
                        )
                        if not candidate_path.is_file() or not provided_fingerprint:
                            raise ConflictError("Managed URL source recovery file is unavailable")
                    elif declared_type == "url":
                        if self.url_source_guard is None:
                            raise ConflictError("URL source imports are not configured")
                        if url_import_directory is None:
                            url_import_directory = self._prepare_url_import_directory(
                                job.workspace_id,
                                job.id,
                            )
                        downloaded = await download_url_source(
                            raw_source,
                            url_import_directory,
                            authorize=lambda url: self.url_source_guard(job.workspace_id, url),
                        )
                        if (
                            provided_fingerprint
                            and str(provided_fingerprint) != downloaded.fingerprint
                        ):
                            raise ConflictError("URL source changed after import preflight")
                        candidate_path = downloaded.path
                        provided_fingerprint = downloaded.fingerprint
                        source_reference = downloaded.final_reference
                    else:
                        candidate_path = Path(raw_source).expanduser()
                        if not candidate_path.is_file():
                            raise ConflictError(
                                "A file import must reference an existing regular local file"
                            )
                    source_path = str(candidate_path.resolve())
                    managed_source = source_path
                    archive_mode = "external"
                    is_local_file = Path(source_path).is_file()
                    if is_local_file and provided_fingerprint:
                        # Preflight already read the file. Use its verified hash
                        # to reject/skip duplicates before allocating an archive.
                        fingerprint = str(provided_fingerprint)
                    elif is_local_file:
                        await self._phase(job_id, index, total, "archiving", 0, 0, 0)
                        archived, fingerprint, archive_mode = await asyncio.to_thread(
                            archive_source,
                            Path(self.workspaces.get(job.workspace_id).path),
                            Path(source_path),
                            None,
                        )
                        managed_source = str(archived)
                    else:
                        fingerprint = await asyncio.to_thread(_source_fingerprint, source_path)
                        if provided_fingerprint and str(provided_fingerprint) != fingerprint:
                            raise ConflictError("Source changed after import preflight")
                    initial = self.store.checkpoint_data(job_id, f"source-init-{index}")
                    generation_id = str(
                        (initial or {}).get("generation_id") or f"gen-{uuid4().hex[:16]}"
                    )
                    if initial is None:
                        self.store.checkpoint(
                            job_id,
                            f"source-init-{index}",
                            {
                                "source": source_reference,
                                "fingerprint": fingerprint,
                                "generation_id": generation_id,
                            },
                        )
                    elif initial.get("fingerprint") != fingerprint:
                        raise ConflictError(
                            "Source changed after this import job was started",
                            details={"source": source_reference},
                        )
                    duplicate = self.store.document_by_fingerprint(job.workspace_id, fingerprint)
                    published_result = self._published_source_result(
                        job,
                        index,
                        fingerprint,
                        generation_id,
                    )
                    if published_result is not None:
                        # Publication won the crash boundary but retirement or
                        # the final source checkpoint did not. Never rebuild or
                        # apply duplicate policy to our own active generation;
                        # only finish the idempotent hidden-row cleanup.
                        await self._publish_ingest_result(
                            job,
                            index,
                            source_reference,
                            fingerprint,
                            published_result,
                            expected_lock,
                            already_published=True,
                        )
                        self.store.checkpoint(job_id, f"source-result-{index}", published_result)
                        imported.append(published_result)
                        continue
                    if duplicate is not None:
                        policy = str(current.payload.get("duplicate_policy", "review"))
                        if policy == "review":
                            raise ConflictError(
                                "This content is already indexed",
                                details={
                                    "source": source_reference,
                                    "existing_source": duplicate["source_path"],
                                    "fingerprint": fingerprint,
                                },
                            )
                        if policy == "skip":
                            if declared_type == "url" and url_recovery is None:
                                # Even a duplicate URL import must cross the
                                # privacy/recovery boundary before the job can
                                # complete. Otherwise the original URL (and
                                # potentially credentials in its query string)
                                # would remain in durable job/preflight JSON.
                                await self._phase(job_id, index, total, "archiving", 0, 0, 0)
                                archived, fingerprint, archive_mode = await asyncio.to_thread(
                                    archive_source,
                                    Path(self.workspaces.get(job.workspace_id).path),
                                    Path(source_path),
                                    str(provided_fingerprint),
                                )
                                managed_source = str(archived)
                                self.store.promote_url_source_to_managed(
                                    job_id,
                                    index,
                                    raw_reference=raw_source,
                                    opaque_reference=source_reference,
                                    managed_source=managed_source,
                                    fingerprint=fingerprint,
                                )
                            result = dict(duplicate["result"])
                            result["duplicate"] = True
                            result["cache_status"] = "duplicate"
                            imported.append(result)
                            self.store.checkpoint(job_id, f"source-result-{index}", result)
                            continue
                        # "replace" deliberately continues through the staged
                        # generation import.  The adapter retires the previous
                        # generation only after every replacement segment is
                        # searchable, so a failed rebuild leaves the old book
                        # intact.
                    if is_local_file and provided_fingerprint and url_recovery is None:
                        await self._phase(job_id, index, total, "archiving", 0, 0, 0)
                        archived, fingerprint, archive_mode = await asyncio.to_thread(
                            archive_source,
                            Path(self.workspaces.get(job.workspace_id).path),
                            Path(source_path),
                            str(provided_fingerprint),
                        )
                        managed_source = str(archived)
                        if declared_type == "url":
                            self.store.promote_url_source_to_managed(
                                job_id,
                                index,
                                raw_reference=raw_source,
                                opaque_reference=source_reference,
                                managed_source=managed_source,
                                fingerprint=fingerprint,
                            )
                    elif url_recovery is not None:
                        managed_source = source_path
                        archive_mode = "managed-url"
                    progress = index / total
                    self.store.update_job(
                        job_id, progress=progress, phase="ingest", checkpoint=f"source-{index}"
                    )
                    checkpoint_source = dict(source)
                    checkpoint_source["path"] = source_reference
                    self.store.checkpoint(job_id, f"source-{index}", {"source": checkpoint_source})
                    await self.events.emit(
                        "job.progress",
                        correlation_id=job_id,
                        workspace_id=job.workspace_id,
                        job_id=job_id,
                        payload={
                            "phase": "ingest",
                            "phase_label": "Importing document",
                            "overall_progress": progress,
                            "units": {"kind": "files", "done": index, "total": total},
                            "checkpoint": f"source-{index}",
                        },
                    )
                    metadata_payload = source.get("metadata")
                    book_metadata = (
                        BookMetadata.model_validate(metadata_payload)
                        if metadata_payload is not None
                        else None
                    )
                    await self._verify_runtime_lock(job.workspace_id, expected_lock)
                    indexing_options = dict(current.payload.get("indexing") or {})
                    # Book-v2/v3 imports stage complete Haiku generations. The
                    # daemon owns the later Store publish + old-row retirement
                    # boundary so queries cannot observe an empty replacement.
                    indexing_options["_defer_previous_generation_retirement"] = True
                    if expected_lock:
                        indexing_options["_runtime_lock"] = expected_lock
                    result = await self.adapter.ingest(
                        self.workspaces.database_path(job.workspace_id),
                        managed_source,
                        parser_id=str(current.payload.get("parser_id", "auto")),
                        processing_profile=str(
                            current.payload.get("processing_profile", "default")
                        ),
                        segment_guard=self.resources.indexing,
                        before_segment=lambda start, end, pages, source_index=index: (
                            self._before_segment(job_id, source_index, total, start, end, pages)
                        ),
                        generation_id=generation_id,
                        document_fingerprint=fingerprint,
                        resume_segments=self.store.list_segments(job_id, index),
                        on_segment=lambda segment, source_index=index: self._segment_committed(
                            job_id, source_index, segment
                        ),
                        on_phase=lambda phase, start, end, pages, source_index=index: self._phase(
                            job_id, source_index, total, phase, start, end, pages
                        ),
                        segment_sizer=self.resources.segment_pages,
                        metadata=book_metadata,
                        original_source=source_reference,
                        indexing_options=indexing_options,
                        llm_url=self.workspaces.ollama_url,
                    )
                    await self._verify_runtime_lock(job.workspace_id, expected_lock)
                    result.setdefault("fingerprint", fingerprint)
                    result.setdefault("generation_id", generation_id)
                    result.setdefault("original_source", source_reference)
                    result.setdefault("managed_source", managed_source)
                    result.setdefault("archive_mode", archive_mode)
                    if expected_lock:
                        result.setdefault("runtime_lock", expected_lock)
                    if Path(managed_source).is_file():
                        result.setdefault("size_bytes", Path(managed_source).stat().st_size)
                    await self._publish_ingest_result(
                        job,
                        index,
                        source_reference,
                        fingerprint,
                        result,
                        expected_lock,
                    )
                    self.store.checkpoint(job_id, f"source-result-{index}", result)
                    imported.append(result)
                await self._verify_runtime_lock(job.workspace_id, expected_lock)
                self.store.update_job(
                    job_id,
                    status=JobStatus.COMPLETED,
                    phase="completed",
                    progress=1.0,
                    result={"documents": imported},
                    checkpoint="completed",
                )
                await self.events.emit(
                    "job.completed",
                    correlation_id=job_id,
                    workspace_id=job.workspace_id,
                    job_id=job_id,
                    payload={"documents": imported, "overall_progress": 1.0},
                )
            except asyncio.CancelledError:
                if self.store.get_job(job_id).status not in TERMINAL_STATUSES:
                    self.store.update_job(job_id, status=JobStatus.PAUSED, phase="interrupted")
                raise
            except OmaRagError as exc:
                await self._fail(job, exc.code, exc.message, exc.details, exc.retryable)
            except Exception as exc:  # daemon boundary: normalize provider errors
                await self._fail(job, "INGEST_FAILED", str(exc), {}, True)
            finally:
                for key in [key for key in self._segment_samples if key[0] == job_id]:
                    self._segment_samples.pop(key, None)
                if url_import_directory is not None:
                    with suppress(ConflictError, OSError):
                        self._cleanup_url_import_directory(job.workspace_id, job.id)

    async def _segment_committed(
        self, job_id: str, source_index: int, segment: dict[str, Any]
    ) -> None:
        self.store.record_segment(job_id, source_index, segment)
        samples = self._segment_samples.setdefault((job_id, source_index), [])
        samples.append((time.monotonic(), int(segment["page_end"])))
        if len(samples) > 8:
            del samples[:-8]
        job = self.store.get_job(job_id)
        detail = job.progress_detail.model_dump(mode="json") if job.progress_detail else {}
        if (
            len(samples) >= 2
            and detail.get("total_pages")
            and detail.get("memory_state") != "waiting"
        ):
            elapsed = samples[-1][0] - samples[0][0]
            pages_done = samples[-1][1] - samples[0][1]
            if elapsed > 0 and pages_done > 0:
                remaining = max(0, int(detail["total_pages"]) - samples[-1][1])
                estimate = remaining * elapsed / pages_done
                detail["eta_seconds_low"] = estimate * 0.8
                detail["eta_seconds_high"] = estimate * 1.25
                self.store.update_job(job_id, progress_detail=detail)
        metadata = segment.get("metadata", {})
        cache_path = metadata.get("cache_path")
        cache_key = metadata.get("cache_key")
        if cache_path and cache_key:
            path = Path(str(cache_path))
            if path.exists():
                self.store.touch_cache(
                    str(cache_key),
                    str(path),
                    path.stat().st_size,
                    {
                        "job_id": job_id,
                        "source_index": source_index,
                        "page_start": segment["page_start"],
                        "page_end": segment["page_end"],
                    },
                )
        await self.events.emit(
            "job.segment.committed",
            correlation_id=job_id,
            workspace_id=self.store.get_job(job_id).workspace_id,
            job_id=job_id,
            payload={
                "source_index": source_index,
                "page_start": segment["page_start"],
                "page_end": segment["page_end"],
                "cache_hit": bool(metadata.get("cache_hit")),
            },
        )

    async def _phase(
        self,
        job_id: str,
        source_index: int,
        source_total: int,
        phase: str,
        start: int,
        end: int,
        pages: int,
    ) -> None:
        labels = {
            "archiving": "Archiving source",
            "profiling": "Profiling pages",
            "converting": "Converting pages",
            "chunking": "Building chunks",
            "embedding": "Embedding chunks",
            "committing": "Committing segment",
            "verifying": "Verifying index",
        }
        current = self.store.get_job(job_id)
        progress = current.progress
        if pages > 0:
            progress = (source_index + end / pages) / max(source_total, 1)
        detail = current.progress_detail.model_dump(mode="json") if current.progress_detail else {}
        detail.update(
            {
                "current_document": str(source_index + 1),
                "page_start": start or None,
                "page_end": end or None,
                "total_pages": pages or detail.get("total_pages"),
                "memory_state": self.resources.memory().state,
            }
        )
        if detail["memory_state"] == "waiting":
            detail["eta_seconds_low"] = None
            detail["eta_seconds_high"] = None
        self.store.update_job(job_id, phase=phase, progress=progress, progress_detail=detail)
        await self.events.emit(
            "job.progress",
            correlation_id=job_id,
            workspace_id=current.workspace_id,
            job_id=job_id,
            payload={
                "phase": phase,
                "phase_label": labels.get(phase, phase.replace("_", " ").title()),
                "overall_progress": progress,
                "page_start": start or None,
                "page_end": end or None,
                "total_pages": pages or None,
                "memory_state": detail["memory_state"],
            },
        )

    async def _before_segment(
        self,
        job_id: str,
        source_index: int,
        source_total: int,
        start: int,
        end: int,
        pages: int,
    ) -> bool:
        if not await self._continue(job_id):
            return False
        progress = (source_index + end / max(pages, 1)) / max(source_total, 1)
        checkpoint = f"source-{source_index}-pages-{start + 1}-{end}"
        self.store.update_job(
            job_id,
            progress=progress,
            phase="ingest",
            checkpoint=checkpoint,
            progress_detail={
                "current_document": str(source_index + 1),
                "page_start": start + 1,
                "page_end": end,
                "total_pages": pages,
                "cache_hits": sum(
                    bool(item.get("metadata", {}).get("cache_hit"))
                    for item in self.store.list_segments(job_id, source_index)
                ),
                "recovered_segments": sum(
                    bool(item.get("metadata", {}).get("recovered"))
                    for item in self.store.list_segments(job_id, source_index)
                ),
                "memory_state": self.resources.memory().state,
            },
        )
        self.store.checkpoint(
            job_id,
            checkpoint,
            {"source_index": source_index, "page_start": start + 1, "page_end": end},
        )
        return True

    async def _continue(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        if job.status in {JobStatus.CANCELLED, JobStatus.PAUSED}:
            return False
        if job.status == JobStatus.PAUSE_REQUESTED:
            self.store.update_job(job_id, status=JobStatus.PAUSED, phase="paused")
            await self.events.emit(
                "job.paused",
                correlation_id=job_id,
                workspace_id=job.workspace_id,
                job_id=job_id,
            )
            # Do not wait here. Both ingest runners call _continue while holding
            # the corpus writer lock. Returning releases the task at its next
            # checkpoint; resume() starts a fresh task from persisted segments.
            return False
        return True

    async def _fail(
        self,
        job: JobSnapshot,
        code: str,
        message: str,
        details: dict[str, Any],
        retryable: bool,
    ) -> None:
        error = {"code": code, "message": message, "details": details, "retryable": retryable}
        self.store.update_job(job.id, status=JobStatus.FAILED, phase="failed", error=error)
        await self.events.emit(
            "job.failed",
            correlation_id=job.id,
            workspace_id=job.workspace_id,
            job_id=job.id,
            payload={"error": error},
        )

    async def pause(self, job_id: str) -> JobSnapshot:
        job = self.store.get_job(job_id)
        if job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            raise ConflictError(f"A job in state {job.status} cannot be paused")
        job = self.store.update_job(job_id, status=JobStatus.PAUSE_REQUESTED)
        await self.events.emit(
            "job.pause.requested",
            correlation_id=job_id,
            workspace_id=job.workspace_id,
            job_id=job_id,
        )
        return job

    async def resume(self, job_id: str) -> JobSnapshot:
        async with self._admission_lock:
            job = self.store.get_job(job_id)
            if job.status not in {
                JobStatus.PAUSED,
                JobStatus.PAUSE_REQUESTED,
                JobStatus.FAILED,
            }:
                raise ConflictError(f"A job in state {job.status} cannot be resumed")
            previous = self._tasks.get(job_id)
            if previous is not None and not previous.done():
                # PAUSED is written at a checkpoint just before the old runner
                # unwinds. Wait for that runner so _spawn cannot silently keep
                # the old task and leave a persisted RUNNING job orphaned.
                await asyncio.gather(previous, return_exceptions=True)
                job = self.store.get_job(job_id)
                if job.status not in {JobStatus.PAUSED, JobStatus.FAILED}:
                    raise ConflictError(
                        f"A job in state {job.status} cannot be resumed after unwind"
                    )
            if job.kind == "reindex":
                checkpoint = self.store.checkpoint_data(job_id, "reindex-generation") or {}
                generation_id = checkpoint.get("generation_id")
                latest = self.store.workspace_index_generation(job.workspace_id)
                if generation_id and latest and latest["generation_id"] != generation_id:
                    raise ConflictError(
                        "A superseded reindex generation cannot be resumed",
                        details={
                            "job_generation_id": generation_id,
                            "current_generation_id": latest["generation_id"],
                        },
                    )
            job = self.store.update_job(job_id, status=JobStatus.RUNNING, error=None)
            await self.events.emit(
                "job.resumed",
                correlation_id=job_id,
                workspace_id=job.workspace_id,
                job_id=job_id,
            )
            self._spawn(job_id)
            return job

    async def cancel(self, job_id: str) -> JobSnapshot:
        job = self.store.get_job(job_id)
        if job.status in TERMINAL_STATUSES:
            return job
        task = self._tasks.get(job_id)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        current = self.store.get_job(job_id)
        if current.status == JobStatus.COMPLETED:
            return current
        if job.kind == "reindex":
            # An in-place rebuild cannot be safely "rolled back" by deleting
            # already committed books. Cancellation therefore means a resumable
            # pause in maintenance, preserving every source and checkpoint.
            checkpoint = self.store.checkpoint_data(job_id, "reindex-generation") or {}
            if generation_id := checkpoint.get("generation_id"):
                generation = self.store.index_generation(job.workspace_id, str(generation_id))
                if generation and generation["status"] == "maintenance":
                    self.store.update_index_generation(
                        job.workspace_id,
                        str(generation_id),
                        status="maintenance_failed",
                        error={
                            "code": "REINDEX_INTERRUPTED",
                            "message": "Rebuild paused by cancellation request",
                        },
                    )
            current = self.store.update_job(job_id, status=JobStatus.PAUSED, phase="interrupted")
            await self.events.emit(
                "job.paused",
                correlation_id=job_id,
                workspace_id=job.workspace_id,
                job_id=job_id,
                payload={"reason": "cancelled_as_resumable_rebuild"},
            )
            return current

        job = self.store.update_job(job_id, status=JobStatus.CANCELLED, phase="cancelled")
        with suppress(ConflictError, OSError):
            self._cleanup_url_import_directory(job.workspace_id, job.id)
        database = self.workspaces.database_path(job.workspace_id)
        cleanup_failed = False
        for source_index, _source in enumerate(job.payload.get("sources", [])):
            # A completed source, or one that crossed the atomic publication
            # boundary just before interruption, must never be deleted merely
            # because retirement or a later source was cancelled.
            if (
                self.store.checkpoint_data(job_id, f"source-result-{source_index}") is not None
                or self.store.checkpoint_data(job_id, f"source-published-{source_index}")
                is not None
            ):
                continue
            for segment in reversed(self.store.list_segments(job_id, source_index)):
                try:
                    await self.adapter.delete_document(database, segment["document_id"])
                except Exception:
                    cleanup_failed = True
        if not cleanup_failed:
            self.store.clear_segments(job_id)
        await self.events.emit(
            "job.cancelled",
            correlation_id=job_id,
            workspace_id=job.workspace_id,
            job_id=job_id,
            payload={"cleanup_pending": cleanup_failed},
        )
        return self.store.get_job(job_id)

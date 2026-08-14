from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import suppress
from importlib import metadata as package_metadata
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..adapters.base import HaikuAdapter
from ..compat_probe import SUPPORTED_DOCLING, SUPPORTED_HAIKU
from ..models.api import IngestRequest, ReindexRequest
from ..models.domain import BookMetadata, JobSnapshot, JobStatus, ReindexPreflight
from ..models.errors import ConflictError, OmaRagError, ReadOnlyError
from ..store import StateStore
from .event_service import EventService
from .resource_coordinator import ResourceCoordinator
from .textbook_service import archive_source, file_sha256
from .workspace_service import WorkspaceService

TERMINAL_STATUSES = {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}


def _same_model_name(left: str, right: str) -> bool:
    return left == right or left == f"{right}:latest" or right == f"{left}:latest"


def _rebuild_runtime_lock(workspace: Path, ollama_url: str) -> dict[str, str]:
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
    config_path = workspace / "haiku.rag.yaml"
    config_bytes = config_path.read_bytes()
    import yaml

    config = yaml.safe_load(config_bytes) or {}
    embedding = str((((config.get("embeddings") or {}).get("model") or {}).get("name")) or "")
    reranker = str((((config.get("reranking") or {}).get("model") or {}).get("name")) or "")
    if not embedding:
        raise RuntimeError("Workspace has no configured embedding model")
    request = urllib.request.Request(f"{ollama_url.rstrip('/')}/api/tags")
    try:
        with urllib.request.urlopen(request, timeout=3.0) as response:  # noqa: S310
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
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
    return {
        "pipeline": "book-index-v2",
        "haiku": haiku,
        "docling": docling,
        "workspace_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "embedding_model": embedding,
        "embedding_digest": str(matches[0]["digest"]),
        "reranker_model": reranker,
    }


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
    ) -> None:
        self.store = store
        self.workspaces = workspaces
        self.events = events
        self.adapter = adapter
        self.resources = resources
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._writer_lock = asyncio.Lock()
        self._segment_samples: dict[tuple[str, int], list[tuple[float, int]]] = {}

    @property
    def active(self) -> bool:
        return bool(self._tasks)

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def start_ingest(
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

    def preflight_reindex(self, workspace_id: str, indexing: dict[str, Any]) -> ReindexPreflight:
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
                    Path(workspace.path), self.workspaces.ollama_url
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

    def _spawn(self, job_id: str) -> None:
        previous = self._tasks.get(job_id)
        if previous is not None and not previous.done():
            return
        job = self.store.get_job(job_id)
        runner = self._run_reindex if job.kind == "reindex" else self._run_ingest
        task = asyncio.create_task(runner(job_id), name=job_id)
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

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
        async with self._writer_lock:
            imported: list[dict[str, Any]] = []
            try:
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
                current_lock = await asyncio.to_thread(
                    _rebuild_runtime_lock,
                    Path(self.workspaces.get(job.workspace_id).path),
                    self.workspaces.ollama_url,
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
                        "book-index-v2",
                        config_hash,
                        status="maintenance",
                        config=current_lock,
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
                    current_lock = await asyncio.to_thread(
                        _rebuild_runtime_lock,
                        Path(self.workspaces.get(job.workspace_id).path),
                        self.workspaces.ollama_url,
                    )
                    if current_lock != expected_lock:
                        raise ConflictError(
                            "Indexing dependencies changed at the rebuild boundary",
                            details={"expected": expected_lock, "actual": current_lock},
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
                        indexing_options=dict(job.payload.get("indexing") or {}),
                    )
                    result.setdefault("fingerprint", source["fingerprint"])
                    result.setdefault("generation_id", generation_id)
                    result.setdefault("original_source", source["original_source"])
                    result.setdefault("managed_source", source["path"])
                    if (
                        Path(str(source["path"])).suffix.casefold() == ".pdf"
                        and str(result.get("pipeline_version")) != "book-index-v2"
                    ):
                        raise ConflictError("A PDF did not complete the homogeneous book-v2 path")
                    self.store.upsert_document(
                        job.workspace_id,
                        str(source["original_source"]),
                        str(source["fingerprint"]),
                        result,
                    )
                    self.store.checkpoint(job_id, f"book-{index}-complete", result)
                    imported.append(result)

                self.store.validate_index_generation(job.workspace_id, generation_id)
                self.store.update_index_generation(job.workspace_id, generation_id, status="ready")
                self.store.clear_answer_cache(job.workspace_id)
                self.store.update_job(
                    job_id,
                    status=JobStatus.COMPLETED,
                    phase="completed",
                    progress=1.0,
                    checkpoint="completed",
                    result={"generation_id": generation_id, "documents": imported},
                )
                await self.events.emit(
                    "index.generation.published",
                    correlation_id=job_id,
                    workspace_id=job.workspace_id,
                    job_id=job_id,
                    payload={"generation_id": generation_id, "documents": len(imported)},
                )
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

    async def _run_ingest(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        async with self._writer_lock:
            current = self.store.get_job(job_id)
            if current.status == JobStatus.CANCELLED:
                return
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
            try:
                total = len(sources)
                for index, source in enumerate(sources):
                    if not await self._continue(job_id):
                        return
                    completed = self.store.checkpoint_data(job_id, f"source-result-{index}")
                    if completed is not None:
                        imported.append(completed)
                        continue
                    raw_source = str(source["path"])
                    candidate_path = Path(raw_source).expanduser()
                    source_path = (
                        str(candidate_path.resolve()) if candidate_path.exists() else raw_source
                    )
                    provided_fingerprint = source.get("fingerprint")
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
                                "source": source_path,
                                "fingerprint": fingerprint,
                                "generation_id": generation_id,
                            },
                        )
                    elif initial.get("fingerprint") != fingerprint:
                        raise ConflictError(
                            "Source changed after this import job was started",
                            details={"source": source_path},
                        )
                    duplicate = self.store.document_by_fingerprint(job.workspace_id, fingerprint)
                    if duplicate is not None:
                        policy = str(current.payload.get("duplicate_policy", "review"))
                        if policy == "review":
                            raise ConflictError(
                                "This content is already indexed",
                                details={
                                    "source": source_path,
                                    "existing_source": duplicate["source_path"],
                                    "fingerprint": fingerprint,
                                },
                            )
                        if policy == "skip":
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
                    if is_local_file and provided_fingerprint:
                        await self._phase(job_id, index, total, "archiving", 0, 0, 0)
                        archived, fingerprint, archive_mode = await asyncio.to_thread(
                            archive_source,
                            Path(self.workspaces.get(job.workspace_id).path),
                            Path(source_path),
                            str(provided_fingerprint),
                        )
                        managed_source = str(archived)
                    progress = index / total
                    self.store.update_job(
                        job_id, progress=progress, phase="ingest", checkpoint=f"source-{index}"
                    )
                    self.store.checkpoint(job_id, f"source-{index}", {"source": source})
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
                        original_source=source_path,
                        indexing_options=dict(current.payload.get("indexing") or {}),
                    )
                    result.setdefault("fingerprint", fingerprint)
                    result.setdefault("generation_id", generation_id)
                    result.setdefault("original_source", source_path)
                    result.setdefault("managed_source", managed_source)
                    result.setdefault("archive_mode", archive_mode)
                    if Path(managed_source).is_file():
                        result.setdefault("size_bytes", Path(managed_source).stat().st_size)
                    self.store.upsert_document(job.workspace_id, source_path, fingerprint, result)
                    self.store.checkpoint(job_id, f"source-result-{index}", result)
                    imported.append(result)
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
        if job.status == JobStatus.CANCELLED:
            return False
        if job.status == JobStatus.PAUSE_REQUESTED:
            self.store.update_job(job_id, status=JobStatus.PAUSED, phase="paused")
            await self.events.emit(
                "job.paused",
                correlation_id=job_id,
                workspace_id=job.workspace_id,
                job_id=job_id,
            )
            while self.store.get_job(job_id).status == JobStatus.PAUSED:
                await asyncio.sleep(0.2)
            return self.store.get_job(job_id).status != JobStatus.CANCELLED
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
        job = self.store.get_job(job_id)
        if job.status not in {JobStatus.PAUSED, JobStatus.PAUSE_REQUESTED, JobStatus.FAILED}:
            raise ConflictError(f"A job in state {job.status} cannot be resumed")
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
        database = self.workspaces.database_path(job.workspace_id)
        cleanup_failed = False
        for source_index, _source in enumerate(job.payload.get("sources", [])):
            # A completed source is already published in Store and must never
            # be deleted merely because a later source was cancelled.
            if self.store.checkpoint_data(job_id, f"source-result-{source_index}") is not None:
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

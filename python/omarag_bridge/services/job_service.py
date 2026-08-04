from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..adapters.base import HaikuAdapter
from ..models.api import IngestRequest
from ..models.domain import BookMetadata, JobSnapshot, JobStatus
from ..models.errors import ConflictError, OmaRagError, ReadOnlyError
from ..store import StateStore
from .event_service import EventService
from .resource_coordinator import ResourceCoordinator
from .textbook_service import archive_source, file_sha256
from .workspace_service import WorkspaceService

TERMINAL_STATUSES = {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}


def _source_fingerprint(source: str) -> str:
    path = Path(source).expanduser()
    if path.is_file():
        return file_sha256(path)
    import hashlib

    return hashlib.sha256(source.encode()).hexdigest()


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
            raise ReadOnlyError("In einen Read-only-Workspace kann nicht importiert werden")
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

    def _spawn(self, job_id: str) -> None:
        previous = self._tasks.get(job_id)
        if previous is not None and not previous.done():
            return
        task = asyncio.create_task(self._run_ingest(job_id), name=job_id)
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

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
                    fingerprint = await asyncio.to_thread(_source_fingerprint, source_path)
                    provided_fingerprint = source.get("fingerprint")
                    if provided_fingerprint and provided_fingerprint != fingerprint:
                        raise ConflictError(
                            "Source changed after import preflight",
                            details={"source": source_path},
                        )
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
                        result = dict(duplicate["result"])
                        result["duplicate"] = True
                        result["cache_status"] = "duplicate"
                        imported.append(result)
                        self.store.checkpoint(job_id, f"source-result-{index}", result)
                        continue
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
                            "phase_label": "Dokument importieren",
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
                    managed_source = source_path
                    if Path(source_path).is_file():
                        managed_source = str(
                            await asyncio.to_thread(
                                archive_source,
                                Path(self.workspaces.get(job.workspace_id).path),
                                Path(source_path),
                                fingerprint,
                            )
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
                        segment_sizer=self.resources.segment_pages,
                        metadata=book_metadata,
                        original_source=source_path,
                    )
                    result.setdefault("fingerprint", fingerprint)
                    result.setdefault("generation_id", generation_id)
                    result.setdefault("original_source", source_path)
                    result.setdefault("managed_source", managed_source)
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

    async def _segment_committed(
        self, job_id: str, source_index: int, segment: dict[str, Any]
    ) -> None:
        self.store.record_segment(job_id, source_index, segment)
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
            raise ConflictError(f"Job im Zustand {job.status} kann nicht pausiert werden")
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
            raise ConflictError(f"Job im Zustand {job.status} kann nicht fortgesetzt werden")
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
        job = self.store.update_job(job_id, status=JobStatus.CANCELLED, phase="cancelled")
        task = self._tasks.get(job_id)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        database = self.workspaces.database_path(job.workspace_id)
        for source_index, _source in enumerate(job.payload.get("sources", [])):
            for segment in reversed(self.store.list_segments(job_id, source_index)):
                with suppress(Exception):
                    await self.adapter.delete_document(database, segment["document_id"])
        self.store.clear_segments(job_id)
        await self.events.emit(
            "job.cancelled",
            correlation_id=job_id,
            workspace_id=job.workspace_id,
            job_id=job_id,
        )
        return self.store.get_job(job_id)

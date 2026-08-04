from __future__ import annotations

import asyncio
import hashlib
import re
import time
import unicodedata
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from .. import __version__
from ..adapters.base import HaikuAdapter
from ..adapters.haiku_v070 import document_filter_for_ids
from ..models.api import RunRequest
from ..models.domain import (
    AnswerCacheStatus,
    Citation,
    EvidenceMode,
    JobStatus,
    RunReceipt,
    RunSnapshot,
    SourceCheck,
)
from ..models.errors import OmaRagError
from ..store import StateStore, request_hash
from .event_service import EventService
from .resource_coordinator import ResourceCoordinator
from .workspace_service import WorkspaceService

STRICT_REFUSAL = "In den bereitgestellten Quellen nicht ausreichend belegt."
_TECHNICAL_TOKEN = re.compile(
    r"(?i)(?:\b(?:DIN|EN|ISO)\s*[A-Z0-9][A-Z0-9 ./:-]*\d\b|"
    r"\b[A-Z]{1,5}\d+(?:[/.-]\d+)+\b|"
    r"\b\d+(?:[.,]\d+)?\s*(?:%|mm|cm|m|km|g|kg|t|Pa|kPa|MPa|N|kN|W|kW|V|A|°C)\b)"
)


def _normalized_tokens(value: str) -> set[str]:
    return {re.sub(r"\s+", "", item.casefold()) for item in _TECHNICAL_TOKEN.findall(value)}


def _strictly_supported(answer: str, citations: list[object]) -> bool:
    if not citations:
        return False
    evidence = "\n".join(str(getattr(item, "excerpt", "")) for item in citations)
    return _normalized_tokens(answer) <= _normalized_tokens(evidence)


def _normalized_question(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _citation_keys(citations: list[Citation]) -> set[str]:
    keys: set[str] = set()
    for citation in citations:
        chunk_ids = citation.chunk_ids or [citation.chunk_id]
        document = citation.logical_document_id or citation.document_id or "unknown"
        keys.update(f"{document}:{chunk_id}" for chunk_id in chunk_ids if chunk_id)
    return keys


class RunService:
    def __init__(
        self,
        store: StateStore,
        workspaces: WorkspaceService,
        events: EventService,
        adapter: HaikuAdapter,
        resources: ResourceCoordinator,
        answer_cache_max_entries: int = 256,
    ) -> None:
        self.store = store
        self.workspaces = workspaces
        self.events = events
        self.adapter = adapter
        self.resources = resources
        self.answer_cache_max_entries = answer_cache_max_entries
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def active(self) -> bool:
        return bool(self._tasks)

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def start(self, workspace_id: str, request: RunRequest) -> RunSnapshot:
        self.workspaces.get(workspace_id)
        run_id = f"run-{uuid4().hex[:12]}"
        payload = request.model_dump(mode="json", exclude_none=True)
        payload["session_id"] = request.session_id or f"session-{uuid4().hex}"
        self.store.create_run(
            run_id,
            workspace_id,
            payload,
        )
        await self.events.emit(
            "run.started",
            correlation_id=run_id,
            workspace_id=workspace_id,
            run_id=run_id,
            payload={
                "question": request.question,
                "evidence_mode": request.evidence_mode,
                "session_id": payload["session_id"],
            },
        )
        task = asyncio.create_task(self._execute(run_id), name=run_id)
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
        return self.store.update_run(run_id, status=JobStatus.RUNNING)

    async def _execute(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        request = self.store.get_run_request(run_id)
        started = time.perf_counter()
        try:
            turn = self.store.session_turn(run.workspace_id, run.session_id)
            await self.events.emit(
                "assistant.started",
                correlation_id=run_id,
                workspace_id=run.workspace_id,
                run_id=run_id,
                payload={"session_id": run.session_id, "turn": turn},
            )
            evidence_mode = EvidenceMode(request.get("evidence_mode", EvidenceMode.STRICT))
            cache_status = AnswerCacheStatus.BYPASS
            index_fingerprint = self.store.workspace_index_fingerprint(run.workspace_id)
            config_fingerprint = self._config_fingerprint(run.workspace_id)
            cache_request = {key: value for key, value in request.items() if key != "session_id"}
            cache_request["question"] = _normalized_question(request["question"])
            cache_key = request_hash(
                {
                    "schema": 1,
                    "omarag_version": __version__,
                    "adapter_version": str(getattr(self.adapter, "version", "unknown")),
                    "workspace_id": run.workspace_id,
                    "index_fingerprint": index_fingerprint,
                    "config_fingerprint": config_fingerprint,
                    "request": cache_request,
                }
            )
            cached = None
            if not request.get("images"):
                cached = self.store.cached_answer(cache_key)
                cache_status = AnswerCacheStatus.HIT if cached else AnswerCacheStatus.MISS

            if cached is not None:
                answer = str(cached["answer"])
                citations = [Citation.model_validate(item) for item in cached["citations"]]
            else:
                operation = (
                    self.adapter.analyze if request.get("mode") == "analysis" else self.adapter.ask
                )
                segment_ids = self.store.resolve_segment_ids(
                    run.workspace_id,
                    request.get("filters", {}),
                    request.get("document_policy", "current-only"),
                )
                async with self.resources.chat():
                    answer, citations = await operation(
                        self.workspaces.database_path(run.workspace_id),
                        request["question"],
                        request.get("images"),
                        document_filter=document_filter_for_ids(segment_ids),
                        evidence_mode=evidence_mode,
                    )
                citations = [
                    citation.model_copy(
                        update={
                            "evidence_id": f"E{index}",
                            "verification_status": "verified",
                        }
                    )
                    for index, citation in enumerate(citations, start=1)
                ]
                for index in range(len(citations), 0, -1):
                    answer = re.sub(rf"\[{index}\]", f"[E{index}]", answer)
                if evidence_mode is EvidenceMode.STRICT and (
                    not _strictly_supported(answer, citations)
                ):
                    answer = STRICT_REFUSAL
                    citations = []

                citation_data = [item.model_dump(mode="json") for item in citations]
                if cache_status is AnswerCacheStatus.MISS:
                    self.store.cache_answer(
                        cache_key=cache_key,
                        workspace_id=run.workspace_id,
                        index_fingerprint=index_fingerprint,
                        config_fingerprint=config_fingerprint,
                        request=cache_request,
                        answer=answer,
                        citations=citation_data,
                        max_entries=self.answer_cache_max_entries,
                    )

            previous = self.store.previous_completed_session_run(
                run.workspace_id, run.session_id, run_id
            )
            previous_keys = _citation_keys(previous.citations) if previous else set()
            reused = sum(bool(_citation_keys([citation]) & previous_keys) for citation in citations)
            source_check = (
                SourceCheck.INSUFFICIENT
                if not citations
                else SourceCheck.VERIFIED
                if all(item.verification_status == "verified" for item in citations)
                else SourceCheck.REVIEWED
            )
            receipt = RunReceipt(
                session_id=run.session_id,
                turn=turn,
                cache_status=cache_status,
                total_ms=(time.perf_counter() - started) * 1000,
                source_count=len(citations),
                reused_source_count=reused,
                new_source_count=max(0, len(citations) - reused),
                source_check=source_check,
            )
            # Normalize the provider result into modest deltas without exposing
            # internal Pydantic-AI events or chain-of-thought.
            for start in range(0, len(answer), 160):
                await self.events.emit(
                    "assistant.delta",
                    correlation_id=run_id,
                    workspace_id=run.workspace_id,
                    run_id=run_id,
                    payload={"delta": answer[start : start + 160]},
                )
            citation_data = [item.model_dump(mode="json") for item in citations]
            for citation in citation_data:
                await self.events.emit(
                    "citation.added",
                    correlation_id=run_id,
                    workspace_id=run.workspace_id,
                    run_id=run_id,
                    payload=citation,
                )
            self.store.update_run(
                run_id,
                status=JobStatus.COMPLETED,
                answer=answer,
                citations=citation_data,
                receipt=receipt,
            )
            await self.events.emit(
                "run.completed",
                correlation_id=run_id,
                workspace_id=run.workspace_id,
                run_id=run_id,
                payload={
                    "answer": answer,
                    "citations": citation_data,
                    "receipt": receipt.model_dump(mode="json"),
                },
            )
        except asyncio.CancelledError:
            raise
        except OmaRagError as exc:
            await self._fail(run, exc.code, exc.message, exc.retryable)
        except Exception as exc:
            await self._fail(run, "RUN_FAILED", str(exc), True)

    def _config_fingerprint(self, workspace_id: str) -> str:
        workspace = Path(self.workspaces.get(workspace_id).path)
        return hashlib.sha256((workspace / "haiku.rag.yaml").read_bytes()).hexdigest()

    async def _fail(self, run: RunSnapshot, code: str, message: str, retryable: bool) -> None:
        error = {"code": code, "message": message, "retryable": retryable}
        self.store.update_run(run.id, status=JobStatus.FAILED, error=error)
        await self.events.emit(
            "run.failed",
            correlation_id=run.id,
            workspace_id=run.workspace_id,
            run_id=run.id,
            payload={"error": error},
        )

    async def cancel(self, run_id: str) -> RunSnapshot:
        run = self.store.get_run(run_id)
        task = self._tasks.get(run_id)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if run.status not in {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}:
            run = self.store.update_run(run_id, status=JobStatus.CANCELLED)
            await self.events.emit(
                "run.cancelled",
                correlation_id=run_id,
                workspace_id=run.workspace_id,
                run_id=run_id,
            )
        return self.store.get_run(run_id)

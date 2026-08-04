from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from uuid import uuid4

from ..adapters.base import HaikuAdapter
from ..adapters.haiku_v070 import document_filter_for_ids
from ..models.api import RunRequest
from ..models.domain import EvidenceMode, JobStatus, RunSnapshot
from ..models.errors import OmaRagError
from ..store import StateStore
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


class RunService:
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
        self.store.create_run(
            run_id,
            workspace_id,
            request.model_dump(mode="json", exclude_none=True),
        )
        await self.events.emit(
            "run.started",
            correlation_id=run_id,
            workspace_id=workspace_id,
            run_id=run_id,
            payload={"question": request.question, "evidence_mode": request.evidence_mode},
        )
        task = asyncio.create_task(self._execute(run_id), name=run_id)
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
        return self.store.update_run(run_id, status=JobStatus.RUNNING)

    async def _execute(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        request = self.store.get_run_request(run_id)
        try:
            await self.events.emit(
                "assistant.started",
                correlation_id=run_id,
                workspace_id=run.workspace_id,
                run_id=run_id,
            )
            operation = (
                self.adapter.analyze if request.get("mode") == "analysis" else self.adapter.ask
            )
            evidence_mode = EvidenceMode(request.get("evidence_mode", EvidenceMode.STRICT))
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
            )
            await self.events.emit(
                "run.completed",
                correlation_id=run_id,
                workspace_id=run.workspace_id,
                run_id=run_id,
                payload={"answer": answer, "citations": citation_data},
            )
        except asyncio.CancelledError:
            raise
        except OmaRagError as exc:
            await self._fail(run, exc.code, exc.message, exc.retryable)
        except Exception as exc:
            await self._fail(run, "RUN_FAILED", str(exc), True)

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

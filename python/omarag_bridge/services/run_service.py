from __future__ import annotations

import asyncio
import hashlib
import re
import time
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import __version__
from ..adapters.base import HaikuAdapter
from ..adapters.haiku_v070 import document_filter_for_ids
from ..models.api import RunRequest
from ..models.domain import (
    AnswerCacheStatus,
    AnswerClaim,
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
from .ollama_stream import OllamaModelIdentity, OllamaStreamClient
from .query_orchestrator import OrchestratedAnswer, QueryOrchestrator
from .query_v2 import classify_query
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
        ollama_url: str = "http://127.0.0.1:11434",
        model_roles: Callable[[str], dict[str, str | None]] | None = None,
    ) -> None:
        self.store = store
        self.workspaces = workspaces
        self.events = events
        self.adapter = adapter
        self.resources = resources
        self.answer_cache_max_entries = answer_cache_max_entries
        self.ollama_url = ollama_url
        self.model_roles = model_roles
        self.query = QueryOrchestrator(store, adapter, ollama_url)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.index_gate: Callable[[str], None] | None = None

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
        deadline_started_at = asyncio.get_running_loop().time()
        request = self.store.get_run_request(run_id)
        run = self.store.get_run(run_id)
        options = dict(request.get("options", {}))
        deadline_question = request["question"]
        deadline_session_reference = False
        deadline_register_entities = 0
        adaptive_request = (
            request.get("mode") == "rag"
            and not request.get("images")
            and getattr(self.adapter.capabilities, "adaptive_retrieval", False)
        )
        if adaptive_request:
            deadline_question, deadline_session_reference = self.query.standalone_question(
                run.workspace_id,
                run.session_id,
                run_id,
                request["question"],
                memory_enabled=options.get("memory", "auto") != "off",
            )
            # The query orchestrator promotes register-heavy questions by the
            # same signal. Include it in the outer plan so the absolute guard
            # can never expire before the budget advertised in the receipt.
            try:
                deadline_routes = self.store.route_book_knowledge(
                    run.workspace_id, deadline_question, limit=36
                )
            except Exception:
                deadline_routes = []
            deadline_register_entities = len({str(item.get("term_id")) for item in deadline_routes})
        deadline_plan = classify_query(
            deadline_question,
            has_session_reference=deadline_session_reference,
            register_entity_count=deadline_register_entities,
        )
        plan_deadline_ms = deadline_plan.budget.deadline_ms
        if options.get("profile") == "fast":
            plan_deadline_ms = min(plan_deadline_ms, 15_000)
        elif options.get("profile") == "balanced":
            plan_deadline_ms = min(plan_deadline_ms, 25_000)
        elif options.get("profile") == "deep":
            plan_deadline_ms = min(35_000, max(plan_deadline_ms, 25_000))
        timeout_ms = int(
            options.get("deadline_ms")
            or (15_000 if options.get("profile") == "fast" else 0)
            or (60_000 if request.get("mode") == "analysis" else plan_deadline_ms)
        )
        try:
            # This is the absolute request budget: readiness, cache validation,
            # resource admission, retrieval, generation and persistence all fit
            # inside the same clock.
            async with asyncio.timeout_at(deadline_started_at + timeout_ms / 1000):
                await self._execute_inner(run_id)
        except TimeoutError:
            run = self.store.get_run(run_id)
            if run.status not in {
                JobStatus.COMPLETED,
                JobStatus.CANCELLED,
                JobStatus.FAILED,
            }:
                await self._fail(
                    run,
                    "QUERY_DEADLINE_EXCEEDED",
                    "Query deadline exceeded",
                    True,
                )

    async def _execute_inner(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        request = self.store.get_run_request(run_id)
        started = time.perf_counter()
        phase_started = started
        current_phase: str | None = None
        phase_timings: dict[str, float] = {}

        async def phase(name: str, label: str) -> None:
            nonlocal current_phase, phase_started
            now = time.perf_counter()
            if current_phase is not None:
                phase_timings[current_phase] = (now - phase_started) * 1000
            current_phase = name
            phase_started = now
            await self.events.emit(
                "run.phase",
                correlation_id=run_id,
                workspace_id=run.workspace_id,
                run_id=run_id,
                payload={
                    "phase": name,
                    "label": label,
                    "elapsed_ms": (now - started) * 1000,
                },
            )

        try:
            if self.index_gate is not None:
                self.index_gate(run.workspace_id)
            turn = self.store.session_turn(run.workspace_id, run.session_id)
            await self.events.emit(
                "assistant.started",
                correlation_id=run_id,
                workspace_id=run.workspace_id,
                run_id=run_id,
                payload={"session_id": run.session_id, "turn": turn},
            )
            evidence_mode = EvidenceMode(request.get("evidence_mode", EvidenceMode.STRICT))
            adaptive = bool(
                request.get("mode") == "rag"
                and not request.get("images")
                and getattr(self.adapter.capabilities, "adaptive_retrieval", False)
            )
            await phase("waiting", "Waiting")
            cache_status = AnswerCacheStatus.BYPASS
            index_fingerprint = self.store.workspace_index_fingerprint(run.workspace_id)
            config_fingerprint = self._config_fingerprint(run.workspace_id)
            runtime_metadata: dict[str, Any] = {}
            model_identity: OllamaModelIdentity | None = None
            if adaptive:
                await phase("readiness", "Checking pinned local models")
                model_identity, runtime_metadata = await self._query_runtime_identity(
                    run.workspace_id
                )

            cache_request = {key: value for key, value in request.items() if key != "session_id"}
            if adaptive:
                memory_enabled = request.get("options", {}).get("memory", "auto") != "off"
                standalone, _ = self.query.standalone_question(
                    run.workspace_id,
                    run.session_id,
                    run_id,
                    request["question"],
                    memory_enabled=memory_enabled,
                )
                cache_request["question"] = _normalized_question(standalone)
                cache_request["memory_context"] = run.session_id if memory_enabled else "off"
            else:
                cache_request["question"] = _normalized_question(request["question"])
            cache_key = request_hash(
                {
                    "schema": 2,
                    "omarag_version": __version__,
                    "adapter_version": str(getattr(self.adapter, "version", "unknown")),
                    "workspace_id": run.workspace_id,
                    "index_fingerprint": index_fingerprint,
                    "config_fingerprint": config_fingerprint,
                    "model_digests": runtime_metadata.get("model_digests", {}),
                    "request": cache_request,
                }
            )
            cached = None
            if not request.get("images"):
                cached = self.store.cached_answer(cache_key)
                cache_status = AnswerCacheStatus.HIT if cached else AnswerCacheStatus.MISS

            claims: list[AnswerClaim] = []
            query_metadata: dict[str, Any] = {}
            streamed_claim_ids: set[str] = set()
            streamed_citation_ids: set[str] = set()

            async def emit_committed_claim(
                claim: AnswerClaim, claim_citations: list[Citation]
            ) -> None:
                # Persist before publishing SSE/outbox events: every replayable
                # delta must already have a durable RunSnapshot counterpart.
                partial = self.store.get_run(run_id)
                persisted_claims = list(partial.claims)
                if all(item.id != claim.id for item in persisted_claims):
                    persisted_claims.append(claim)
                persisted_citations = list(partial.citations)
                known = {item.evidence_id or item.chunk_id for item in persisted_citations}
                persisted_citations.extend(
                    item
                    for item in claim_citations
                    if (item.evidence_id or item.chunk_id) not in known
                )
                self.store.update_run(
                    run_id,
                    answer="\n\n".join(item.text for item in persisted_claims),
                    claims=[item.model_dump(mode="json") for item in persisted_claims],
                    citations=[item.model_dump(mode="json") for item in persisted_citations],
                )
                for citation in claim_citations:
                    key = citation.evidence_id or citation.chunk_id
                    if key in streamed_citation_ids:
                        continue
                    streamed_citation_ids.add(key)
                    await self.events.emit(
                        "citation.added",
                        correlation_id=run_id,
                        workspace_id=run.workspace_id,
                        run_id=run_id,
                        payload=citation.model_dump(mode="json"),
                    )
                prefix = "\n\n" if streamed_claim_ids else ""
                streamed_claim_ids.add(claim.id)
                await self.events.emit(
                    "assistant.delta",
                    correlation_id=run_id,
                    workspace_id=run.workspace_id,
                    run_id=run_id,
                    payload={"delta": prefix + claim.text, "claim_id": claim.id},
                )

            if cached is not None:
                if self.index_gate is not None:
                    self.index_gate(run.workspace_id)
                current_fingerprint = self.store.workspace_index_fingerprint(run.workspace_id)
                if current_fingerprint != index_fingerprint:
                    cached = None
                    cache_status = AnswerCacheStatus.MISS
            if cached is not None:
                await phase("checking_sources", "Checking cached evidence")
                answer = str(cached["answer"])
                citations = [Citation.model_validate(item) for item in cached["citations"]]
                claims = [AnswerClaim.model_validate(item) for item in cached.get("claims", [])]
                query_metadata = dict(cached.get("metadata", {}))
            else:
                segment_ids = self.store.resolve_segment_ids(
                    run.workspace_id,
                    request.get("filters", {}),
                    request.get("document_policy", "current-only"),
                )
                database = self.workspaces.database_path(run.workspace_id)
                async with self.resources.chat():
                    if self.index_gate is not None:
                        self.index_gate(run.workspace_id)
                    options = dict(request.get("options", {}))
                    await phase("warming", "Warming retrieval models")
                    await self.adapter.warm(database)
                    if adaptive:
                        assert model_identity is not None
                        self.query.adapter = self.adapter
                        await phase("retrieving_and_answering", "Retrieving evidence")
                        result = await self.query.answer(
                            workspace_id=run.workspace_id,
                            database=database,
                            run_id=run_id,
                            session_id=run.session_id,
                            question=request["question"],
                            evidence_mode=evidence_mode,
                            document_filter=document_filter_for_ids(segment_ids),
                            options=options,
                            model=model_identity.name,
                            expected_model_digest=model_identity.digest,
                            resolved_model_identity=model_identity,
                            images=request.get("images"),
                            emit_claim=emit_committed_claim,
                            memory_enabled=options.get("memory", "auto") != "off",
                            allowed_document_ids=(
                                set(segment_ids) if segment_ids is not None else None
                            ),
                            keep_alive=(f"{max(1, round(self.resources.residency_seconds()))}s"),
                        )
                        answer = result.answer
                        citations = list(result.citations)
                        claims = list(result.claims)
                        query_metadata = self._orchestration_metadata(result)
                        query_metadata.setdefault("model_digests", {}).update(
                            runtime_metadata.get("model_digests", {})
                        )
                        phase_timings.update(
                            {
                                f"query_{key}": value
                                for key, value in result.phase_timings_ms.items()
                            }
                        )
                    else:
                        await phase("searching_and_drafting", "Searching & drafting")
                        operation = (
                            self.adapter.analyze
                            if request.get("mode") == "analysis"
                            else self.adapter.ask
                        )
                        answer, citations = await operation(
                            database,
                            request["question"],
                            request.get("images"),
                            document_filter=document_filter_for_ids(segment_ids),
                            evidence_mode=evidence_mode,
                        )
                        try:
                            legacy_hits = {
                                hit.chunk_id: hit
                                for hit in await self.adapter.get_chunks(
                                    database, [item.chunk_id for item in citations]
                                )
                            }
                        except Exception:
                            legacy_hits = {}
                await phase("checking_sources", "Checking sources")
                if not adaptive:
                    enriched: list[Citation] = []
                    for index, citation in enumerate(citations, start=1):
                        prompt_id = f"E{index}"
                        hit = legacy_hits.get(citation.chunk_id)
                        metadata = hit.metadata if hit is not None else {}
                        raw_content = hit.content if hit is not None else ""
                        start = raw_content.find(citation.excerpt) if raw_content else -1
                        enriched.append(
                            citation.model_copy(
                                update={
                                    "evidence_id": str(metadata.get("evidence_id") or prompt_id),
                                    "prompt_evidence_id": prompt_id,
                                    "logical_document_id": str(
                                        metadata.get("logical_document_id")
                                        or citation.logical_document_id
                                        or ""
                                    )
                                    or None,
                                    "source_uri": str(
                                        metadata.get("source_uri") or citation.source_uri or ""
                                    )
                                    or None,
                                    "headings": list(
                                        metadata.get("citation_headings")
                                        or metadata.get("headings")
                                        or citation.headings
                                    ),
                                    "doc_item_refs": list(
                                        metadata.get("doc_item_refs") or citation.doc_item_refs
                                    ),
                                    "excerpt_char_start": start if start >= 0 else None,
                                    "excerpt_char_end": (
                                        start + len(citation.excerpt) if start >= 0 else None
                                    ),
                                    "chunk_content_hash": (
                                        hashlib.sha256(raw_content.encode()).hexdigest()
                                        if raw_content
                                        else None
                                    ),
                                    "verification_status": "provider-grounded",
                                }
                            )
                        )
                    citations = enriched
                    for index in range(len(citations), 0, -1):
                        answer = re.sub(rf"\[{index}\]", f"[E{index}]", answer)
                    if evidence_mode is EvidenceMode.STRICT and (
                        not _strictly_supported(answer, citations)
                    ):
                        answer = STRICT_REFUSAL
                        citations = []

                citation_data = [item.model_dump(mode="json") for item in citations]
                claim_data = [item.model_dump(mode="json") for item in claims]
                degraded = (
                    bool(query_metadata.get("fallbacks"))
                    or str(query_metadata.get("abstention", "none")) != "none"
                )
                if cache_status is AnswerCacheStatus.MISS and not degraded:
                    self.store.cache_answer(
                        cache_key=cache_key,
                        workspace_id=run.workspace_id,
                        index_fingerprint=index_fingerprint,
                        config_fingerprint=config_fingerprint,
                        request=cache_request,
                        answer=answer,
                        citations=citation_data,
                        claims=claim_data,
                        metadata=query_metadata,
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
                or (claims and all(claim.status.value == "insufficient" for claim in claims))
                else SourceCheck.REVIEWED
            )
            await phase("preparing_evidence", "Preparing evidence")
            rerank_status = str(
                query_metadata.get("rerank_status")
                or (
                    "applied"
                    if any(item.rerank_score is not None for item in citations)
                    else "configured"
                    if self._reranker_configured(run.workspace_id)
                    else "not_configured"
                )
            )
            if current_phase is not None:
                phase_timings[current_phase] = (time.perf_counter() - phase_started) * 1000
            receipt = RunReceipt(
                session_id=run.session_id,
                turn=turn,
                cache_status=cache_status,
                total_ms=(time.perf_counter() - started) * 1000,
                source_count=len(citations),
                reused_source_count=reused,
                new_source_count=max(0, len(citations) - reused),
                source_check=source_check,
                phase_timings_ms=phase_timings,
                retrieval_mode="adaptive-hybrid" if adaptive else "hybrid",
                rerank_status=rerank_status,
                complexity=str(query_metadata.get("complexity", "standard")),
                route="book-kg+hybrid" if adaptive else "hybrid",
                facets=list(query_metadata.get("facets", [])),
                budgets=dict(query_metadata.get("budgets", {})),
                candidate_count=int(query_metadata.get("candidate_count", 0)),
                selected_count=int(query_metadata.get("selected_count", len(citations))),
                cut_reason=str(query_metadata.get("cut_reason", "legacy")),
                facet_coverage=dict(query_metadata.get("facet_coverage", {})),
                fallbacks=list(
                    dict.fromkeys(
                        [
                            *query_metadata.get("fallbacks", []),
                            *(
                                [str(runtime_metadata["readiness_status"])]
                                if runtime_metadata.get("readiness_status")
                                and runtime_metadata.get("readiness_status") != "ready"
                                else []
                            ),
                        ]
                    )
                ),
                model_digests=dict(query_metadata.get("model_digests", {})),
                prompt_tokens=query_metadata.get("prompt_tokens"),
                output_tokens=query_metadata.get("output_tokens"),
                tokens_per_second=query_metadata.get("tokens_per_second"),
                time_to_first_token_ms=query_metadata.get("time_to_first_token_ms"),
                abstention=str(query_metadata.get("abstention", "none")),
                rejected_claims=int(query_metadata.get("rejected_claims", 0)),
                done_reason=str(query_metadata.get("done_reason", "stop")),
            )
            citation_data = [item.model_dump(mode="json") for item in citations]
            claim_data = [item.model_dump(mode="json") for item in claims]
            for citation in citations:
                key = citation.evidence_id or citation.chunk_id
                if key in streamed_citation_ids:
                    continue
                streamed_citation_ids.add(key)
                await self.events.emit(
                    "citation.added",
                    correlation_id=run_id,
                    workspace_id=run.workspace_id,
                    run_id=run_id,
                    payload=citation.model_dump(mode="json"),
                )
            if claims:
                for claim in claims:
                    if claim.id not in streamed_claim_ids:
                        await emit_committed_claim(claim, [])
            else:
                # Legacy Haiku exposes only the completed provider response.
                for start in range(0, len(answer), 160):
                    await self.events.emit(
                        "assistant.delta",
                        correlation_id=run_id,
                        workspace_id=run.workspace_id,
                        run_id=run_id,
                        payload={"delta": answer[start : start + 160]},
                    )
            self.store.update_run(
                run_id,
                status=JobStatus.COMPLETED,
                answer=answer,
                claims=claim_data,
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
                    "claims": claim_data,
                    "citations": citation_data,
                    "receipt": receipt.model_dump(mode="json"),
                },
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self._fail(
                run,
                "QUERY_DEADLINE_EXCEEDED",
                "Query deadline exceeded",
                True,
            )
        except OmaRagError as exc:
            await self._fail(run, exc.code, exc.message, exc.retryable)
        except Exception as exc:
            await self._fail(run, "RUN_FAILED", str(exc), True)

    def _config_fingerprint(self, workspace_id: str) -> str:
        workspace = Path(self.workspaces.get(workspace_id).path)
        return hashlib.sha256((workspace / "haiku.rag.yaml").read_bytes()).hexdigest()

    async def _query_runtime_identity(
        self, workspace_id: str
    ) -> tuple[OllamaModelIdentity, dict[str, Any]]:
        if self.model_roles is None:
            raise RuntimeError("Query model roles are not configured")
        roles = self.model_roles(workspace_id)
        chat = roles.get("chat")
        embedding = roles.get("embedding")
        if not chat or not embedding:
            raise RuntimeError("Generator and embedding models must both be configured")

        def normalize(value: str) -> str:
            return value.removesuffix(":latest")

        async with OllamaStreamClient(self.ollama_url) as ollama:
            installed, residents = await asyncio.gather(
                ollama.list_models(), ollama.running_models()
            )

        def identity(model: str) -> OllamaModelIdentity:
            matches = [item for item in installed if normalize(item.name) == normalize(model)]
            if len(matches) != 1:
                state = "not installed" if not matches else "ambiguous"
                raise RuntimeError(f"Configured Ollama model is {state}: {model}")
            return matches[0]

        chat_identity = identity(chat)
        embedding_identity = identity(embedding)
        resident_by_name = {normalize(item.name): item for item in residents}
        required = (chat_identity, embedding_identity)
        mismatched = [
            item.name
            for item in required
            if normalize(item.name) in resident_by_name
            and resident_by_name[normalize(item.name)].digest not in {None, item.digest}
        ]
        missing = [item.name for item in required if normalize(item.name) not in resident_by_name]
        readiness = (
            "resident_digest_mismatch" if mismatched else "latency_degraded" if missing else "ready"
        )
        if mismatched:
            raise RuntimeError(
                "Resident Ollama model digest does not match the installed pinned model: "
                + ", ".join(mismatched)
            )
        generation = self.store.workspace_index_generation(workspace_id)
        generation_config = dict(generation.get("config") or {}) if generation is not None else {}
        indexed_embedding_digest = generation_config.get("embedding_digest")
        if indexed_embedding_digest and indexed_embedding_digest != embedding_identity.digest:
            raise RuntimeError("The embedding model digest differs from the READY index generation")
        return chat_identity, {
            "readiness_status": readiness,
            "missing_resident_models": missing,
            "mismatched_resident_models": mismatched,
            "model_digests": {
                "generator": chat_identity.digest,
                "embedding": embedding_identity.digest,
            },
        }

    @staticmethod
    def _orchestration_metadata(result: OrchestratedAnswer) -> dict[str, Any]:
        return {
            "complexity": result.complexity,
            "facets": list(result.facets),
            "budgets": result.budgets,
            "candidate_count": result.candidate_count,
            "selected_count": result.selected_count,
            "rerank_status": result.rerank_status,
            "cut_reason": result.cut_reason,
            "facet_coverage": result.facet_coverage,
            "fallbacks": list(result.fallbacks),
            "model_digests": result.model_digests,
            "time_to_first_token_ms": result.time_to_first_token_ms,
            "prompt_tokens": result.prompt_tokens,
            "output_tokens": result.output_tokens,
            "tokens_per_second": result.tokens_per_second,
            "rejected_claims": result.rejected_claims,
            "abstention": result.abstention,
            "done_reason": result.done_reason,
        }

    def _reranker_configured(self, workspace_id: str) -> bool:
        config = Path(self.workspaces.get(workspace_id).path) / "haiku.rag.yaml"
        try:
            text = config.read_text(encoding="utf-8").casefold()
        except OSError:
            return False
        return "rerank" in text and not re.search(r"rerank[^\n]*:\s*(?:null|none|false)\b", text)

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
        run = self.store.get_run(run_id)
        if run.status not in {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}:
            run = self.store.update_run(run_id, status=JobStatus.CANCELLED)
            await self.events.emit(
                "run.cancelled",
                correlation_id=run_id,
                workspace_id=run.workspace_id,
                run_id=run_id,
            )
        return self.store.get_run(run_id)

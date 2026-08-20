from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar
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
from ..models.errors import ConflictError, OmaRagError
from ..store import StateStore, request_hash
from .event_service import EventService
from .ollama_stream import OllamaModelIdentity, OllamaStreamClient
from .query_orchestrator import OrchestratedAnswer, QueryOrchestrator
from .query_v2 import classify_query, performance_budget
from .reranker_service import DEFAULT_RERANKER, DEFAULT_RERANKER_REVISION, _model_digest
from .resource_coordinator import ResourceCoordinator
from .workspace_service import WorkspaceService


@dataclass
class _QueryDeadline:
    """The absolute request budget, plus what the request actually spent it on.

    The budget is meant to bound the *work* a question causes. Time spent
    queueing for a free model slot is not work — charging it to the answer is
    how a cold model load turned a 15s budget into QUERY_DEADLINE_EXCEEDED
    before the search had even started. Waiting is therefore credited back,
    but only up to a ceiling, so a stuck lease still fails instead of hanging.
    """

    started_at: float
    timeout_ms: int
    handle: Any = None
    admission_wait_ms: float = 0.0
    granted_ms: float = 0.0
    reasons: list[str] = field(default_factory=list)
    phase_label: str = field(default="Starting")

    #: Total slack a request may be granted, however many reasons apply. Past
    #: this it fails, so no combination of excuses can leave a caller hanging.
    MAX_EXTENSION_MS: ClassVar[float] = 60_000.0
    #: What an Ollama model that is not resident costs to load. Measured at
    #: ~14s for the pinned generator on an 8 GB machine; the allowance is
    #: deliberately generous because being wrong here means a failed question,
    #: and it is only ever granted when readiness reports the model missing.
    COLD_START_ALLOWANCE_MS: ClassVar[float] = 30_000.0
    #: What a query worker costs to rebuild before it can rank anything:
    #: importing sentence_transformers and reading the reranker's weights,
    #: measured at 5.6s and 3.1s against 0.65s of actual scoring. Same
    #: category as the cold model above — loading is not the question's work.
    RERANKER_LOAD_ALLOWANCE_MS: ClassVar[float] = 12_000.0

    def grant(self, milliseconds: float, reason: str) -> None:
        """Push the deadline back for work the question did not ask for."""

        room = self.MAX_EXTENSION_MS - self.granted_ms
        granted = min(max(milliseconds, 0.0), room)
        if granted <= 0.0 or self.handle is None:
            return
        self.granted_ms += granted
        self.reasons.append(reason)
        with suppress(RuntimeError, AttributeError):
            # RuntimeError: the budget already expired and the reschedule is
            # moot — the cancellation is on its way to us either way.
            self.handle.reschedule(self.started_at + (self.timeout_ms + self.granted_ms) / 1000)

    def credit_admission_wait(self, waited_ms: float) -> None:
        self.admission_wait_ms = waited_ms
        if waited_ms >= 250.0:
            self.grant(waited_ms, "waiting for a free model slot")

    def allow_reranker_load(self) -> None:
        """The query worker has to be rebuilt, so its models load from scratch."""

        self.grant(self.RERANKER_LOAD_ALLOWANCE_MS, "rebuilding the query worker")

    def allow_cold_start(self, models: list[str]) -> None:
        """Loading a model that is not resident is not the question's fault.

        A cold generator costs more than the whole budget of a simple question,
        so without this the first question after a pause could not succeed no
        matter how fast retrieval was.
        """
        if models:
            self.grant(self.COLD_START_ALLOWANCE_MS, "loading a model that was not resident")

    def expiry_message(self, elapsed_seconds: float) -> str:
        message = (
            f"Query deadline exceeded after {elapsed_seconds:.1f}s "
            f"while {self.phase_label.casefold()}"
        )
        if self.reasons:
            message += f" (already extended for {', '.join(dict.fromkeys(self.reasons))})"
        return message


# Fallbacks that describe the conditions a request ran under, not the answer it
# produced. Everything else still bars the answer from the cache.
#
# `latency_degraded` only says the models were not resident — it is on almost
# every request on a machine that unloads them, and treating it as a quality
# signal meant the answer cache was never written at all: the same question
# asked three times in a row missed three times and paid for three full runs.
# `retrieval_escalated` says the second retrieval stage ran, which produces a
# better answer, not a worse one.
_CACHEABLE_FALLBACKS = frozenset({"latency_degraded", "retrieval_escalated"})

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
        answer_cache_max_entries: int = 64,
        answer_cache_max_bytes: int = 128 * 1024**2,
        ollama_url: str = "http://127.0.0.1:11434",
        model_roles: Callable[[str], dict[str, str | None]] | None = None,
        model_settings: Callable[[str], dict[str, object]] | None = None,
        workspace_profile: Callable[[str], str | None] | None = None,
        workspace_context_tokens: Callable[[str], int | None] | None = None,
    ) -> None:
        self.store = store
        self.workspaces = workspaces
        self.events = events
        self.adapter = adapter
        self.resources = resources
        self.answer_cache_max_entries = answer_cache_max_entries
        self.answer_cache_max_bytes = answer_cache_max_bytes
        self.ollama_url = ollama_url
        self.model_roles = model_roles
        self.model_settings = model_settings
        self.workspace_profile = workspace_profile
        self.workspace_context_tokens = workspace_context_tokens
        self.query = QueryOrchestrator(store, adapter, ollama_url)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._deadlines: dict[str, _QueryDeadline] = {}
        self.index_gate: Callable[[str], None] | None = None
        self.visual_evidence_builder: Callable[[str], object] | None = None
        self.content_egress_guard: Callable[[str, str], None] | None = None
        self.runtime_identity_resolver: (
            Callable[..., Awaitable[tuple[OllamaModelIdentity | None, dict[str, Any]]]] | None
        ) = None

    def _effective_request(self, workspace_id: str, request: dict[str, Any]) -> dict[str, Any]:
        resolved = dict(request)
        options = dict(resolved.get("options", {}))
        profile_resolver = getattr(self, "workspace_profile", None)
        if str(options.get("profile") or "auto") == "auto" and profile_resolver:
            configured = profile_resolver(workspace_id)
            if configured in {"fast", "normal", "quality"}:
                options["profile"] = configured
        context_resolver = getattr(self, "workspace_context_tokens", None)
        if context_resolver:
            context_tokens = context_resolver(workspace_id)
            if context_tokens and context_tokens >= 4096:
                options["_model_context_tokens"] = context_tokens
        resolved["options"] = options
        return resolved

    def _adaptive_retrieval_enabled(self, request: dict[str, Any]) -> bool:
        """Use the source-bound V3 path for every supported answer mode."""

        return bool(
            not request.get("images")
            and getattr(self.adapter.capabilities, "adaptive_retrieval", False)
        )

    @property
    def active(self) -> bool:
        return bool(self._tasks)

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def invalidate_model_inventory(self) -> None:
        """Make the next model identity check hit Ollama after a mutation."""

        invalidate = getattr(OllamaStreamClient, "invalidate_inventory", None)
        if callable(invalidate):
            invalidate(self.ollama_url)

    async def start(self, workspace_id: str, request: RunRequest) -> RunSnapshot:
        self.workspaces.get(workspace_id)
        if request.options.verifier == "off" and not self._expert_mode(workspace_id):
            raise ConflictError(
                "verifier=off is restricted to an explicitly configured expert workspace"
            )
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
            payload={"status": JobStatus.RUNNING.value},
        )
        task = asyncio.create_task(self._execute(run_id), name=run_id)
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
        return self.store.update_run(run_id, status=JobStatus.RUNNING)

    async def _execute(self, run_id: str) -> None:
        deadline_started_at = asyncio.get_running_loop().time()
        run = self.store.get_run(run_id)
        request = self._effective_request(
            run.workspace_id,
            self.store.get_run_request(run_id),
        )
        options = dict(request.get("options", {}))
        deadline_question = request["question"]
        deadline_session_reference = False
        deadline_register_entities = 0
        adaptive_request = self._adaptive_retrieval_enabled(request)
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
        plan_deadline_ms = performance_budget(
            deadline_plan.complexity, str(options.get("profile") or "auto")
        ).deadline_ms
        timeout_ms = int(
            options.get("deadline_ms")
            or (15_000 if options.get("profile") == "fast" else 0)
            or (60_000 if request.get("mode") == "analysis" else plan_deadline_ms)
        )
        deadline = _QueryDeadline(started_at=deadline_started_at, timeout_ms=timeout_ms)
        self._deadline_registry()[run_id] = deadline
        try:
            # This is the absolute request budget: readiness, cache validation,
            # retrieval, generation and persistence all fit inside the same
            # clock. Queueing for a free model slot does not — see
            # `_QueryDeadline.credit_admission_wait`.
            async with asyncio.timeout_at(deadline_started_at + timeout_ms / 1000) as budget:
                deadline.handle = budget
                await self._execute_inner(run_id)
        except TimeoutError:
            run = self.store.get_run(run_id)
            if run.status not in {
                JobStatus.COMPLETED,
                JobStatus.CANCELLED,
                JobStatus.FAILED,
            }:
                elapsed = asyncio.get_running_loop().time() - deadline_started_at
                await self._fail(
                    run,
                    "QUERY_DEADLINE_EXCEEDED",
                    deadline.expiry_message(elapsed),
                    True,
                )
        finally:
            self._deadline_registry().pop(run_id, None)

    def _deadline_registry(self) -> dict[str, _QueryDeadline]:
        registry = getattr(self, "_deadlines", None)
        if registry is None:
            registry = {}
            self._deadlines = registry
        return registry

    async def _execute_inner(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        request = self._effective_request(
            run.workspace_id,
            self.store.get_run_request(run_id),
        )
        started = time.perf_counter()
        phase_started = started
        current_phase: str | None = None
        phase_timings: dict[str, float] = {}
        deadline = self._deadline_registry().get(run_id)

        async def phase(name: str, label: str) -> None:
            nonlocal current_phase, phase_started
            if deadline is not None:
                # So an expiry can name what the request was doing instead of
                # only that it ran out of time.
                deadline.phase_label = label
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
            adaptive = self._adaptive_retrieval_enabled(request)
            await phase("waiting", "Waiting")
            cache_status = AnswerCacheStatus.BYPASS
            index_fingerprint = self.store.workspace_index_fingerprint(run.workspace_id)
            config_fingerprint = self._config_fingerprint(run.workspace_id)
            runtime_metadata: dict[str, Any] = {}
            model_identity: OllamaModelIdentity | None = None
            uses_images = bool(request.get("images"))
            await phase("readiness", "Checking pinned local models")
            model_identity, runtime_metadata = await self._resolve_query_runtime_identity(
                run.workspace_id,
                require_vl=uses_images,
                allow_uncalibrated_reranker=adaptive,
            )
            if deadline is not None:
                deadline.allow_cold_start(
                    [str(name) for name in runtime_metadata.get("missing_resident_models") or []]
                )
                if getattr(self.adapter, "query_worker_state", "ready") != "ready":
                    deadline.allow_reranker_load()

            cache_request = {key: value for key, value in request.items() if key != "session_id"}
            if adaptive:
                memory_enabled = request.get("options", {}).get("memory", "auto") != "off"
                standalone, history_used = self.query.standalone_question(
                    run.workspace_id,
                    run.session_id,
                    run_id,
                    request["question"],
                    memory_enabled=memory_enabled,
                )
                cache_request["question"] = _normalized_question(standalone)
                # Not the session id. Keying on it gave every conversation its
                # own cache, so the same question missed every time it was asked
                # afresh — which is the case the cache exists for. What actually
                # varies the answer is whether history shaped the question, and
                # when it did, the rewritten question above already carries it.
                cache_request["memory_context"] = "history" if history_used else "off"
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
                    "model_identities": runtime_metadata.get("model_identities", {}),
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

            async def emit_draft(text: str) -> None:
                """Publish provisional prose while a claim is still being written.

                Deliberately *not* persisted: a draft is not an answer. It exists
                only so the reader sees words immediately; the validated claim
                that follows replaces it.
                """
                await self.events.emit(
                    "assistant.draft",
                    correlation_id=run_id,
                    workspace_id=run.workspace_id,
                    run_id=run_id,
                    payload={"draft": text},
                )

            if cached is not None:
                # Cache acceptance is a model/index read as well: take the
                # foreground lease, bypass the two-second inventory cache and
                # reject a result if any request-local pin drifted.
                async with self.resources.chat():
                    if self.index_gate is not None:
                        self.index_gate(run.workspace_id)
                    _, confirmed_runtime = await self._resolve_query_runtime_identity(
                        run.workspace_id,
                        require_vl=uses_images,
                        allow_uncalibrated_reranker=adaptive,
                        force_inventory_refresh=True,
                        check_residency=False,
                    )
                    self._assert_runtime_pins_unchanged(runtime_metadata, confirmed_runtime)
                    current_fingerprint = self.store.workspace_index_fingerprint(run.workspace_id)
                    current_config_fingerprint = self._config_fingerprint(run.workspace_id)
                    if (
                        current_fingerprint != index_fingerprint
                        or current_config_fingerprint != config_fingerprint
                    ):
                        cached = None
                        cache_status = AnswerCacheStatus.MISS
            if cached is not None:
                await phase("checking_sources", "Checking cached evidence")
                answer = str(cached["answer"])
                citations = [Citation.model_validate(item) for item in cached["citations"]]
                claims = [AnswerClaim.model_validate(item) for item in cached.get("claims", [])]
                query_metadata = dict(cached.get("metadata", {}))
            else:
                if self.content_egress_guard is not None:
                    self.content_egress_guard(run.workspace_id, self.ollama_url)
                database = self.workspaces.database_path(run.workspace_id)
                await phase("admission", "Waiting for a free model slot")
                admission_started = asyncio.get_running_loop().time()
                async with self.resources.chat():
                    if deadline is not None:
                        deadline.credit_admission_wait(
                            (asyncio.get_running_loop().time() - admission_started) * 1000
                        )
                    if self.index_gate is not None:
                        self.index_gate(run.workspace_id)
                    # The inventory used for the cache key was read before
                    # resource admission. Re-pin inside the model/index reader
                    # lease so a consented publish or mutation cannot win the
                    # gap. Segment IDs are deliberately resolved only now.
                    (
                        confirmed_identity,
                        confirmed_runtime,
                    ) = await self._resolve_query_runtime_identity(
                        run.workspace_id,
                        require_vl=uses_images,
                        allow_uncalibrated_reranker=adaptive,
                        force_inventory_refresh=True,
                    )
                    self._assert_runtime_pins_unchanged(runtime_metadata, confirmed_runtime)
                    if (
                        self.store.workspace_index_fingerprint(run.workspace_id)
                        != index_fingerprint
                        or self._config_fingerprint(run.workspace_id) != config_fingerprint
                    ):
                        raise RuntimeError(
                            "The workspace index generation changed while the request was waiting"
                        )
                    model_identity = confirmed_identity
                    runtime_metadata = confirmed_runtime
                    segment_ids = self.store.resolve_segment_ids(
                        run.workspace_id,
                        request.get("filters", {}),
                        request.get("document_policy", "current-only"),
                    )
                    options = dict(request.get("options", {}))
                    await phase("warming", "Preparing retrieval")
                    # Adaptive retrieval opens and hydrates the isolated worker
                    # in its batched search_many RPC. A separate warm RPC adds
                    # one process transition without improving readiness.
                    if not adaptive:
                        await self.adapter.warm(database)
                    if adaptive:
                        assert model_identity is not None
                        self.query.adapter = self.adapter
                        residency_seconds = self.resources.residency_seconds()
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
                            emit_draft=emit_draft,
                            extend_deadline=deadline.grant if deadline is not None else None,
                            memory_enabled=options.get("memory", "auto") != "off",
                            allowed_document_ids=(
                                set(segment_ids) if segment_ids is not None else None
                            ),
                            keep_alive=(
                                0 if residency_seconds <= 0 else f"{round(residency_seconds)}s"
                            ),
                            reranker_digest=str(
                                runtime_metadata.get("model_digests", {}).get("reranker") or ""
                            )
                            or None,
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
                    _, confirmed_runtime = await self._resolve_query_runtime_identity(
                        run.workspace_id,
                        require_vl=uses_images,
                        allow_uncalibrated_reranker=adaptive,
                        force_inventory_refresh=True,
                        check_residency=False,
                    )
                    self._assert_runtime_pins_unchanged(runtime_metadata, confirmed_runtime)
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
                                    "generation_id": str(metadata.get("generation_id") or "")
                                    or None,
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
                    if not _strictly_supported(answer, citations):
                        answer = STRICT_REFUSAL
                        citations = []

                query_metadata.setdefault("model_digests", {}).update(
                    runtime_metadata.get("model_digests", {})
                )
                query_metadata["model_identities"] = runtime_metadata.get("model_identities", {})
                citation_data = [item.model_dump(mode="json") for item in citations]
                claim_data = [item.model_dump(mode="json") for item in claims]
                degraded = (
                    bool(set(query_metadata.get("fallbacks") or ()) - _CACHEABLE_FALLBACKS)
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
                        max_bytes=self.answer_cache_max_bytes,
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
                retrieval_stages=list(query_metadata.get("retrieval_stages", [])),
                escalation_reasons=list(query_metadata.get("escalation_reasons", [])),
                calibrator_digest=query_metadata.get("calibrator_digest"),
                calibrator_status=str(query_metadata.get("calibrator_status", "unknown")),
                verifier_digest=query_metadata.get("verifier_digest"),
                verifier_status=str(query_metadata.get("verifier_status", "not-run")),
                typed_evidence_status=str(query_metadata.get("typed_evidence_status", "unknown")),
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
                    "status": JobStatus.COMPLETED.value,
                    "receipt": receipt.model_dump(mode="json"),
                },
            )
            # Publish the completed text answer before doing optional crop I/O.
            # The follow-up chat lease excludes reindexing during selection;
            # VisualEvidenceService additionally compares the exact citation
            # and document-generation pins before it persists the cache.
            visual_builder = getattr(self, "visual_evidence_builder", None)
            if visual_builder is not None:
                with suppress(Exception):
                    async with self.resources.chat():
                        if self.index_gate is not None:
                            self.index_gate(run.workspace_id)
                        await asyncio.to_thread(visual_builder, run_id)
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

    def _expert_mode(self, workspace_id: str) -> bool:
        if self.model_settings is None:
            return False
        settings = self.model_settings(workspace_id)
        profile = settings.get("profile")
        return isinstance(profile, dict) and profile.get("expert_mode") is True

    async def _resolve_query_runtime_identity(
        self, workspace_id: str, **options: Any
    ) -> tuple[OllamaModelIdentity | None, dict[str, Any]]:
        """Resolve runtime pins, with an explicit seam for isolated test providers."""

        if self.runtime_identity_resolver is not None:
            return await self.runtime_identity_resolver(workspace_id, **options)
        return await self._query_runtime_identity(workspace_id, **options)

    async def _query_runtime_identity(
        self,
        workspace_id: str,
        *,
        require_generator: bool = True,
        require_vl: bool = False,
        allow_uncalibrated_reranker: bool = True,
        force_inventory_refresh: bool = False,
        check_residency: bool = True,
    ) -> tuple[OllamaModelIdentity | None, dict[str, Any]]:
        if self.model_roles is None:
            raise RuntimeError("Query model roles are not configured")
        roles = self.model_roles(workspace_id)
        chat = roles.get("chat")
        vl = roles.get("vl")
        embedding = roles.get("embedding")
        reranker = roles.get("rerank")
        if not embedding or (require_generator and not chat):
            raise RuntimeError(
                "The embedding model and every requested generator must be configured"
            )

        def normalize(value: str) -> str:
            return value.strip().casefold().removesuffix(":latest")

        configured_settings = (
            self.model_settings(workspace_id) if self.model_settings is not None else {}
        )
        embedding_provider = str(
            configured_settings.get("embedding_provider") or "ollama"
        ).casefold()
        chat_provider = str(configured_settings.get("chat_provider") or "ollama").casefold()
        reranker_provider = str(
            configured_settings.get("rerank_provider") or "cross-encoder"
        ).casefold()
        profile = configured_settings.get("profile")
        profile_artifacts = (
            profile.get("artifacts")
            if isinstance(profile, dict) and profile.get("expert_mode") is False
            else None
        )
        auto_profile = isinstance(profile_artifacts, dict)
        if not auto_profile:
            profile_artifacts = {}

        def pinned_artifact(role: str) -> dict[str, Any] | None:
            value = profile_artifacts.get(role)
            return value if isinstance(value, dict) else None

        def pin_ollama(
            role: str, configured_model: str, identity: OllamaModelIdentity
        ) -> dict[str, str | None]:
            pinned = pinned_artifact(role)
            settings_role = "chat" if role in {"chat", "vl"} else role
            revision = (
                str(configured_settings.get(f"{settings_role}_revision") or "")
                or configured_model.partition(":")[2]
                or None
            )
            if pinned is not None:
                if str(pinned.get("provider") or "").casefold() != "ollama":
                    raise RuntimeError(f"Pinned {role} provider no longer matches workspace config")
                if normalize(str(pinned.get("model") or "")) != normalize(configured_model):
                    raise RuntimeError(f"Pinned {role} model no longer matches workspace config")
                expected = str(pinned.get("digest") or "").removeprefix("sha256:")
                actual = identity.digest.removeprefix("sha256:")
                if not expected or not hmac.compare_digest(expected.casefold(), actual.casefold()):
                    raise RuntimeError(
                        f"Installed {role} model digest differs from the applied model catalog"
                    )
                revision = str(pinned.get("revision") or "") or revision
            elif auto_profile:
                raise RuntimeError(f"Applied model profile has no pinned {role} artifact")
            else:
                configured_digest = str(
                    configured_settings.get(f"{settings_role}_digest") or ""
                ).removeprefix("sha256:")
                actual_digest = identity.digest.removeprefix("sha256:")
                if configured_digest and not hmac.compare_digest(
                    configured_digest.casefold(), actual_digest.casefold()
                ):
                    raise RuntimeError(
                        f"Installed {role} model digest differs from workspace config"
                    )
            return {
                "provider": "ollama",
                "model": identity.name,
                "revision": revision or f"digest:{identity.digest}",
                "digest": identity.digest,
                "status": "pinned",
            }

        if embedding_provider != "ollama":
            raise RuntimeError("The configured embedding provider has no request-local digest pin")
        if require_generator and chat_provider != "ollama":
            raise RuntimeError("The configured generator provider has no request-local digest pin")
        if reranker and reranker_provider != "cross-encoder":
            raise RuntimeError("The configured reranker provider cannot be pinned locally")
        if require_vl and (not chat or not vl or normalize(vl) != normalize(chat)):
            # The public Haiku image call currently sends images to qa.model.
            # Refuse a misleading separate VL selection until that provider
            # exposes a role-specific, digest-bound request.
            raise RuntimeError(
                "The configured VL role is not the digest-pinned QA model used for images"
            )

        if force_inventory_refresh:
            self.invalidate_model_inventory()
        async with OllamaStreamClient(self.ollama_url) as ollama:
            if check_residency:
                installed, residents = await asyncio.gather(
                    ollama.list_models(), ollama.running_models()
                )
            else:
                installed = await ollama.list_models()
                residents = ()

        def identity(model: str) -> OllamaModelIdentity:
            matches = [item for item in installed if normalize(item.name) == normalize(model)]
            if len(matches) != 1:
                state = "not installed" if not matches else "ambiguous"
                raise RuntimeError(f"Configured Ollama model is {state}: {model}")
            return matches[0]

        chat_identity = identity(chat) if require_generator and chat else None
        model_identities: dict[str, dict[str, str | None]] = {}
        if chat_identity is not None and chat is not None:
            model_identities["generator"] = pin_ollama("chat", chat, chat_identity)
            if require_vl:
                # Validate both catalog assignments even though one Ollama
                # artifact deliberately serves both roles on consumer hardware.
                model_identities["vl"] = pin_ollama("vl", chat, chat_identity)
        embedding_identity = identity(embedding)
        if embedding_identity is not None:
            model_identities["embedding"] = pin_ollama("embedding", embedding, embedding_identity)
        pinned_reranker = pinned_artifact("rerank")
        reranker_revision: str | None = None
        reranker_artifact_digest: str | None = None
        if reranker:
            if pinned_reranker is not None:
                if str(pinned_reranker.get("provider") or "").casefold() not in {
                    "hugging-face",
                    "cross-encoder",
                }:
                    raise RuntimeError(
                        "Pinned reranker provider no longer matches the query runtime"
                    )
                if normalize(str(pinned_reranker.get("model") or "")) != normalize(reranker):
                    raise RuntimeError("Pinned reranker model no longer matches the query runtime")
                reranker_revision = str(pinned_reranker.get("revision") or "") or None
                reranker_artifact_digest = str(pinned_reranker.get("digest") or "") or None
            elif auto_profile:
                raise RuntimeError("Applied model profile has no pinned reranker artifact")
            else:
                reranker_revision = (
                    str(configured_settings.get("rerank_revision") or "")
                    or (DEFAULT_RERANKER_REVISION if reranker == DEFAULT_RERANKER else "")
                    or None
                )
                reranker_artifact_digest = (
                    str(configured_settings.get("rerank_digest") or "") or reranker_revision
                ) or None
            if reranker_revision and reranker_artifact_digest:
                from .model_service import ModelService

                if not hmac.compare_digest(
                    reranker_artifact_digest.removeprefix("sha256:").casefold(),
                    reranker_revision.removeprefix("sha256:").casefold(),
                ):
                    raise RuntimeError(
                        "The configured reranker digest cannot be verified from its revision"
                    )
                if not ModelService._hugging_face_revision_present(reranker, reranker_revision):
                    raise RuntimeError(
                        "The exact pinned reranker revision is not present in the local cache"
                    )
                model_identities["reranker"] = {
                    "provider": "cross-encoder",
                    "model": reranker,
                    "revision": reranker_revision,
                    "digest": reranker_revision,
                    "status": "pinned",
                }
            else:
                model_identities["reranker"] = {
                    "provider": "cross-encoder",
                    "model": reranker,
                    "revision": None,
                    "digest": None,
                    "status": "uncalibrated",
                }
                if not allow_uncalibrated_reranker:
                    raise RuntimeError(
                        "The configured reranker has no immutable local revision pin"
                    )
        resident_by_name = {normalize(item.name): item for item in residents}
        required = tuple(item for item in (chat_identity, embedding_identity) if item is not None)
        mismatched = [
            item.name
            for item in required
            if normalize(item.name) in resident_by_name
            and resident_by_name[normalize(item.name)].digest not in {None, item.digest}
        ]
        missing = [item.name for item in required if normalize(item.name) not in resident_by_name]
        readiness = (
            "identity_pinned"
            if not check_residency
            else "resident_digest_mismatch"
            if mismatched
            else "latency_degraded"
            if missing
            else "ready"
        )
        if mismatched:
            raise RuntimeError(
                "Resident Ollama model digest does not match the installed pinned model: "
                + ", ".join(mismatched)
            )
        generation = self.store.workspace_index_generation(workspace_id)
        generation_config = dict(generation.get("config") or {}) if generation is not None else {}
        indexed_embedding_digest = generation_config.get("embedding_digest")
        if (
            indexed_embedding_digest
            and embedding_identity is not None
            and not hmac.compare_digest(
                str(indexed_embedding_digest).removeprefix("sha256:").casefold(),
                embedding_identity.digest.removeprefix("sha256:").casefold(),
            )
        ):
            raise RuntimeError("The embedding model digest differs from the READY index generation")
        published_locks = self.store.workspace_index_runtime_locks(workspace_id)
        for lock in published_locks:
            if str(
                lock.get("embedding_provider") or ""
            ).casefold() != embedding_provider or normalize(
                str(lock.get("embedding_model") or "")
            ) != normalize(embedding):
                raise RuntimeError(
                    "The configured embedding identity differs from a published document"
                )
        published_digests = {
            str(lock.get("embedding_digest") or "").removeprefix("sha256:").casefold()
            for lock in published_locks
            if lock.get("embedding_digest")
        }
        if len(published_digests) > 1:
            raise RuntimeError("Published documents contain mixed embedding model digests")
        if embedding_identity is not None and published_digests:
            actual = embedding_identity.digest.removeprefix("sha256:").casefold()
            if not hmac.compare_digest(next(iter(published_digests)), actual):
                raise RuntimeError(
                    "The embedding model digest differs from the published document index"
                )
        reranker_calibration_digest = (
            _model_digest(reranker, reranker_revision) if reranker else None
        )
        return chat_identity, {
            "readiness_status": readiness,
            "missing_resident_models": missing,
            "mismatched_resident_models": mismatched,
            "model_identities": model_identities,
            "model_digests": {
                **({"generator": chat_identity.digest} if chat_identity is not None else {}),
                **(
                    {"embedding": embedding_identity.digest}
                    if embedding_identity is not None
                    else {}
                ),
                **(
                    {"reranker": reranker_calibration_digest} if reranker_calibration_digest else {}
                ),
                **(
                    {"vl": chat_identity.digest} if require_vl and chat_identity is not None else {}
                ),
            },
        }

    @staticmethod
    def _assert_runtime_pins_unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
        """Reject a result if any used provider identity changed mid-request."""

        if before.get("model_identities") != after.get("model_identities"):
            raise RuntimeError("A model provider identity changed while the request was running")

    async def verify_retrieval_identity(
        self,
        workspace_id: str,
        *,
        force_inventory_refresh: bool = False,
        check_residency: bool = True,
    ) -> dict[str, Any]:
        """Fail closed when direct search would use a drifted embedding space."""

        try:
            _, metadata = await self._query_runtime_identity(
                workspace_id,
                require_generator=False,
                force_inventory_refresh=force_inventory_refresh,
                check_residency=check_residency,
            )
        except RuntimeError as exc:
            raise ConflictError(
                "Retrieval model integrity check failed",
                details={"reason": str(exc)},
            ) from exc
        return metadata

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
            "retrieval_stages": list(result.retrieval_stages),
            "escalation_reasons": list(result.escalation_reasons),
            "calibrator_digest": result.calibrator_digest,
            "calibrator_status": result.calibrator_status,
            "verifier_digest": result.verifier_digest,
            "verifier_status": result.verifier_status,
            "typed_evidence_status": result.typed_evidence_status,
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

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapters.base import HaikuAdapter, SearchManyRequest, SearchManyResult
from ..models.domain import (
    BookMetadata,
    EvidenceMode,
    RetrievalExplanation,
    RetrievalTiming,
    SearchHit,
)
from ..store import StateStore
from .query_v2 import (
    EvidenceKind,
    ProgressiveRetrievalPolicy,
    ProvenanceKind,
    RetrievalCandidate,
    adaptive_select,
    classify_query,
    performance_budget,
    weighted_rrf,
)
from .reranker_service import DEFAULT_RERANKER, DEFAULT_RERANKER_REVISION, _model_digest


@dataclass(frozen=True, slots=True)
class _FacetSearchBatch:
    rows: dict[str, Any]
    hydrated_chunks: list[SearchHit]
    hydration_failed: bool = False
    # Why hydration failed.  Without it, "book_router_hydration=degraded" says
    # the register contributed nothing but not whether the worker died, the
    # deadline expired, or the chunk ids were stale.
    hydration_reason: str | None = None


@dataclass(slots=True)
class AdaptiveSearchService:
    adapter: HaikuAdapter
    store: StateStore | None = None

    async def search(
        self,
        database: Path,
        query: str,
        *,
        requested_limit: int,
        max_sources: int | None = None,
        document_filter: str | None = None,
        allowed_document_ids: set[str] | None = None,
        profile: str = "auto",
        reranker_digest: str | None = None,
    ) -> tuple[list[SearchHit], RetrievalExplanation]:
        started = time.perf_counter()
        plan = classify_query(query)
        profile_budget = performance_budget(plan.complexity, profile)
        effective_reranker_digest = reranker_digest or _model_digest(
            DEFAULT_RERANKER, DEFAULT_RERANKER_REVISION
        )
        cap = profile_budget.candidate_cap
        stage_a_cap = max(12, math.ceil(cap / 2))
        required_facets = tuple(facet for facet in plan.facets if facet.required)
        optional_facets = tuple(facet for facet in plan.facets if not facet.required)
        per_channel_limit = max(
            4,
            math.ceil(stage_a_cap / max(1, len(required_facets) * 2)),
        )
        routes: list[dict[str, Any]] = []
        route_notes: list[str] = []
        workspace_id: str | None = None
        if self.store is not None:
            workspace_id = self._workspace_id(database)
            if workspace_id:
                try:
                    routes = self.store.route_book_knowledge(
                        workspace_id,
                        query,
                        limit=stage_a_cap,
                        allowed_segment_ids=allowed_document_ids,
                        expand_sections=True,
                        global_query="global" in plan.reasons,
                    )
                except Exception:
                    route_notes.append("book_router=degraded")

        search_started = time.perf_counter()
        search_batch = await self._search_facets(
            database,
            required_facets,
            per_channel_limit,
            document_filter=document_filter,
            hydrate_chunk_ids=[str(item["chunk_id"]) for item in routes if item.get("chunk_id")],
        )
        hit_by_chunk: dict[str, SearchHit] = {}
        rankings: dict[str, list[RetrievalCandidate]] = {}
        weights: dict[str, float] = {}
        degraded_facets: list[str] = []

        def absorb_searches(batch: _FacetSearchBatch, facets: tuple[Any, ...]) -> None:
            for facet in facets:
                for channel, channel_weight in (("fts", 0.85), ("vector", 1.0)):
                    key = f"{facet.id}:{channel}"
                    hits = batch.rows.get(key, RuntimeError(f"missing {key}"))
                    if isinstance(hits, BaseException):
                        degraded_facets.append(key)
                        continue
                    path = f"{channel}:facet:{facet.id}"
                    weights[path] = channel_weight * (1.0 if facet.id == "F1" else 0.9)
                    rankings[path] = []
                    for hit in hits:
                        hit_by_chunk.setdefault(hit.chunk_id, hit)
                        rankings[path].append(self._candidate(hit, facet.id))

        def absorb_routes(route_rows: list[dict[str, Any]], hydrated: list[SearchHit]) -> None:
            routed_hits = {
                hit.chunk_id: hit
                for hit in hydrated
                if allowed_document_ids is None or hit.document_id in allowed_document_ids
            }
            for route in route_rows:
                hit = routed_hits.get(str(route.get("chunk_id") or ""))
                if hit is None:
                    continue
                path = str(route.get("retrieval_path") or "book-term")
                rankings.setdefault(path, []).append(self._candidate(hit, "F1"))
                weights[path] = (
                    1.1 if path in {"book-located_in", "book-defined_in", "book-section"} else 0.8
                )
                hit_by_chunk.setdefault(hit.chunk_id, hit)

        absorb_searches(search_batch, required_facets)
        absorb_routes(routes, search_batch.hydrated_chunks)
        if search_batch.hydration_failed:
            route_notes.append(
                "book_router_hydration=degraded"
                + (f"; {search_batch.hydration_reason}" if search_batch.hydration_reason else "")
            )
        fused = weighted_rrf(rankings, weights, limit=stage_a_cap)

        rerank_started = time.perf_counter()
        rerank_failure: str | None = None
        try:
            scores = await self.adapter.rerank(
                database,
                query,
                [hit_by_chunk[item.candidate.chunk_id] for item in fused],
            )
            if len(scores) != len(fused):
                raise RuntimeError("reranker score count mismatch")
        except Exception as exc:  # noqa: BLE001 - degrade safely, but say why
            # A worker that died, a deadline that expired and a score-count
            # mismatch all end here and all need different fixes.  Discarding
            # the cause made an intermittent failure impossible to diagnose.
            rerank_failure = f"{type(exc).__name__}: {exc}"[:160]
            scores = []
        search_ms = (time.perf_counter() - search_started) * 1000
        if not scores:
            ranked = [
                hit_by_chunk[item.candidate.chunk_id] for item in fused[: min(requested_limit, 3)]
            ]
            notes = self._provider_notes(
                plan,
                cap=cap,
                selected=len(ranked),
                cut="reranker_degraded_uncalibrated",
                degraded_facets=degraded_facets,
                stages=("stage-a",),
            )
            notes.insert(
                1,
                "reranker=degraded; scores are uncalibrated and no relevance is claimed"
                + (f"; {rerank_failure}" if rerank_failure else ""),
            )
            notes.extend(route_notes)
            return ranked, RetrievalExplanation(
                query=query,
                candidates=[hit_by_chunk[item.candidate.chunk_id] for item in fused],
                ranked=ranked,
                timing=RetrievalTiming(
                    search_ms=search_ms,
                    rerank_ms=(time.perf_counter() - rerank_started) * 1000,
                    total_ms=(time.perf_counter() - started) * 1000,
                ),
                provider_notes=notes,
            )

        raw_score_by_chunk = {
            item.candidate.chunk_id: score for item, score in zip(fused, scores, strict=True)
        }
        profile_limit = profile_budget.final_max
        selection = adaptive_select(
            [(item.candidate, score) for item, score in zip(fused, scores, strict=True)],
            complexity=plan.complexity,
            evidence_mode=EvidenceMode.NORMAL.value,
            required_facets=(facet.id for facet in required_facets),
            max_candidates=profile_limit,
            budget=profile_budget,
            reranker_digest=effective_reranker_digest,
        )
        stages = ["stage-a"]
        escalation_reasons: list[str] = []
        progressive = ProgressiveRetrievalPolicy()
        top = selection.selected[0].relevance if selection.selected else None
        second = selection.selected[1].relevance if len(selection.selected) > 1 else None
        missing_evidence_requirements = progressive.missing_evidence_requirements(
            query, selection.selected
        )
        if progressive.should_escalate(
            selected_count=len(selection.selected),
            missing_facets=selection.missing_facets,
            top_relevance=top,
            second_relevance=second,
            optional_facets_available=cap > stage_a_cap,
            missing_evidence_requirements=missing_evidence_requirements,
        ):
            stages.append("stage-b")
            if selection.missing_facets:
                escalation_reasons.append("missing-required-facet")
            escalation_reasons.extend(
                f"missing-{requirement}" for requirement in missing_evidence_requirements
            )
            if not selection.selected:
                escalation_reasons.append("no-eligible-evidence")
            elif top is not None and top < progressive.minimum_relevance:
                escalation_reasons.append("low-top-relevance")
            if top is not None and second is not None and top - second < progressive.minimum_margin:
                escalation_reasons.append("uncertain-score-margin")

            stage_b_facets = tuple(dict.fromkeys((*required_facets, *optional_facets)))
            stage_b_limit = max(
                per_channel_limit + 1,
                math.ceil(cap / max(1, len(stage_b_facets) * 2)),
            )
            stage_b_routes = routes
            if self.store is not None and workspace_id:
                try:
                    stage_b_routes = self.store.route_book_knowledge(
                        workspace_id,
                        query,
                        limit=cap,
                        allowed_segment_ids=allowed_document_ids,
                        expand_sections=True,
                        global_query="global" in plan.reasons,
                        include_adjacency=True,
                    )
                except Exception:
                    route_notes.append("book_graph_stage_b=degraded")
            stage_b_batch = await self._search_facets(
                database,
                stage_b_facets,
                stage_b_limit,
                document_filter=document_filter,
                hydrate_chunk_ids=[
                    str(item["chunk_id"]) for item in stage_b_routes if item.get("chunk_id")
                ],
            )
            absorb_searches(stage_b_batch, stage_b_facets)
            for path in [path for path in rankings if path.startswith("book-")]:
                rankings.pop(path, None)
                weights.pop(path, None)
            absorb_routes(stage_b_routes, stage_b_batch.hydrated_chunks)
            if stage_b_batch.hydration_failed:
                route_notes.append(
                    "book_router_hydration_stage_b=degraded"
                    + (
                        f"; {stage_b_batch.hydration_reason}"
                        if stage_b_batch.hydration_reason
                        else ""
                    )
                )
            fused = weighted_rrf(rankings, weights, limit=cap)
            new_items = [
                item for item in fused if item.candidate.chunk_id not in raw_score_by_chunk
            ]
            try:
                new_scores = await self.adapter.rerank(
                    database,
                    query,
                    [hit_by_chunk[item.candidate.chunk_id] for item in new_items],
                )
                if len(new_scores) != len(new_items):
                    raise RuntimeError("reranker score count mismatch")
                raw_score_by_chunk.update(
                    {
                        item.candidate.chunk_id: score
                        for item, score in zip(new_items, new_scores, strict=True)
                    }
                )
                selection = adaptive_select(
                    [
                        (item.candidate, raw_score_by_chunk[item.candidate.chunk_id])
                        for item in fused
                    ],
                    complexity=plan.complexity,
                    evidence_mode=EvidenceMode.NORMAL.value,
                    required_facets=(facet.id for facet in required_facets),
                    max_candidates=profile_limit,
                    budget=profile_budget,
                    reranker_digest=effective_reranker_digest,
                )
            except Exception as exc:  # noqa: BLE001 - degrade safely, but say why
                route_notes.append(
                    f"progressive_reranker=degraded; {type(exc).__name__}: {exc}"[:180]
                )

        search_ms = (time.perf_counter() - search_started) * 1000
        rerank_ms = (time.perf_counter() - rerank_started) * 1000
        limit = min(requested_limit, max_sources or profile_limit, profile_limit)
        selected = list(selection.selected[:limit])
        ranked = [
            hit_by_chunk[item.candidate.chunk_id].model_copy(
                update={
                    "score": item.relevance,
                    "metadata": {
                        **hit_by_chunk[item.candidate.chunk_id].metadata,
                        "rerank_score": item.raw_score,
                        "relevance_score": item.relevance,
                    },
                }
            )
            for item in selected
        ]
        explanation = RetrievalExplanation(
            query=query,
            candidates=[hit_by_chunk[item.candidate.chunk_id] for item in fused],
            ranked=ranked,
            timing=RetrievalTiming(
                search_ms=search_ms,
                rerank_ms=rerank_ms,
                total_ms=(time.perf_counter() - started) * 1000,
            ),
            provider_notes=[
                *self._provider_notes(
                    plan,
                    cap=cap,
                    selected=len(ranked),
                    cut=selection.cutoff_reason,
                    degraded_facets=degraded_facets,
                    stages=tuple(stages),
                    escalation_reasons=tuple(escalation_reasons),
                ),
                *route_notes,
            ],
        )
        return ranked, explanation

    def _workspace_id(self, database: Path) -> str | None:
        if self.store is None:
            return None
        target = database.resolve()
        for workspace in self.store.list_workspaces():
            workspace_path = Path(workspace.path).resolve()
            if target == workspace_path / "database" / "knowledge.lancedb":
                return workspace.id
        return None

    @staticmethod
    def _provider_notes(
        plan: Any,
        *,
        cap: int,
        selected: int,
        cut: str,
        degraded_facets: list[str],
        stages: tuple[str, ...] = ("stage-a",),
        escalation_reasons: tuple[str, ...] = (),
    ) -> list[str]:
        facet_summary = "|".join(f"{facet.id}:{facet.query}" for facet in plan.facets)
        return [
            "Only the documented public API of Haiku is used for search and evidence hydration.",
            f"complexity={plan.complexity.value}",
            f"candidate_cap={cap}",
            f"selected={selected}",
            f"cut={cut}",
            f"retrieval_stages={'|'.join(stages)}",
            f"escalation_reasons={'|'.join(escalation_reasons) or 'none'}",
            f"facets={facet_summary}",
            *[f"facet_failed={facet}" for facet in degraded_facets],
        ]

    @staticmethod
    def _candidate(hit: SearchHit, facet_id: str) -> RetrievalCandidate:
        metadata: dict[str, Any] = hit.metadata
        document_meta = metadata.get("document_meta") or {}
        try:
            book = BookMetadata.model_validate(document_meta.get("book_metadata"))
        except (TypeError, ValueError):
            book = None
        raw_kind = str(metadata.get("evidence_kind") or "unknown").casefold()
        try:
            evidence_kind = EvidenceKind(raw_kind)
        except ValueError:
            labels = {str(item).casefold() for item in metadata.get("labels", [])}
            evidence_kind = (
                EvidenceKind.TABLE
                if labels & {"table", "table_item"}
                else EvidenceKind.FORMULA
                if labels & {"formula", "equation"}
                else EvidenceKind.FIGURE
                if labels & {"picture", "figure", "image", "diagram", "chart"}
                else EvidenceKind.UNKNOWN
            )
        raw_provenance = str(metadata.get("provenance_kind") or "unknown").casefold()
        try:
            provenance_kind = ProvenanceKind(raw_provenance)
        except ValueError:
            provenance_kind = ProvenanceKind.UNKNOWN
        return RetrievalCandidate(
            chunk_id=hit.chunk_id,
            content=hit.content,
            document_id=hit.document_id,
            document_title=hit.document_title,
            source_uri=str(metadata.get("source_uri") or "") or None,
            logical_document_id=str(metadata.get("logical_document_id") or hit.document_id or "")
            or None,
            section_id=str(metadata.get("section_node_id") or "") or None,
            pages=tuple(hit.pages),
            headings=tuple(str(item) for item in metadata.get("headings", [])),
            facet_ids=(facet_id,),
            content_hash=str(metadata.get("content_hash") or "") or None,
            evidence_id=str(metadata.get("evidence_id") or "") or None,
            element_types=tuple(str(item) for item in metadata.get("labels", [])),
            doc_item_refs=tuple(str(item) for item in metadata.get("doc_item_refs", [])),
            book=book,
            evidence_kind=evidence_kind,
            provenance_kind=provenance_kind,
            quality_flags=tuple(str(item) for item in metadata.get("quality_flags", [])),
        )

    async def _search_facets(
        self,
        database: Path,
        facets: tuple[Any, ...],
        limit: int,
        *,
        document_filter: str | None,
        hydrate_chunk_ids: list[str],
    ) -> _FacetSearchBatch:
        requests = [
            SearchManyRequest(
                key=f"{facet.id}:{channel}",
                query=facet.query,
                limit=limit,
                document_filter=document_filter,
                search_type=channel,
                rerank=False,
            )
            for facet in facets
            for channel in ("fts", "vector")
        ]
        search_many = getattr(self.adapter, "search_many", None)
        if callable(search_many):
            try:
                result = await search_many(
                    database,
                    requests,
                    hydrate_chunk_ids=hydrate_chunk_ids,
                )
                if not isinstance(result, SearchManyResult):
                    raise TypeError("search_many must return SearchManyResult")
                items = {item.key: item for item in result.items}
                rows: dict[str, Any] = {}
                for request in requests:
                    item = items.get(request.key)
                    if item is None:
                        rows[request.key] = RuntimeError(
                            f"search_many omitted request {request.key}"
                        )
                    elif item.failure is not None:
                        rows[request.key] = RuntimeError(item.failure.message)
                    else:
                        rows[request.key] = item.hits
                failure = result.hydration_failure
                return _FacetSearchBatch(
                    rows=rows,
                    hydrated_chunks=result.hydrated_chunks,
                    hydration_failed=failure is not None,
                    hydration_reason=(
                        f"{failure.code}: {failure.message}"[:160] if failure else None
                    ),
                )
            except (AttributeError, NotImplementedError):
                pass
        return _FacetSearchBatch(
            rows=dict(
                zip(
                    (request.key for request in requests),
                    await asyncio.gather(
                        *(
                            self.adapter.search(
                                database,
                                request.query,
                                request.limit,
                                document_filter=request.document_filter,
                                search_type=request.search_type,
                                rerank=False,
                            )
                            for request in requests
                        ),
                        return_exceptions=True,
                    ),
                    strict=True,
                )
            ),
            hydrated_chunks=(
                await self.adapter.get_chunks(database, hydrate_chunk_ids)
                if hydrate_chunk_ids
                else []
            ),
        )

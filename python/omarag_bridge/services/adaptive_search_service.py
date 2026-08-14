from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapters.base import HaikuAdapter
from ..models.domain import (
    BookMetadata,
    EvidenceMode,
    RetrievalExplanation,
    RetrievalTiming,
    SearchHit,
)
from ..store import StateStore
from .query_v2 import (
    QueryComplexity,
    RetrievalCandidate,
    adaptive_select,
    classify_query,
    weighted_rrf,
)


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
    ) -> tuple[list[SearchHit], RetrievalExplanation]:
        started = time.perf_counter()
        plan = classify_query(query)
        cap = (
            min(plan.budget.candidate_cap, 24)
            if profile == "fast"
            else min(plan.budget.candidate_cap, 40)
            if profile == "balanced"
            else min(72, max(plan.budget.candidate_cap, 40))
            if profile == "deep"
            else plan.budget.candidate_cap
        )
        per_facet_limit = max(8, math.ceil(cap / max(1, len(plan.facets))))
        jobs = [
            self.adapter.search(
                database,
                facet.query,
                per_facet_limit,
                document_filter=document_filter,
                search_type="hybrid",
                rerank=False,
            )
            for facet in plan.facets
        ]
        search_started = time.perf_counter()
        rows = await asyncio.gather(*jobs, return_exceptions=True)
        search_ms = (time.perf_counter() - search_started) * 1000
        hit_by_chunk: dict[str, SearchHit] = {}
        rankings: dict[str, list[RetrievalCandidate]] = {}
        degraded_facets: list[str] = []
        for facet, hits in zip(plan.facets, rows, strict=True):
            if isinstance(hits, BaseException):
                degraded_facets.append(facet.id)
                continue
            rankings[f"facet:{facet.id}"] = []
            for hit in hits:
                hit_by_chunk.setdefault(hit.chunk_id, hit)
                rankings[f"facet:{facet.id}"].append(self._candidate(hit, facet.id))
        fused = weighted_rrf(rankings, limit=cap)
        route_notes: list[str] = []
        if self.store is not None:
            workspace_id = self._workspace_id(database)
            if workspace_id:
                try:
                    routes = self.store.route_book_knowledge(workspace_id, query, limit=cap)
                    route_ids = [str(item["chunk_id"]) for item in routes if item.get("chunk_id")]
                    routed_hits = {
                        hit.chunk_id: hit
                        for hit in await self.adapter.get_chunks(database, route_ids)
                        if allowed_document_ids is None or hit.document_id in allowed_document_ids
                    }
                    route_rankings: dict[str, list[RetrievalCandidate]] = {}
                    for route in routes:
                        hit = routed_hits.get(str(route.get("chunk_id") or ""))
                        if hit is None:
                            continue
                        path = str(route.get("retrieval_path") or "book-term")
                        candidate = self._candidate(hit, "F1")
                        route_rankings.setdefault(path, []).append(candidate)
                        hit_by_chunk.setdefault(hit.chunk_id, hit)
                    if route_rankings:
                        rankings.update(route_rankings)
                        fused = weighted_rrf(
                            rankings,
                            {path: (1.1 if path.startswith("book-") else 1.0) for path in rankings},
                            limit=cap,
                        )
                        route_notes.append(f"book_router={sum(map(len, route_rankings.values()))}")
                except Exception:
                    route_notes.append("book_router=degraded")
        rerank_started = time.perf_counter()
        rerank_hits = [hit_by_chunk[item.candidate.chunk_id] for item in fused]
        try:
            scores = await self.adapter.rerank(database, query, rerank_hits)
            if len(scores) != len(fused):
                raise RuntimeError("reranker score count mismatch")
        except Exception:
            scores = []
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
            )
            notes.insert(
                1,
                "reranker=degraded; scores are uncalibrated and no relevance is claimed",
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
        profile_limit = (
            5
            if profile == "fast"
            else min(8, plan.budget.final_max)
            if profile == "balanced"
            else min(14, max(8, plan.budget.final_max))
            if profile == "deep"
            else plan.budget.final_max
        )
        selection_complexity = plan.complexity
        if profile == "deep":
            selection_complexity = {
                QueryComplexity.SIMPLE: QueryComplexity.STANDARD,
                QueryComplexity.STANDARD: QueryComplexity.COMPLEX,
                QueryComplexity.COMPLEX: QueryComplexity.COMPLEX,
            }[plan.complexity]
        elif profile == "balanced" and plan.complexity is QueryComplexity.COMPLEX:
            selection_complexity = QueryComplexity.STANDARD
        selection = adaptive_select(
            [(item.candidate, score) for item, score in zip(fused, scores, strict=True)],
            complexity=selection_complexity,
            evidence_mode=EvidenceMode.NORMAL.value,
            required_facets=(facet.id for facet in plan.facets),
            max_candidates=profile_limit,
        )
        rerank_ms = (time.perf_counter() - rerank_started) * 1000
        limit = min(
            requested_limit,
            max_sources or profile_limit,
            profile_limit,
        )
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
            if target == workspace_path / "knowledge.lancedb":
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
    ) -> list[str]:
        facet_summary = "|".join(f"{facet.id}:{facet.query}" for facet in plan.facets)
        return [
            "Only the documented public API of Haiku is used for search and evidence hydration.",
            f"complexity={plan.complexity.value}",
            f"candidate_cap={cap}",
            f"selected={selected}",
            f"cut={cut}",
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
        )

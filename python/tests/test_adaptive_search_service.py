from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omarag_bridge.adapters.base import SearchManyItem, SearchManyResult, SearchManyStats
from omarag_bridge.models.domain import SearchHit
from omarag_bridge.services.adaptive_search_service import AdaptiveSearchService


class DegradedRerankerAdapter:
    async def search(
        self, _database: Path, query: str, *_args: Any, **_kwargs: Any
    ) -> list[SearchHit]:
        return [
            SearchHit(
                chunk_id=f"chunk-{query.casefold()}",
                content=f"Fachinformation über {query}.",
                pages=[1],
                document_id="book-1",
            )
        ]

    async def rerank(self, *_args: Any, **_kwargs: Any) -> list[float]:
        raise RuntimeError("reranker unavailable")


def test_book_router_resolves_the_real_workspace_database_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    store = SimpleNamespace(
        list_workspaces=lambda: [SimpleNamespace(id="ws-1", path=str(workspace))]
    )
    service = AdaptiveSearchService(DegradedRerankerAdapter(), store)

    assert service._workspace_id(workspace / "database" / "knowledge.lancedb") == "ws-1"


@pytest.mark.asyncio
async def test_degraded_search_explanation_keeps_the_complete_plan() -> None:
    ranked, explanation = await AdaptiveSearchService(DegradedRerankerAdapter()).search(
        Path("/tmp/db"),
        "Vergleiche Beton und Stahl.",
        requested_limit=8,
    )

    assert len(ranked) == 2
    notes = explanation.provider_notes
    assert "complexity=standard" in notes
    assert "candidate_cap=40" in notes
    assert "selected=2" in notes
    assert "cut=reranker_degraded_uncalibrated" in notes
    assert "facets=F1:Beton|F2:Stahl" in notes
    assert any(note.startswith("reranker=degraded") for note in notes)


@pytest.mark.asyncio
async def test_search_prefers_search_many_for_facet_retrieval() -> None:
    class Batched(DegradedRerankerAdapter):
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        async def search_many(self, _database, requests, **_kwargs):
            self.calls.append(tuple(requests))
            return SearchManyResult(
                items=[
                    SearchManyItem(
                        key=request.key,
                        hits=await self.search(None, request.query),
                    )
                    for request in requests
                ],
                hydrated_chunks=[],
                stats=SearchManyStats(
                    search_requests=len(requests),
                    successful_searches=len(requests),
                ),
            )

    adapter = Batched()
    await AdaptiveSearchService(adapter).search(
        Path("/tmp/db"),
        "Vergleiche Beton und Stahl.",
        requested_limit=8,
    )

    assert [request.query for request in adapter.calls[0]] == [
        "Beton",
        "Beton",
        "Stahl",
        "Stahl",
    ]
    assert [request.search_type for request in adapter.calls[0]] == [
        "fts",
        "vector",
        "fts",
        "vector",
    ]
    assert all(request.rerank is False for request in adapter.calls[0])


@pytest.mark.asyncio
async def test_search_routes_with_filters_and_global_graph_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    class RecordingStore:
        def __init__(self) -> None:
            self.options: dict[str, Any] = {}

        @staticmethod
        def list_workspaces():
            return [SimpleNamespace(id="ws-1", path=str(workspace))]

        def route_book_knowledge(self, *_args: Any, **kwargs: Any):
            self.options = kwargs
            return []

    store = RecordingStore()
    service = AdaptiveSearchService(DegradedRerankerAdapter(), store)
    await service.search(
        workspace / "database" / "knowledge.lancedb",
        "Gib einen Gesamtüberblick über das Buch.",
        requested_limit=4,
        allowed_document_ids={"segment-a"},
    )

    assert store.options["allowed_segment_ids"] == {"segment-a"}
    assert store.options["expand_sections"] is True
    assert store.options["global_query"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rerank_score", "expected_calls", "expected_stages"),
    [
        (3.0, 1, "retrieval_stages=stage-a"),
        (-3.0, 2, "retrieval_stages=stage-a|stage-b"),
    ],
)
async def test_progressive_search_stops_or_expands_from_calibrated_evidence(
    rerank_score: float,
    expected_calls: int,
    expected_stages: str,
) -> None:
    class ProgressiveAdapter(DegradedRerankerAdapter):
        def __init__(self) -> None:
            self.batch_calls: list[tuple[object, ...]] = []

        async def search_many(self, _database, requests, **_kwargs):
            self.batch_calls.append(tuple(requests))
            shared = SearchHit(
                chunk_id="shared-evidence",
                content="Der geprüfte Fachbeleg.",
                pages=[1],
                document_id="book-1",
            )
            return SearchManyResult(
                items=[SearchManyItem(key=request.key, hits=[shared]) for request in requests],
                hydrated_chunks=[],
                stats=SearchManyStats(
                    search_requests=len(requests),
                    successful_searches=len(requests),
                ),
            )

        async def rerank(self, _database, _query, candidates):
            return [rerank_score] * len(candidates)

    adapter = ProgressiveAdapter()
    _ranked, explanation = await AdaptiveSearchService(adapter).search(
        Path("/tmp/db"),
        "Was ist Kriechen?",
        requested_limit=4,
    )

    assert len(adapter.batch_calls) == expected_calls
    assert expected_stages in explanation.provider_notes


@pytest.mark.asyncio
async def test_custom_reranker_never_receives_default_calibrated_scores() -> None:
    class CustomAdapter(DegradedRerankerAdapter):
        async def rerank(self, _database, _query, candidates):
            return [5.0] * len(candidates)

    ranked, explanation = await AdaptiveSearchService(CustomAdapter()).search(
        Path("/tmp/db"),
        "Was ist Kriechen?",
        requested_limit=4,
        reranker_digest="custom-reranker-without-gold-calibration",
    )

    assert ranked == []
    assert "cut=calibration_mismatch" in explanation.provider_notes


@pytest.mark.asyncio
async def test_a_degraded_reranker_reports_why_it_failed() -> None:
    """Swallowing the reason makes an intermittent fault undiagnosable.

    Retrieval degrades safely -- no relevance is claimed -- but a bare
    ``except Exception`` discarded the cause, so a search that silently
    returned nothing on one call and twelve candidates on the next left
    nothing to investigate.  The exception type is the diagnostic value:
    a broken pipe means the query worker died, a timeout means it was too
    slow, and the two need opposite fixes.
    """

    _ranked, explanation = await AdaptiveSearchService(DegradedRerankerAdapter()).search(
        Path("/tmp/db"),
        "Vergleiche Beton und Stahl.",
        requested_limit=8,
    )

    note = next(note for note in explanation.provider_notes if note.startswith("reranker=degraded"))
    assert "RuntimeError" in note, note

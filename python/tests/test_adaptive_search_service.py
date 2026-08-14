from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

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

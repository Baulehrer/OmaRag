from __future__ import annotations

import pytest

from omarag_bridge.services.query_v2 import FusedCandidate, RetrievalCandidate
from omarag_bridge.services.reranker_service import PersistentCrossEncoder


async def test_persistent_reranker_loads_once_and_keeps_breadcrumbs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[list[str]]] = []

    class FakeModel:
        def predict(self, pairs, **_options):
            calls.append(pairs)
            return [2.0 for _ in pairs]

    reranker = PersistentCrossEncoder(model_name="test/model")
    monkeypatch.setattr(reranker, "_load", lambda: FakeModel())
    candidate = RetrievalCandidate(
        chunk_id="chunk-1",
        content="Der Wasserzementwert ist maßgebend.",
        headings=("Beton", "Zusammensetzung"),
    )
    fused = FusedCandidate(candidate, 0.1, (("hybrid", 1),), ("hybrid",))

    first = await reranker.score("Wasserzementwert", [fused])
    second = await reranker.score("Wasserzementwert", [fused])

    assert first[0][1] == 2.0
    assert second[0][1] == 2.0
    assert reranker.loaded is True
    assert calls[0][0][1].startswith("Beton › Zusammensetzung\n")

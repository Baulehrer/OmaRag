from __future__ import annotations

import pickle
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omarag_bridge.adapters.base import (
    HaikuAdapter,
    SearchManyItem,
    SearchManyRequest,
    SearchManyResult,
    SearchManyStats,
)
from omarag_bridge.adapters.haiku_v070 import VanillaHaikuAdapter
from omarag_bridge.models.domain import SearchHit


def test_search_many_protocol_is_worker_pickle_safe() -> None:
    request = SearchManyRequest(key="F1", query="question", limit=8, rerank=False)
    result = SearchManyResult(
        items=[
            SearchManyItem(
                key="F1",
                hits=[SearchHit(chunk_id="chunk-1", content="evidence")],
            )
        ],
        hydrated_chunks=[],
        stats=SearchManyStats(search_requests=1, successful_searches=1),
    )

    restored_request = pickle.loads(pickle.dumps(request))
    restored_result = pickle.loads(pickle.dumps(result))

    assert restored_request == request
    assert restored_result == result


@pytest.mark.asyncio
async def test_base_search_many_fallback_preserves_partial_results_and_deduplicates(
    tmp_path: Path,
) -> None:
    class LegacyAdapter:
        def __init__(self) -> None:
            self.hydration_ids: list[str] = []

        async def search(
            self, _database: Path, query: str, _limit: int, **_kwargs: Any
        ) -> list[SearchHit]:
            if query == "broken":
                raise RuntimeError("facet failed")
            return [SearchHit(chunk_id=f"chunk-{query}", content=query)]

        async def get_chunks(self, _database: Path, chunk_ids: list[str]) -> list[SearchHit]:
            self.hydration_ids = chunk_ids
            return [SearchHit(chunk_id=chunk_id, content=chunk_id) for chunk_id in chunk_ids]

    adapter = LegacyAdapter()
    result = await HaikuAdapter.search_many(
        adapter,  # type: ignore[arg-type]
        tmp_path / "knowledge.lancedb",
        [
            SearchManyRequest(key="good", query="working", limit=4),
            SearchManyRequest(key="bad", query="broken", limit=4),
        ],
        hydrate_chunk_ids=["route-1", "route-1", "route-2"],
    )

    assert [hit.chunk_id for hit in result.hits_for("good")] == ["chunk-working"]
    assert result.hits_for("bad") == []
    assert result.items[1].failure is not None
    assert result.items[1].failure.code == "RuntimeError"
    assert adapter.hydration_ids == ["route-1", "route-2"]
    assert result.stats.native_batch is False
    assert result.stats.chunk_hydrations_saved == 1


@pytest.mark.asyncio
async def test_compact_search_many_shape_remains_iterable_for_v11_callers(
    tmp_path: Path,
) -> None:
    class LegacyAdapter:
        async def search(
            self, _database: Path, query: str, limit: int, **kwargs: Any
        ) -> list[SearchHit]:
            assert limit == 6
            assert kwargs["rerank"] is False
            if query == "broken":
                raise ValueError("bad facet")
            return [SearchHit(chunk_id=query, content=query)]

        async def get_chunks(self, _database: Path, _chunk_ids: list[str]) -> list[SearchHit]:
            return []

    result = await HaikuAdapter.search_many(
        LegacyAdapter(),  # type: ignore[arg-type]
        tmp_path / "knowledge.lancedb",
        ["working", "broken"],
        6,
        rerank=False,
    )
    compact = list(result)

    assert [hit.chunk_id for hit in compact[0]] == ["working"]
    assert isinstance(compact[1], BaseException)


@pytest.mark.asyncio
async def test_vanilla_search_many_uses_one_client_and_request_scoped_hydration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: Counter[str] = Counter()
    search_options: list[dict[str, Any]] = []
    chunks = {
        "shared": {
            "id": "shared",
            "content": "stored shared",
            "document_id": "doc-1",
            "metadata": {"page_numbers": [2], "citation_headings": ["Shared"]},
        },
        "only-1": {
            "id": "only-1",
            "content": "stored only",
            "document_id": "doc-1",
            "metadata": {"page_numbers": [3]},
        },
        "route": {
            "id": "route",
            "content": "routed evidence",
            "document_id": "doc-1",
            "metadata": {"page_numbers": [7]},
        },
    }

    class PublicClient:
        async def search(
            self,
            query: str,
            *,
            limit: int,
            search_type: str,
            filter: str | None,
            include_images: bool,
        ) -> list[dict[str, Any]]:
            calls["search"] += 1
            search_options.append(
                {
                    "limit": limit,
                    "search_type": search_type,
                    "filter": filter,
                    "include_images": include_images,
                }
            )
            if query == "first":
                return [
                    {
                        "chunk_id": "shared",
                        "content": "first shared",
                        "score": 0.9,
                        "document_id": "doc-1",
                    },
                    {
                        "chunk_id": "only-1",
                        "content": "first only",
                        "score": 0.8,
                        "document_id": "doc-1",
                    },
                ]
            return [
                {
                    "chunk_id": "shared",
                    "content": "second shared",
                    "score": 0.7,
                    "document_id": "doc-1",
                }
            ]

        async def get_chunk_by_id(self, chunk_id: str) -> dict[str, Any] | None:
            calls[f"chunk:{chunk_id}"] += 1
            return chunks.get(chunk_id)

        async def get_document_by_id(self, document_id: str) -> dict[str, Any]:
            calls[f"document:{document_id}"] += 1
            return {
                "id": document_id,
                "title": "The Book",
                "uri": "book.pdf",
                "metadata": {"logical_document_id": "logical-1"},
            }

    public_client = PublicClient()

    class ClientContext:
        async def __aenter__(self) -> PublicClient:
            calls["client_enter"] += 1
            return public_client

        async def __aexit__(self, *_args: object) -> None:
            calls["client_exit"] += 1

    adapter = object.__new__(VanillaHaikuAdapter)
    adapter._available = True

    async def ensure_database(_database: Path) -> None:
        calls["ensure_database"] += 1

    config = SimpleNamespace(reranking=SimpleNamespace(model="configured"))
    monkeypatch.setattr(adapter, "ensure_database", ensure_database)
    monkeypatch.setattr(adapter, "_config", lambda _database: config)
    monkeypatch.setattr(
        adapter,
        "_client",
        lambda _database, **_kwargs: ClientContext(),
    )

    result = await adapter.search_many(
        tmp_path / "knowledge.lancedb",
        [
            SearchManyRequest(
                key="F1",
                query="first",
                limit=8,
                document_filter="id = 'doc-1'",
                rerank=False,
            ),
            SearchManyRequest(key="F2", query="second", limit=4, rerank=False),
        ],
        hydrate_chunk_ids=["shared", "route", "route"],
    )

    assert calls["client_enter"] == calls["client_exit"] == 1
    assert calls["search"] == 2
    assert calls["chunk:shared"] == 1
    assert calls["chunk:only-1"] == 1
    assert calls["chunk:route"] == 1
    assert calls["document:doc-1"] == 1
    assert search_options[0] == {
        "limit": 8,
        "search_type": "hybrid",
        "filter": "id = 'doc-1'",
        "include_images": False,
    }
    assert [hit.chunk_id for hit in result.hits_for("F1")] == ["shared", "only-1"]
    assert [hit.chunk_id for hit in result.hydrated_chunks] == ["shared", "route"]
    assert result.hydrated_chunks[1].document_title == "The Book"
    assert result.stats.backend_sessions == 1
    assert result.stats.native_batch is True
    assert result.stats.requested_chunk_hydrations == 5
    assert result.stats.unique_chunk_hydrations == 3
    assert result.stats.chunk_hydrations_saved == 2
    assert result.stats.requested_document_hydrations == 3
    assert result.stats.unique_document_hydrations == 1
    assert result.stats.document_hydrations_saved == 2


@pytest.mark.asyncio
async def test_vanilla_search_many_falls_back_for_mixed_rerank_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = object.__new__(VanillaHaikuAdapter)
    search_calls: list[tuple[str, bool]] = []

    async def search(
        _database: Path,
        query: str,
        _limit: int,
        *,
        rerank: bool,
        **_kwargs: Any,
    ) -> list[SearchHit]:
        search_calls.append((query, rerank))
        return [SearchHit(chunk_id=query, content=query)]

    async def get_chunks(_database: Path, _ids: list[str]) -> list[SearchHit]:
        return []

    monkeypatch.setattr(adapter, "search", search)
    monkeypatch.setattr(adapter, "get_chunks", get_chunks)

    result = await adapter.search_many(
        tmp_path / "knowledge.lancedb",
        [
            SearchManyRequest(key="fast", query="fast", limit=3, rerank=False),
            SearchManyRequest(key="ranked", query="ranked", limit=3, rerank=True),
        ],
    )

    assert search_calls == [("fast", False), ("ranked", True)]
    assert result.stats.native_batch is False
    assert result.stats.backend_sessions == 2
    assert result.stats.fallback_reason == "mixed_rerank_settings"


@pytest.mark.asyncio
async def test_vanilla_search_many_contains_batch_setup_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = object.__new__(VanillaHaikuAdapter)

    async def ensure_database(_database: Path) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(adapter, "ensure_database", ensure_database)
    result = await adapter.search_many(
        tmp_path / "knowledge.lancedb",
        [SearchManyRequest(key="F1", query="question", limit=4, rerank=False)],
        hydrate_chunk_ids=["route"],
    )

    assert result.items[0].failure is not None
    assert result.items[0].failure.code == "RuntimeError"
    assert result.hydration_failure is not None
    assert result.stats.fallback_reason == "batch_request_failed"

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ..models.domain import BookMetadata, CapabilitySet, Citation, EvidenceMode, SearchHit


@dataclass(frozen=True, slots=True)
class SearchManyRequest:
    """One independently configurable search in a request-scoped batch."""

    key: str
    query: str
    limit: int
    document_filter: str | None = None
    search_type: str = "hybrid"
    rerank: bool = True

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("search batch keys must not be empty")
        if self.limit < 1:
            raise ValueError("search batch limits must be positive")


@dataclass(frozen=True, slots=True)
class SearchManyFailure:
    """Pickle-safe partial failure returned by a batched search."""

    code: str
    message: str
    retryable: bool = False

    @classmethod
    def from_exception(cls, exc: Exception) -> SearchManyFailure:
        return cls(
            code=str(getattr(exc, "code", type(exc).__name__)),
            message=str(getattr(exc, "message", str(exc))),
            retryable=bool(getattr(exc, "retryable", False)),
        )


@dataclass(frozen=True, slots=True)
class SearchManyItem:
    """The result for one key; failures do not discard successful siblings."""

    key: str
    hits: list[SearchHit]
    failure: SearchManyFailure | None = None


class SearchManyCompatError(RuntimeError):
    """Exception view used by callers of the compact V1.1 batch shape."""

    def __init__(self, failure: SearchManyFailure) -> None:
        super().__init__(failure.message)
        self.code = failure.code
        self.retryable = failure.retryable


@dataclass(frozen=True, slots=True)
class SearchManyStats:
    """Request-local counters used to verify batching and hydration savings."""

    search_requests: int = 0
    successful_searches: int = 0
    result_rows: int = 0
    requested_chunk_hydrations: int = 0
    unique_chunk_hydrations: int = 0
    chunk_hydrations_saved: int = 0
    requested_document_hydrations: int = 0
    unique_document_hydrations: int = 0
    document_hydrations_saved: int = 0
    backend_sessions: int | None = None
    ipc_round_trips: int = 0
    ipc_round_trips_saved: int = 0
    native_batch: bool = False
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SearchManyResult:
    """Search results plus chunks explicitly hydrated for the same request."""

    items: list[SearchManyItem]
    hydrated_chunks: list[SearchHit]
    stats: SearchManyStats
    hydration_failure: SearchManyFailure | None = None

    def hits_for(self, key: str) -> list[SearchHit]:
        for item in self.items:
            if item.key == key:
                return item.hits
        return []

    def __iter__(self) -> Iterator[list[SearchHit] | BaseException]:
        """Expose the compact list-of-results shape used by V1.1 callers."""
        for item in self.items:
            if item.failure is not None:
                yield SearchManyCompatError(item.failure)
            else:
                yield item.hits


def normalize_search_many_requests(
    requests: list[SearchManyRequest] | list[str],
    limit: int | None = None,
    *,
    document_filter: str | None = None,
    search_type: str = "hybrid",
    rerank: bool = True,
) -> list[SearchManyRequest]:
    """Normalize the compact V1.1 call into the request-scoped V1.2 shape."""
    if not requests:
        return []
    if all(isinstance(request, SearchManyRequest) for request in requests):
        if limit is not None:
            raise TypeError("limit belongs to each SearchManyRequest in the V1.2 batch shape")
        normalized = list(cast(list[SearchManyRequest], requests))
        keys = [request.key for request in normalized]
        if len(keys) != len(set(keys)):
            raise ValueError("search_many request keys must be unique")
        return normalized
    if not all(isinstance(request, str) for request in requests):
        raise TypeError("search_many requests must be all strings or all SearchManyRequest values")
    if limit is None:
        raise TypeError("limit is required when search_many receives query strings")
    return [
        SearchManyRequest(
            key=f"query:{index}",
            query=request,
            limit=limit,
            document_filter=document_filter,
            search_type=search_type,
            rerank=rerank,
        )
        for index, request in enumerate(requests)
    ]


class HaikuAdapter(ABC):
    name: str
    version: str | None
    capabilities: CapabilitySet

    @property
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    async def ensure_database(self, database: Path) -> None: ...

    async def warm(self, database: Path) -> None:
        """Prepare the lightweight query runtime without answering a question."""
        await self.ensure_database(database)

    async def citation_details(self, database: Path, citation: Citation) -> Citation:
        """Resolve expensive page anchors only when a client asks for them."""
        return citation

    @abstractmethod
    async def ingest(
        self,
        database: Path,
        source: str,
        *,
        parser_id: str = "auto",
        processing_profile: str = "default",
        segment_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
        before_segment: Callable[[int, int, int], Awaitable[bool]] | None = None,
        generation_id: str | None = None,
        document_fingerprint: str | None = None,
        resume_segments: list[dict[str, Any]] | None = None,
        on_segment: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_phase: Callable[[str, int, int, int], Awaitable[None]] | None = None,
        segment_sizer: Callable[[int, bool], int] | None = None,
        metadata: BookMetadata | None = None,
        original_source: str | None = None,
        indexing_options: dict[str, Any] | None = None,
        llm_url: str | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def delete_document(self, database: Path, document_id: str) -> bool: ...

    @abstractmethod
    async def search(
        self,
        database: Path,
        query: str,
        limit: int,
        *,
        document_filter: str | None = None,
        search_type: str = "hybrid",
        rerank: bool = True,
    ) -> list[SearchHit]: ...

    @abstractmethod
    async def get_chunk(self, database: Path, chunk_id: str) -> SearchHit | None:
        """Resolve raw evidence through the provider's documented public API."""
        ...

    async def get_chunks(self, database: Path, chunk_ids: list[str]) -> list[SearchHit]:
        """Resolve several raw chunks; adapters may collapse this into one worker call."""
        resolved = [await self.get_chunk(database, chunk_id) for chunk_id in chunk_ids]
        return [item for item in resolved if item is not None]

    @property
    def supports_native_search_many(self) -> bool:
        """Whether ``search_many`` collapses work below this adapter boundary."""
        return False

    async def search_many(
        self,
        database: Path,
        requests: list[SearchManyRequest] | list[str],
        limit: int | None = None,
        *,
        document_filter: str | None = None,
        search_type: str = "hybrid",
        rerank: bool = True,
        hydrate_chunk_ids: list[str] | None = None,
    ) -> SearchManyResult:
        """Compatibility fallback for adapters without native request batching.

        The fallback preserves partial search results and still deduplicates the
        explicitly requested hydration IDs. Native adapters can additionally
        share their client session and hydration caches across all searches.
        """
        requests = normalize_search_many_requests(
            requests,
            limit,
            document_filter=document_filter,
            search_type=search_type,
            rerank=rerank,
        )
        hydration_ids = list(dict.fromkeys(hydrate_chunk_ids or []))

        async def execute(request: SearchManyRequest) -> SearchManyItem:
            try:
                hits = await self.search(
                    database,
                    request.query,
                    request.limit,
                    document_filter=request.document_filter,
                    search_type=request.search_type,
                    rerank=request.rerank,
                )
                return SearchManyItem(key=request.key, hits=hits)
            except Exception as exc:
                return SearchManyItem(
                    key=request.key,
                    hits=[],
                    failure=SearchManyFailure.from_exception(exc),
                )

        items = list(await asyncio.gather(*(execute(request) for request in requests)))
        hydrated_chunks: list[SearchHit] = []
        hydration_failure: SearchManyFailure | None = None
        if hydration_ids:
            try:
                hydrated_chunks = await self.get_chunks(database, hydration_ids)
            except Exception as exc:
                hydration_failure = SearchManyFailure.from_exception(exc)
        successful = sum(item.failure is None for item in items)
        stats = SearchManyStats(
            search_requests=len(requests),
            successful_searches=successful,
            result_rows=sum(len(item.hits) for item in items),
            requested_chunk_hydrations=len(hydrate_chunk_ids or []),
            unique_chunk_hydrations=len(hydration_ids),
            chunk_hydrations_saved=len(hydrate_chunk_ids or []) - len(hydration_ids),
            native_batch=False,
        )
        return SearchManyResult(
            items=items,
            hydrated_chunks=hydrated_chunks,
            stats=stats,
            hydration_failure=hydration_failure,
        )

    @abstractmethod
    async def rerank(
        self, database: Path, question: str, candidates: list[SearchHit]
    ) -> list[float]:
        """Score a bounded candidate pool in the persistent query worker."""
        ...

    @abstractmethod
    async def ask(
        self,
        database: Path,
        question: str,
        images: list[str] | None = None,
        *,
        document_filter: str | None = None,
        evidence_mode: EvidenceMode = EvidenceMode.STRICT,
    ) -> tuple[str, list[Citation]]: ...

    @abstractmethod
    async def analyze(
        self,
        database: Path,
        question: str,
        images: list[str] | None = None,
        *,
        document_filter: str | None = None,
        evidence_mode: EvidenceMode = EvidenceMode.STRICT,
    ) -> tuple[str, list[Citation]]: ...

    @abstractmethod
    async def update_document_metadata(
        self, database: Path, document_ids: list[str], metadata: dict[str, Any]
    ) -> None: ...

    @abstractmethod
    def validate_config(self, content: str) -> None: ...

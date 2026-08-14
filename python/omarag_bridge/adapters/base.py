from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from ..models.domain import BookMetadata, CapabilitySet, Citation, EvidenceMode, SearchHit


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

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from ..models.domain import CapabilitySet, Citation, SearchHit


class HaikuAdapter(ABC):
    name: str
    version: str | None
    capabilities: CapabilitySet

    @property
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    async def ensure_database(self, database: Path) -> None: ...

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
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def delete_document(self, database: Path, document_id: str) -> bool: ...

    @abstractmethod
    async def search(self, database: Path, query: str, limit: int) -> list[SearchHit]: ...

    @abstractmethod
    async def ask(
        self, database: Path, question: str, images: list[str] | None = None
    ) -> tuple[str, list[Citation]]: ...

    @abstractmethod
    async def analyze(
        self, database: Path, question: str, images: list[str] | None = None
    ) -> tuple[str, list[Citation]]: ...

    @abstractmethod
    def validate_config(self, content: str) -> None: ...

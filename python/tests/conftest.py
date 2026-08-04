from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omarag_bridge.app import create_app
from omarag_bridge.config import Settings
from omarag_bridge.models.domain import (
    CapabilitySet,
    Citation,
    HardwareInfo,
    HardwareProfile,
    ModelCatalogEntry,
    ModelCatalogResponse,
    ModelCategory,
    ModelFit,
    ModelOperationResult,
    ModelRuntime,
    ModelRuntimeResponse,
    ModelSource,
    SearchHit,
)


class FakeHaikuAdapter:
    name = "haiku-v070-test"
    version = "0.70.0"
    available = True
    capabilities = CapabilitySet(
        streaming_chat=True,
        event_replay=True,
        workspaces=True,
    )

    async def ensure_database(self, database: Path) -> None:
        database.mkdir(parents=True, exist_ok=True)

    async def ingest(self, database: Path, source: str, **options: Any) -> dict[str, Any]:
        await self.ensure_database(database)
        metadata = options.get("metadata")
        return {
            "source": source,
            "managed_source": source,
            "original_source": options.get("original_source") or source,
            "document_id": f"doc-{Path(source).stem}",
            "book_metadata": metadata.model_dump(mode="json") if metadata else None,
            "pipeline_version": "test-public-api",
        }

    async def delete_document(self, database: Path, document_id: str) -> bool:
        await self.ensure_database(database)
        return bool(document_id)

    async def search(self, database: Path, query: str, limit: int, **_: Any) -> list[SearchHit]:
        await self.ensure_database(database)
        return [
            SearchHit(
                chunk_id="chunk-1",
                content=f"Treffer fuer {query}",
                score=0.9,
                pages=[1],
            )
        ][:limit]

    async def ask(
        self, database: Path, question: str, images: list[str] | None = None, **_: Any
    ) -> tuple[str, list[Citation]]:
        await self.ensure_database(database)
        return (
            f"Antwort auf: {question}",
            [Citation(chunk_id="chunk-1", pages=[1], excerpt="Beleg")],
        )

    async def analyze(
        self, database: Path, question: str, images: list[str] | None = None, **_: Any
    ) -> tuple[str, list[Citation]]:
        await self.ensure_database(database)
        return (
            f"Analyse von: {question}",
            [Citation(chunk_id="analysis-1", pages=[2], excerpt="Analysebeleg")],
        )

    async def update_document_metadata(
        self, database: Path, document_ids: list[str], metadata: dict[str, Any]
    ) -> None:
        await self.ensure_database(database)

    def validate_config(self, content: str) -> None:
        if "embeddings:" not in content or "qa:" not in content:
            raise ValueError("invalid test configuration")


class FakeModelService:
    async def catalog(
        self,
        source: ModelSource,
        category: ModelCategory,
        query: str,
        quantization: str,
        context_tokens: int,
        profile: HardwareProfile,
    ) -> ModelCatalogResponse:
        return ModelCatalogResponse(
            entries=[
                ModelCatalogEntry(
                    id=f"test/{category.value}-2b",
                    source=source,
                    category=category,
                    description=f"{profile.value} {quantization} {context_tokens}",
                    parameter_count=2_000_000_000,
                    estimated_memory=2_000_000_000,
                    fit=ModelFit.COMFORTABLE,
                    recommended_rank=1,
                )
            ],
            hardware=HardwareInfo(memory_total=16 * 1024**3, memory_available=8 * 1024**3),
            scanned=500,
            compatible=1,
            truncated=source == ModelSource.HUGGING_FACE,
        )

    async def runtime(self) -> ModelRuntimeResponse:
        return ModelRuntimeResponse(models=[ModelRuntime(name="test/chat-2b", size=1234)])

    async def pull(self, model: str) -> AsyncIterator[bytes]:
        yield (f'{{"model":"{model}","status":"success"}}\n').encode()

    async def load(self, model: str, context_tokens: int, keep_alive: str) -> ModelOperationResult:
        return ModelOperationResult(model=model, operation="load", status="ok")

    async def unload(self, model: str) -> ModelOperationResult:
        return ModelOperationResult(model=model, operation="unload", status="ok")

    async def delete(self, model: str) -> ModelOperationResult:
        return ModelOperationResult(model=model, operation="delete", status="ok")


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    result = create_app(
        Settings(
            data_dir=tmp_path / "data",
            auth_enabled=False,
            event_poll_seconds=0.01,
            event_keepalive_seconds=0.03,
        )
    )
    adapter = FakeHaikuAdapter()
    result.state.services.adapter = adapter
    result.state.services.jobs.adapter = adapter
    result.state.services.runs.adapter = adapter
    result.state.services.features.adapter = adapter
    result.state.services.textbooks.adapter = adapter
    result.state.services.evaluations.adapter = adapter
    result.state.services.models = FakeModelService()
    return result


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as test_client:
            yield test_client


@pytest_asyncio.fixture
async def workspace(client: httpx2.AsyncClient) -> dict[str, Any]:
    response = await client.post("/v1/workspaces", json={"name": "Baustoffkunde"})
    assert response.status_code == 201
    return response.json()

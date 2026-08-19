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
    CatalogRole,
    Citation,
    HardwareBenchmark,
    HardwareClassification,
    HardwareInfo,
    HardwareProfile,
    HardwareProfileView,
    HardwareReadiness,
    HardwareTier,
    ModelAssignment,
    ModelCatalogEntry,
    ModelCatalogManifest,
    ModelCatalogResponse,
    ModelCategory,
    ModelFit,
    ModelInstallState,
    ModelOperationResult,
    ModelProfilePreflight,
    ModelResidency,
    ModelRoleRuntime,
    ModelRuntime,
    ModelRuntimeResponse,
    ModelSource,
    ModelStackRecommendation,
    PerformanceProfile,
    SearchHit,
    SimpleModelRecommendation,
)
from omarag_bridge.services.model_service import ModelService
from omarag_bridge.services.ollama_stream import OllamaModelIdentity
from omarag_bridge.services.reranker_service import (
    DEFAULT_RERANKER,
    DEFAULT_RERANKER_REVISION,
    _model_digest,
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

    def __init__(self) -> None:
        self.ask_calls = 0
        self.analyze_calls = 0
        self.ingest_calls = 0
        self.ingest_options: list[dict[str, Any]] = []

    async def ensure_database(self, database: Path) -> None:
        database.mkdir(parents=True, exist_ok=True)

    async def warm(self, database: Path) -> None:
        await self.ensure_database(database)

    async def ingest(self, database: Path, source: str, **options: Any) -> dict[str, Any]:
        self.ingest_calls += 1
        self.ingest_options.append(dict(options))
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
        self.ask_calls += 1
        await self.ensure_database(database)
        return (
            f"Antwort auf: {question}",
            [Citation(chunk_id="chunk-1", pages=[1], excerpt="Beleg")],
        )

    async def analyze(
        self, database: Path, question: str, images: list[str] | None = None, **_: Any
    ) -> tuple[str, list[Citation]]:
        self.analyze_calls += 1
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
    imported_gguf: tuple[str, str, ModelCategory, str] | None = None

    def __init__(self) -> None:
        self.installed_assignments: list[ModelAssignment] = []

    def curated_catalog(self) -> ModelCatalogManifest:
        return ModelService.curated_catalog()

    def conversion_artifacts_report(self, tokenizer: str, reranker: str | None = None):
        return ModelService.conversion_artifacts_report(tokenizer, reranker)

    def _reranker_name(self, config):
        return ModelService._reranker_name(config)

    def hardware(self, _: Path) -> HardwareInfo:
        memory = 16 * 1024**3
        return HardwareInfo(
            cpu_model="Test CPU",
            logical_cores=8,
            physical_cores=4,
            memory_total=memory,
            memory_capacity=memory,
            memory_available=12 * 1024**3,
            storage_total=512 * 1024**3,
            storage_available=256 * 1024**3,
            capacity_tier=HardwareTier.TIER_4,
            readiness_tier=HardwareTier.TIER_4,
            readiness=HardwareReadiness.READY,
        )

    async def recommend(
        self,
        profile: PerformanceProfile | HardwareProfile | str = PerformanceProfile.NORMAL,
        *,
        hardware: HardwareInfo | None = None,
        benchmark: HardwareBenchmark | None = None,
        tier_override: HardwareTier | int | None = None,
    ) -> ModelStackRecommendation:
        del hardware, benchmark
        selected_profile = ModelService.performance_profile(profile)
        tier = HardwareTier(tier_override or HardwareTier.TIER_4)
        catalog = self.curated_catalog()
        definition = next(item for item in catalog.tiers if item.tier == tier)
        artifacts = {item.id: item for item in catalog.artifacts}
        selected = [
            (CatalogRole.CHAT, definition.generator),
            (CatalogRole.VL, definition.generator),
            (CatalogRole.EMBEDDING, definition.embedding),
            (CatalogRole.RERANK, definition.reranker),
        ]
        if definition.visual_embedding is not None:
            selected.append((CatalogRole.VISUAL_EMBEDDING, definition.visual_embedding))
        assignments = [
            ModelAssignment(
                role=role,
                artifact_id=artifact_id,
                provider=artifacts[artifact_id].provider,
                model=artifacts[artifact_id].model,
                revision=artifacts[artifact_id].revision,
                digest=artifacts[artifact_id].digest,
                quantization=artifacts[artifact_id].quantization,
                install_state=ModelInstallState.INSTALLED,
                installed_digest=artifacts[artifact_id].digest,
                download_bytes=artifacts[artifact_id].download_bytes,
            )
            for role, artifact_id in selected
        ]
        return ModelStackRecommendation(
            recommendation_id=f"rec-test-{tier.value}-{selected_profile.value}",
            catalog_id=catalog.catalog_id,
            catalog_release=catalog.release,
            catalog_as_of=catalog.as_of,
            profile=selected_profile,
            classification=HardwareClassification(
                capacity_tier=tier,
                readiness_tier=tier,
                effective_tier=tier,
                readiness=HardwareReadiness.READY,
                benchmark_required=False,
            ),
            stack_tier=tier,
            assignments=assignments,
            context_tokens=min(
                definition.max_context_tokens,
                catalog.performance_profiles[selected_profile].context_ceiling_tokens,
            ),
            residency_slots=definition.residency_slots,
            retrieval_budgets=catalog.performance_profiles[selected_profile].budgets,
            ready_now=True,
            fallback_tiers=[HardwareTier(value) for value in range(tier.value - 1, 0, -1)],
        )

    async def profile_preflight(
        self,
        profile: PerformanceProfile | HardwareProfile | str,
        *,
        current_roles: dict[str, str | None] | None = None,
        current_vector_dimension: int | None = None,
        current_embedding_provider: str | None = None,
        current_embedding_digest: str | None = None,
        current_visual_embedding: str | None = None,
        hardware: HardwareInfo | None = None,
        benchmark: HardwareBenchmark | None = None,
        index_has_documents: bool = True,
    ) -> ModelProfilePreflight:
        del (
            current_embedding_digest,
            current_embedding_provider,
            current_visual_embedding,
            hardware,
            benchmark,
        )
        recommendation = await self.recommend(profile)
        roles = current_roles or {}
        expected = {
            item.role.value: item.model
            for item in recommendation.assignments
            if item.role != CatalogRole.VISUAL_EMBEDDING
        }
        changes = {role: model for role, model in expected.items() if roles.get(role) != model}
        definition = next(
            item for item in self.curated_catalog().tiers if item.tier == recommendation.stack_tier
        )
        requires_reindex = index_has_documents and bool(
            (roles.get("embedding") and roles.get("embedding") != expected["embedding"])
            or (
                current_vector_dimension
                and current_vector_dimension != definition.embedding_dimension
            )
        )
        return ModelProfilePreflight(
            recommendation=recommendation,
            changes=changes,
            requires_reindex=requires_reindex,
            can_apply=True,
        )

    async def benchmark(
        self,
        profile: PerformanceProfile | HardwareProfile | str = PerformanceProfile.NORMAL,
        *,
        tier: HardwareTier | int | None = None,
        hardware: HardwareInfo | None = None,
    ) -> HardwareBenchmark:
        del hardware
        recommendation = await self.recommend(profile, tier_override=tier)
        return HardwareBenchmark(
            tested_tier=recommendation.stack_tier,
            performance_tier=recommendation.stack_tier,
            stack_id=recommendation.recommendation_id,
            passed=True,
            not_measured=["rerank", "visual-embedding"],
        )

    def recommendation_view(
        self,
        recommendation: ModelStackRecommendation,
        *,
        scanned_at: Any = None,
        expert_mode: bool = False,
    ) -> HardwareProfileView:
        definition = next(
            item for item in self.curated_catalog().tiers if item.tier == recommendation.stack_tier
        )
        return HardwareProfileView(
            tier=recommendation.stack_tier,
            tier_label=definition.label,
            limiting_factor="balanced capacity",
            catalog_version=recommendation.catalog_release,
            scanned_at=scanned_at,
            profile=recommendation.profile,
            expert_mode=expert_mode,
            recommendations=[
                SimpleModelRecommendation(
                    role=item.role.value,
                    model=item.model,
                    reason="deterministic test recommendation",
                    required_bytes=0,
                    context_tokens=recommendation.context_tokens,
                )
                for item in recommendation.assignments
            ],
        )

    async def install_assignments(self, assignments: list[ModelAssignment]) -> None:
        self.installed_assignments.extend(assignments)

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

    async def runtime(
        self,
        roles: dict[str, str | None] | None = None,
        *,
        active_roles: set[ModelCategory] | None = None,
        worker_timeout_seconds: float = 0.0,
    ) -> ModelRuntimeResponse:
        configured = roles or {}
        active = active_roles or set()
        role_rows = []
        for category in ModelCategory:
            model = configured.get(category.value)
            role_rows.append(
                ModelRoleRuntime(
                    role=category,
                    model=model,
                    residency=(
                        ModelResidency.ACTIVE
                        if category in active
                        else ModelResidency.LOADED
                        if model == "test/chat-2b"
                        else ModelResidency.IDLE
                        if model
                        else ModelResidency.UNCONFIGURED
                    ),
                    shared_with=[
                        other
                        for other in ModelCategory
                        if other != category and model and configured.get(other.value) == model
                    ],
                )
            )
        return ModelRuntimeResponse(
            models=[ModelRuntime(name="test/chat-2b", size=1234)],
            roles=role_rows,
            query_worker_state="active" if active else "idle",
            query_worker_timeout_seconds=worker_timeout_seconds,
        )

    async def import_gguf(
        self,
        path: Path,
        filename: str,
        model: str,
        category: ModelCategory,
        digest: str,
    ) -> AsyncIterator[bytes]:
        assert path.read_bytes().startswith(b"GGUF")
        self.imported_gguf = (filename, model, category, digest)
        yield b'{"status":"success"}\n'

    async def pull(self, model: str) -> AsyncIterator[bytes]:
        yield (f'{{"model":"{model}","status":"success"}}\n').encode()

    async def load(self, model: str, context_tokens: int, keep_alive: str) -> ModelOperationResult:
        return ModelOperationResult(model=model, operation="load", status="ok")

    async def warm_embedding(self, model: str, keep_alive: str = "120s") -> None:
        return None

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
    result.state.services.runs.query.adapter = adapter

    async def pinned_test_runtime(
        workspace_id: str, **options: object
    ) -> tuple[OllamaModelIdentity | None, dict[str, object]]:
        del options
        roles = result.state.services.features.configured_model_roles(workspace_id)
        chat = str(roles.get("chat") or "test-chat")
        embedding = str(roles.get("embedding") or "test-embedding")
        generator = OllamaModelIdentity(chat, f"test-digest:{chat}", 1)
        identities = {
            "generator": {
                "provider": "ollama",
                "model": chat,
                "revision": "test",
                "digest": generator.digest,
                "status": "pinned",
            },
            "embedding": {
                "provider": "ollama",
                "model": embedding,
                "revision": "test",
                "digest": f"test-digest:{embedding}",
                "status": "pinned",
            },
            "reranker": {
                "provider": "cross-encoder",
                "model": DEFAULT_RERANKER,
                "revision": DEFAULT_RERANKER_REVISION,
                "digest": DEFAULT_RERANKER_REVISION,
                "status": "pinned",
            },
        }
        return generator, {
            "readiness_status": "ready",
            "model_identities": identities,
            "model_digests": {
                "generator": generator.digest,
                "embedding": f"test-digest:{embedding}",
                "reranker": _model_digest(DEFAULT_RERANKER, DEFAULT_RERANKER_REVISION),
            },
        }

    result.state.services.runs.runtime_identity_resolver = pinned_test_runtime

    async def verified_test_retrieval(_: str, **_options: object) -> dict[str, object]:
        return {}

    result.state.services.runs.verify_retrieval_identity = verified_test_retrieval
    result.state.services.search.adapter = adapter
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

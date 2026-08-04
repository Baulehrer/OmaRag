from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import __version__
from .adapters.base import HaikuAdapter
from .adapters.haiku_v070 import document_filter_for_ids
from .adapters.isolated import IsolatedHaikuAdapter, WorkerLimits
from .config import Settings
from .models.api import (
    CloneWorkspaceRequest,
    CommitImportRequest,
    ConfigUpdateRequest,
    CreateSourceRequest,
    CreateWorkspaceRequest,
    DeleteModelRequest,
    DeleteWorkspaceRequest,
    ErrorBody,
    ErrorResponse,
    GenerateEvaluationRequest,
    IdempotentResult,
    IngestRequest,
    LoadModelRequest,
    PatchBookMetadataRequest,
    PatchWorkspaceRequest,
    PreflightImportRequest,
    PullModelRequest,
    RestoreBackupRequest,
    RunEvaluationRequest,
    RunRequest,
    SearchRequest,
    UnloadModelRequest,
)
from .models.domain import (
    BackendMeta,
    BackupSummary,
    BookMetadata,
    ConfigDocument,
    DocumentSummary,
    EvaluationReport,
    HardwareProfile,
    HealthReport,
    ImportPreflightBatch,
    JobSnapshot,
    ModelCatalogResponse,
    ModelCategory,
    ModelOperationResult,
    ModelRuntimeResponse,
    ModelSource,
    ParserDefinition,
    QualityReport,
    RetrievalExplanation,
    RetrievalTiming,
    RunSnapshot,
    SearchHit,
    SourceDefinition,
    WorkspaceManifest,
    WorkspaceSummary,
)
from .models.errors import ConflictError, OmaRagError
from .preview import render_citation_preview
from .runtime import configure_process_environment
from .services import (
    EvaluationService,
    EventService,
    JobService,
    ModelService,
    ResourceCoordinator,
    RunService,
    TextbookService,
    WorkspaceFeatureService,
    WorkspaceService,
)
from .store import StateStore


@dataclass(slots=True)
class Services:
    settings: Settings
    store: StateStore
    adapter: HaikuAdapter
    workspaces: WorkspaceService
    events: EventService
    jobs: JobService
    runs: RunService
    resources: ResourceCoordinator
    features: WorkspaceFeatureService
    models: ModelService
    textbooks: TextbookService
    evaluations: EvaluationService
    token: str | None
    token_path: Path | None


def _resolve_bearer_token(settings: Settings) -> tuple[str | None, Path | None]:
    if not settings.auth_enabled:
        return None, None
    if settings.bearer_token:
        return settings.bearer_token, None
    token_path = settings.data_dir / "auth-token"
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if token:
            return token, token_path
    token = secrets.token_urlsafe(32)
    temporary = token_path.with_suffix(".tmp")
    temporary.write_text(token + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(token_path)
    token_path.chmod(0o600)
    return token, token_path


def build_services(settings: Settings) -> Services:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = settings.data_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Keep model/runtime caches inside OmaRag's writable data area. This also
    # makes AppImage, service and container behavior independent of $HOME.
    configure_process_environment(cache_dir)
    store = StateStore(settings.state_database)
    mib = 1024**2
    adapter = IsolatedHaikuAdapter(
        api_limits=WorkerLimits(
            settings.api_memory_high_mb * mib,
            settings.api_memory_max_mb * mib,
            settings.api_swap_max_mb * mib,
            settings.api_tasks_max,
        ),
        import_limits=WorkerLimits(
            settings.worker_import_memory_high_mb * mib,
            settings.worker_import_memory_max_mb * mib,
            settings.worker_import_swap_max_mb * mib,
            settings.worker_tasks_max,
        ),
        query_limits=WorkerLimits(
            settings.worker_query_memory_high_mb * mib,
            settings.worker_query_memory_max_mb * mib,
            settings.worker_query_swap_max_mb * mib,
            settings.worker_tasks_max,
        ),
        utility_limits=WorkerLimits(
            settings.worker_utility_memory_high_mb * mib,
            settings.worker_utility_memory_max_mb * mib,
            settings.worker_utility_swap_max_mb * mib,
            settings.worker_tasks_max,
        ),
        query_idle_seconds=settings.worker_query_idle_seconds,
        ollama_url=settings.ollama_url,
        unload_ollama_models=settings.unload_ollama_models_on_worker_exit,
    )
    workspaces = WorkspaceService(settings.workspaces_dir, store, settings.ollama_url)
    events = EventService(store, settings.event_poll_seconds, settings.event_keepalive_seconds)
    token, token_path = _resolve_bearer_token(settings)
    resources = ResourceCoordinator()
    jobs = JobService(store, workspaces, events, adapter, resources)
    runs = RunService(store, workspaces, events, adapter, resources)
    features = WorkspaceFeatureService(store, workspaces, adapter)
    models = ModelService(settings)
    textbooks = TextbookService(store, workspaces, adapter)
    evaluations = EvaluationService(store, workspaces, adapter)
    return Services(
        settings,
        store,
        adapter,
        workspaces,
        events,
        jobs,
        runs,
        resources,
        features,
        models,
        textbooks,
        evaluations,
        token,
        token_path,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    services = build_services(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await services.jobs.shutdown()
        await services.runs.shutdown()
        shutdown_adapter = getattr(services.adapter, "shutdown", None)
        if shutdown_adapter is not None:
            await shutdown_adapter()
        services.store.close()

    app = FastAPI(
        title="Oracle of Daedalus API",
        version=__version__,
        openapi_version="3.1.0",
        lifespan=lifespan,
        description=(
            "Offline Retrieval-Augmented Command-Line Environment; "
            "stable operations and quality layer for vanilla Haiku RAG"
        ),
    )
    app.state.services = services
    security = HTTPBearer(auto_error=False)

    async def authorize(
        credentials: HTTPAuthorizationCredentials | None = Depends(security),  # noqa: B008
    ) -> None:
        if not services.settings.auth_enabled:
            return
        if credentials is None or not secrets.compare_digest(
            credentials.credentials, services.token or ""
        ):
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="Ungueltiges Bearer-Token")

    protected = [Depends(authorize)]

    @app.get("/v1/parsers", response_model=list[ParserDefinition], dependencies=protected)
    async def list_parsers() -> list[ParserDefinition]:
        return [
            ParserDefinition(
                id="auto",
                name="Automatic",
                description=(
                    "Select the safest available parser; PDF currently resolves to Docling."
                ),
                formats=["pdf", "docx", "pptx", "html", "md", "txt"],
                provenance=True,
                structured_chunking=True,
            ),
            ParserDefinition(
                id="docling",
                name="Docling",
                description=(
                    "Layout-aware parsing with pages, bounding boxes, headings, tables and "
                    "HybridChunker provenance."
                ),
                formats=["pdf", "docx", "pptx", "html", "md", "txt"],
                provenance=True,
                structured_chunking=True,
                available=services.adapter.available,
            ),
        ]

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next: Any) -> Response:
        request.state.correlation_id = request.headers.get("X-Correlation-ID", str(uuid4()))
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        return response

    @app.exception_handler(OmaRagError)
    async def omarag_error_handler(request: Request, exc: OmaRagError) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorBody(
                code=exc.code,
                message=exc.message,
                details=exc.details,
                correlation_id=getattr(request.state, "correlation_id", None),
                retryable=exc.retryable,
            )
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))

    @app.get("/v1/meta", response_model=BackendMeta, dependencies=protected)
    async def meta() -> BackendMeta:
        return BackendMeta(
            backend_id=settings.backend_id,
            haiku_version=services.adapter.version,
            adapter=services.adapter.name if services.adapter.available else None,
            capabilities=services.adapter.capabilities,
        )

    @app.get("/v1/health", response_model=HealthReport)
    async def health() -> HealthReport:
        return HealthReport(status="ok", ready=True, checks={"sqlite": True})

    @app.get(
        "/v1/readiness",
        response_model=HealthReport,
        dependencies=protected,
        responses={503: {"model": HealthReport}},
    )
    async def readiness(response: Response) -> HealthReport:
        ready = services.adapter.available
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthReport(
            status="ready" if ready else "degraded",
            ready=ready,
            checks={
                "sqlite": True,
                "haiku": services.adapter.version,
                "haiku_available": ready,
            },
        )

    @app.get(
        "/v1/models/catalog",
        response_model=ModelCatalogResponse,
        dependencies=protected,
    )
    async def model_catalog(
        source: ModelSource = ModelSource.INSTALLED,
        category: ModelCategory = ModelCategory.CHAT,
        query: str = "",
        quantization: str = "Q4_K_M",
        context_tokens: int = Query(default=8192, ge=1024, le=131072),
        profile: HardwareProfile = HardwareProfile.LAPTOP,
    ) -> ModelCatalogResponse:
        return await services.models.catalog(
            source, category, query, quantization, context_tokens, profile
        )

    @app.get(
        "/v1/models/runtime",
        response_model=ModelRuntimeResponse,
        dependencies=protected,
    )
    async def model_runtime() -> ModelRuntimeResponse:
        return await services.models.runtime()

    @app.post("/v1/models/pull", dependencies=protected)
    async def pull_model(request: PullModelRequest) -> StreamingResponse:
        return StreamingResponse(
            services.models.pull(request.model), media_type="application/x-ndjson"
        )

    @app.post(
        "/v1/models/load",
        response_model=ModelOperationResult,
        dependencies=protected,
    )
    async def load_model(request: LoadModelRequest) -> ModelOperationResult:
        if services.jobs.active or services.runs.active:
            from .models.errors import ConflictError

            raise ConflictError("A Haiku operation is active; model loading is locked")
        return await services.models.load(request.model, request.context_tokens, request.keep_alive)

    @app.post(
        "/v1/models/unload",
        response_model=ModelOperationResult,
        dependencies=protected,
    )
    async def unload_model(request: UnloadModelRequest) -> ModelOperationResult:
        if services.jobs.active or services.runs.active:
            raise ConflictError("A Haiku operation is active; configured models are protected")
        return await services.models.unload(request.model)

    @app.delete(
        "/v1/models",
        response_model=ModelOperationResult,
        dependencies=protected,
    )
    async def delete_model(request: DeleteModelRequest) -> ModelOperationResult:
        if services.jobs.active or services.runs.active:
            raise ConflictError("A Haiku operation is active; model deletion is locked")

        runtime = await services.models.runtime()
        normalized = request.model.removesuffix(":latest")
        if any(item.name.removesuffix(":latest") == normalized for item in runtime.models):
            raise ConflictError("The model is loaded; unload it before deleting it")

        referenced_by = []
        for workspace in services.workspaces.list():
            content = services.features.config(workspace.id).content
            if request.model in content or normalized in content:
                referenced_by.append(workspace.name)
        if referenced_by:
            names = ", ".join(referenced_by)
            raise ConflictError(
                f"The model is referenced by workspace configuration: {names}. "
                "Change the workspace configuration first"
            )
        return await services.models.delete(request.model)

    @app.get(
        "/v1/workspaces",
        response_model=list[WorkspaceSummary],
        dependencies=protected,
    )
    async def list_workspaces() -> list[WorkspaceSummary]:
        return services.workspaces.list()

    @app.post(
        "/v1/workspaces",
        response_model=WorkspaceManifest,
        status_code=201,
        dependencies=protected,
    )
    async def create_workspace(request: CreateWorkspaceRequest) -> WorkspaceManifest:
        workspace = services.workspaces.create(request)
        await services.events.emit(
            "workspace.changed",
            correlation_id=workspace.id,
            workspace_id=workspace.id,
            payload={"operation": "created", "name": workspace.name},
        )
        return workspace

    @app.get(
        "/v1/workspaces/{workspace_id}",
        response_model=WorkspaceManifest,
        dependencies=protected,
    )
    async def get_workspace(workspace_id: str, response: Response) -> WorkspaceManifest:
        workspace = services.workspaces.get(workspace_id)
        response.headers["ETag"] = f'"{workspace.etag}"'
        return workspace

    @app.patch(
        "/v1/workspaces/{workspace_id}",
        response_model=WorkspaceManifest,
        dependencies=protected,
    )
    async def patch_workspace(
        workspace_id: str,
        request: PatchWorkspaceRequest,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> WorkspaceManifest:
        workspace = services.workspaces.patch(workspace_id, request, if_match)
        response.headers["ETag"] = f'"{workspace.etag}"'
        return workspace

    @app.post(
        "/v1/workspaces/{workspace_id}/open",
        response_model=WorkspaceManifest,
        dependencies=protected,
    )
    async def open_workspace(workspace_id: str) -> WorkspaceManifest:
        workspace = services.workspaces.get(workspace_id)
        await services.events.emit(
            "workspace.opened",
            correlation_id=workspace_id,
            workspace_id=workspace_id,
            payload={"read_only": workspace.read_only},
        )
        return workspace

    @app.post(
        "/v1/workspaces/{workspace_id}/clone",
        response_model=WorkspaceManifest,
        status_code=201,
        dependencies=protected,
    )
    async def clone_workspace(
        workspace_id: str, request: CloneWorkspaceRequest
    ) -> WorkspaceManifest:
        return services.workspaces.clone(workspace_id, request)

    @app.delete(
        "/v1/workspaces/{workspace_id}",
        status_code=204,
        dependencies=protected,
    )
    async def delete_workspace(workspace_id: str, request: DeleteWorkspaceRequest) -> Response:
        services.workspaces.delete(workspace_id, request.mode == "physical")
        return Response(status_code=204)

    @app.post(
        "/v1/workspaces/{workspace_id}/documents/ingest",
        response_model=IdempotentResult,
        status_code=202,
        dependencies=protected,
    )
    async def ingest(
        workspace_id: str,
        request: IngestRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> IdempotentResult:
        job, reused = await services.jobs.start_ingest(workspace_id, request, idempotency_key)
        return IdempotentResult(id=job.id, reused=reused)

    @app.post(
        "/v1/workspaces/{workspace_id}/imports/preflight",
        response_model=ImportPreflightBatch,
        dependencies=protected,
    )
    async def preflight_import(
        workspace_id: str, request: PreflightImportRequest
    ) -> ImportPreflightBatch:
        return await asyncio.to_thread(services.textbooks.preflight, workspace_id, request.sources)

    @app.post(
        "/v1/workspaces/{workspace_id}/imports/commit",
        response_model=IdempotentResult,
        status_code=202,
        dependencies=protected,
    )
    async def commit_import(
        workspace_id: str,
        request: CommitImportRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> IdempotentResult:
        sources = await asyncio.to_thread(
            services.textbooks.validate_commit,
            workspace_id,
            request.preflight_id,
            request.sources,
        )
        ingest_request = IngestRequest(
            sources=sources,
            processing_profile=request.processing_profile,
            duplicate_policy=request.duplicate_policy,
            validity_policy=request.validity_policy,
        )
        job, reused = await services.jobs.start_ingest(
            workspace_id, ingest_request, idempotency_key
        )
        return IdempotentResult(id=job.id, reused=reused)

    @app.get(
        "/v1/workspaces/{workspace_id}/documents",
        response_model=list[DocumentSummary],
        dependencies=protected,
    )
    async def list_documents(workspace_id: str) -> list[DocumentSummary]:
        return services.features.documents(workspace_id)

    @app.patch(
        "/v1/workspaces/{workspace_id}/documents/{document_id}/metadata",
        response_model=BookMetadata,
        dependencies=protected,
    )
    async def patch_book_metadata(
        workspace_id: str,
        document_id: str,
        request: PatchBookMetadataRequest,
    ) -> BookMetadata:
        async with services.resources.indexing():
            await services.textbooks.update_metadata(workspace_id, document_id, request.metadata)
        await services.events.emit(
            "document.changed",
            correlation_id=document_id,
            workspace_id=workspace_id,
            payload={"operation": "metadata-updated", "document_id": document_id},
        )
        return request.metadata

    @app.delete(
        "/v1/workspaces/{workspace_id}/documents/{document_id}",
        status_code=204,
        dependencies=protected,
    )
    async def delete_document(workspace_id: str, document_id: str) -> Response:
        async with services.resources.indexing():
            await services.features.delete_document(workspace_id, document_id)
        await services.events.emit(
            "document.changed",
            correlation_id=document_id,
            workspace_id=workspace_id,
            payload={"operation": "removed", "document_id": document_id},
        )
        return Response(status_code=204)

    @app.post(
        "/v1/workspaces/{workspace_id}/documents/{document_id}/restore",
        status_code=204,
        dependencies=protected,
    )
    async def restore_document(workspace_id: str, document_id: str) -> Response:
        async with services.resources.indexing():
            await services.features.restore_document(workspace_id, document_id)
        return Response(status_code=204)

    @app.get(
        "/v1/workspaces/{workspace_id}/sources",
        response_model=list[SourceDefinition],
        dependencies=protected,
    )
    async def list_sources(workspace_id: str) -> list[SourceDefinition]:
        return services.features.list_sources(workspace_id)

    @app.post(
        "/v1/workspaces/{workspace_id}/sources",
        response_model=SourceDefinition,
        status_code=201,
        dependencies=protected,
    )
    async def create_source(workspace_id: str, request: CreateSourceRequest) -> SourceDefinition:
        source = services.features.add_source(workspace_id, request)
        await services.events.emit(
            "source.changed",
            correlation_id=source.id,
            workspace_id=workspace_id,
            payload={"operation": "created", "source_id": source.id},
        )
        return source

    @app.delete(
        "/v1/workspaces/{workspace_id}/sources/{source_id}",
        status_code=204,
        dependencies=protected,
    )
    async def delete_source(workspace_id: str, source_id: str) -> Response:
        services.features.delete_source(workspace_id, source_id)
        await services.events.emit(
            "source.changed",
            correlation_id=source_id,
            workspace_id=workspace_id,
            payload={"operation": "deleted", "source_id": source_id},
        )
        return Response(status_code=204)

    @app.post(
        "/v1/workspaces/{workspace_id}/sources/{source_id}/sync",
        response_model=IdempotentResult,
        status_code=202,
        dependencies=protected,
    )
    async def sync_source(
        workspace_id: str,
        source_id: str,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> IdempotentResult:
        source = services.features.get_source(workspace_id, source_id)
        request = IngestRequest(
            sources=[
                {
                    "type": "url" if source.type == "url" else "file",
                    "path": source.location,
                }
            ]
        )
        job, reused = await services.jobs.start_ingest(workspace_id, request, idempotency_key)
        return IdempotentResult(id=job.id, reused=reused)

    @app.get(
        "/v1/workspaces/{workspace_id}/quality",
        response_model=QualityReport,
        dependencies=protected,
    )
    async def quality_report(workspace_id: str) -> QualityReport:
        return services.features.quality(workspace_id)

    @app.post(
        "/v1/workspaces/{workspace_id}/evaluations/generate",
        response_model=EvaluationReport,
        dependencies=protected,
    )
    async def generate_evaluation(
        workspace_id: str, request: GenerateEvaluationRequest
    ) -> EvaluationReport:
        return await asyncio.to_thread(services.evaluations.generate, workspace_id, request.limit)

    @app.post(
        "/v1/workspaces/{workspace_id}/evaluations/run",
        response_model=EvaluationReport,
        dependencies=protected,
    )
    async def run_evaluation(workspace_id: str, request: RunEvaluationRequest) -> EvaluationReport:
        async with services.resources.chat():
            return await services.evaluations.run(
                workspace_id,
                request.evaluation_id,
                request.variants,
                request.top_k,
            )

    @app.get(
        "/v1/workspaces/{workspace_id}/evaluations/{evaluation_id}",
        response_model=EvaluationReport,
        dependencies=protected,
    )
    async def get_evaluation(workspace_id: str, evaluation_id: str) -> EvaluationReport:
        return EvaluationReport.model_validate(
            services.store.evaluation(workspace_id, evaluation_id)
        )

    @app.get(
        "/v1/workspaces/{workspace_id}/config",
        response_model=ConfigDocument,
        dependencies=protected,
    )
    async def get_config(workspace_id: str, response: Response) -> ConfigDocument:
        config = services.features.config(workspace_id)
        response.headers["ETag"] = f'"{config.etag}"'
        return config

    @app.put(
        "/v1/workspaces/{workspace_id}/config",
        response_model=ConfigDocument,
        dependencies=protected,
    )
    async def update_config(
        workspace_id: str,
        request: ConfigUpdateRequest,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> ConfigDocument:
        config = services.features.update_config(workspace_id, request.content, if_match)
        response.headers["ETag"] = f'"{config.etag}"'
        await services.events.emit(
            "config.changed",
            correlation_id=workspace_id,
            workspace_id=workspace_id,
            payload={"etag": config.etag},
        )
        return config

    @app.get(
        "/v1/workspaces/{workspace_id}/backups",
        response_model=list[BackupSummary],
        dependencies=protected,
    )
    async def list_backups(workspace_id: str) -> list[BackupSummary]:
        return services.features.list_backups(workspace_id)

    @app.post(
        "/v1/workspaces/{workspace_id}/backups",
        response_model=BackupSummary,
        status_code=201,
        dependencies=protected,
    )
    async def create_backup(workspace_id: str) -> BackupSummary:
        async with services.resources.indexing():
            backup = await asyncio.to_thread(services.features.create_backup, workspace_id)
        await services.events.emit(
            "backup.completed",
            correlation_id=backup.id,
            workspace_id=workspace_id,
            payload=backup.model_dump(mode="json"),
        )
        return backup

    @app.post(
        "/v1/workspaces/{workspace_id}/backups/{backup_id}/verify",
        response_model=BackupSummary,
        dependencies=protected,
    )
    async def verify_backup(workspace_id: str, backup_id: str) -> BackupSummary:
        return await asyncio.to_thread(services.features.verify_backup, workspace_id, backup_id)

    @app.post(
        "/v1/workspaces/{workspace_id}/backups/{backup_id}/restore",
        response_model=BackupSummary,
        dependencies=protected,
    )
    async def restore_backup(
        workspace_id: str, backup_id: str, request: RestoreBackupRequest
    ) -> BackupSummary:
        async with services.resources.indexing():
            restored, safety = await asyncio.to_thread(
                services.features.restore_backup, workspace_id, backup_id
            )
        await services.events.emit(
            "backup.restored",
            correlation_id=backup_id,
            workspace_id=workspace_id,
            payload={"backup_id": restored.id, "safety_backup_id": safety.id},
        )
        return restored

    @app.post(
        "/v1/workspaces/{workspace_id}/search",
        response_model=list[SearchHit],
        dependencies=protected,
    )
    async def search(workspace_id: str, request: SearchRequest) -> list[SearchHit]:
        services.workspaces.get(workspace_id)
        document_ids = services.store.resolve_segment_ids(
            workspace_id, request.filters.active(), request.document_policy
        )
        async with services.resources.chat():
            return await services.adapter.search(
                services.workspaces.database_path(workspace_id),
                request.query,
                request.limit,
                document_filter=document_filter_for_ids(document_ids),
            )

    @app.post(
        "/v1/workspaces/{workspace_id}/search/explain",
        response_model=RetrievalExplanation,
        dependencies=protected,
    )
    async def explain_search(workspace_id: str, request: SearchRequest) -> RetrievalExplanation:
        services.workspaces.get(workspace_id)
        document_ids = services.store.resolve_segment_ids(
            workspace_id, request.filters.active(), request.document_policy
        )
        started = time.perf_counter()
        async with services.resources.chat():
            search_started = time.perf_counter()
            ranked = await services.adapter.search(
                services.workspaces.database_path(workspace_id),
                request.query,
                request.limit,
                document_filter=document_filter_for_ids(document_ids),
            )
            search_ms = (time.perf_counter() - search_started) * 1000
        total_ms = (time.perf_counter() - started) * 1000
        return RetrievalExplanation(
            query=request.query,
            ranked=ranked,
            timing=RetrievalTiming(search_ms=search_ms, total_ms=total_ms),
            provider_notes=[
                "Vanilla Haiku RAG exposes final hybrid-ranked hits through its public API; "
                "private candidate stages are intentionally not inspected."
            ],
        )

    @app.post(
        "/v1/workspaces/{workspace_id}/runs",
        response_model=RunSnapshot,
        status_code=202,
        dependencies=protected,
    )
    async def start_run(workspace_id: str, request: RunRequest) -> RunSnapshot:
        return await services.runs.start(workspace_id, request)

    @app.get("/v1/runs/{run_id}", response_model=RunSnapshot, dependencies=protected)
    async def get_run(run_id: str) -> RunSnapshot:
        return services.store.get_run(run_id)

    @app.get(
        "/v1/workspaces/{workspace_id}/runs/{run_id}/citations/{citation_index}/preview",
        dependencies=protected,
    )
    async def citation_preview(
        workspace_id: str,
        run_id: str,
        citation_index: int,
        max_px: Annotated[int, Query(ge=256, le=2400)] = 1400,
    ) -> Response:
        workspace = services.workspaces.get(workspace_id)
        run = services.store.get_run(run_id)
        if run.workspace_id != workspace_id:
            raise ConflictError("Run does not belong to this workspace")
        if citation_index < 0 or citation_index >= len(run.citations):
            from .models.errors import NotFoundError

            raise NotFoundError("Citation was not found")
        payload = await render_citation_preview(
            run.citations[citation_index],
            Path(workspace.path) / ".oracle-cache" / "previews",
            max_px,
        )
        return Response(
            content=payload,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.delete("/v1/runs/{run_id}", response_model=RunSnapshot, dependencies=protected)
    async def cancel_run(run_id: str) -> RunSnapshot:
        return await services.runs.cancel(run_id)

    @app.get("/v1/jobs", response_model=list[JobSnapshot], dependencies=protected)
    async def list_jobs(workspace_id: str | None = None) -> list[JobSnapshot]:
        return services.store.list_jobs(workspace_id)

    @app.get("/v1/jobs/{job_id}", response_model=JobSnapshot, dependencies=protected)
    @app.get("/v1/jobs/{job_id}/snapshot", response_model=JobSnapshot, dependencies=protected)
    async def get_job(job_id: str) -> JobSnapshot:
        return services.store.get_job(job_id)

    @app.post("/v1/jobs/{job_id}/pause", response_model=JobSnapshot, dependencies=protected)
    async def pause_job(job_id: str) -> JobSnapshot:
        return await services.jobs.pause(job_id)

    @app.post("/v1/jobs/{job_id}/resume", response_model=JobSnapshot, dependencies=protected)
    async def resume_job(job_id: str) -> JobSnapshot:
        return await services.jobs.resume(job_id)

    @app.delete("/v1/jobs/{job_id}", response_model=JobSnapshot, dependencies=protected)
    async def cancel_job(job_id: str) -> JobSnapshot:
        return await services.jobs.cancel(job_id)

    def _last_event_id(header: str | None, query_value: int | None) -> int:
        if query_value is not None:
            return query_value
        if header is None:
            return 0
        try:
            return max(0, int(header))
        except ValueError:
            return 0

    def event_response(
        after: int,
        *,
        workspace_id: str | None = None,
        job_id: str | None = None,
        run_id: str | None = None,
    ) -> StreamingResponse:
        return StreamingResponse(
            services.events.stream(after, workspace_id=workspace_id, job_id=job_id, run_id=run_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    sse_responses = {
        200: {
            "description": "Wiederaufnehmbarer OmaRag-Ereignisstrom",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    }

    @app.get("/v1/events", dependencies=protected, responses=sse_responses)
    async def all_events(
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        after: int | None = None,
    ) -> StreamingResponse:
        return event_response(_last_event_id(last_event_id, after))

    @app.get(
        "/v1/workspaces/{workspace_id}/events",
        dependencies=protected,
        responses=sse_responses,
    )
    async def workspace_events(
        workspace_id: str,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        after: int | None = None,
    ) -> StreamingResponse:
        services.workspaces.get(workspace_id)
        return event_response(_last_event_id(last_event_id, after), workspace_id=workspace_id)

    @app.get("/v1/jobs/{job_id}/events", dependencies=protected, responses=sse_responses)
    async def job_events(
        job_id: str,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        after: int | None = None,
    ) -> StreamingResponse:
        services.store.get_job(job_id)
        return event_response(_last_event_id(last_event_id, after), job_id=job_id)

    @app.get("/v1/runs/{run_id}/events", dependencies=protected, responses=sse_responses)
    async def run_events(
        run_id: str,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        after: int | None = None,
    ) -> StreamingResponse:
        services.store.get_run(run_id)
        return event_response(_last_event_id(last_event_id, after), run_id=run_id)

    @app.get("/v1/system/dependencies", dependencies=protected)
    async def dependencies() -> dict[str, Any]:
        return {
            "components": [
                {"name": "python", "status": "available"},
                {
                    "name": "haiku-rag",
                    "status": "available" if services.adapter.available else "missing",
                    "version": services.adapter.version,
                },
            ]
        }

    return app

from __future__ import annotations

import asyncio
import hashlib
import secrets
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
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
    ModelDefaultsRequest,
    PatchBookMetadataRequest,
    PatchWorkspaceRequest,
    PreflightImportRequest,
    PullModelRequest,
    ReindexPreflightRequest,
    ReindexRequest,
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
    Citation,
    ConfigDocument,
    DocumentSummary,
    EvaluationReport,
    HardwareProfile,
    HealthReport,
    ImportPreflightBatch,
    JobSnapshot,
    JobStatus,
    ModelCatalogResponse,
    ModelCategory,
    ModelDefaultsPreflight,
    ModelOperationResult,
    ModelRuntimeResponse,
    ModelSource,
    ParserDefinition,
    QualityReport,
    QueryReadiness,
    ReindexPreflight,
    RetrievalExplanation,
    RunSnapshot,
    SearchHit,
    SourceDefinition,
    WarmupResponse,
    WarmupStatus,
    WorkspaceManifest,
    WorkspaceSummary,
)
from .models.errors import (
    ConflictError,
    IndexNotReadyError,
    IndexRebuildInProgressError,
    NotFoundError,
    OmaRagError,
    QueryDeadlineExceededError,
)
from .preview import render_citation_preview
from .runtime import configure_process_environment
from .services import (
    AdaptiveSearchService,
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
    search: AdaptiveSearchService
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
    adapter.set_residency_policy(resources.residency_seconds)
    jobs = JobService(store, workspaces, events, adapter, resources)
    features = WorkspaceFeatureService(store, workspaces, adapter)
    runs = RunService(
        store,
        workspaces,
        events,
        adapter,
        resources,
        answer_cache_max_entries=settings.answer_cache_max_entries,
        ollama_url=settings.ollama_url,
        model_roles=features.configured_model_roles,
    )
    models = ModelService(settings)
    textbooks = TextbookService(store, workspaces, adapter)
    evaluations = EvaluationService(store, workspaces, adapter)
    search = AdaptiveSearchService(adapter, store)
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
        search,
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
        title="OmaRag API",
        version=__version__,
        openapi_version="3.1.0",
        lifespan=lifespan,
        description=(
            "Oracle of Metis & Aletheia; stable operations and quality layer for vanilla Haiku RAG"
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

            raise HTTPException(status_code=401, detail="Invalid bearer token")

    protected = [Depends(authorize)]

    def ensure_index_queryable(workspace_id: str) -> None:
        services.workspaces.get(workspace_id)
        generation_reader = getattr(services.store, "workspace_index_generation", None)
        generation = generation_reader(workspace_id) if callable(generation_reader) else None
        status_value = str((generation or {}).get("status") or "legacy_ready").casefold()
        if status_value in {"maintenance", "building"}:
            jobs = [
                item
                for item in services.store.list_jobs(workspace_id)
                if item.kind == "reindex"
                and item.status
                in {
                    JobStatus.QUEUED,
                    JobStatus.RUNNING,
                    JobStatus.PAUSE_REQUESTED,
                    JobStatus.PAUSED,
                }
            ]
            raise IndexRebuildInProgressError(
                "The workspace index is being rebuilt",
                details={
                    "generation_id": (generation or {}).get("generation_id"),
                    "job_id": jobs[0].id if jobs else None,
                    "status": status_value,
                },
            )
        if status_value == "maintenance_failed":
            raise IndexNotReadyError(
                "The workspace rebuild failed and must be resumed before querying",
                details={
                    "generation_id": (generation or {}).get("generation_id"),
                    "status": status_value,
                },
            )

    services.runs.index_gate = ensure_index_queryable

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
    async def model_runtime(workspace_id: str | None = None) -> ModelRuntimeResponse:
        roles = (
            services.features.configured_model_roles(workspace_id)
            if workspace_id is not None
            else None
        )
        active_roles: set[ModelCategory] = set()
        if services.runs.active:
            active_roles.update({ModelCategory.CHAT, ModelCategory.VL, ModelCategory.RERANK})
        if services.jobs.active:
            active_roles.add(ModelCategory.EMBEDDING)
        runtime = await services.models.runtime(
            roles,
            active_roles=active_roles,
            worker_timeout_seconds=services.settings.worker_query_idle_seconds,
        )
        return runtime.model_copy(
            update={
                "residency_policy": "adaptive",
                "memory_state": services.resources.memory().state,
                "query_worker_state": getattr(
                    services.adapter, "query_worker_state", runtime.query_worker_state
                ),
                "worker_expires_in_seconds": float(
                    getattr(services.adapter, "worker_expires_in_seconds", 0.0)
                ),
            }
        )

    @app.post(
        "/v1/workspaces/{workspace_id}/runtime/warmup",
        response_model=WarmupResponse,
        dependencies=protected,
    )
    async def runtime_warmup(workspace_id: str) -> WarmupResponse:
        services.workspaces.get(workspace_id)
        async with services.resources.warmup() as admission:
            if admission != "ready":
                status_value = (
                    WarmupStatus.SKIPPED_MEMORY
                    if admission == "skipped_memory"
                    else WarmupStatus.SKIPPED_BUSY
                )
                return WarmupResponse(
                    status=status_value,
                    detail="Foreground work or memory pressure has priority.",
                )
            roles = services.features.configured_model_roles(workspace_id)
            chat = roles.get("chat")
            embedding = roles.get("embedding")
            if not chat and not embedding:
                return WarmupResponse(
                    status=WarmupStatus.NOT_NEEDED,
                    detail="No chat or embedding model is configured.",
                )
            keep_seconds = services.resources.residency_seconds()
            keep_alive = f"{max(1, round(keep_seconds))}s"
            warmed: list[str] = []
            await services.adapter.warm(services.workspaces.database_path(workspace_id))
            if chat:
                await services.models.load(chat, 8192, keep_alive)
                warmed.append("chat")
            if embedding and embedding != chat:
                await services.models.warm_embedding(embedding, keep_alive)
                warmed.append("embedding")
            return WarmupResponse(
                status=WarmupStatus.READY,
                warmed_roles=warmed,
                keep_alive_seconds=keep_seconds,
                detail="Query runtime is ready.",
            )

    @app.get(
        "/v1/workspaces/{workspace_id}/readiness",
        response_model=QueryReadiness,
        dependencies=protected,
    )
    async def workspace_query_readiness(workspace_id: str) -> QueryReadiness:
        services.workspaces.get(workspace_id)
        roles = services.features.configured_model_roles(workspace_id)
        runtime = await services.models.runtime(
            roles,
            worker_timeout_seconds=services.settings.worker_query_idle_seconds,
        )

        def normalized(model: str | None) -> str:
            return (model or "").removesuffix(":latest")

        loaded = {normalized(item.name): item for item in runtime.models}
        required = {
            "embedding": roles.get("embedding"),
            "generator": roles.get("chat"),
        }
        resident = {
            role: bool(model and normalized(model) in loaded) for role, model in required.items()
        }
        generation_status = "ready"
        generation_reader = getattr(services.store, "workspace_index_generation", None)
        if callable(generation_reader):
            generation = generation_reader(workspace_id)
            if generation is not None:
                generation_status = str(generation.get("status", "not_ready")).casefold()
        index_ready = generation_status in {"ready", "none"}
        models_ready = all(resident.values()) and all(required.values())
        query_ready = bool(services.adapter.available and index_ready and models_ready)
        digests = {
            role: loaded[normalized(model)].digest
            for role, model in required.items()
            if model and normalized(model) in loaded and loaded[normalized(model)].digest
        }
        return QueryReadiness(
            workspace_id=workspace_id,
            index_ready=index_ready,
            query_ready=query_ready,
            latency_status="ready" if query_ready else "latency_degraded",
            loaded_models=[item.model_dump(mode="json") for item in runtime.models],
            model_digests=digests,
            checks={
                "adapter_available": services.adapter.available,
                "index_generation": generation_status,
                "embedding_resident": resident["embedding"],
                "generator_resident": resident["generator"],
                "required_concurrent_residency": 2,
                "configuration_hint": (
                    "OLLAMA_MAX_LOADED_MODELS=2; OLLAMA_NUM_PARALLEL=1"
                    if not models_ready
                    else None
                ),
            },
        )

    @app.post(
        "/v1/workspaces/{workspace_id}/model-defaults/preflight",
        response_model=ModelDefaultsPreflight,
        dependencies=protected,
    )
    async def model_defaults_preflight(
        workspace_id: str, request: ModelDefaultsRequest
    ) -> ModelDefaultsPreflight:
        return services.features.model_defaults_preflight(workspace_id, request)

    @app.post(
        "/v1/workspaces/{workspace_id}/model-defaults/apply",
        response_model=ConfigDocument,
        dependencies=protected,
    )
    async def apply_model_defaults(
        workspace_id: str,
        request: ModelDefaultsRequest,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> ConfigDocument:
        config = services.features.apply_model_defaults(workspace_id, request, if_match)
        response.headers["ETag"] = f'"{config.etag}"'
        await services.events.emit(
            "config.changed",
            correlation_id=workspace_id,
            workspace_id=workspace_id,
            payload={"etag": config.etag, "operation": "model-defaults"},
        )
        return config

    @app.post("/v1/models/import/gguf", dependencies=protected)
    async def import_gguf(
        file: Annotated[UploadFile, File()],
        model: Annotated[str, Form(min_length=1, max_length=120)],
        category: Annotated[ModelCategory, Form()],
    ) -> StreamingResponse:
        if category == ModelCategory.RERANK:
            raise ConflictError("Rerank requires a cross-encoder; GGUF import is unsupported")
        filename = Path(file.filename or "model.gguf").name
        if Path(filename).suffix.casefold() != ".gguf":
            raise ConflictError("Only .gguf files can be imported")
        maximum = services.settings.model_upload_max_bytes
        if file.size is not None and file.size > maximum:
            raise ConflictError(f"GGUF exceeds the configured {maximum}-byte upload limit")
        import_dir = services.settings.data_dir / "cache" / "model-imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        required = (file.size or 1024**3) + 1024**3
        if shutil.disk_usage(import_dir).free < required:
            raise ConflictError("Not enough free disk space for a safe GGUF import")
        path: Path | None = None
        digest = hashlib.sha256()
        total = 0
        magic = b""
        try:
            with tempfile.NamedTemporaryFile(
                prefix="gguf-", suffix=".part", dir=import_dir, delete=False
            ) as temporary:
                path = Path(temporary.name)
                while chunk := await file.read(8 * 1024**2):
                    total += len(chunk)
                    if total > maximum:
                        raise ConflictError(
                            f"GGUF exceeds the configured {maximum}-byte upload limit"
                        )
                    if not magic:
                        magic = chunk[:4]
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
            await file.close()
            if total == 0 or magic != b"GGUF":
                raise ConflictError("The upload is not a valid GGUF container")
        except Exception:
            await file.close()
            if path is not None:
                path.unlink(missing_ok=True)
            raise
        assert path is not None

        async def stream_import():
            try:
                async for line in services.models.import_gguf(
                    path, filename, model, category, digest.hexdigest()
                ):
                    yield line
            finally:
                path.unlink(missing_ok=True)

        return StreamingResponse(stream_import(), media_type="application/x-ndjson")

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
            indexing=request.indexing,
        )
        job, reused = await services.jobs.start_ingest(
            workspace_id, ingest_request, idempotency_key
        )
        return IdempotentResult(id=job.id, reused=reused)

    @app.post(
        "/v1/workspaces/{workspace_id}/reindex/preflight",
        response_model=ReindexPreflight,
        dependencies=protected,
    )
    async def preflight_reindex(
        workspace_id: str, request: ReindexPreflightRequest
    ) -> ReindexPreflight:
        return await asyncio.to_thread(
            services.jobs.preflight_reindex,
            workspace_id,
            request.indexing.model_dump(mode="json"),
        )

    @app.post(
        "/v1/workspaces/{workspace_id}/reindex",
        response_model=IdempotentResult,
        status_code=202,
        dependencies=protected,
    )
    async def reindex_workspace(
        workspace_id: str,
        request: ReindexRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> IdempotentResult:
        job, reused = await services.jobs.start_reindex(workspace_id, request, idempotency_key)
        return IdempotentResult(id=job.id, reused=reused)

    @app.get(
        "/v1/workspaces/{workspace_id}/documents",
        response_model=list[DocumentSummary],
        dependencies=protected,
    )
    async def list_documents(workspace_id: str) -> list[DocumentSummary]:
        return services.features.documents(workspace_id)

    @app.get(
        "/v1/workspaces/{workspace_id}/documents/{document_id}/structure",
        dependencies=protected,
    )
    async def document_structure(workspace_id: str, document_id: str) -> dict[str, Any]:
        services.workspaces.get(workspace_id)
        structure = services.store.book_structure(workspace_id, document_id)
        if structure is None:
            raise NotFoundError(f"Buchstruktur {document_id} wurde nicht gefunden")
        return structure

    @app.get(
        "/v1/workspaces/{workspace_id}/documents/{document_id}/knowledge-snapshot",
        dependencies=protected,
    )
    async def document_knowledge_snapshot(workspace_id: str, document_id: str) -> dict[str, Any]:
        services.workspaces.get(workspace_id)
        snapshot = services.store.book_knowledge_snapshot(workspace_id, document_id)
        if snapshot is None:
            raise NotFoundError(f"Knowledge-Snapshot {document_id} wurde nicht gefunden")
        return snapshot

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
        ensure_index_queryable(workspace_id)
        async with services.resources.indexing():
            ensure_index_queryable(workspace_id)
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
            ensure_index_queryable(workspace_id)
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
        ensure_index_queryable(workspace_id)
        async with services.resources.indexing():
            ensure_index_queryable(workspace_id)
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
        deadline = time.monotonic() + (request.options.deadline_ms or 35_000) / 1000
        ensure_index_queryable(workspace_id)
        document_ids = services.store.resolve_segment_ids(
            workspace_id, request.filters.active(), request.document_policy
        )
        try:
            async with asyncio.timeout_at(deadline), services.resources.chat():
                ensure_index_queryable(workspace_id)
                ranked, _ = await services.search.search(
                    services.workspaces.database_path(workspace_id),
                    request.query,
                    requested_limit=request.limit,
                    max_sources=request.options.max_sources,
                    document_filter=document_filter_for_ids(document_ids),
                    allowed_document_ids=(set(document_ids) if document_ids is not None else None),
                    profile=request.options.profile,
                )
                return ranked
        except TimeoutError as exc:
            raise QueryDeadlineExceededError("Search deadline exceeded") from exc

    @app.post(
        "/v1/workspaces/{workspace_id}/search/explain",
        response_model=RetrievalExplanation,
        dependencies=protected,
    )
    async def explain_search(workspace_id: str, request: SearchRequest) -> RetrievalExplanation:
        deadline = time.monotonic() + (request.options.deadline_ms or 35_000) / 1000
        ensure_index_queryable(workspace_id)
        document_ids = services.store.resolve_segment_ids(
            workspace_id, request.filters.active(), request.document_policy
        )
        started = time.perf_counter()
        try:
            async with asyncio.timeout_at(deadline), services.resources.chat():
                ensure_index_queryable(workspace_id)
                search_started = time.perf_counter()
                _, explanation = await services.search.search(
                    services.workspaces.database_path(workspace_id),
                    request.query,
                    requested_limit=request.limit,
                    max_sources=request.options.max_sources,
                    document_filter=document_filter_for_ids(document_ids),
                    allowed_document_ids=(set(document_ids) if document_ids is not None else None),
                    profile=request.options.profile,
                )
                outer_ms = (time.perf_counter() - search_started) * 1000
        except TimeoutError as exc:
            raise QueryDeadlineExceededError("Search explanation deadline exceeded") from exc
        total_ms = (time.perf_counter() - started) * 1000
        return explanation.model_copy(
            update={
                "timing": explanation.timing.model_copy(
                    update={
                        "search_ms": max(explanation.timing.search_ms, outer_ms),
                        "total_ms": max(explanation.timing.total_ms, total_ms),
                    }
                )
            }
        )

    @app.post(
        "/v1/workspaces/{workspace_id}/runs",
        response_model=RunSnapshot,
        status_code=202,
        dependencies=protected,
    )
    async def start_run(workspace_id: str, request: RunRequest) -> RunSnapshot:
        ensure_index_queryable(workspace_id)
        return await services.runs.start(workspace_id, request)

    @app.get("/v1/runs/{run_id}", response_model=RunSnapshot, dependencies=protected)
    async def get_run(run_id: str) -> RunSnapshot:
        return services.store.get_run(run_id)

    @app.get(
        "/v1/workspaces/{workspace_id}/runs/{run_id}/citations/{citation_index}",
        response_model=Citation,
        dependencies=protected,
    )
    async def citation_details(workspace_id: str, run_id: str, citation_index: int) -> Citation:
        services.workspaces.get(workspace_id)
        run = services.store.get_run(run_id)
        if run.workspace_id != workspace_id:
            raise ConflictError("Run does not belong to this workspace")
        if citation_index < 0 or citation_index >= len(run.citations):
            from .models.errors import NotFoundError

            raise NotFoundError("Citation was not found")
        citation = run.citations[citation_index]
        if not citation.primary_anchors and not citation.context_anchors:
            async with services.resources.chat():
                # Another visible page may have queued the same lazy citation.
                # Re-read after admission so only the first request pays for it.
                latest = services.store.get_run(run_id)
                citation = latest.citations[citation_index]
                if not citation.primary_anchors and not citation.context_anchors:
                    citation = await services.adapter.citation_details(
                        services.workspaces.database_path(workspace_id), citation
                    )
                    citations = list(latest.citations)
                    citations[citation_index] = citation
                    services.store.update_run(
                        run_id,
                        citations=[item.model_dump(mode="json") for item in citations],
                    )
        return citation

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
        citation = await citation_details(workspace_id, run_id, citation_index)
        payload = await render_citation_preview(
            citation,
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

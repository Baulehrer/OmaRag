from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import shutil
import tempfile
import time
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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
    ExecuteDocumentPurgeRequest,
    ExecuteRetentionCleanupRequest,
    GenerateEvaluationRequest,
    HardwareBenchmarkRequest,
    HardwareScanRequest,
    IdempotentResult,
    ImportEvaluationRequest,
    IngestRequest,
    LoadModelRequest,
    ModelDefaultsRequest,
    ModelProfileApplyAndReindexRequest,
    ModelProfileApplyRequest,
    ModelProfilePreflightRequest,
    ModelRecommendationRequest,
    PatchBookMetadataRequest,
    PatchWorkspaceRequest,
    PinRequest,
    PreflightImportRequest,
    PullModelRequest,
    ReindexPreflightRequest,
    ReindexRequest,
    RestoreBackupRequest,
    RunEvaluationRequest,
    RunRequest,
    SearchRequest,
    UnloadModelRequest,
    UpdatePrivacyPolicyRequest,
    UpdateRetentionPolicyRequest,
)
from .models.domain import (
    BackendMeta,
    BackupSummary,
    BookMetadata,
    CatalogProvider,
    Citation,
    ConfigDocument,
    ConversionArtifactsReport,
    DocumentPurgePlan,
    DocumentPurgeResult,
    DocumentSummary,
    EgressPayloadClass,
    EvaluationReport,
    HardwareBenchmark,
    HardwareInfo,
    HardwareProfile,
    HardwareProfileView,
    HealthReport,
    ImportPreflightBatch,
    JobSnapshot,
    JobStatus,
    ModelAssignment,
    ModelCatalogResponse,
    ModelCategory,
    ModelDefaultsPreflight,
    ModelInstallState,
    ModelOperationResult,
    ModelProfilePreflight,
    ModelRuntimeResponse,
    ModelSource,
    ModelStackRecommendation,
    ParserDefinition,
    PerformanceProfile,
    PrivacyPolicy,
    QualityReport,
    QueryReadiness,
    ReindexPreflight,
    RetentionCleanupPlan,
    RetentionPolicy,
    RetentionPurgeResult,
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
    EtagConflictError,
    IndexNotReadyError,
    IndexRebuildInProgressError,
    NotFoundError,
    OmaRagError,
    QueryDeadlineExceededError,
    ReadOnlyError,
)
from .models.media import MediaAsset, OKFMediaProposal, VisualEvidenceResponse
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
    VisualEvidenceService,
    WorkspaceFeatureService,
    WorkspaceService,
)
from .services.egress_policy import EgressPolicy
from .services.media_service import mark_media_blob_references, sweep_unreferenced_media_blobs
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
    visual_evidence: VisualEvidenceService
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
    settings.data_dir.chmod(0o700)
    cache_dir = settings.data_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.chmod(0o700)
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
    resources = ResourceCoordinator(settings.worker_query_idle_seconds)
    adapter.set_residency_policy(resources.residency_seconds)
    features = WorkspaceFeatureService(store, workspaces, adapter)
    for workspace in workspaces.list():
        features.reconcile_hidden_documents(workspace.id)
    jobs = JobService(
        store,
        workspaces,
        events,
        adapter,
        resources,
        profile_config_activator=features.activate_model_defaults_for_reindex,
    )

    def workspace_performance_profile(workspace_id: str) -> str | None:
        profile = features.configured_model_settings(workspace_id).get("profile")
        if not isinstance(profile, dict):
            return None
        value = str(profile.get("performance_profile") or "")
        return value or None

    def workspace_context_tokens(workspace_id: str) -> int | None:
        profile = features.configured_model_settings(workspace_id).get("profile")
        if not isinstance(profile, dict):
            return None
        value = int(profile.get("context_tokens") or 0)
        return value or None

    runs = RunService(
        store,
        workspaces,
        events,
        adapter,
        resources,
        answer_cache_max_entries=settings.answer_cache_max_entries,
        answer_cache_max_bytes=settings.answer_cache_max_bytes,
        ollama_url=settings.ollama_url,
        model_roles=features.configured_model_roles,
        model_settings=features.configured_model_settings,
        workspace_profile=workspace_performance_profile,
        workspace_context_tokens=workspace_context_tokens,
    )
    models = ModelService(settings)
    textbooks = TextbookService(store, workspaces, adapter)
    evaluations = EvaluationService(store, workspaces, adapter)
    search = AdaptiveSearchService(adapter, store)
    visual_evidence = VisualEvidenceService(store, workspaces)
    runs.visual_evidence_builder = visual_evidence.get_or_build
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
        visual_evidence,
        token,
        token_path,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    services = build_services(settings)

    async def automatic_retention_sweep() -> None:
        """Apply finite V1.2 retention without touching opted-in legacy workspaces."""

        await asyncio.to_thread(services.jobs.sweep_url_import_orphans)
        for workspace in services.workspaces.list():
            policy = services.store.get_retention_policy(workspace.id)
            if policy.profile.value != "minimal":
                continue
            try:
                async with (
                    services.jobs.writer(fail_if_active=True),
                    services.resources.indexing(),
                ):
                    plan = services.store.plan_retention_cleanup(workspace.id)
                    if not plan.eligible_records:
                        continue
                    services.store.purge_retention_cleanup(
                        plan,
                        confirmation="PURGE_EXPIRED",
                    )
                    assets = [
                        MediaAsset.model_validate(item)
                        for item in services.store.all_book_media_assets(workspace.id)
                    ]
                    marked = mark_media_blob_references(
                        assets,
                        visual_evidence=services.store.run_visual_evidence(workspace.id),
                    )
                    await asyncio.to_thread(
                        sweep_unreferenced_media_blobs,
                        Path(workspace.path) / "database",
                        marked,
                        dry_run=False,
                    )
            except (ConflictError, NotFoundError):
                # Active/paused work and concurrent policy changes defer this
                # workspace to the next bounded sweep.
                continue

    async def retention_loop() -> None:
        while True:
            await automatic_retention_sweep()
            await asyncio.sleep(settings.retention_sweep_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # StateStore has already converted crash-interrupted jobs to PAUSED.
        # Sweep only old, unowned URL work paths before accepting requests;
        # the periodic retention loop repeats the bounded cleanup.
        await asyncio.to_thread(services.jobs.sweep_url_import_orphans)
        retention_task = asyncio.create_task(retention_loop(), name="omarag-retention-sweeper")
        try:
            yield
        finally:
            retention_task.cancel()
            with suppress(asyncio.CancelledError):
                await retention_task
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
        description=("Stable operations and quality layer for vanilla Haiku RAG"),
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
    retention_cleanup_plans: dict[str, RetentionCleanupPlan] = {}
    document_purge_plans: dict[str, DocumentPurgePlan] = {}

    def require_mutable_workspace(workspace_id: str, if_match: str | None) -> WorkspaceManifest:
        workspace = services.workspaces.get(workspace_id)
        if if_match is not None and if_match.strip('"') != workspace.etag:
            raise EtagConflictError("Workspace wurde zwischenzeitlich geaendert")
        if workspace.read_only:
            raise ReadOnlyError("A read-only workspace cannot change privacy or retention")
        return workspace

    def persist_workspace_change(
        current: WorkspaceManifest, updates: dict[str, object] | None = None
    ) -> WorkspaceManifest:
        timestamp = datetime.now(UTC)
        updated = current.model_copy(
            update={
                **(updates or {}),
                "updated_at": timestamp,
                "etag": services.workspaces._etag(current.id, current.name, timestamp),
            }
        )
        try:
            services.workspaces._write_manifest(updated)
            services.store.update_workspace(updated)
        except Exception:
            with suppress(Exception):
                services.workspaces._write_manifest(current)
            raise
        return updated

    def privacy_policy_path(workspace: WorkspaceManifest, *, create: bool) -> Path:
        workspace_root = Path(workspace.path).resolve()
        metadata_dir = workspace_root / ".omarag"
        if create:
            metadata_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if metadata_dir.exists() and (
            metadata_dir.is_symlink() or metadata_dir.resolve().parent != workspace_root
        ):
            raise ConflictError("Workspace privacy storage is not trustworthy")
        if create:
            metadata_dir.chmod(0o700)
        target = metadata_dir / "privacy-policy.json"
        if target.is_symlink():
            raise ConflictError("Workspace privacy storage is not trustworthy")
        return target

    def read_privacy_policy(workspace: WorkspaceManifest) -> PrivacyPolicy:
        target = privacy_policy_path(workspace, create=False)
        if not target.is_file():
            return PrivacyPolicy(
                mode=workspace.privacy_mode,
                cloud_acknowledged=workspace.cloud_acknowledged,
            )
        try:
            return PrivacyPolicy.model_validate_json(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConflictError("Stored workspace privacy policy is invalid") from exc

    def write_privacy_policy(workspace: WorkspaceManifest, policy: PrivacyPolicy) -> None:
        target = privacy_policy_path(workspace, create=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".privacy-policy-",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as stream:
                stream.write(policy.model_dump_json(indent=2) + "\n")
                temporary = Path(stream.name)
            temporary.chmod(0o600)
            temporary.replace(target)
            target.chmod(0o600)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def checked_egress_policy(policy: PrivacyPolicy) -> EgressPolicy:
        try:
            return EgressPolicy(policy)
        except ValueError as exc:
            raise ConflictError("Privacy policy contains an invalid trusted endpoint") from exc

    def enforce_url_import_policy(workspace_id: str, sources: list[Any]) -> None:
        urls: list[str] = []
        for source in sources:
            source_type = str(getattr(source, "type", "file"))
            path = str(source.path).strip()
            is_http = path.casefold().startswith(("http://", "https://"))
            if source_type == "file" and is_http:
                raise ConflictError("An HTTP(S) import must declare type=url")
            if source_type == "url" and not is_http:
                raise ConflictError("A URL import must use HTTP(S)")
            if source_type == "url":
                urls.append(path)
        if not urls:
            return
        workspace = services.workspaces.get(workspace_id)
        guard = checked_egress_policy(read_privacy_policy(workspace))
        for url in urls:
            guard.authorize_http(url, EgressPayloadClass.URL_SOURCE)

    def enforce_url_source(workspace_id: str, url: str) -> None:
        workspace = services.workspaces.get(workspace_id)
        checked_egress_policy(read_privacy_policy(workspace)).authorize_http(
            url, EgressPayloadClass.URL_SOURCE
        )

    def enforce_content_egress(workspace_id: str, url: str) -> None:
        workspace = services.workspaces.get(workspace_id)
        checked_egress_policy(read_privacy_policy(workspace)).authorize_http(
            url, EgressPayloadClass.USER_CONTENT
        )

    services.runs.content_egress_guard = enforce_content_egress
    services.jobs.content_egress_guard = enforce_content_egress
    services.jobs.url_source_guard = enforce_url_source
    services.evaluations.content_egress_guard = enforce_content_egress
    services.features.content_egress_guard = enforce_content_egress

    def remember_cleanup_plan(plan: RetentionCleanupPlan) -> None:
        current = datetime.now(UTC)
        expired = [
            plan_id
            for plan_id, candidate in retention_cleanup_plans.items()
            if candidate.expires_at <= current
        ]
        for plan_id in expired:
            retention_cleanup_plans.pop(plan_id, None)
        if len(retention_cleanup_plans) >= 128:
            oldest = min(
                retention_cleanup_plans.values(), key=lambda candidate: candidate.expires_at
            )
            retention_cleanup_plans.pop(oldest.plan_id, None)
        retention_cleanup_plans[plan.plan_id] = plan

    def remember_document_purge_plan(plan: DocumentPurgePlan) -> None:
        current = datetime.now(UTC)
        expired = [
            plan_id
            for plan_id, candidate in document_purge_plans.items()
            if candidate.expires_at <= current
        ]
        for plan_id in expired:
            document_purge_plans.pop(plan_id, None)
        if len(document_purge_plans) >= 128:
            oldest = min(document_purge_plans.values(), key=lambda candidate: candidate.expires_at)
            document_purge_plans.pop(oldest.plan_id, None)
        document_purge_plans[plan.plan_id] = plan

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

    def effective_retrieval_profile(workspace_id: str, requested: str) -> str:
        if requested != "auto":
            return requested
        configured = services.features.configured_model_settings(workspace_id).get("profile")
        if isinstance(configured, dict):
            value = str(configured.get("performance_profile") or "")
            if value in {"fast", "normal", "quality"}:
                return value
        return "normal"

    def model_profile_preflight_key(workspace_id: str, recommendation_id: str) -> str:
        material = f"{workspace_id}\0{recommendation_id}".encode()
        return "model-profile-" + hashlib.sha256(material).hexdigest()

    def current_embedding_identity(
        workspace_id: str, configured: dict[str, object]
    ) -> tuple[str | None, str | None]:
        profile = configured.get("profile")
        artifacts = profile.get("artifacts") if isinstance(profile, dict) else None
        embedding = artifacts.get("embedding") if isinstance(artifacts, dict) else None
        profile_digest = str(embedding.get("digest") or "") if isinstance(embedding, dict) else ""
        generation = services.store.workspace_index_generation(workspace_id)
        generation_config = dict(generation.get("config") or {}) if generation is not None else {}
        return (
            str(configured.get("embedding_provider") or "") or None,
            str(generation_config.get("embedding_digest") or profile_digest or "") or None,
        )

    async def acquire_model_mutation_lease() -> AsyncExitStack:
        """Exclude imports, rebuilds and active model consumers until stream completion."""

        stack = AsyncExitStack()
        try:
            await stack.enter_async_context(services.jobs.writer(fail_if_active=True))
            await stack.enter_async_context(services.resources.indexing())
        except BaseException:
            await stack.aclose()
            raise
        return stack

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

    def _missing_conversion_artifacts() -> list[str]:
        """Model repositories no registered workspace could convert without.

        Serving and indexing fail independently: the API can answer while an
        import worker cannot start, because workers run offline and resolve
        their own models.  Reporting them apart keeps a service that only
        *looks* ready from being called ready.
        """

        missing: list[str] = []
        for workspace in services.workspaces.list():
            try:
                config = services.workspaces.app_config(workspace.id)
            except Exception:  # noqa: BLE001 - a broken workspace must not fail health
                continue
            tokenizer = str(getattr(config.processing, "chunking_tokenizer", "") or "")
            report = services.models.conversion_artifacts_report(
                tokenizer, services.models._reranker_name(config)
            )
            missing.extend(repo for repo in report.missing if repo not in missing)
        return missing

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
        missing = await asyncio.to_thread(_missing_conversion_artifacts)
        return HealthReport(
            status="ready" if ready else "degraded",
            ready=ready,
            checks={
                "sqlite": True,
                "haiku": services.adapter.version,
                "haiku_available": ready,
                "indexing_ready": not missing,
                "missing_conversion_artifacts": missing,
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
        "/v1/models/hardware/scan",
        response_model=HardwareProfileView,
        dependencies=protected,
    )
    async def hardware_scan_view() -> HardwareProfileView:
        """Compact first-run view; scanning and recommendation never mutate state."""

        hardware = await asyncio.to_thread(
            services.models.hardware,
            services.settings.data_dir,
        )
        recommendation = await services.models.recommend(
            PerformanceProfile.NORMAL,
            hardware=hardware,
        )
        return services.models.recommendation_view(
            recommendation,
            scanned_at=hardware.collected_at,
        )

    @app.get(
        "/v1/models/recommendation",
        response_model=HardwareProfileView,
        dependencies=protected,
    )
    async def model_recommendation_view(
        profile: PerformanceProfile = PerformanceProfile.NORMAL,
    ) -> HardwareProfileView:
        hardware = await asyncio.to_thread(
            services.models.hardware,
            services.settings.data_dir,
        )
        recommendation = await services.models.recommend(profile, hardware=hardware)
        return services.models.recommendation_view(
            recommendation,
            scanned_at=hardware.collected_at,
        )

    @app.post(
        "/v1/models/hardware/scan",
        response_model=HardwareInfo,
        dependencies=protected,
    )
    async def hardware_scan(_: HardwareScanRequest) -> HardwareInfo:
        return await asyncio.to_thread(
            services.models.hardware,
            services.settings.data_dir,
        )

    @app.post(
        "/v1/models/hardware/benchmark",
        response_model=HardwareBenchmark,
        dependencies=protected,
    )
    async def hardware_benchmark(request: HardwareBenchmarkRequest) -> HardwareBenchmark:
        # Pydantic has already verified the explicit BENCHMARK confirmation.
        async with services.jobs.writer(fail_if_active=True), services.resources.indexing():
            return await services.models.benchmark(request.profile, tier=request.tier)

    @app.post(
        "/v1/models/recommendation",
        response_model=ModelStackRecommendation,
        dependencies=protected,
    )
    async def model_recommendation(
        request: ModelRecommendationRequest,
    ) -> ModelStackRecommendation:
        if request.workspace_id is not None:
            services.workspaces.get(request.workspace_id)
        return await services.models.recommend(request.performance_profile)

    @app.post(
        "/v1/workspaces/{workspace_id}/model-profile/preflight",
        response_model=ModelProfilePreflight,
        dependencies=protected,
    )
    async def model_profile_preflight(
        workspace_id: str,
        request: ModelProfilePreflightRequest,
    ) -> ModelProfilePreflight:
        services.workspaces.get(workspace_id)
        if request.workspace_id is not None and request.workspace_id != workspace_id:
            raise ConflictError("Model profile request belongs to another workspace")
        if request.benchmark_tier is not None:
            raise ConflictError(
                "A benchmark tier cannot be asserted in profile preflight; run the explicitly "
                "confirmed hardware benchmark first"
            )
        current = services.features.config(workspace_id)
        configured = services.features.configured_model_settings(workspace_id)
        index_has_documents = services.store.workspace_has_corpus(workspace_id)
        embedding_provider, embedding_digest = current_embedding_identity(workspace_id, configured)
        preflight = await services.models.profile_preflight(
            request.performance_profile,
            current_roles={
                role: str(configured[role]) if configured.get(role) else None
                for role in ("chat", "vl", "embedding", "rerank")
            },
            current_vector_dimension=int(configured.get("vector_dimension") or 0),
            current_embedding_provider=embedding_provider,
            current_embedding_digest=embedding_digest,
            index_has_documents=index_has_documents,
        )
        services.store.save_import_preflight(
            model_profile_preflight_key(workspace_id, preflight.recommendation.recommendation_id),
            workspace_id,
            {
                "kind": "model-profile-v1.1",
                "config_etag": current.etag,
                "preflight": preflight.model_dump(mode="json"),
            },
        )
        return preflight

    @app.post(
        "/v1/workspaces/{workspace_id}/model-profile/apply",
        response_model=ConfigDocument,
        dependencies=protected,
    )
    async def apply_model_profile(
        workspace_id: str,
        request: ModelProfileApplyRequest,
        response: Response,
    ) -> ConfigDocument:
        workspace_manifest = services.workspaces.get(workspace_id)
        if workspace_manifest.read_only:
            raise ConflictError("Read-only workspaces cannot change or download model profiles")
        stored = services.store.get_import_preflight(
            model_profile_preflight_key(workspace_id, request.preflight_id),
            workspace_id,
        )
        if stored.get("kind") != "model-profile-v1.1":
            raise ConflictError("Preflight is not a V1.1 model-profile preflight")
        preflight = ModelProfilePreflight.model_validate(stored.get("preflight"))
        async with services.jobs.writer(fail_if_active=True):
            ensure_index_queryable(workspace_id)
            current = services.features.config(workspace_id)
            if current.etag != stored.get("config_etag"):
                raise ConflictError("Workspace configuration changed after model preflight")
            if not preflight.can_apply:
                raise ConflictError(
                    "The pinned model stack could not be verified; no models were downloaded "
                    "and no profile was applied"
                )
            if preflight.requires_reindex:
                raise ConflictError(
                    "The recommended embedding model changes the vector space. Use the full "
                    "reindex workflow before applying this profile; no models were downloaded "
                    "and no partial profile was applied."
                )
            if preflight.downloads and request.download_consent is None:
                raise ConflictError(
                    "Recommended models are not installed; explicit DOWNLOAD_MODELS consent is "
                    "required",
                    details={"models": [item.model for item in preflight.downloads]},
                )
            if preflight.downloads:
                hugging_face_downloads = {
                    assignment.artifact_id: assignment
                    for assignment in preflight.downloads
                    if assignment.provider == CatalogProvider.HUGGING_FACE
                }
                required_bytes = sum(
                    item.download_bytes for item in hugging_face_downloads.values()
                )
                if required_bytes:
                    cache_path = services.models.hugging_face_cache_root()
                    storage_path = cache_path
                    while not storage_path.exists() and storage_path != storage_path.parent:
                        storage_path = storage_path.parent
                    free_bytes = shutil.disk_usage(storage_path).free
                    # snapshot_download may materialize metadata and files not
                    # represented by the headline weight size.
                    safety_margin = 2 * 1024**3 + required_bytes // 2
                    if free_bytes < required_bytes + safety_margin:
                        raise ConflictError(
                            "Not enough free storage in the Hugging Face cache filesystem",
                            details={
                                "cache_path": str(cache_path),
                                "required_bytes": required_bytes,
                                "safety_margin_bytes": safety_margin,
                                "available_bytes": free_bytes,
                            },
                        )
                async with services.resources.indexing():
                    await services.models.install_assignments(preflight.downloads)
                    services.runs.invalidate_model_inventory()
            async with services.resources.indexing():
                ensure_index_queryable(workspace_id)
                current = services.features.config(workspace_id)
                if current.etag != stored.get("config_etag"):
                    raise ConflictError("Workspace configuration changed after model preflight")
                configured = services.features.configured_model_settings(workspace_id)
                embedding_provider, embedding_digest = current_embedding_identity(
                    workspace_id, configured
                )
                refreshed = await services.models.profile_preflight(
                    preflight.recommendation.profile,
                    current_roles={
                        role: str(configured[role]) if configured.get(role) else None
                        for role in ("chat", "vl", "embedding", "rerank")
                    },
                    current_vector_dimension=int(configured.get("vector_dimension") or 0),
                    current_embedding_provider=embedding_provider,
                    current_embedding_digest=embedding_digest,
                    index_has_documents=services.store.workspace_has_corpus(workspace_id),
                )
                if (
                    refreshed.recommendation.recommendation_id
                    != preflight.recommendation.recommendation_id
                ):
                    raise ConflictError(
                        "Hardware or catalog recommendation changed; run model preflight again"
                    )
                if refreshed.downloads:
                    raise ConflictError(
                        "One or more pinned models could not be verified after download",
                        details={"models": [item.model for item in refreshed.downloads]},
                    )
                if refreshed.requires_reindex:
                    raise ConflictError(
                        "The library changed after model preflight and now requires a full "
                        "rebuild; no partial profile was applied."
                    )
                if not refreshed.can_apply:
                    raise ConflictError("The pinned model stack failed its integrity checks")

                assignments = {
                    item.role.value: item for item in refreshed.recommendation.assignments
                }
                definition = next(
                    item
                    for item in services.models.curated_catalog().tiers
                    if item.tier == refreshed.recommendation.stack_tier
                )
                defaults = ModelDefaultsRequest(
                    chat=assignments["chat"].model,
                    vl=assignments["vl"].model,
                    embedding=assignments["embedding"].model,
                    rerank=assignments["rerank"].model,
                    embedding_provider="ollama",
                    rerank_provider="cross-encoder",
                    vector_dim=definition.embedding_dimension,
                )
                config = services.features.apply_model_defaults(
                    workspace_id,
                    defaults,
                    current.etag,
                    profile_metadata={
                        "catalog_id": refreshed.recommendation.catalog_id,
                        "catalog_release": refreshed.recommendation.catalog_release,
                        "recommendation_id": refreshed.recommendation.recommendation_id,
                        "hardware_tier": refreshed.recommendation.stack_tier.value,
                        "performance_profile": refreshed.recommendation.profile.value,
                        "context_tokens": refreshed.recommendation.context_tokens,
                        "expert_mode": False,
                        "artifacts": {
                            role: {
                                "model": item.model,
                                "provider": item.provider.value,
                                "digest": item.digest,
                                "revision": item.revision,
                            }
                            for role, item in assignments.items()
                            if role in {"chat", "vl", "embedding", "rerank"}
                        },
                    },
                )
        response.headers["ETag"] = f'"{config.etag}"'
        await services.events.emit(
            "config.changed",
            correlation_id=workspace_id,
            workspace_id=workspace_id,
            payload={
                "etag": config.etag,
                "operation": "model-profile-v1.1",
                "hardware_tier": refreshed.recommendation.stack_tier.value,
                "performance_profile": refreshed.recommendation.profile.value,
            },
        )
        return config

    @app.post(
        "/v1/workspaces/{workspace_id}/model-profile/apply-and-reindex",
        response_model=IdempotentResult,
        status_code=202,
        dependencies=protected,
    )
    async def apply_model_profile_and_reindex(
        workspace_id: str,
        request: ModelProfileApplyAndReindexRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> IdempotentResult:
        """Stage a verified profile and activate it at the rebuild boundary."""

        workspace_manifest = services.workspaces.get(workspace_id)
        if workspace_manifest.read_only:
            raise ConflictError("Read-only workspaces cannot rebuild model profiles")
        enforce_content_egress(workspace_id, services.workspaces.ollama_url)
        stored = services.store.get_import_preflight(
            model_profile_preflight_key(workspace_id, request.preflight_id),
            workspace_id,
        )
        if stored.get("kind") != "model-profile-v1.1":
            raise ConflictError("Preflight is not a model-profile preflight")
        preflight = ModelProfilePreflight.model_validate(stored.get("preflight"))
        replay = services.jobs.profile_reindex_replay(
            workspace_id,
            profile_preflight_id=request.preflight_id,
            indexing=request.indexing.model_dump(mode="json"),
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            return IdempotentResult(id=replay.id, reused=True)
        if not preflight.requires_reindex:
            raise ConflictError(
                "This profile does not change the embedding space; use APPLY instead"
            )

        async with (
            services.jobs.writer(fail_if_active=True) as writer_lease,
            services.resources.indexing(),
        ):
            # Up to this point the old config and old index remain paired and
            # queryable. Model installation is consented but does not publish
            # any workspace mutation.
            ensure_index_queryable(workspace_id)
            current = services.features.config(workspace_id)
            if current.etag != stored.get("config_etag"):
                raise ConflictError("Workspace configuration changed after model preflight")
            if not preflight.can_apply:
                raise ConflictError(
                    "The pinned model stack could not be verified; no profile was staged"
                )
            if preflight.downloads and request.download_consent is None:
                raise ConflictError(
                    "Recommended models are not installed; explicit DOWNLOAD_MODELS consent "
                    "is required",
                    details={"models": [item.model for item in preflight.downloads]},
                )
            if preflight.downloads:
                hugging_face_downloads = {
                    assignment.artifact_id: assignment
                    for assignment in preflight.downloads
                    if assignment.provider == CatalogProvider.HUGGING_FACE
                }
                required_bytes = sum(
                    item.download_bytes for item in hugging_face_downloads.values()
                )
                if required_bytes:
                    cache_path = services.models.hugging_face_cache_root()
                    storage_path = cache_path
                    while not storage_path.exists() and storage_path != storage_path.parent:
                        storage_path = storage_path.parent
                    free_bytes = shutil.disk_usage(storage_path).free
                    safety_margin = 2 * 1024**3 + required_bytes // 2
                    if free_bytes < required_bytes + safety_margin:
                        raise ConflictError(
                            "Not enough free storage in the Hugging Face cache filesystem",
                            details={
                                "cache_path": str(cache_path),
                                "required_bytes": required_bytes,
                                "safety_margin_bytes": safety_margin,
                                "available_bytes": free_bytes,
                            },
                        )
                await services.models.install_assignments(preflight.downloads)
                services.runs.invalidate_model_inventory()

            current = services.features.config(workspace_id)
            if current.etag != stored.get("config_etag"):
                raise ConflictError("Workspace configuration changed after model preflight")
            configured = services.features.configured_model_settings(workspace_id)
            embedding_provider, embedding_digest = current_embedding_identity(
                workspace_id, configured
            )
            refreshed = await services.models.profile_preflight(
                preflight.recommendation.profile,
                current_roles={
                    role: str(configured[role]) if configured.get(role) else None
                    for role in ("chat", "vl", "embedding", "rerank")
                },
                current_vector_dimension=int(configured.get("vector_dimension") or 0),
                current_embedding_provider=embedding_provider,
                current_embedding_digest=embedding_digest,
                index_has_documents=services.store.workspace_has_corpus(workspace_id),
            )
            if (
                refreshed.recommendation.recommendation_id
                != preflight.recommendation.recommendation_id
            ):
                raise ConflictError(
                    "Hardware or catalog recommendation changed; run model preflight again"
                )
            if refreshed.downloads:
                raise ConflictError(
                    "One or more pinned models could not be verified after download",
                    details={"models": [item.model for item in refreshed.downloads]},
                )
            if not refreshed.requires_reindex:
                raise ConflictError(
                    "Embedding identity changed while preparing the rebuild; run preflight again"
                )
            if not refreshed.can_apply:
                raise ConflictError("The pinned model stack failed its integrity checks")
            required_assignments = [
                item
                for item in refreshed.recommendation.assignments
                if item.role.value != "visual-embedding"
            ]

            def normalized_digest(value: str | None) -> str:
                return (value or "").casefold().removeprefix("sha256:")

            unverified = [
                item.model
                for item in required_assignments
                if normalized_digest(item.installed_digest) != normalized_digest(item.digest)
            ]
            if unverified:
                raise ConflictError(
                    "Every required model must be installed and digest-verified before staging",
                    details={"models": unverified},
                )
            assignments = {item.role.value: item for item in refreshed.recommendation.assignments}
            embedding_assignment = assignments["embedding"]
            if embedding_assignment.provider.value != "ollama":
                raise ConflictError(
                    "Profile rebuild currently requires a locally digestable Ollama embedder"
                )
            definition = next(
                item
                for item in services.models.curated_catalog().tiers
                if item.tier == refreshed.recommendation.stack_tier
            )
            defaults = ModelDefaultsRequest(
                chat=assignments["chat"].model,
                vl=assignments["vl"].model,
                embedding=embedding_assignment.model,
                rerank=assignments["rerank"].model,
                embedding_provider="ollama",
                rerank_provider="cross-encoder",
                vector_dim=definition.embedding_dimension,
            )
            profile_metadata = {
                "catalog_id": refreshed.recommendation.catalog_id,
                "catalog_release": refreshed.recommendation.catalog_release,
                "recommendation_id": refreshed.recommendation.recommendation_id,
                "hardware_tier": refreshed.recommendation.stack_tier.value,
                "performance_profile": refreshed.recommendation.profile.value,
                "context_tokens": refreshed.recommendation.context_tokens,
                "expert_mode": False,
                "artifacts": {
                    role: {
                        "model": item.model,
                        "provider": item.provider.value,
                        "digest": item.digest,
                        "revision": item.revision,
                    }
                    for role, item in assignments.items()
                    if role in {"chat", "vl", "embedding", "rerank"}
                },
            }
            target = services.features.render_model_defaults(
                workspace_id,
                defaults,
                current.etag,
                profile_metadata=profile_metadata,
            )
            job, reused = await services.jobs.start_profile_reindex_under_writer(
                workspace_id,
                writer_lease=writer_lease,
                profile_preflight_id=request.preflight_id,
                target_config_content=target.content,
                expected_current_etag=current.etag,
                target_config_etag=target.etag,
                expected_embedding_model=embedding_assignment.model,
                expected_embedding_digest=embedding_assignment.digest,
                recommendation_id=refreshed.recommendation.recommendation_id,
                indexing=request.indexing.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            )
        services.jobs.spawn_profile_reindex(job.id)
        return IdempotentResult(id=job.id, reused=reused)

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
            model_settings = services.features.configured_model_settings(workspace_id)
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
                profile = model_settings.get("profile")
                profile_context = (
                    int(profile.get("context_tokens") or 0) if isinstance(profile, dict) else 0
                )
                await services.models.load(
                    chat,
                    min(max(profile_context or 8192, 4096), 131072),
                    keep_alive,
                )
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
        model_settings = services.features.configured_model_settings(workspace_id)
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
        generation: dict[str, object] | None = None
        generation_reader = getattr(services.store, "workspace_index_generation", None)
        if callable(generation_reader):
            generation = generation_reader(workspace_id)
            if generation is not None:
                generation_status = str(generation.get("status", "not_ready")).casefold()
        index_ready = generation_status in {"ready", "none"}
        profile = model_settings.get("profile")
        profile_artifacts = (
            profile.get("artifacts")
            if isinstance(profile, dict) and profile.get("expert_mode") is False
            else {}
        )
        profile_artifacts = profile_artifacts if isinstance(profile_artifacts, dict) else {}

        def digest_matches(role: str, model: str | None) -> bool:
            artifact = profile_artifacts.get("embedding" if role == "embedding" else "chat")
            if not isinstance(artifact, dict):
                return True
            if str(artifact.get("provider") or "") != "ollama" or normalized(
                str(artifact.get("model") or "")
            ) != normalized(model):
                return False
            expected = str(artifact.get("digest") or "").removeprefix("sha256:")
            actual = (
                loaded[normalized(model)].digest.removeprefix("sha256:")
                if model and normalized(model) in loaded
                else ""
            )
            return bool(
                expected and actual and hmac.compare_digest(expected.casefold(), actual.casefold())
            )

        catalog_digest_matches = {
            role: digest_matches(role, model) for role, model in required.items()
        }
        generation_config = (
            dict(generation.get("config") or {}) if isinstance(generation, dict) else {}
        )
        indexed_embedding_digest = str(
            generation_config.get("embedding_digest") or ""
        ).removeprefix("sha256:")
        loaded_embedding_digest = (
            loaded[normalized(required["embedding"])].digest.removeprefix("sha256:")
            if required["embedding"] and normalized(required["embedding"]) in loaded
            else ""
        )
        index_embedding_digest_match = bool(
            not indexed_embedding_digest
            or (
                loaded_embedding_digest
                and hmac.compare_digest(
                    indexed_embedding_digest.casefold(), loaded_embedding_digest.casefold()
                )
            )
        )
        models_ready = (
            all(resident.values())
            and all(required.values())
            and all(catalog_digest_matches.values())
            and index_embedding_digest_match
        )
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
                "catalog_digest_matches": catalog_digest_matches,
                "index_embedding_digest_match": index_embedding_digest_match,
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
        ensure_index_queryable(workspace_id)
        async with services.jobs.writer(fail_if_active=True), services.resources.indexing():
            ensure_index_queryable(workspace_id)
            config = services.features.apply_model_defaults(
                workspace_id,
                request,
                if_match,
                profile_metadata={"expert_mode": True},
            )
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

        try:
            mutation_lease = await acquire_model_mutation_lease()
        except BaseException:
            path.unlink(missing_ok=True)
            raise

        async def stream_import():
            try:
                async for line in services.models.import_gguf(
                    path, filename, model, category, digest.hexdigest()
                ):
                    yield line
            finally:
                services.runs.invalidate_model_inventory()
                path.unlink(missing_ok=True)
                await mutation_lease.aclose()

        return StreamingResponse(stream_import(), media_type="application/x-ndjson")

    @app.post("/v1/models/pull", dependencies=protected)
    async def pull_model(request: PullModelRequest) -> StreamingResponse:
        mutation_lease = await acquire_model_mutation_lease()

        async def stream_pull():
            try:
                async for line in services.models.pull(request.model):
                    yield line
            finally:
                services.runs.invalidate_model_inventory()
                await mutation_lease.aclose()

        return StreamingResponse(stream_pull(), media_type="application/x-ndjson")

    @app.get(
        "/v1/workspaces/{workspace_id}/conversion-artifacts",
        response_model=ConversionArtifactsReport,
        dependencies=protected,
    )
    async def conversion_artifacts(workspace_id: str) -> ConversionArtifactsReport:
        """What an import worker would find in the offline model cache."""

        config = await asyncio.to_thread(services.workspaces.app_config, workspace_id)
        tokenizer = str(getattr(config.processing, "chunking_tokenizer", "") or "")
        return services.models.conversion_artifacts_report(
            tokenizer, services.models._reranker_name(config)
        )

    @app.post(
        "/v1/workspaces/{workspace_id}/conversion-artifacts",
        dependencies=protected,
    )
    async def admit_conversion_artifacts(workspace_id: str) -> StreamingResponse:
        """Admit the models the ingest pipeline needs, by running it once.

        Import workers are deliberately offline, so the artifacts have to be
        admitted here, in the parent, before the first import.  This converts a
        tiny probe document with the workspace's own configuration, which pulls
        exactly the models that configuration resolves.
        """

        config = await asyncio.to_thread(services.workspaces.app_config, workspace_id)
        mutation_lease = await acquire_model_mutation_lease()

        async def stream_admission():
            try:
                async with services.resources.indexing():
                    async for event in services.models.admit_conversion_artifacts(config):
                        yield (json.dumps(event) + "\n").encode()
            except Exception as exc:
                yield (json.dumps({"error": str(exc)}) + "\n").encode()
            finally:
                await mutation_lease.aclose()

        return StreamingResponse(stream_admission(), media_type="application/x-ndjson")

    @app.post("/v1/models/install-hugging-face", dependencies=protected)
    async def install_hugging_face_model(request: PullModelRequest) -> StreamingResponse:
        """Install one revision-pinned Hugging Face artifact from the release catalog.

        Presets may combine Ollama and cross-encoder artifacts.  This endpoint keeps
        arbitrary repository downloads disabled: only an exact model from the signed,
        release-bound catalog can be installed.
        """

        artifact = next(
            (
                candidate
                for candidate in services.models.curated_catalog().artifacts
                if candidate.provider == CatalogProvider.HUGGING_FACE
                and candidate.model == request.model
            ),
            None,
        )
        if artifact is None:
            raise ConflictError(
                "Hugging Face preset model is not pinned in the release catalog",
                details={"model": request.model},
            )
        assignment = ModelAssignment(
            role=artifact.roles[0],
            artifact_id=artifact.id,
            provider=artifact.provider,
            model=artifact.model,
            revision=artifact.revision,
            digest=artifact.digest,
            quantization=artifact.quantization,
            install_state=ModelInstallState.NOT_INSTALLED,
            installed_digest=None,
            download_bytes=artifact.download_bytes,
        )
        mutation_lease = await acquire_model_mutation_lease()

        async def stream_install():
            try:
                yield (
                    json.dumps(
                        {
                            "status": "downloading pinned cross-encoder",
                            "completed": 0,
                            "total": artifact.download_bytes,
                        }
                    )
                    + "\n"
                ).encode()
                await services.models.install_assignments([assignment])
                yield (
                    json.dumps(
                        {
                            "status": "verified",
                            "completed": artifact.download_bytes,
                            "total": artifact.download_bytes,
                        }
                    )
                    + "\n"
                ).encode()
            except Exception as exc:
                yield (json.dumps({"error": str(exc)}) + "\n").encode()
            finally:
                services.runs.invalidate_model_inventory()
                await mutation_lease.aclose()

        return StreamingResponse(stream_install(), media_type="application/x-ndjson")

    @app.post(
        "/v1/models/load",
        response_model=ModelOperationResult,
        dependencies=protected,
    )
    async def load_model(request: LoadModelRequest) -> ModelOperationResult:
        async with await acquire_model_mutation_lease():
            return await services.models.load(
                request.model, request.context_tokens, request.keep_alive
            )

    @app.post(
        "/v1/models/unload",
        response_model=ModelOperationResult,
        dependencies=protected,
    )
    async def unload_model(request: UnloadModelRequest) -> ModelOperationResult:
        async with await acquire_model_mutation_lease():
            return await services.models.unload(request.model)

    @app.delete(
        "/v1/models",
        response_model=ModelOperationResult,
        dependencies=protected,
    )
    async def delete_model(request: DeleteModelRequest) -> ModelOperationResult:
        async with await acquire_model_mutation_lease():
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
            result = await services.models.delete(request.model)
            services.runs.invalidate_model_inventory()
            return result

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

    @app.get(
        "/v1/workspaces/{workspace_id}/privacy",
        response_model=PrivacyPolicy,
        dependencies=protected,
    )
    async def get_workspace_privacy(workspace_id: str, response: Response) -> PrivacyPolicy:
        workspace = services.workspaces.get(workspace_id)
        response.headers["ETag"] = f'"{workspace.etag}"'
        response.headers["Cache-Control"] = "no-store"
        return read_privacy_policy(workspace)

    @app.put(
        "/v1/workspaces/{workspace_id}/privacy",
        response_model=PrivacyPolicy,
        dependencies=protected,
    )
    async def update_workspace_privacy(
        workspace_id: str,
        request: UpdatePrivacyPolicyRequest,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> PrivacyPolicy:
        # A policy change is a content-routing mutation.  Drain active readers
        # and reject queued/paused corpus writers so no request can retain the
        # permissions from the previous policy after this call returns.
        async with services.jobs.writer(fail_if_active=True), services.resources.indexing():
            current = require_mutable_workspace(workspace_id, if_match)
            policy = request.policy
            checked_egress_policy(policy)
            target = privacy_policy_path(current, create=False)
            previous_file = target.is_file()
            previous_policy = read_privacy_policy(current)
            write_privacy_policy(current, policy)
            try:
                updated = persist_workspace_change(
                    current,
                    {
                        "privacy_mode": policy.mode,
                        "cloud_acknowledged": policy.cloud_acknowledged,
                    },
                )
            except Exception:
                if previous_file:
                    write_privacy_policy(current, previous_policy)
                else:
                    target.unlink(missing_ok=True)
                raise
        response.headers["ETag"] = f'"{updated.etag}"'
        response.headers["Cache-Control"] = "no-store"
        await services.events.emit(
            "workspace.privacy.changed",
            correlation_id=workspace_id,
            workspace_id=workspace_id,
            payload={
                "mode": policy.mode.value,
                "trusted_endpoint_count": len(policy.trusted_endpoints),
                "cloud_acknowledged": policy.cloud_acknowledged,
            },
        )
        return policy

    @app.get(
        "/v1/workspaces/{workspace_id}/retention",
        response_model=RetentionPolicy,
        dependencies=protected,
    )
    async def get_workspace_retention(workspace_id: str, response: Response) -> RetentionPolicy:
        workspace = services.workspaces.get(workspace_id)
        response.headers["ETag"] = f'"{workspace.etag}"'
        response.headers["Cache-Control"] = "no-store"
        return services.store.get_retention_policy(workspace_id)

    @app.put(
        "/v1/workspaces/{workspace_id}/retention",
        response_model=RetentionPolicy,
        dependencies=protected,
    )
    async def update_workspace_retention(
        workspace_id: str,
        request: UpdateRetentionPolicyRequest,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> RetentionPolicy:
        current = require_mutable_workspace(workspace_id, if_match)
        previous = services.store.get_retention_policy(workspace_id)
        policy = services.store.set_retention_policy(workspace_id, request.policy)
        try:
            updated = persist_workspace_change(current)
        except Exception:
            services.store.set_retention_policy(workspace_id, previous)
            raise
        response.headers["ETag"] = f'"{updated.etag}"'
        response.headers["Cache-Control"] = "no-store"
        await services.events.emit(
            "workspace.retention.changed",
            correlation_id=workspace_id,
            workspace_id=workspace_id,
            payload={"profile": policy.profile.value},
        )
        return policy

    @app.post(
        "/v1/workspaces/{workspace_id}/retention/cleanup/preflight",
        response_model=RetentionCleanupPlan,
        dependencies=protected,
    )
    async def preflight_retention_cleanup(
        workspace_id: str, response: Response
    ) -> RetentionCleanupPlan:
        workspace = services.workspaces.get(workspace_id)
        plan = services.store.plan_retention_cleanup(workspace_id)
        remember_cleanup_plan(plan)
        response.headers["ETag"] = f'"{workspace.etag}"'
        response.headers["Cache-Control"] = "no-store"
        return plan

    @app.post(
        "/v1/workspaces/{workspace_id}/retention/cleanup",
        response_model=RetentionPurgeResult,
        dependencies=protected,
    )
    async def execute_retention_cleanup(
        workspace_id: str,
        request: ExecuteRetentionCleanupRequest,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> RetentionPurgeResult:
        # Expired runs and their immutable media blobs form one retention
        # operation. Drain active readers and reject active import/rebuild jobs
        # so a newly completed run cannot appear between mark and sweep.
        async with services.jobs.writer(fail_if_active=True), services.resources.indexing():
            current = require_mutable_workspace(workspace_id, if_match)
            plan = retention_cleanup_plans.get(request.plan_id)
            if plan is None or plan.workspace_id != workspace_id:
                raise NotFoundError("Retention cleanup plan was not found; create a new preflight")
            retention_cleanup_plans.pop(request.plan_id, None)
            result = services.store.purge_retention_cleanup(
                plan,
                confirmation=request.confirm,
            )
            assets = [
                MediaAsset.model_validate(item)
                for item in services.store.all_book_media_assets(workspace_id)
            ]
            visual_evidence = services.store.run_visual_evidence(workspace_id)
            marked = mark_media_blob_references(
                assets,
                visual_evidence=visual_evidence,
            )
            media_sweep = await asyncio.to_thread(
                sweep_unreferenced_media_blobs,
                Path(current.path) / "database",
                marked,
                dry_run=False,
            )
            updated = persist_workspace_change(current)
        response.headers["ETag"] = f'"{updated.etag}"'
        response.headers["Cache-Control"] = "no-store"
        await services.events.emit(
            "workspace.retention.cleaned",
            correlation_id=workspace_id,
            workspace_id=workspace_id,
            payload={
                "purged_records": sum(result.purged_records.values()),
                "dependent_records": result.dependent_records,
                "media_blobs_removed": len(media_sweep.removed),
                "media_bytes_reclaimed": media_sweep.reclaimed_bytes,
            },
        )
        return result

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
        cloned = services.workspaces.clone(workspace_id, request)
        services.features.reconcile_hidden_documents(cloned.id)
        return cloned

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
        enforce_content_egress(workspace_id, services.workspaces.ollama_url)
        enforce_url_import_policy(workspace_id, request.sources)
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
        enforce_url_import_policy(workspace_id, request.sources)
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
        enforce_content_egress(workspace_id, services.workspaces.ollama_url)
        enforce_url_import_policy(workspace_id, request.sources)
        sources = await asyncio.to_thread(
            services.textbooks.validate_commit,
            workspace_id,
            request.preflight_id,
            request.sources,
        )
        enforce_url_import_policy(workspace_id, sources)
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
        enforce_content_egress(workspace_id, services.workspaces.ollama_url)
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
        enforce_content_egress(workspace_id, services.workspaces.ollama_url)
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

    @app.get(
        "/v1/workspaces/{workspace_id}/documents/{document_id}/media",
        response_model=list[MediaAsset],
        dependencies=protected,
    )
    async def document_media(
        workspace_id: str,
        document_id: str,
        page: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> list[MediaAsset]:
        services.workspaces.get(workspace_id)
        services.store.book_record(workspace_id, document_id)
        return [
            MediaAsset.model_validate(item)
            for item in services.store.book_media_assets(
                workspace_id,
                document_id,
                page_no=page,
                limit=limit,
            )
        ]

    @app.get(
        "/v1/workspaces/{workspace_id}/media/search",
        response_model=list[MediaAsset],
        dependencies=protected,
    )
    async def search_media(
        workspace_id: str,
        query: Annotated[str, Query(min_length=1, max_length=1000)],
        document_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 16,
    ) -> list[MediaAsset]:
        services.workspaces.get(workspace_id)
        results = services.store.search_book_media(
            workspace_id,
            query,
            logical_document_id=document_id,
            limit=limit,
        )
        return [
            MediaAsset.model_validate(
                {key: item[key] for key in MediaAsset.model_fields if key in item}
            )
            for item in results
        ]

    @app.get(
        "/v1/workspaces/{workspace_id}/media/{media_id}",
        response_model=MediaAsset,
        dependencies=protected,
    )
    async def media_asset(workspace_id: str, media_id: str) -> MediaAsset:
        services.workspaces.get(workspace_id)
        return MediaAsset.model_validate(services.store.book_media_asset(workspace_id, media_id))

    @app.get(
        "/v1/workspaces/{workspace_id}/media/{media_id}/okf-proposal",
        response_model=OKFMediaProposal,
        dependencies=protected,
    )
    async def media_okf_proposal(workspace_id: str, media_id: str) -> OKFMediaProposal:
        try:
            return OKFMediaProposal.model_validate(
                services.visual_evidence.okf_proposal(workspace_id, media_id)
            )
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc

    def media_response(path: Path) -> FileResponse:
        return FileResponse(
            path,
            media_type="image/webp",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def media_file(workspace_id: str, media_id: str, *, thumbnail: bool) -> FileResponse:
        try:
            path = services.visual_evidence.asset_path(
                workspace_id,
                media_id,
                thumbnail=thumbnail,
            )
        except FileNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc
        return media_response(path)

    @app.get(
        "/v1/workspaces/{workspace_id}/media/{media_id}/thumbnail",
        dependencies=protected,
    )
    async def media_thumbnail(workspace_id: str, media_id: str) -> FileResponse:
        return await media_file(workspace_id, media_id, thumbnail=True)

    @app.get(
        "/v1/workspaces/{workspace_id}/media/{media_id}/crop",
        dependencies=protected,
    )
    async def media_crop(workspace_id: str, media_id: str) -> FileResponse:
        return await media_file(workspace_id, media_id, thumbnail=False)

    async def media_blob_file(
        workspace_id: str,
        pixel_sha256: str,
        *,
        thumbnail: bool,
    ) -> FileResponse:
        try:
            path = services.visual_evidence.blob_path(
                workspace_id,
                pixel_sha256,
                thumbnail=thumbnail,
            )
        except FileNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc
        return media_response(path)

    @app.get(
        "/v1/workspaces/{workspace_id}/media/blobs/{pixel_sha256}/thumbnail",
        dependencies=protected,
    )
    async def media_blob_thumbnail(workspace_id: str, pixel_sha256: str) -> FileResponse:
        return await media_blob_file(workspace_id, pixel_sha256, thumbnail=True)

    @app.get(
        "/v1/workspaces/{workspace_id}/media/blobs/{pixel_sha256}/crop",
        dependencies=protected,
    )
    async def media_blob_crop(workspace_id: str, pixel_sha256: str) -> FileResponse:
        return await media_blob_file(workspace_id, pixel_sha256, thumbnail=False)

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
        async with services.jobs.writer(fail_if_active=True), services.resources.indexing():
            await services.features.delete_document(workspace_id, document_id)
        await services.events.emit(
            "document.changed",
            correlation_id=document_id,
            workspace_id=workspace_id,
            payload={"operation": "removed", "document_id": document_id},
        )
        return Response(status_code=204)

    @app.post(
        "/v1/workspaces/{workspace_id}/documents/{document_id}/purge/preflight",
        response_model=DocumentPurgePlan,
        dependencies=protected,
    )
    async def document_purge_preflight(workspace_id: str, document_id: str) -> DocumentPurgePlan:
        require_mutable_workspace(workspace_id, None)
        plan = services.features.document_purge_preflight(workspace_id, document_id)
        remember_document_purge_plan(plan)
        return plan

    @app.post(
        "/v1/workspaces/{workspace_id}/documents/{document_id}/purge",
        response_model=DocumentPurgeResult,
        dependencies=protected,
    )
    async def purge_document(
        workspace_id: str,
        document_id: str,
        request: ExecuteDocumentPurgeRequest,
    ) -> DocumentPurgeResult:
        plan = document_purge_plans.pop(request.plan_id, None)
        if plan is None or plan.workspace_id != workspace_id or plan.document_id != document_id:
            raise ConflictError("Document purge preflight is missing, stale, or already used")
        async with services.jobs.writer(fail_if_active=True), services.resources.indexing():
            return await services.features.purge_document(
                plan,
                backup_confirmed=request.backup_confirm == "PURGE_BACKUPS",
            )

    @app.post(
        "/v1/workspaces/{workspace_id}/documents/{document_id}/restore",
        status_code=204,
        dependencies=protected,
    )
    async def restore_document(workspace_id: str, document_id: str) -> Response:
        async with services.jobs.writer(fail_if_active=True), services.resources.indexing():
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
        enforce_content_egress(workspace_id, services.workspaces.ollama_url)
        source = services.features.get_source(workspace_id, source_id)
        request = IngestRequest(
            sources=[
                {
                    "type": "url" if source.type == "url" else "file",
                    "path": source.location,
                }
            ]
        )
        enforce_url_import_policy(workspace_id, request.sources)
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
        "/v1/workspaces/{workspace_id}/evaluations/import",
        response_model=EvaluationReport,
        status_code=201,
        dependencies=protected,
    )
    async def import_evaluation(
        workspace_id: str, request: ImportEvaluationRequest
    ) -> EvaluationReport:
        require_mutable_workspace(workspace_id, None)
        return await asyncio.to_thread(
            services.evaluations.import_gold,
            workspace_id,
            request.cases,
            evaluation_id=request.id,
            baseline_id=request.baseline_id,
            require_reviewed=request.require_reviewed,
        )

    @app.post(
        "/v1/workspaces/{workspace_id}/evaluations/run",
        response_model=EvaluationReport,
        dependencies=protected,
    )
    async def run_evaluation(workspace_id: str, request: RunEvaluationRequest) -> EvaluationReport:
        async with services.resources.chat():
            ensure_index_queryable(workspace_id)
            enforce_content_egress(workspace_id, services.workspaces.ollama_url)
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
        async with services.jobs.writer(fail_if_active=True), services.resources.indexing():
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
        async with services.jobs.writer(fail_if_active=True), services.resources.indexing():
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

    @app.put(
        "/v1/workspaces/{workspace_id}/backups/{backup_id}/pin",
        response_model=BackupSummary,
        dependencies=protected,
    )
    async def pin_backup(workspace_id: str, backup_id: str, request: PinRequest) -> BackupSummary:
        async with services.jobs.writer(fail_if_active=True):
            return await asyncio.to_thread(
                services.features.set_backup_pinned,
                workspace_id,
                backup_id,
                request.pinned,
            )

    @app.post(
        "/v1/workspaces/{workspace_id}/backups/{backup_id}/restore",
        response_model=BackupSummary,
        dependencies=protected,
    )
    async def restore_backup(
        workspace_id: str, backup_id: str, request: RestoreBackupRequest
    ) -> BackupSummary:
        async with services.jobs.writer(fail_if_active=True), services.resources.indexing():
            restored, safety = await asyncio.to_thread(
                services.features.restore_backup, workspace_id, backup_id
            )
            services.features.reconcile_hidden_documents(workspace_id)
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
        try:
            async with asyncio.timeout_at(deadline), services.resources.chat():
                ensure_index_queryable(workspace_id)
                # Resolve the published generation only after reader admission.
                # The corpus writer swaps the Store mapping and retires the old
                # Haiku rows under the exclusive side of this same lease.
                document_ids = services.store.resolve_segment_ids(
                    workspace_id, request.filters.active(), request.document_policy
                )
                enforce_content_egress(workspace_id, settings.ollama_url)
                retrieval_identity = await services.runs.verify_retrieval_identity(workspace_id)
                ranked, _ = await services.search.search(
                    services.workspaces.database_path(workspace_id),
                    request.query,
                    requested_limit=request.limit,
                    max_sources=request.options.max_sources,
                    document_filter=document_filter_for_ids(document_ids),
                    allowed_document_ids=(set(document_ids) if document_ids is not None else None),
                    profile=effective_retrieval_profile(workspace_id, request.options.profile),
                    reranker_digest=str(
                        retrieval_identity.get("model_digests", {}).get("reranker") or ""
                    )
                    or None,
                )
                confirmed_identity = await services.runs.verify_retrieval_identity(
                    workspace_id,
                    force_inventory_refresh=True,
                    check_residency=False,
                )
                services.runs._assert_runtime_pins_unchanged(retrieval_identity, confirmed_identity)
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
        started = time.perf_counter()
        try:
            async with asyncio.timeout_at(deadline), services.resources.chat():
                ensure_index_queryable(workspace_id)
                document_ids = services.store.resolve_segment_ids(
                    workspace_id, request.filters.active(), request.document_policy
                )
                enforce_content_egress(workspace_id, settings.ollama_url)
                retrieval_identity = await services.runs.verify_retrieval_identity(workspace_id)
                search_started = time.perf_counter()
                _, explanation = await services.search.search(
                    services.workspaces.database_path(workspace_id),
                    request.query,
                    requested_limit=request.limit,
                    max_sources=request.options.max_sources,
                    document_filter=document_filter_for_ids(document_ids),
                    allowed_document_ids=(set(document_ids) if document_ids is not None else None),
                    profile=effective_retrieval_profile(workspace_id, request.options.profile),
                    reranker_digest=str(
                        retrieval_identity.get("model_digests", {}).get("reranker") or ""
                    )
                    or None,
                )
                confirmed_identity = await services.runs.verify_retrieval_identity(
                    workspace_id,
                    force_inventory_refresh=True,
                    check_residency=False,
                )
                services.runs._assert_runtime_pins_unchanged(retrieval_identity, confirmed_identity)
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

    @app.put("/v1/runs/{run_id}/pin", response_model=RunSnapshot, dependencies=protected)
    async def pin_run(run_id: str, request: PinRequest) -> RunSnapshot:
        run = services.store.get_run(run_id)
        require_mutable_workspace(run.workspace_id, None)
        return services.store.update_run(run_id, pinned=request.pinned)

    @app.get(
        "/v1/runs/{run_id}/visual-evidence",
        response_model=VisualEvidenceResponse,
        dependencies=protected,
    )
    async def run_visual_evidence(run_id: str) -> VisualEvidenceResponse:
        cached = services.store.get_run_visual_evidence(run_id)
        if cached is not None:
            return VisualEvidenceResponse.model_validate(cached)
        run = services.store.get_run(run_id)
        ensure_index_queryable(run.workspace_id)
        async with services.resources.chat():
            ensure_index_queryable(run.workspace_id)
            return await asyncio.to_thread(services.visual_evidence.get_or_build, run_id)

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
        managed_source = None
        if citation.logical_document_id:
            with suppress(Exception):
                record = services.store.book_record(workspace_id, citation.logical_document_id)
                candidate = Path(str(record.get("managed_source") or "")).resolve()
                workspace_root = Path(workspace.path).resolve()
                if candidate.is_relative_to(workspace_root):
                    managed_source = candidate
        payload = await render_citation_preview(
            citation,
            Path(workspace.path) / ".oracle-cache" / "previews",
            max_px,
            managed_source=managed_source,
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

    @app.put("/v1/jobs/{job_id}/pin", response_model=JobSnapshot, dependencies=protected)
    async def pin_job(job_id: str, request: PinRequest) -> JobSnapshot:
        job = services.store.get_job(job_id)
        require_mutable_workspace(job.workspace_id, None)
        return services.store.update_job(job_id, pinned=request.pinned)

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

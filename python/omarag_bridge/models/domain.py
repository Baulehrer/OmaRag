from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapabilitySet(StrictModel):
    streaming_chat: bool = False
    question_images: bool = False
    analysis_images: bool = False
    multimodal_search: bool = False
    multimodal_reranking: bool = False
    visual_grounding: bool = False
    database_tags: bool = False
    native_ingester: bool = False
    evaluation: bool = False
    event_replay: bool = True
    workspaces: bool = True


class BackendMeta(StrictModel):
    api_version: str = "1.0"
    min_client_version: str = "1.0"
    max_client_version: str = "1.x"
    omarag_version: str = "1.0.0"
    haiku_version: str | None = None
    adapter: str | None = None
    backend_id: str
    capabilities: CapabilitySet
    deprecations: list[str] = Field(default_factory=list)


class HealthReport(StrictModel):
    status: str
    ready: bool
    checks: dict[str, bool | str | None] = Field(default_factory=dict)


class PrivacyMode(StrEnum):
    LOCAL = "local"
    CLOUD_ALLOWED = "cloud-allowed"


class EvidenceMode(StrEnum):
    STRICT = "strict"
    NORMAL = "normal"
    EXPLORE = "explore"


class DocumentStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REFERENCE = "reference"


class MetadataProposal(StrictModel):
    field: str
    value: Any
    source: str
    confidence: float = Field(ge=0.0, le=1.0)


class BookMetadata(StrictModel):
    """Confirmed bibliographic identity shared by every Haiku page segment."""

    work_id: str = ""
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    edition_label: str | None = None
    edition_number: int | None = Field(default=None, ge=1)
    publication_year: int | None = Field(default=None, ge=1000, le=3000)
    isbn: list[str] = Field(default_factory=list)
    language: str = "de"
    curriculum: str | None = None
    tags: list[str] = Field(default_factory=list)
    document_status: DocumentStatus = DocumentStatus.ACTIVE
    valid_from: date | None = None
    valid_to: date | None = None
    confirmed: bool = False


class ImportCandidate(StrictModel):
    id: str
    source: str
    fingerprint: str
    size_bytes: int = Field(default=0, ge=0)
    mtime_ns: int = Field(default=0, ge=0)
    metadata: BookMetadata
    proposals: list[MetadataProposal] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class ImportPreflightBatch(StrictModel):
    id: str
    candidates: list[ImportCandidate]


class DocumentQuality(StrictModel):
    score: float = Field(default=1.0, ge=0.0, le=1.0)
    pages_total: int = 0
    native_text_pages: int = 0
    ocr_pages: int = 0
    chunks: int = 0
    tables: int = 0
    formulas: int = 0
    pictures: int = 0
    provenance_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list)


class WorkspaceManifest(StrictModel):
    schema_version: int = 1
    id: str
    name: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    path: str
    read_only: bool = False
    haiku_compatible_range: str = "latest-gated"
    haiku_update_policy: str = "latest-gated"
    haiku_last_verified: str | None = None
    database_schema_version: str = "detected"
    embedding_provider: str = "ollama"
    embedding_model: str = ""
    vector_dimension: int | None = None
    processing_profile: str = "default"
    evidence_mode: EvidenceMode = EvidenceMode.STRICT
    document_policy: str = "prefer-current"
    privacy_mode: PrivacyMode = PrivacyMode.LOCAL
    cloud_acknowledged: bool = False
    etag: str


class WorkspaceSummary(StrictModel):
    id: str
    name: str
    path: str
    read_only: bool
    updated_at: datetime
    etag: str


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class JobProgressDetail(StrictModel):
    current_document: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    total_pages: int | None = None
    cache_hits: int = 0
    recovered_segments: int = 0
    memory_state: str = "ready"
    eta_seconds_low: float | None = Field(default=None, ge=0.0)
    eta_seconds_high: float | None = Field(default=None, ge=0.0)


class JobSnapshot(StrictModel):
    id: str
    workspace_id: str
    kind: str
    status: JobStatus
    progress: float = 0.0
    phase: str = "queued"
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    last_event_id: int | None = None
    checkpoint: str | None = None
    progress_detail: JobProgressDetail | None = None


class CitationAnchor(StrictModel):
    """A page-local Docling provenance target.

    Coordinates use a normalized top-left origin so every client can draw the
    highlight without knowing Docling's coordinate convention or PDF page size.
    """

    page: int
    doc_item_ref: str
    element_type: str | None = None
    x0: float = Field(ge=0.0, le=1.0)
    y0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)


class Citation(StrictModel):
    evidence_id: str | None = None
    chunk_id: str
    chunk_ids: list[str] = Field(default_factory=list)
    document_id: str | None = None
    logical_document_id: str | None = None
    source_uri: str | None = None
    document_title: str | None = None
    pages: list[int] = Field(default_factory=list)
    headings: list[str] = Field(default_factory=list)
    element_types: list[str] = Field(default_factory=list)
    doc_item_refs: list[str] = Field(default_factory=list)
    picture_refs: list[str] = Field(default_factory=list)
    primary_anchors: list[CitationAnchor] = Field(default_factory=list)
    context_anchors: list[CitationAnchor] = Field(default_factory=list)
    excerpt: str
    retrieval_rank: int | None = None
    rerank_score: float | None = None
    book: BookMetadata | None = None
    verification_status: str = "unverified"


class SearchHit(StrictModel):
    chunk_id: str
    content: str
    score: float | None = None
    pages: list[int] = Field(default_factory=list)
    document_id: str | None = None
    document_title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    search_type: str = "hybrid"


class RetrievalTiming(StrictModel):
    search_ms: float
    total_ms: float


class RetrievalExplanation(StrictModel):
    query: str
    candidates: list[SearchHit] = Field(default_factory=list)
    ranked: list[SearchHit] = Field(default_factory=list)
    timing: RetrievalTiming
    provider_notes: list[str] = Field(default_factory=list)


class AnswerCacheStatus(StrEnum):
    HIT = "hit"
    MISS = "miss"
    BYPASS = "bypass"


class SourceCheck(StrEnum):
    VERIFIED = "verified"
    REVIEWED = "reviewed"
    INSUFFICIENT = "insufficient"


class RunReceipt(StrictModel):
    """Small, user-facing account of how an answer was produced."""

    session_id: str
    turn: int = Field(ge=1)
    cache_status: AnswerCacheStatus
    total_ms: float = Field(ge=0.0)
    source_count: int = Field(ge=0)
    reused_source_count: int = Field(ge=0)
    new_source_count: int = Field(ge=0)
    source_check: SourceCheck
    phase_timings_ms: dict[str, float] = Field(default_factory=dict)
    retrieval_mode: str = "hybrid"
    rerank_status: str = "unknown"


class RunSnapshot(StrictModel):
    id: str
    workspace_id: str
    session_id: str
    status: JobStatus
    question: str
    evidence_mode: EvidenceMode
    answer: str = ""
    citations: list[Citation] = Field(default_factory=list)
    receipt: RunReceipt | None = None
    error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    last_event_id: int | None = None


class DocumentSummary(StrictModel):
    id: str
    title: str
    source: str
    segment_document_ids: list[str] = Field(default_factory=list)
    page_count: int | None = None
    parser_id: str = "docling"
    status: str = "indexed"
    imported_at: datetime
    fingerprint: str | None = None
    generation_id: str | None = None
    cache_status: str | None = None
    pipeline_stats: dict[str, Any] = Field(default_factory=dict)
    managed_source: str | None = None
    book: BookMetadata | None = None
    quality: DocumentQuality | None = None
    pipeline_version: str = "textbook-v1"
    size_bytes: int = Field(default=0, ge=0)
    archive_mode: str = "unknown"


class SourceDefinition(StrictModel):
    id: str
    name: str
    type: str
    location: str
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)


class ParserDefinition(StrictModel):
    id: str
    name: str
    description: str
    formats: list[str]
    provenance: bool
    structured_chunking: bool
    available: bool = True


class QualityReport(StrictModel):
    workspace_id: str
    status: str
    document_count: int
    completed_imports: int
    failed_jobs: int
    issues: list[str] = Field(default_factory=list)
    latest_evaluation_id: str | None = None
    retrieval_metrics: dict[str, float] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=utc_now)


class EvaluationCase(StrictModel):
    id: str
    question: str
    category: str = "section-location"
    expected_chunk_id: str
    expected_document_id: str
    expected_pages: list[int] = Field(default_factory=list)
    origin: str = "silver-structure"
    reviewed: bool = False


class EvaluationReport(StrictModel):
    id: str
    workspace_id: str
    cases: list[EvaluationCase] = Field(default_factory=list)
    variants: dict[str, dict[str, float]] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class BackupSummary(StrictModel):
    id: str
    workspace_id: str
    created_at: datetime
    path: str
    size_bytes: int
    sha256: str
    verified: bool = False


class ConfigDocument(StrictModel):
    content: str
    etag: str


class ModelSource(StrEnum):
    INSTALLED = "installed"
    OLLAMA = "ollama"
    HUGGING_FACE = "hugging-face"


class ModelCategory(StrEnum):
    CHAT = "chat"
    VL = "vl"
    EMBEDDING = "embedding"
    RERANK = "rerank"


class ModelFit(StrEnum):
    COMFORTABLE = "comfortable"
    TIGHT = "tight"


class HardwareProfile(StrEnum):
    ECO = "eco"
    LAPTOP = "laptop"
    QUALITY = "quality"


class HardwareInfo(StrictModel):
    memory_total: int = 0
    memory_available: int = 0
    gpu: str = "Detected GPU"
    vram_total: int = 0
    vram_used: int = 0
    shared_memory: int = 0


class ModelCatalogEntry(StrictModel):
    id: str
    source: ModelSource
    category: ModelCategory
    description: str = ""
    likes: int | None = None
    downloads: int | None = None
    parameter_count: int | None = None
    estimated_size: int | None = None
    estimated_memory: int
    installed: bool = False
    quantization: str | None = None
    fit: ModelFit
    recommended_rank: int | None = None
    capabilities: list[str] = Field(default_factory=list)


class ModelPackageItem(StrictModel):
    role: ModelCategory
    model: str
    download_name: str
    source: ModelSource
    installed: bool = False


class ModelPackage(StrictModel):
    id: str
    name: str
    summary: str
    synergy: str
    recommended_rank: int
    total_estimated_memory: int
    fit: ModelFit
    models: list[ModelPackageItem]


class ModelCatalogResponse(StrictModel):
    entries: list[ModelCatalogEntry]
    packages: list[ModelPackage] = Field(default_factory=list)
    hardware: HardwareInfo
    scanned: int
    compatible: int
    truncated: bool = False


class ModelRuntime(StrictModel):
    name: str
    size: int = 0
    size_vram: int = 0
    context_length: int = 0
    capabilities: list[str] = Field(default_factory=list)
    parameter_size: str = ""
    quantization_level: str = ""


class ModelRuntimeResponse(StrictModel):
    models: list[ModelRuntime] = Field(default_factory=list)
    roles: list[ModelRoleRuntime] = Field(default_factory=list)
    query_worker_state: str = "idle"
    query_worker_timeout_seconds: float = 0.0
    residency_policy: str = "adaptive"
    memory_state: str = "ready"
    worker_expires_in_seconds: float = 0.0


class WarmupStatus(StrEnum):
    READY = "ready"
    SKIPPED_BUSY = "skipped_busy"
    SKIPPED_MEMORY = "skipped_memory"
    NOT_NEEDED = "not_needed"


class WarmupResponse(StrictModel):
    status: WarmupStatus
    warmed_roles: list[str] = Field(default_factory=list)
    keep_alive_seconds: float = 0.0
    detail: str = ""


class ModelResidency(StrEnum):
    UNCONFIGURED = "unconfigured"
    IDLE = "idle"
    LOADING = "loading"
    LOADED = "loaded"
    ACTIVE = "active"


class ModelRoleRuntime(StrictModel):
    role: ModelCategory
    model: str | None = None
    provider: str | None = None
    residency: ModelResidency = ModelResidency.UNCONFIGURED
    shared_with: list[ModelCategory] = Field(default_factory=list)


class ModelDefaultsPreflight(StrictModel):
    workspace_id: str
    changes: dict[str, str] = Field(default_factory=dict)
    requires_reindex: bool = False
    warnings: list[str] = Field(default_factory=list)


class ModelOperationResult(StrictModel):
    model: str
    operation: str
    status: str

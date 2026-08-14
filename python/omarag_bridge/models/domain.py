from __future__ import annotations

from datetime import UTC, date, datetime
from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    book_index_v2: bool = False
    adaptive_retrieval: bool = False
    claim_streaming: bool = False
    knowledge_snapshots: bool = False


class BackendMeta(StrictModel):
    api_version: str = "1.0"
    min_client_version: str = "1.0"
    max_client_version: str = "1.x"
    omarag_version: str = "1.2.0"
    haiku_version: str | None = None
    adapter: str | None = None
    backend_id: str
    capabilities: CapabilitySet
    deprecations: list[str] = Field(default_factory=list)


class HealthReport(StrictModel):
    status: str
    ready: bool
    checks: dict[str, Any] = Field(default_factory=dict)


class PrivacyMode(StrEnum):
    DEVICE_ONLY = "device-only"
    TRUSTED_ENDPOINT = "trusted-endpoint"
    CLOUD_ALLOWED = "cloud-allowed"

    @classmethod
    def _missing_(cls, value: object) -> PrivacyMode | None:
        normalized = str(value).strip().casefold()
        if normalized == "local":
            return cls.DEVICE_ONLY
        if normalized == "trusted-endpoints":
            return cls.TRUSTED_ENDPOINT
        return None


# Source compatibility without duplicate values in generated JSON Schema.
# Legacy serialized values are handled by PrivacyMode._missing_.
PrivacyMode.LOCAL = PrivacyMode.DEVICE_ONLY  # type: ignore[attr-defined]


class PrivacyPolicy(StrictModel):
    """Fail-closed policy for HTTP requests that can carry private content."""

    mode: PrivacyMode = PrivacyMode.DEVICE_ONLY
    trusted_endpoints: list[str] = Field(default_factory=list, max_length=32)
    cloud_acknowledged: bool = False
    allow_insecure_trusted_http: bool = False


class EgressPayloadClass(StrEnum):
    CONTROL_PLANE = "control-plane"
    USER_CONTENT = "user-content"
    DERIVED_CONTENT = "derived-content"
    URL_SOURCE = "url-source"

    @property
    def content_bearing(self) -> bool:
        return self is not self.CONTROL_PLANE


class EgressEndpointScope(StrEnum):
    LOOPBACK = "loopback"
    TRUSTED = "trusted"
    CLOUD = "cloud"
    INVALID = "invalid"


class EgressReasonCode(StrEnum):
    ALLOW_LOOPBACK = "allow-loopback"
    ALLOW_TRUSTED = "allow-trusted"
    ALLOW_CLOUD = "allow-cloud"
    DENY_DEVICE_ONLY = "deny-device-only"
    DENY_UNTRUSTED = "deny-untrusted"
    DENY_CLOUD_ACK_REQUIRED = "deny-cloud-ack-required"
    DENY_INSECURE_TRANSPORT = "deny-insecure-transport"
    DENY_INVALID_ENDPOINT = "deny-invalid-endpoint"


class EgressDecision(StrictModel):
    """Telemetry-safe decision: deliberately contains neither URL nor payload."""

    allowed: bool
    privacy_mode: PrivacyMode
    payload_class: EgressPayloadClass
    endpoint_scope: EgressEndpointScope
    endpoint_id: str = Field(pattern=r"^sha256:[0-9a-f]{16}$")
    reason_code: EgressReasonCode


class RetentionProfile(StrEnum):
    MINIMAL = "minimal"
    LEGACY = "legacy"


class RetentionPolicy(StrictModel):
    """Workspace history limits; indefinite legacy retention needs explicit opt-in."""

    profile: RetentionProfile = RetentionProfile.MINIMAL
    answer_cache_days: int | None = Field(default=7, ge=1, le=3650)
    event_hours: int | None = Field(default=24, ge=1, le=87600)
    terminal_run_days: int | None = Field(default=30, ge=1, le=3650)
    terminal_job_days: int | None = Field(default=30, ge=1, le=3650)
    idempotency_days: int | None = Field(default=7, ge=1, le=3650)
    evaluation_limit: int | None = Field(default=10, ge=1, le=10000)
    import_preflight_hours: int | None = Field(default=24, ge=1, le=87600)
    legacy_opt_in: bool = False

    @model_validator(mode="before")
    @classmethod
    def populate_explicit_legacy_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        profile = getattr(value.get("profile"), "value", value.get("profile"))
        if profile != RetentionProfile.LEGACY.value or value.get("legacy_opt_in") is not True:
            return value
        updated = dict(value)
        for field_name in (
            "answer_cache_days",
            "event_hours",
            "terminal_run_days",
            "terminal_job_days",
            "idempotency_days",
            "evaluation_limit",
            "import_preflight_hours",
        ):
            updated.setdefault(field_name, None)
        return updated

    @model_validator(mode="after")
    def validate_legacy_is_explicit(self) -> RetentionPolicy:
        durations = (
            self.answer_cache_days,
            self.event_hours,
            self.terminal_run_days,
            self.terminal_job_days,
            self.idempotency_days,
            self.evaluation_limit,
            self.import_preflight_hours,
        )
        if self.profile is RetentionProfile.LEGACY:
            if not self.legacy_opt_in:
                raise ValueError("legacy retention requires an explicit opt-in")
            if any(value is not None for value in durations):
                raise ValueError("legacy retention must disable every automatic expiry")
        elif self.legacy_opt_in or any(value is None for value in durations):
            raise ValueError("minimal retention requires finite limits and no legacy opt-in")
        return self


class RetentionCategory(StrEnum):
    ANSWER_CACHE = "answer-cache"
    EVENTS = "events"
    RUNS = "runs"
    JOBS = "jobs"
    IDEMPOTENCY_KEYS = "idempotency-keys"
    EVALUATIONS = "evaluations"
    IMPORT_PREFLIGHTS = "import-preflights"


class RetentionCleanupAction(StrictModel):
    category: RetentionCategory
    cutoff_at: datetime | None = None
    eligible_records: int = Field(default=0, ge=0)
    protected_records: int = Field(default=0, ge=0)
    dependent_records: int = Field(default=0, ge=0)
    selection_digest: str = Field(
        default="sha256:4f53cda18c2baa0c", pattern=r"^sha256:[0-9a-f]{16}$"
    )
    dependent_digest: str = Field(
        default="sha256:4f53cda18c2baa0c", pattern=r"^sha256:[0-9a-f]{16}$"
    )


class RetentionCleanupPlan(StrictModel):
    plan_id: str = Field(pattern=r"^sha256:[0-9a-f]{24}$")
    workspace_id: str
    generated_at: datetime
    expires_at: datetime
    policy: RetentionPolicy
    actions: list[RetentionCleanupAction] = Field(default_factory=list)
    eligible_records: int = Field(default=0, ge=0)
    dry_run: bool = True


class RetentionPurgeResult(StrictModel):
    plan_id: str = Field(pattern=r"^sha256:[0-9a-f]{24}$")
    workspace_id: str
    completed_at: datetime
    purged_records: dict[RetentionCategory, int] = Field(default_factory=dict)
    dependent_records: int = Field(default=0, ge=0)


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
    substantive_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    structure_mode: str = "unknown"
    structure_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    toc_found: bool = False
    index_found: bool = False
    glossary_found: bool = False
    fallback_used: bool = False
    llm_fallback_used: bool = False
    exact_duplicate_count: int = Field(default=0, ge=0)
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
    pinned: bool = False


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
    prompt_evidence_id: str | None = None
    generation_id: str | None = None
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
    excerpt_char_start: int | None = Field(default=None, ge=0)
    excerpt_char_end: int | None = Field(default=None, ge=0)
    chunk_content_hash: str | None = None
    retrieval_rank: int | None = None
    rerank_score: float | None = None
    claim_ids: list[str] = Field(default_factory=list)
    retrieval_paths: list[str] = Field(default_factory=list)
    relevance_score: float | None = Field(default=None, ge=0.0, le=1.0)
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
    routing_ms: float = 0.0
    rerank_ms: float = 0.0
    pack_ms: float = 0.0


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


class ClaimStatus(StrEnum):
    SUPPORTED = "supported"
    INSUFFICIENT = "insufficient"


class ClaimSupportSpan(StrictModel):
    """Exact, chunk-relative source span used to support one atomic claim."""

    evidence_id: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    content_hash: str | None = None
    kind: Literal["literal", "semantic", "verifier"] = "semantic"

    @model_validator(mode="after")
    def end_follows_start(self) -> ClaimSupportSpan:
        if self.char_end <= self.char_start:
            raise ValueError("support span must not be empty")
        return self


class AnswerClaim(StrictModel):
    id: str
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    facet_id: str | None = None
    status: ClaimStatus = ClaimStatus.SUPPORTED
    alignment_score: float | None = Field(default=None, ge=0.0, le=1.0)
    verification_status: str = "protocol-checked"
    verification_score: float | None = Field(default=None, ge=0.0, le=1.0)
    support_spans: list[ClaimSupportSpan] = Field(default_factory=list)


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
    complexity: str = "standard"
    route: str = "hybrid"
    facets: list[str] = Field(default_factory=list)
    budgets: dict[str, int] = Field(default_factory=dict)
    candidate_count: int = Field(default=0, ge=0)
    selected_count: int = Field(default=0, ge=0)
    cut_reason: str = "legacy"
    facet_coverage: dict[str, bool] = Field(default_factory=dict)
    fallbacks: list[str] = Field(default_factory=list)
    model_digests: dict[str, str] = Field(default_factory=dict)
    prompt_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    tokens_per_second: float | None = Field(default=None, ge=0.0)
    time_to_first_token_ms: float | None = Field(default=None, ge=0.0)
    singleflight_status: str = "none"
    abstention: str = "none"
    rejected_claims: int = Field(default=0, ge=0)
    done_reason: str = "stop"
    retrieval_stages: list[str] = Field(default_factory=list)
    escalation_reasons: list[str] = Field(default_factory=list)
    calibrator_digest: str | None = None
    calibrator_status: str = "unknown"
    verifier_digest: str | None = None
    verifier_status: str = "not-run"
    typed_evidence_status: str = "unknown"


class RunSnapshot(StrictModel):
    id: str
    workspace_id: str
    session_id: str
    status: JobStatus
    question: str
    evidence_mode: EvidenceMode
    answer: str = ""
    claims: list[AnswerClaim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    receipt: RunReceipt | None = None
    error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    last_event_id: int | None = None
    pinned: bool = False


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
    structure_mode: str = "unknown"
    structure_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    toc_found: bool = False
    index_found: bool = False
    glossary_found: bool = False
    fallback_used: bool = False
    size_bytes: int = Field(default=0, ge=0)
    archive_mode: str = "unknown"


class ReindexPreflight(StrictModel):
    id: str
    workspace_id: str
    mode: str = "full"
    ready: bool
    documents: int = Field(default=0, ge=0)
    estimated_source_bytes: int = Field(default=0, ge=0)
    available_bytes: int = Field(default=0, ge=0)
    checks: dict[str, Any] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class QueryReadiness(StrictModel):
    workspace_id: str
    index_ready: bool
    query_ready: bool
    latency_status: str
    required_loaded_models: int = 2
    loaded_models: list[dict[str, Any]] = Field(default_factory=list)
    model_digests: dict[str, str] = Field(default_factory=dict)
    checks: dict[str, Any] = Field(default_factory=dict)


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
    schema_version: Literal[1, 2] = 2
    id: str
    question: str
    category: str = "section-location"
    expected_chunk_id: str | None = None
    expected_document_id: str | None = None
    expected_pages: list[int] = Field(default_factory=list)
    origin: str = "silver-structure"
    reviewed: bool = False
    conversation: list[str] = Field(default_factory=list)
    document_filters: dict[str, Any] = Field(default_factory=dict)
    answerable: bool = True
    required_facets: list[str] = Field(default_factory=list)
    allowed_evidence_sets: list[list[str]] = Field(default_factory=list)
    expected_claims: list[str] = Field(default_factory=list)
    split: Literal["calibration", "validation", "test", "unspecified"] = "unspecified"
    book_group: str | None = None

    @model_validator(mode="after")
    def answerable_case_has_gold_evidence(self) -> EvaluationCase:
        if self.answerable and not self.expected_chunk_id and not self.allowed_evidence_sets:
            raise ValueError("answerable evaluation case needs expected evidence")
        return self


class EvaluationReport(StrictModel):
    schema_version: Literal[1, 2] = 2
    id: str
    workspace_id: str
    cases: list[EvaluationCase] = Field(default_factory=list)
    variants: dict[str, dict[str, float]] = Field(default_factory=dict)
    dataset_digest: str | None = None
    baseline_id: str | None = None
    retrieval_metrics: dict[str, float] = Field(default_factory=dict)
    rerank_metrics: dict[str, float] = Field(default_factory=dict)
    packing_metrics: dict[str, float] = Field(default_factory=dict)
    claim_metrics: dict[str, float] = Field(default_factory=dict)
    abstention_metrics: dict[str, float] = Field(default_factory=dict)
    latency_metrics: dict[str, float] = Field(default_factory=dict)
    resource_metrics: dict[str, float] = Field(default_factory=dict)
    release_gates: dict[str, bool] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class BackupSummary(StrictModel):
    id: str
    workspace_id: str
    created_at: datetime
    path: str
    size_bytes: int
    sha256: str
    verified: bool = False
    pinned: bool = False


class DocumentPurgePlan(StrictModel):
    plan_id: str = Field(pattern=r"^sha256:[0-9a-f]{24}$")
    workspace_id: str
    document_id: str
    generation_id: str
    fingerprint: str
    segment_document_ids: list[str] = Field(default_factory=list)
    media_assets: int = 0
    pinned_run_ids: list[str] = Field(default_factory=list)
    backup_ids: list[str] = Field(default_factory=list)
    pinned_backup_ids: list[str] = Field(default_factory=list)
    requires_backup_confirmation: bool = False
    can_purge: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime


class DocumentPurgeResult(StrictModel):
    workspace_id: str
    document_id: str
    generation_id: str
    removed_segments: int = 0
    removed_media_assets: int = 0
    removed_backups: int = 0
    original_removed: bool = False


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
    """Legacy catalog profile kept for API and TUI compatibility."""

    ECO = "eco"
    LAPTOP = "laptop"
    QUALITY = "quality"


class PerformanceProfile(StrEnum):
    """User-facing V1.1 behavior profile with legacy input aliases."""

    FAST = "fast"
    NORMAL = "normal"
    QUALITY = "quality"

    @classmethod
    def _missing_(cls, value: object) -> PerformanceProfile | None:
        aliases = {
            "eco": cls.FAST,
            "laptop": cls.NORMAL,
            "balanced": cls.NORMAL,
            "deep": cls.QUALITY,
            "auto": cls.NORMAL,
        }
        return aliases.get(str(value).strip().casefold())


class HardwareTier(IntEnum):
    TIER_1 = 1
    TIER_2 = 2
    TIER_3 = 3
    TIER_4 = 4
    TIER_5 = 5
    TIER_6 = 6
    TIER_7 = 7
    TIER_8 = 8
    TIER_9 = 9
    TIER_10 = 10


class HardwareReadiness(StrEnum):
    READY = "ready"
    GUARDED = "guarded"
    CONSTRAINED = "constrained"
    UNSUPPORTED = "unsupported"


class AcceleratorInfo(StrictModel):
    id: str
    name: str
    vendor: str = "unknown"
    device_id: str | None = None
    driver: str | None = None
    backends: list[str] = Field(default_factory=list)
    dedicated_memory_total: int = Field(default=0, ge=0)
    dedicated_memory_used: int = Field(default=0, ge=0)
    shared_memory_total: int = Field(default=0, ge=0)
    integrated: bool = False
    multi_gpu_verified: bool = False


class HardwareInfo(StrictModel):
    schema_version: int = 2
    collected_at: datetime = Field(default_factory=utc_now)
    platform: str = "linux"
    architecture: str = "x86_64"
    cpu_model: str = "Unknown CPU"
    logical_cores: int = Field(default=0, ge=0)
    physical_cores: int | None = Field(default=None, ge=0)
    cpu_features: list[str] = Field(default_factory=list)
    memory_total: int = 0
    memory_capacity: int = Field(default=0, ge=0)
    memory_available: int = 0
    gpu: str = "Detected GPU"
    vram_total: int = 0
    vram_used: int = 0
    shared_memory: int = 0
    dedicated_vram_total: int = Field(default=0, ge=0)
    dedicated_vram_used: int = Field(default=0, ge=0)
    accelerators: list[AcceleratorInfo] = Field(default_factory=list)
    storage_total: int = Field(default=0, ge=0)
    storage_available: int = Field(default=0, ge=0)
    capacity_tier: HardwareTier = HardwareTier.TIER_1
    readiness_tier: HardwareTier = HardwareTier.TIER_1
    readiness: HardwareReadiness = HardwareReadiness.READY
    limiting_factors: list[str] = Field(default_factory=list)
    scan_warnings: list[str] = Field(default_factory=list)


class HardwareBenchmark(StrictModel):
    """Recorded result of an explicitly requested, download-free local probe."""

    benchmark_version: str = "book-canary-v1"
    measured_at: datetime = Field(default_factory=utc_now)
    tested_tier: HardwareTier
    performance_tier: HardwareTier
    stack_id: str
    model_digests: dict[str, str] = Field(default_factory=dict)
    prompt_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    embedding_items: int = Field(default=0, ge=0)
    time_to_first_token_ms: float | None = Field(default=None, ge=0.0)
    tokens_per_second: float | None = Field(default=None, ge=0.0)
    embedding_items_per_second: float | None = Field(default=None, ge=0.0)
    rerank_pairs_per_second: float | None = Field(default=None, ge=0.0)
    visual_items_per_second: float | None = Field(default=None, ge=0.0)
    peak_memory_bytes: int = Field(default=0, ge=0)
    peak_vram_bytes: int = Field(default=0, ge=0)
    passed: bool = True
    not_measured: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class HardwareClassification(StrictModel):
    capacity_tier: HardwareTier
    readiness_tier: HardwareTier
    performance_tier: HardwareTier | None = None
    effective_tier: HardwareTier
    readiness: HardwareReadiness
    limiting_factors: list[str] = Field(default_factory=list)
    benchmark_required: bool = True


class QuestionComplexity(StrEnum):
    SIMPLE = "simple"
    STANDARD = "standard"
    COMPLEX = "complex"


class RetrievalBudget(StrictModel):
    candidate_limit: int = Field(ge=1, le=256)
    min_sources: int = Field(ge=0, le=32)
    max_sources: int = Field(ge=1, le=32)
    evidence_tokens: int = Field(ge=128, le=32768)
    image_candidates: int = Field(default=8, ge=0, le=64)
    max_images: int = Field(default=4, ge=0, le=4)


class ReasoningPolicy(StrEnum):
    OFF = "off"
    ADAPTIVE = "adaptive"
    ENHANCED = "enhanced"


class PerformanceProfilePolicy(StrictModel):
    profile: PerformanceProfile
    context_ceiling_tokens: int = Field(ge=1024, le=131072)
    reasoning: ReasoningPolicy
    budgets: dict[QuestionComplexity, RetrievalBudget]


class CatalogProvider(StrEnum):
    OLLAMA = "ollama"
    HUGGING_FACE = "hugging-face"


class CatalogRole(StrEnum):
    CHAT = "chat"
    VL = "vl"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    VISUAL_EMBEDDING = "visual-embedding"


class CatalogArtifactStatus(StrEnum):
    STABLE = "stable"
    BETA = "beta"
    REVOKED = "revoked"


class ModelArtifact(StrictModel):
    id: str
    provider: CatalogProvider
    model: str
    family: str
    roles: list[CatalogRole]
    revision: str
    digest: str
    digest_kind: str
    quantization: str | None = None
    parameter_count: int = Field(ge=1)
    download_bytes: int = Field(ge=0)
    estimated_runtime_bytes: int = Field(ge=0)
    context_tokens: int = Field(ge=1)
    dimensions: list[int] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    backends: list[str] = Field(default_factory=list)
    license: str
    source_url: str
    status: CatalogArtifactStatus = CatalogArtifactStatus.STABLE


class ModelTierDefinition(StrictModel):
    tier: HardwareTier
    label: str
    hardware_hint: str
    generator: str
    embedding: str
    reranker: str
    visual_embedding: str | None = None
    fallback_generator: str | None = None
    fallback_visual_embedding: str | None = None
    embedding_dimension: int = Field(default=1024, ge=64, le=8192)
    max_context_tokens: int = Field(ge=1024, le=131072)
    residency_slots: int = Field(default=1, ge=1, le=3)


class ModelCatalogManifest(StrictModel):
    schema_version: int
    catalog_id: str
    release: str
    as_of: date
    stale_after_days: int = Field(ge=1)
    supported_platforms: list[str]
    artifacts: list[ModelArtifact]
    tiers: list[ModelTierDefinition]
    performance_profiles: dict[PerformanceProfile, PerformanceProfilePolicy]

    @model_validator(mode="after")
    def references_are_complete(self) -> ModelCatalogManifest:
        artifact_ids = {artifact.id for artifact in self.artifacts}
        if len(artifact_ids) != len(self.artifacts):
            raise ValueError("model catalog contains duplicate artifact ids")
        tiers = {definition.tier for definition in self.tiers}
        if tiers != set(HardwareTier):
            raise ValueError("model catalog must define hardware tiers 1 through 10")
        for definition in self.tiers:
            references = {
                definition.generator,
                definition.embedding,
                definition.reranker,
            }
            references.update(
                value
                for value in (
                    definition.visual_embedding,
                    definition.fallback_generator,
                    definition.fallback_visual_embedding,
                )
                if value
            )
            unknown = references - artifact_ids
            if unknown:
                raise ValueError(
                    f"tier {definition.tier.value} references unknown artifacts: {unknown}"
                )
        if set(self.performance_profiles) != set(PerformanceProfile):
            raise ValueError("model catalog must define fast, normal and quality policies")
        return self


class ModelInstallState(StrEnum):
    INSTALLED = "installed"
    NOT_INSTALLED = "not-installed"
    DIGEST_MISMATCH = "digest-mismatch"
    UNKNOWN = "unknown"


class ModelAssignment(StrictModel):
    role: CatalogRole
    artifact_id: str
    provider: CatalogProvider
    model: str
    revision: str
    digest: str
    quantization: str | None = None
    install_state: ModelInstallState = ModelInstallState.UNKNOWN
    installed_digest: str | None = None
    download_bytes: int = Field(default=0, ge=0)


class ModelStackRecommendation(StrictModel):
    recommendation_id: str
    catalog_id: str
    catalog_release: str
    catalog_as_of: date
    catalog_stale: bool = False
    profile: PerformanceProfile
    classification: HardwareClassification
    stack_tier: HardwareTier
    assignments: list[ModelAssignment]
    context_tokens: int = Field(ge=1024, le=131072)
    residency_slots: int = Field(ge=1, le=3)
    retrieval_budgets: dict[QuestionComplexity, RetrievalBudget]
    estimated_peak_memory: int = Field(default=0, ge=0)
    total_download_bytes: int = Field(default=0, ge=0)
    ready_now: bool = False
    fallback_tiers: list[HardwareTier] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ModelProfilePreflight(StrictModel):
    recommendation: ModelStackRecommendation
    changes: dict[str, str] = Field(default_factory=dict)
    downloads: list[ModelAssignment] = Field(default_factory=list)
    requires_reindex: bool = False
    requires_visual_reindex: bool = False
    can_apply: bool = True
    warnings: list[str] = Field(default_factory=list)


class SimpleModelRecommendation(StrictModel):
    role: str
    model: str
    reason: str
    required_bytes: int = Field(default=0, ge=0)
    context_tokens: int = Field(default=0, ge=0)


class HardwareProfileView(StrictModel):
    """Compact V1.1 wire view consumed by the Rust setup screen."""

    schema_version: int = 1
    tier: HardwareTier
    tier_label: str
    limiting_factor: str
    catalog_version: str
    scanned_at: datetime
    profile: PerformanceProfile
    expert_mode: bool = False
    recommendations: list[SimpleModelRecommendation] = Field(default_factory=list)


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
    artifact_digest: str | None = None
    artifact_revision: str | None = None
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
    digest: str = ""
    size: int = 0
    size_vram: int = 0
    context_length: int = 0
    expires_at: str | None = None
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

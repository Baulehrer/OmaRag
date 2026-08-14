from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .domain import (
    BookMetadata,
    EvaluationCase,
    EvidenceMode,
    HardwareTier,
    PerformanceProfile,
    PrivacyPolicy,
    RetentionPolicy,
    StrictModel,
)

ProcessingProfile = Literal[
    "default",
    "technical",
    "low-memory",
    "fast",
    "quality",
    "image-heavy",
    "eco",
    "balanced",
]
RetrievalProfile = Literal["auto", "fast", "normal", "quality", "balanced", "deep"]
ValidityPolicy = Literal["prefer-current", "strict"]
DocumentPolicy = Literal["current-only", "all-editions"]
TextFilter = str | list[str]
NumericFilter = int | list[int]


class SearchFilters(StrictModel):
    document_id: TextFilter | None = None
    logical_document_id: TextFilter | None = None
    document_ids: list[str] | None = None
    logical_document_ids: list[str] | None = None
    work_id: TextFilter | None = None
    title: TextFilter | None = None
    edition: TextFilter | None = None
    edition_number: NumericFilter | None = None
    publication_year: NumericFilter | None = None
    document_status: TextFilter | None = None
    language: TextFilter | None = None
    author: TextFilter | None = None
    authors: TextFilter | None = None
    isbn: TextFilter | None = None
    tags: TextFilter | None = None

    def active(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class CreateWorkspaceRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    read_only: bool = False


class PatchWorkspaceRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    read_only: bool | None = None


class UpdatePrivacyPolicyRequest(StrictModel):
    policy: PrivacyPolicy = Field(default_factory=PrivacyPolicy)


class UpdateRetentionPolicyRequest(StrictModel):
    policy: RetentionPolicy = Field(default_factory=RetentionPolicy)


class ExecuteRetentionCleanupRequest(StrictModel):
    plan_id: str = Field(pattern=r"^sha256:[0-9a-f]{24}$")
    confirm: Literal["PURGE_EXPIRED"]


class ExecuteDocumentPurgeRequest(StrictModel):
    plan_id: str = Field(pattern=r"^sha256:[0-9a-f]{24}$")
    confirm: Literal["PURGE_DOCUMENT"]
    backup_confirm: Literal["PURGE_BACKUPS"] | None = None


class CloneWorkspaceRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")


class SourceInput(StrictModel):
    type: Literal["file", "url"] = "file"
    path: str
    fingerprint: str | None = None
    candidate_id: str | None = None
    metadata: BookMetadata | None = None

    @model_validator(mode="after")
    def path_matches_declared_type(self) -> SourceInput:
        normalized = self.path.strip()
        scheme = urlsplit(normalized).scheme.casefold()
        is_http = scheme in {"http", "https"}
        if self.type == "file" and scheme:
            raise ValueError("a file source must be a local path without a URI scheme")
        if self.type == "url" and not is_http:
            raise ValueError("type=url requires an HTTP(S) URL")
        return self


class IndexingOptions(StrictModel):
    """Safe, versioned controls for the public book indexing pipeline.

    Parser thresholds deliberately stay server-owned so a workspace cannot
    accidentally create an index that is incompatible with its generation.
    """

    pipeline: Literal["book-v3", "book-v2", "compatible"] = "book-v3"
    enrichment: Literal["captions", "vlm"] = "captions"
    llm_fallback: Literal["auto", "off"] = "auto"
    visual_dense: Literal["off", "on"] = "off"


class IngestRequest(StrictModel):
    sources: list[SourceInput] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parser_id: Literal["auto", "docling"] = "auto"
    processing_profile: ProcessingProfile = "default"
    duplicate_policy: Literal["review", "skip", "replace"] = "review"
    validity_policy: ValidityPolicy = "prefer-current"
    indexing: IndexingOptions = Field(default_factory=IndexingOptions)


class SearchOptions(StrictModel):
    profile: RetrievalProfile = "auto"
    max_sources: int | None = Field(default=None, ge=1, le=18)
    deadline_ms: int | None = Field(default=None, ge=3000, le=35000)


class SearchRequest(StrictModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    document_policy: DocumentPolicy = "current-only"
    options: SearchOptions = Field(default_factory=SearchOptions)


class PreflightImportRequest(StrictModel):
    sources: list[SourceInput] = Field(min_length=1)


class CommitImportRequest(StrictModel):
    preflight_id: str
    sources: list[SourceInput] = Field(min_length=1)
    processing_profile: ProcessingProfile = "default"
    duplicate_policy: Literal["review", "skip", "replace"] = "review"
    validity_policy: ValidityPolicy = "prefer-current"
    indexing: IndexingOptions = Field(default_factory=IndexingOptions)


class PatchBookMetadataRequest(StrictModel):
    metadata: BookMetadata


class GenerateEvaluationRequest(StrictModel):
    limit: int = Field(default=30, ge=5, le=300)


class ImportEvaluationRequest(StrictModel):
    id: str | None = Field(default=None, pattern=r"^eval-[A-Za-z0-9._-]{1,64}$")
    cases: list[EvaluationCase] = Field(min_length=1, max_length=2000)
    baseline_id: str | None = None
    require_reviewed: bool = True


class RunEvaluationRequest(StrictModel):
    evaluation_id: str | None = None
    variants: list[Literal["fts", "vector", "hybrid"]] = Field(
        default_factory=lambda: ["fts", "vector", "hybrid"]
    )
    top_k: int = Field(default=10, ge=3, le=50)


class RunOptions(StrictModel):
    profile: RetrievalProfile = "auto"
    memory: Literal["auto", "off"] = "auto"
    max_sources: int | None = Field(default=None, ge=1, le=18)
    max_answer_tokens: int | None = Field(default=None, ge=64, le=768)
    deadline_ms: int | None = Field(default=None, ge=3000, le=60000)
    verifier: Literal["auto", "off"] = "auto"


class RunRequest(StrictModel):
    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$",
    )
    mode: Literal["rag", "analysis"] = "rag"
    question: str = Field(min_length=1)
    images: list[str] = Field(default_factory=list)
    evidence_mode: EvidenceMode = EvidenceMode.STRICT
    document_policy: DocumentPolicy = "current-only"
    filters: SearchFilters = Field(default_factory=SearchFilters)
    options: RunOptions = Field(default_factory=RunOptions)

    @model_validator(mode="after")
    def deadline_matches_mode(self) -> RunRequest:
        if self.images:
            raise ValueError(
                "image question inputs are not source-bound in V1.2; "
                "use the cited visual-evidence panel instead"
            )
        if (
            self.mode == "rag"
            and self.options.deadline_ms is not None
            and self.options.deadline_ms > 35000
        ):
            raise ValueError("rag deadline_ms must not exceed 35000")
        return self


class ReindexPreflightRequest(StrictModel):
    mode: Literal["full"] = "full"
    indexing: IndexingOptions = Field(default_factory=IndexingOptions)


class ReindexRequest(StrictModel):
    preflight_id: str = Field(min_length=1)
    mode: Literal["full"] = "full"
    confirm: Literal["REINDEX"]
    indexing: IndexingOptions = Field(default_factory=IndexingOptions)


class ErrorBody(StrictModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    retryable: bool = False


class ErrorResponse(StrictModel):
    error: ErrorBody


class IdempotentResult(StrictModel):
    id: str
    reused: bool = False


class DeleteWorkspaceRequest(StrictModel):
    confirm: str
    mode: Literal["unregister", "physical"] = "unregister"

    @field_validator("confirm")
    @classmethod
    def confirmation_is_explicit(cls, value: str) -> str:
        if value != "DELETE":
            raise ValueError("confirm must equal DELETE")
        return value


class CreateSourceRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    type: Literal["file", "directory", "url"] = "file"
    location: str = Field(min_length=1)
    enabled: bool = True


class ConfigUpdateRequest(StrictModel):
    content: str = Field(min_length=1)


class PullModelRequest(StrictModel):
    model: str = Field(min_length=1)


class LoadModelRequest(StrictModel):
    model: str = Field(min_length=1)
    context_tokens: int = Field(default=8192, ge=1024, le=131072)
    keep_alive: str = "5m"


class UnloadModelRequest(StrictModel):
    model: str = Field(min_length=1)


class DeleteModelRequest(StrictModel):
    model: str = Field(min_length=1)
    confirm: str = Field(min_length=1)

    @model_validator(mode="after")
    def confirmation_matches_model(self) -> DeleteModelRequest:
        if self.confirm != self.model:
            raise ValueError("confirm must exactly match model")
        return self


class ModelDefaultsRequest(StrictModel):
    chat: str = Field(min_length=1)
    vl: str = Field(min_length=1)
    embedding: str = Field(min_length=1)
    rerank: str = Field(min_length=1)
    embedding_provider: Literal["ollama"] = "ollama"
    rerank_provider: Literal["cross-encoder", "vllm"] = "cross-encoder"
    vector_dim: int = Field(default=1024, ge=64, le=8192)


class HardwareScanRequest(StrictModel):
    """Read-only hardware discovery; ``force`` bypasses an application cache."""

    force: bool = False


class HardwareBenchmarkRequest(StrictModel):
    """Explicit consent to benchmark already installed models; never pulls weights."""

    profile: PerformanceProfile = PerformanceProfile.NORMAL
    tier: HardwareTier | None = None
    confirm: Literal["BENCHMARK"]


class ModelRecommendationRequest(StrictModel):
    performance_profile: PerformanceProfile = PerformanceProfile.NORMAL
    workspace_id: str | None = Field(
        default=None,
        min_length=2,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9-]{1,62}$",
    )


class ModelProfilePreflightRequest(ModelRecommendationRequest):
    """Preview a profile without downloads, config writes or reindexing."""

    benchmark_tier: HardwareTier | None = None


class ModelProfileApplyRequest(StrictModel):
    """Consent envelope for the mutations described by a prior preflight."""

    preflight_id: str = Field(min_length=1, max_length=160)
    confirm: Literal["APPLY"]
    download_consent: Literal["DOWNLOAD_MODELS"] | None = None


class ModelProfileApplyAndReindexRequest(StrictModel):
    """Consent envelope for an embedding-changing, staged profile rebuild."""

    preflight_id: str = Field(min_length=1, max_length=160)
    confirm: Literal["APPLY_AND_REINDEX"]
    download_consent: Literal["DOWNLOAD_MODELS"] | None = None
    indexing: IndexingOptions = Field(default_factory=IndexingOptions)


class RestoreBackupRequest(StrictModel):
    confirm: str

    @field_validator("confirm")
    @classmethod
    def confirmation_is_explicit(cls, value: str) -> str:
        if value != "RESTORE":
            raise ValueError("confirm must equal RESTORE")
        return value


class PinRequest(StrictModel):
    pinned: bool = True

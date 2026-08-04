from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .domain import BookMetadata, EvidenceMode, StrictModel

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


class CloneWorkspaceRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")


class SourceInput(StrictModel):
    type: Literal["file", "url"] = "file"
    path: str
    fingerprint: str | None = None
    candidate_id: str | None = None
    metadata: BookMetadata | None = None


class IngestRequest(StrictModel):
    sources: list[SourceInput] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parser_id: Literal["auto", "docling"] = "auto"
    processing_profile: ProcessingProfile = "default"
    duplicate_policy: Literal["review", "skip", "replace"] = "review"
    validity_policy: ValidityPolicy = "prefer-current"


class SearchRequest(StrictModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    document_policy: DocumentPolicy = "current-only"


class PreflightImportRequest(StrictModel):
    sources: list[SourceInput] = Field(min_length=1)


class CommitImportRequest(StrictModel):
    preflight_id: str
    sources: list[SourceInput] = Field(min_length=1)
    processing_profile: ProcessingProfile = "default"
    duplicate_policy: Literal["review", "skip", "replace"] = "review"
    validity_policy: ValidityPolicy = "prefer-current"


class PatchBookMetadataRequest(StrictModel):
    metadata: BookMetadata


class GenerateEvaluationRequest(StrictModel):
    limit: int = Field(default=30, ge=5, le=300)


class RunEvaluationRequest(StrictModel):
    evaluation_id: str | None = None
    variants: list[Literal["fts", "vector", "hybrid"]] = Field(
        default_factory=lambda: ["fts", "vector", "hybrid"]
    )
    top_k: int = Field(default=10, ge=3, le=50)


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
    embedding_provider: Literal["ollama", "sentence-transformers"] = "ollama"
    rerank_provider: Literal["cross-encoder", "vllm"] = "cross-encoder"
    vector_dim: int = Field(default=1024, ge=64, le=8192)


class RestoreBackupRequest(StrictModel):
    confirm: str

    @field_validator("confirm")
    @classmethod
    def confirmation_is_explicit(cls, value: str) -> str:
        if value != "RESTORE":
            raise ValueError("confirm must equal RESTORE")
        return value

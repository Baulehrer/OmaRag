from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .domain import EvidenceMode, StrictModel


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


class IngestRequest(StrictModel):
    sources: list[SourceInput] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parser_id: Literal["auto", "docling"] = "auto"
    processing_profile: str = "default"
    duplicate_policy: Literal["review", "skip", "replace"] = "review"
    validity_policy: str = "prefer-current"


class SearchRequest(StrictModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)
    filters: dict[str, Any] = Field(default_factory=dict)


class RunRequest(StrictModel):
    session_id: str | None = None
    mode: Literal["rag", "analysis"] = "rag"
    question: str = Field(min_length=1)
    images: list[str] = Field(default_factory=list)
    evidence_mode: EvidenceMode = EvidenceMode.STRICT
    document_policy: str = "current-only"
    filters: dict[str, Any] = Field(default_factory=dict)


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


class RestoreBackupRequest(StrictModel):
    confirm: str

    @field_validator("confirm")
    @classmethod
    def confirmation_is_explicit(cls, value: str) -> str:
        if value != "RESTORE":
            raise ValueError("confirm must equal RESTORE")
        return value

from __future__ import annotations

from typing import Any


class OmaRagError(Exception):
    status_code = 400
    code = "OMARAG_ERROR"
    retryable = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(OmaRagError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(OmaRagError):
    status_code = 409
    code = "CONFLICT"


class EtagConflictError(ConflictError):
    code = "ETAG_CONFLICT"


class IdempotencyConflictError(ConflictError):
    code = "IDEMPOTENCY_CONFLICT"


class AdapterUnavailableError(OmaRagError):
    status_code = 503
    code = "HAIKU_UNAVAILABLE"
    retryable = True


class ReadOnlyError(ConflictError):
    code = "WORKSPACE_READ_ONLY"


class UpstreamUnavailableError(OmaRagError):
    status_code = 503
    code = "UPSTREAM_UNAVAILABLE"
    retryable = True


class IndexRebuildInProgressError(OmaRagError):
    status_code = 503
    code = "INDEX_REBUILD_IN_PROGRESS"
    retryable = True


class IndexNotReadyError(OmaRagError):
    status_code = 503
    code = "INDEX_NOT_READY"
    retryable = True


class IndexModelMismatchError(ConflictError):
    code = "INDEX_MODEL_MISMATCH"


class QueryNotReadyError(OmaRagError):
    status_code = 503
    code = "QUERY_NOT_READY"
    retryable = True


class QueryBusyError(OmaRagError):
    status_code = 429
    code = "QUERY_BUSY"
    retryable = True


class QueryDeadlineExceededError(OmaRagError):
    status_code = 504
    code = "QUERY_DEADLINE_EXCEEDED"
    retryable = True

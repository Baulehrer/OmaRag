from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .domain import StrictModel


class DomainEvent(StrictModel):
    event_id: int
    sequence: int
    timestamp: datetime
    type: str
    workspace_id: str | None = None
    job_id: str | None = None
    run_id: str | None = None
    correlation_id: str
    schema_version: int = 1
    payload: dict[str, Any] = Field(default_factory=dict)


def format_sse(event: DomainEvent) -> str:
    data = event.model_dump_json()
    return f"id: {event.event_id}\nevent: {event.type}\ndata: {data}\n\n"

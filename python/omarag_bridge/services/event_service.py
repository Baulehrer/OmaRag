from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from ..models.events import DomainEvent, format_sse
from ..store import StateStore


class EventService:
    def __init__(self, store: StateStore, poll_seconds: float, keepalive_seconds: float) -> None:
        self.store = store
        self.poll_seconds = poll_seconds
        self.keepalive_seconds = keepalive_seconds
        self._changed = asyncio.Condition()

    async def emit(self, event_type: str, correlation_id: str, **kwargs: Any) -> DomainEvent:
        event = self.store.append_event(
            event_type=event_type,
            correlation_id=correlation_id,
            payload=kwargs.pop("payload", {}),
            **kwargs,
        )
        async with self._changed:
            self._changed.notify_all()
        return event

    async def stream(
        self,
        after_id: int,
        *,
        workspace_id: str | None = None,
        job_id: str | None = None,
        run_id: str | None = None,
    ) -> AsyncIterator[str]:
        cursor = after_id
        idle = 0.0
        while True:
            events = self.store.events_after(
                cursor, workspace_id=workspace_id, job_id=job_id, run_id=run_id
            )
            if events:
                for event in events:
                    cursor = event.event_id
                    yield format_sse(event)
                idle = 0.0
                continue
            try:
                async with self._changed:
                    await asyncio.wait_for(self._changed.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                idle += self.poll_seconds
                if idle >= self.keepalive_seconds:
                    yield ": keepalive\n\n"
                    idle = 0.0

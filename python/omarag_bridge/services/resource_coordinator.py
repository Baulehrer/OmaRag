from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MemorySnapshot:
    total: int
    available: int
    reserve: int

    @property
    def state(self) -> str:
        if self.available <= self.reserve:
            return "waiting"
        if self.available <= self.reserve * 2:
            return "guarded"
        return "ready"


def _memory_snapshot() -> MemorySnapshot:
    values: dict[str, int] = {}
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.strip().split()[0]) * 1024
    total = values.get("MemTotal", 8 * 1024**3)
    available = values.get("MemAvailable", total // 2)
    reserve = max(1536 * 1024**2, int(total * 0.15))
    return MemorySnapshot(total=total, available=available, reserve=reserve)


class ResourceCoordinator:
    """Serialize memory-heavy work and prioritize interactive questions."""

    def __init__(self, max_residency_seconds: float = 300.0) -> None:
        if not 0.0 <= max_residency_seconds <= 600.0:
            raise ValueError("max_residency_seconds must be between 0 and 600")
        self._condition = asyncio.Condition()
        self._busy = False
        self._waiting_chats = 0
        self._max_residency_seconds = max_residency_seconds
        self._recent_query_uses = 0
        self._last_query_completed_at = 0.0

    def memory(self) -> MemorySnapshot:
        return _memory_snapshot()

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def waiting_chats(self) -> int:
        return self._waiting_chats

    def residency_seconds(self) -> float:
        """Grow hot-query residency from 30s to 5m, but yield on pressure."""

        # Keeping a cross-encoder or Ollama model resident is only an
        # optimization.  As soon as the reserve is guarded it must not compete
        # with the active request/indexer for memory.
        if self.memory().state != "ready":
            return 0.0
        now = time.monotonic()
        if self._last_query_completed_at and now - self._last_query_completed_at > 300.0:
            self._recent_query_uses = 0
        return min(
            self._max_residency_seconds,
            300.0,
            30.0 * (2 ** min(self._recent_query_uses, 4)),
        )

    def _record_query_use(self) -> None:
        now = time.monotonic()
        if self._last_query_completed_at and now - self._last_query_completed_at > 300.0:
            self._recent_query_uses = 0
        self._recent_query_uses = min(4, self._recent_query_uses + 1)
        self._last_query_completed_at = now

    def segment_pages(self, preferred: int, scanned: bool) -> int:
        """Reduce future conversion units before memory pressure becomes an OOM."""
        snapshot = self.memory()
        headroom = max(0, snapshot.available - snapshot.reserve)
        # OCR and table models have a much steeper per-page peak than native text.
        per_page = 180 * 1024**2 if scanned else 72 * 1024**2
        if snapshot.total <= 10 * 1024**3:
            preferred = min(preferred, 4 if scanned else 12)
        safe_pages = max(1, headroom // per_page)
        return max(1, min(preferred, int(safe_pages)))

    async def _wait_for_memory(self) -> None:
        # Deliberately wait instead of gambling on the kernel OOM killer. The
        # context is entered once per segment, so chat and cancellation remain
        # responsive between conversion units.
        while True:
            snapshot = self.memory()
            if snapshot.available > snapshot.reserve:
                return
            await asyncio.sleep(0.5)

    @asynccontextmanager
    async def chat(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting_chats += 1
            try:
                await self._condition.wait_for(lambda: not self._busy)
                self._busy = True
            finally:
                self._waiting_chats -= 1
        completed = False
        try:
            yield
            completed = True
        finally:
            if completed:
                self._record_query_use()
            async with self._condition:
                self._busy = False
                self._condition.notify_all()

    @asynccontextmanager
    async def warmup(self) -> AsyncIterator[str]:
        """Claim a heavy slot without ever delaying foreground work."""
        if self.memory().state == "waiting":
            yield "skipped_memory"
            return
        async with self._condition:
            if self._busy or self._waiting_chats:
                admission = "skipped_busy"
            else:
                self._busy = True
                admission = "ready"
        if admission != "ready":
            yield admission
            return
        try:
            yield "ready"
        finally:
            async with self._condition:
                self._busy = False
                self._condition.notify_all()

    @asynccontextmanager
    async def indexing(self) -> AsyncIterator[None]:
        await self._wait_for_memory()
        async with self._condition:
            await self._condition.wait_for(lambda: not self._busy and self._waiting_chats == 0)
            self._busy = True
        try:
            yield
        finally:
            async with self._condition:
                self._busy = False
                self._condition.notify_all()

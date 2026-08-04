from __future__ import annotations

import asyncio
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

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._busy = False
        self._waiting_chats = 0

    def memory(self) -> MemorySnapshot:
        return _memory_snapshot()

    def segment_pages(self, preferred: int, scanned: bool) -> int:
        """Reduce future conversion units before memory pressure becomes an OOM."""
        snapshot = self.memory()
        headroom = max(0, snapshot.available - snapshot.reserve)
        # OCR and table models have a much steeper per-page peak than native text.
        per_page = 180 * 1024**2 if scanned else 72 * 1024**2
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
        try:
            yield
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

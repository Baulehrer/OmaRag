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


# Worst-case resident bytes for one conversion unit. Derived from the per-page
# budget in `segment_pages` at the largest unit it will hand out.
_CONVERSION_PEAK_BYTES = 2 * 1024**3

# Roughly what a query worker holds: the process, the store and the loaded
# cross-encoder. Used only to decide whether keeping one around is affordable.
_QUERY_WORKER_BYTES = 1536 * 1024**2


class ResourceCoordinator:
    """Serialize memory-heavy work and prioritize interactive questions."""

    def __init__(self, max_residency_seconds: float = 300.0) -> None:
        if not 0.0 <= max_residency_seconds <= 600.0:
            raise ValueError("max_residency_seconds must be between 0 and 600")
        self._condition = asyncio.Condition()
        # Interactive work is exclusive; indexing is counted, so a second
        # conversion may start when — and only when — memory clearly allows it.
        self._chat_active = False
        self._active_indexers = 0
        self._waiting_chats = 0
        # Warming up is preparation *for* a question, so it deliberately does
        # not occupy the chat slot. It is tracked separately: indexing still
        # yields to it, and it aborts as soon as a question actually arrives.
        self._warming = False
        self._warm_preempt = asyncio.Event()
        self._max_residency_seconds = max_residency_seconds
        self._recent_query_uses = 0
        self._last_query_completed_at = 0.0

    def memory(self) -> MemorySnapshot:
        return _memory_snapshot()

    @property
    def busy(self) -> bool:
        return self._chat_active or self._warming or self._active_indexers > 0

    @property
    def warming(self) -> bool:
        return self._warming

    def warm_preempted(self) -> bool:
        """True once a question is waiting; a warm-up must stop at this point."""

        return self._warm_preempt.is_set()

    @property
    def active_indexers(self) -> int:
        return self._active_indexers

    def conversion_slots(self) -> int:
        """How many document conversions may run at once. Currently always one.

        Handing out a second slot breaks the guarantee that a question is served
        next: an indexer can take the free slot in the window before a question
        registers as waiting, so the question ends up behind two conversion units
        instead of one. That showed up as `index-1, index-2, chat` on a machine
        with enough memory for two slots.

        The counting lease below is kept because it expresses the rule directly,
        but the count stays at one until two things are true: the conversion loop
        in `book_v2.py` actually runs ranges concurrently (it is sequential, so
        today a second slot would never be used anyway), and admission reserves
        capacity for an imminent question rather than racing it.
        """
        return 1

    @property
    def waiting_chats(self) -> int:
        return self._waiting_chats

    def residency_seconds(self) -> float:
        """How long a warm query runtime may be kept, given the memory at hand."""

        # Keeping a cross-encoder or Ollama model resident is only an
        # optimization.  As soon as the reserve is guarded it must not compete
        # with the active request/indexer for memory.
        snapshot = self.memory()
        if snapshot.state != "ready":
            return 0.0
        now = time.monotonic()
        if self._last_query_completed_at and now - self._last_query_completed_at > 300.0:
            self._recent_query_uses = 0
        ramp = 30.0 * (2 ** min(self._recent_query_uses, 4))
        # The ramp starts low in case a question was a one-off. That trade is
        # badly priced: rebuilding a reaped query worker reloads the reranker's
        # 201 weight tensors, measured at 7.2s against 0.65s of actual scoring,
        # so a single question asked a minute later pays ten times over for the
        # memory a short residency saved. Where there is plainly room for the
        # worker several times over, start from a floor that outlives one
        # question and one pause for thought instead.
        headroom = snapshot.available - snapshot.reserve
        floor = 180.0 if headroom >= _QUERY_WORKER_BYTES * 3 else 30.0
        return min(self._max_residency_seconds, 300.0, max(floor, ramp))

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
            # A warm-up exists to make this very question faster. Letting it
            # finish first would charge its model load to the question's own
            # deadline, which is how a cold model turned a 15s budget into a
            # QUERY_DEADLINE_EXCEEDED. Tell it to stop and do not wait for it.
            self._warm_preempt.set()
            try:
                await self._condition.wait_for(
                    lambda: not self._chat_active and self._active_indexers == 0
                )
                self._chat_active = True
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
                self._chat_active = False
                self._condition.notify_all()

    @asynccontextmanager
    async def warmup(self) -> AsyncIterator[str]:
        """Claim a heavy slot without ever delaying foreground work."""
        if self.memory().state == "waiting":
            yield "skipped_memory"
            return
        async with self._condition:
            if self.busy or self._waiting_chats:
                admission = "skipped_busy"
            else:
                self._warm_preempt.clear()
                self._warming = True
                admission = "ready"
        if admission != "ready":
            yield admission
            return
        try:
            yield "ready"
        finally:
            async with self._condition:
                self._warming = False
                self._condition.notify_all()

    @asynccontextmanager
    async def indexing(self) -> AsyncIterator[None]:
        """Admit one conversion unit.

        Questions keep absolute priority: a waiting chat blocks new admissions,
        and an active chat excludes indexing entirely. A warm-up counts the same
        way, because it is a question that has not been sent yet. Beyond that,
        `slots` conversions may overlap — 1 unless memory is plainly abundant.
        """
        await self._wait_for_memory()
        async with self._condition:
            await self._condition.wait_for(
                lambda: (
                    not self._chat_active
                    and not self._warming
                    and self._waiting_chats == 0
                    and self._active_indexers < self.conversion_slots()
                )
            )
            self._active_indexers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active_indexers -= 1
                self._condition.notify_all()

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ResourceCoordinator:
    """Serialize memory-heavy work and prioritize interactive questions."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._busy = False
        self._waiting_chats = 0

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
        async with self._condition:
            await self._condition.wait_for(lambda: not self._busy and self._waiting_chats == 0)
            self._busy = True
        try:
            yield
        finally:
            async with self._condition:
                self._busy = False
                self._condition.notify_all()

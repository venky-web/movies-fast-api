"""Per-showtime locks shared by every operation that reads and then writes a showtime's seats or layout."""

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, nullcontext
from typing import Protocol

# Taken by every admin write (movies, showtimes, uploads, reset). They are rare, so one lock is enough.
# The order is always catalog first, then showtime locks, so they cannot deadlock. Bookings never take it.
CATALOG_LOCK = "catalog"


class Locks(Protocol):
    """What the services use. With SQL storage the release happens after the commit."""

    def get(self, showtime_id: str) -> AbstractAsyncContextManager[object]: ...
    def hold_all(self, showtime_ids: Iterable[str]) -> AbstractAsyncContextManager[None]: ...


class ShowtimeLocks:
    """`async with locks.get(showtime_id): ...` makes "check seats, then write" atomic for that showtime.

    With `enabled=False` every lock is a no-op (used to show the race, and in Phase 7 to prove that the
    database constraint alone prevents double booking).
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, showtime_id: str) -> AbstractAsyncContextManager[object]:
        if not self.enabled:
            return nullcontext()
        lock = self._locks.get(showtime_id)
        if lock is None:
            lock = self._locks[showtime_id] = asyncio.Lock()
        return lock

    @asynccontextmanager
    async def hold_all(self, showtime_ids: Iterable[str]) -> AsyncIterator[None]:
        """Takes several locks, always in id order so that two callers cannot deadlock."""
        async with AsyncExitStack() as stack:
            for showtime_id in sorted(set(showtime_ids)):
                await stack.enter_async_context(self.get(showtime_id))
            yield

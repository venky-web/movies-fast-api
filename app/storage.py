"""What the app needs from a storage backend, and the in-memory implementation.

Nothing here imports SQLAlchemy, so memory mode also works where it isn't installed (or can't be loaded).
The SQL implementation is `app/repositories/sql/storage.py`.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from app.domain.seed import SeedData
from app.repositories.base import Repos
from app.repositories.memory import MemoryRepos
from app.services.bookings import Publisher
from app.services.locks import Locks, ShowtimeLocks


class UnitOfWork(Protocol):
    """One request's (or background job's) view of storage. It commits when it ends without an exception."""

    @property
    def repos(self) -> Repos: ...

    @property
    def locks(self) -> Locks:
        """In SQL mode these are released after the commit, not at the end of the `async with`."""
        ...

    @property
    def publisher(self) -> Publisher:
        """In SQL mode this publishes after the commit."""
        ...

    async def after_commit(self, action: Callable[[], Awaitable[object]]) -> None: ...

    async def install(self, seed: SeedData) -> None:
        """Replaces all data (reset)."""
        ...


class Storage(Protocol):
    def unit_of_work(self, publisher: Publisher) -> AbstractAsyncContextManager[UnitOfWork]: ...

    def open_repos(self) -> AbstractAsyncContextManager[Repos]:
        """A short unit of work for code outside a request: the seat hub and the email task."""
        ...

    async def close(self) -> None: ...


class MemoryUnitOfWork:
    """Writes are immediate, so "after commit" is now."""

    def __init__(self, storage: "MemoryStorage", publisher: Publisher) -> None:
        self._storage = storage
        # Captured now: requests already running keep the store they started with when a reset swaps it.
        self.repos: Repos = storage.repos
        self.locks: Locks = storage.locks
        self.publisher: Publisher = publisher

    async def after_commit(self, action: Callable[[], Awaitable[object]]) -> None:
        await action()

    async def install(self, seed: SeedData) -> None:
        self._storage.repos = MemoryRepos.from_seed(seed)  # no await: the swap is atomic


class MemoryStorage:
    def __init__(self, seed: SeedData, locks: ShowtimeLocks) -> None:
        self.repos = MemoryRepos.from_seed(seed)
        self.locks = locks

    @asynccontextmanager
    async def unit_of_work(self, publisher: Publisher) -> AsyncIterator[UnitOfWork]:
        yield MemoryUnitOfWork(self, publisher)

    @asynccontextmanager
    async def open_repos(self) -> AsyncIterator[Repos]:
        yield self.repos

    async def close(self) -> None:
        pass

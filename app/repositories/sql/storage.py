"""The SQL unit of work: locks are held until after the commit (the Phase 7 "locks vs commit" decision).

A service writes `async with locks.get(id): check; write`, and the commit happens later, when the unit of
work ends. If the lock were released at the end of that block, the next request would check against data
that isn't committed yet. So here leaving the block keeps the lock, and the unit of work releases it after
the commit. The order at the end is: commit, publish, release the locks, send the response.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.domain.seed import SeedData
from app.repositories.base import Repos
from app.repositories.sql.engine import create_engine
from app.repositories.sql.models import Base
from app.repositories.sql.repos import SqlRepos
from app.schemas.booking import BookingDetails
from app.schemas.showtime import ShowtimeRecord
from app.services.bookings import NoopPublisher, Publisher
from app.services.locks import Locks, ShowtimeLocks
from app.storage import UnitOfWork

log = logging.getLogger(__name__)


class SchemaMissingError(RuntimeError):
    pass


class CommitScopedLocks:
    """`Locks` whose release waits for the end of the unit of work."""

    def __init__(self, uow: "SqlUnitOfWork", locks: ShowtimeLocks, held: AsyncExitStack) -> None:
        self._uow = uow
        self._locks = locks
        self._held = held

    @asynccontextmanager
    async def get(self, showtime_id: str) -> AsyncIterator[object]:
        await self._uow.before_waiting()
        await self._held.enter_async_context(self._locks.get(showtime_id))
        yield None

    @asynccontextmanager
    async def hold_all(self, showtime_ids: Iterable[str]) -> AsyncIterator[None]:
        await self._uow.before_waiting()
        for showtime_id in sorted(set(showtime_ids)):
            await self._held.enter_async_context(self._locks.get(showtime_id))
        yield


class AfterCommitPublisher:
    def __init__(self, uow: "SqlUnitOfWork", publisher: Publisher) -> None:
        self._uow = uow
        self._publisher = publisher

    async def booking_created(self, booking: BookingDetails) -> None:
        await self._uow.after_commit(partial(self._publisher.booking_created, booking))

    async def booking_cancelled(self, booking: BookingDetails) -> None:
        await self._uow.after_commit(partial(self._publisher.booking_cancelled, booking))

    async def showtime_changed(self, showtime: ShowtimeRecord) -> None:
        await self._uow.after_commit(partial(self._publisher.showtime_changed, showtime))

    async def showtime_deleted(self, showtime_id: str) -> None:
        await self._uow.after_commit(partial(self._publisher.showtime_deleted, showtime_id))

    async def demo_reset(self) -> None:
        await self._uow.after_commit(self._publisher.demo_reset)


class SqlUnitOfWork:
    def __init__(
        self,
        session: AsyncSession,
        locks: ShowtimeLocks,
        held: AsyncExitStack,
        write_lock: asyncio.Lock,
        publisher: Publisher,
    ) -> None:
        self._session = session
        self._held = held
        self._write_lock = write_lock
        self._wrote = False
        self._after_commit: list[Callable[[], Awaitable[object]]] = []
        self._sql = SqlRepos(session, on_write=self._before_write)
        self.repos: Repos = self._sql
        self.locks: Locks = CommitScopedLocks(self, locks, held)
        self.publisher: Publisher = AfterCommitPublisher(self, publisher)

    async def _before_write(self) -> None:
        """Queue for SQLite's one writer slot here, in a fair queue, and keep it until after commit + publish.

        Left to SQLite's busy timeout, writers retry at random and unlucky ones fail with "database is locked"
        under a burst. This also makes events go out in commit order. It can't deadlock: whoever holds it
        already has a connection, and takes no other lock afterwards (see `before_waiting`).
        """
        if not self._wrote:
            await self._held.enter_async_context(self._write_lock)
            self._wrote = True

    async def before_waiting(self) -> None:
        """Never wait for a lock while holding a pooled connection, or SQLite's write lock.

        Otherwise the waiters use up the pool (or the write lock), the lock's holder can't get one to
        finish with, and everything stops until a timeout.
        """
        if self._wrote:
            raise RuntimeError("Take every lock before the first write")
        if self._session.in_transaction():
            await self._session.rollback()  # nothing was written: this only returns the connection

    async def after_commit(self, action: Callable[[], Awaitable[object]]) -> None:
        self._after_commit.append(action)

    async def install(self, seed: SeedData) -> None:
        await self._sql.install(seed)

    async def commit(self) -> None:
        if self._wrote:
            await self._session.commit()
        await self._run_after_commit()

    async def _run_after_commit(self) -> None:
        for action in self._after_commit:
            try:
                await action()
            except Exception:  # the data is committed, so this must not turn the response into an error
                log.exception("An after-commit action failed")


class SqlStorage:
    def __init__(self, engine: AsyncEngine, locks: ShowtimeLocks) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._locks = locks
        self._write_lock = asyncio.Lock()

    @classmethod
    async def connect(cls, url: str, locks: ShowtimeLocks) -> "SqlStorage":
        """Alembic owns the schema, so this only checks that it is there."""
        engine = create_engine(url)
        try:
            async with engine.connect() as connection:
                tables = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
            if missing := Base.metadata.tables.keys() - tables:
                where = engine.url.render_as_string(hide_password=True)
                names = ", ".join(sorted(missing))
                raise SchemaMissingError(
                    f"{where} is missing the tables {names}. Run: python -m alembic upgrade head"
                )
        except BaseException:
            await engine.dispose()
            raise
        return cls(engine, locks)

    @asynccontextmanager
    async def unit_of_work(self, publisher: Publisher) -> AsyncIterator[UnitOfWork]:
        # The nesting is the release order: the session ends (commit, or rollback when it closes), and only
        # then are the locks released.
        async with AsyncExitStack() as held:
            async with self._sessions() as session:
                uow = SqlUnitOfWork(session, self._locks, held, self._write_lock, publisher)
                yield uow
                await uow.commit()  # not reached after an exception

    @asynccontextmanager
    async def open_repos(self) -> AsyncIterator[Repos]:
        async with self.unit_of_work(NoopPublisher()) as uow:
            yield uow.repos

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A bare session, for seeding. The caller commits."""
        async with self._sessions() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()

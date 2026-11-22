"""The tests' back door into storage (the Angular spec's `storage.get` / `storage.set`), for both backends.

The SQL side has its own engine and its own event loop thread, so it works from sync and async tests alike and
only ever sees committed data. It blocks the caller, so write through it only while the app is idle.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from functools import cache
from typing import Any

from fastapi import FastAPI

from app.schemas.booking import Booking
from app.schemas.movie import MovieRecord
from app.schemas.outbox import OutboxEmail
from app.schemas.showtime import ShowtimeRecord


@cache
def _loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="test-storage", daemon=True).start()
    return loop


class MemoryStored:
    def __init__(self, app: FastAPI) -> None:
        self._app = app

    @property
    def _store(self):
        return self._app.state.storage.repos.store  # each time: a reset swaps it

    def movies(self) -> dict[str, MovieRecord]:
        return dict(self._store.movies)

    def showtimes(self) -> dict[str, ShowtimeRecord]:
        return dict(self._store.showtimes)

    def showtime(self, showtime_id: str) -> ShowtimeRecord:
        return self._store.showtimes[showtime_id]

    def bookings(self) -> dict[str, Booking]:
        return dict(self._store.bookings)

    def tombstones(self) -> set[str]:
        return set(self._store.tombstones)

    def outbox(self) -> list[OutboxEmail]:
        return list(self._store.outbox)

    def put_showtime(self, showtime: ShowtimeRecord) -> None:
        self._store.showtimes[showtime.id] = showtime

    def put_booking(self, booking: Booking) -> None:
        self._store.bookings[booking.id] = booking


class SqlStored:
    def __init__(self, app: FastAPI) -> None:
        self._url = app.state.settings.database_url

    def _run[T](self, action: Callable[[Any, Any], Awaitable[T]]) -> T:
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.repositories.sql.engine import create_engine
        from app.repositories.sql.repos import SqlRepos

        async def run() -> T:
            engine = create_engine(self._url)
            try:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    result = await action(SqlRepos(session), session)
                    await session.commit()
                    return result
            finally:
                await engine.dispose()

        return asyncio.run_coroutine_threadsafe(run(), _loop()).result(timeout=20)

    def movies(self) -> dict[str, MovieRecord]:
        return {m.id: m for m in self._run(lambda repos, _: repos.movies.list_all(include_deleted=True))}

    def showtimes(self) -> dict[str, ShowtimeRecord]:
        return {s.id: s for s in self._run(lambda repos, _: repos.showtimes.list_all())}

    def showtime(self, showtime_id: str) -> ShowtimeRecord:
        found = self._run(lambda repos, _: repos.showtimes.get(showtime_id))
        if found is None:
            raise KeyError(showtime_id)
        return found

    def bookings(self) -> dict[str, Booking]:
        return {b.id: b for b in self._run(lambda repos, _: repos.bookings.list_all())}

    def tombstones(self) -> set[str]:
        return self._run(lambda repos, _: repos.tombstones.list_all())

    def outbox(self) -> list[OutboxEmail]:
        return self._run(lambda repos, _: repos.outbox.list_all())

    def put_showtime(self, showtime: ShowtimeRecord) -> None:
        """Unlike `showtimes.save`, this also overwrites the seats."""
        from sqlalchemy import delete, insert

        from app.repositories.sql.models import BookedSeatRow
        from app.repositories.sql.repos import prebooked_values

        async def put(repos, session) -> None:
            await repos.showtimes.save(showtime)
            await session.execute(delete(BookedSeatRow).where(BookedSeatRow.showtime_id == showtime.id))
            if rows := prebooked_values(showtime):
                await session.execute(insert(BookedSeatRow), rows)

        self._run(put)

    def put_booking(self, booking: Booking) -> None:
        """Unlike `bookings.add`, this claims no seats."""
        from sqlalchemy import insert

        from app.repositories.sql.models import BookingRow, BookingSeatRow

        async def put(repos, session) -> None:
            values = booking.model_dump(by_alias=False, exclude={"seats"})
            await session.execute(insert(BookingRow).values(**values))
            seats = [{"booking_id": booking.id, "seat_id": seat} for seat in booking.seats]
            await session.execute(insert(BookingSeatRow), seats)

        self._run(put)


def stored(app: FastAPI) -> MemoryStored | SqlStored:
    return SqlStored(app) if app.state.settings.storage_backend == "sql" else MemoryStored(app)

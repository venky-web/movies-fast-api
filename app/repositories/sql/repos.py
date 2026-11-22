"""The repository protocols on SQLAlchemy.

Two rules keep this correct under concurrency (plan 5.4):

- Every read uses `populate_existing`. The session's identity map would otherwise hand back the object it
  loaded *before* waiting for a lock, and the re-read inside the lock would be stale.
- Seats are claimed and freed by bookings, in `booked_seats`. Saving a showtime never rewrites them: a list
  from a stale read would erase or duplicate someone else's seats without the unique constraint firing.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import Select, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.base import Executable

from app.core.errors import seats_taken
from app.domain.seed import SeedData
from app.repositories.sql.models import (
    BookedSeatRow,
    BookingRow,
    BookingSeatRow,
    MovieRow,
    OutboxRow,
    ReviewRow,
    ShowtimeRow,
    ShowtimeTombstoneRow,
    UserRow,
)
from app.schemas.booking import Booking, BookingStatus
from app.schemas.movie import MovieRecord
from app.schemas.outbox import OutboxEmail
from app.schemas.showtime import ShowtimeRecord
from app.schemas.user import User


async def write_directly() -> None:
    """The `on_write` of repositories used outside a unit of work (seeding)."""


class _SqlRepo:
    def __init__(self, session: AsyncSession, on_write: Callable[[], Awaitable[None]]) -> None:
        self._session = session
        self._on_write = on_write

    async def _all[R](self, query: Select[R]) -> Sequence[R]:
        result = await self._session.execute(query.execution_options(populate_existing=True))
        return result.scalars().all()

    async def _first[R](self, query: Select[R]) -> R | None:
        result = await self._session.execute(query.execution_options(populate_existing=True))
        return result.scalars().first()

    async def _write(self, statement: Executable, rows: list[dict[str, Any]] | None = None) -> None:
        if rows is not None and not rows:
            return  # an empty list would mean "insert one row of defaults"
        await self._on_write()
        if rows is None:
            # No need to sync the identity map: every read repopulates it.
            await self._session.execute(statement, execution_options={"synchronize_session": False})
        else:
            await self._session.execute(statement, rows)


def movie_values(movie: MovieRecord) -> dict[str, Any]:
    return movie.model_dump(by_alias=False, exclude={"reviews"})


def review_values(movie: MovieRecord) -> list[dict[str, Any]]:
    return [{"movie_id": movie.id, **review.model_dump(by_alias=False)} for review in movie.reviews]


def showtime_values(showtime: ShowtimeRecord) -> dict[str, Any]:
    return showtime.model_dump(by_alias=False, exclude={"booked_seats", "seats_remaining"})


def prebooked_values(showtime: ShowtimeRecord) -> list[dict[str, Any]]:
    return [
        {"showtime_id": showtime.id, "seat_id": seat, "booking_id": None} for seat in showtime.booked_seats
    ]


class SqlMovieRepo(_SqlRepo):
    async def list_all(self, *, include_deleted: bool = False) -> list[MovieRecord]:
        query = select(MovieRow).options(selectinload(MovieRow.reviews)).order_by(MovieRow.position)
        if not include_deleted:
            query = query.where(MovieRow.deleted_at.is_(None))
        return [MovieRecord.model_validate(row, from_attributes=True) for row in await self._all(query)]

    async def get(self, movie_id: str, *, include_deleted: bool = False) -> MovieRecord | None:
        query = select(MovieRow).options(selectinload(MovieRow.reviews)).where(MovieRow.id == movie_id)
        if not include_deleted:
            query = query.where(MovieRow.deleted_at.is_(None))
        row = await self._first(query)
        return MovieRecord.model_validate(row, from_attributes=True) if row else None

    async def save(self, movie: MovieRecord) -> None:
        if await self._first(select(MovieRow.id).where(MovieRow.id == movie.id)) is None:
            last = await self._first(select(func.max(MovieRow.position)))
            position = 0 if last is None else last + 1
            await self._write(insert(MovieRow).values(position=position, **movie_values(movie)))
        else:
            await self._write(update(MovieRow).where(MovieRow.id == movie.id).values(**movie_values(movie)))
            await self._write(delete(ReviewRow).where(ReviewRow.movie_id == movie.id))
        await self._write(insert(ReviewRow), review_values(movie))


class SqlShowtimeRepo(_SqlRepo):
    async def list_all(self) -> list[ShowtimeRecord]:
        query = (
            select(ShowtimeRow)
            .options(selectinload(ShowtimeRow.seat_rows))
            .order_by(ShowtimeRow.starts_at, ShowtimeRow.id)
        )
        return [ShowtimeRecord.model_validate(row, from_attributes=True) for row in await self._all(query)]

    async def list_for_movie(self, movie_id: str) -> list[ShowtimeRecord]:
        query = (
            select(ShowtimeRow)
            .options(selectinload(ShowtimeRow.seat_rows))
            .where(ShowtimeRow.movie_id == movie_id)
            .order_by(ShowtimeRow.starts_at, ShowtimeRow.id)
        )
        return [ShowtimeRecord.model_validate(row, from_attributes=True) for row in await self._all(query)]

    async def get(self, showtime_id: str) -> ShowtimeRecord | None:
        query = (
            select(ShowtimeRow)
            .options(selectinload(ShowtimeRow.seat_rows))
            .where(ShowtimeRow.id == showtime_id)
        )
        row = await self._first(query)
        return ShowtimeRecord.model_validate(row, from_attributes=True) if row else None

    async def save(self, showtime: ShowtimeRecord) -> None:
        await self.save_many([showtime])

    async def save_many(self, showtimes: Sequence[ShowtimeRecord]) -> None:
        """A handful of statements however many showtimes: each statement costs thread hops with aiosqlite."""
        if not showtimes:
            return
        ids = [s.id for s in showtimes]
        existing = set(await self._all(select(ShowtimeRow.id).where(ShowtimeRow.id.in_(ids))))
        new = [s for s in showtimes if s.id not in existing]
        await self._write(insert(ShowtimeRow), [showtime_values(s) for s in new])
        await self._write(insert(BookedSeatRow), [row for s in new for row in prebooked_values(s)])
        # For existing ones not the seats: `bookings.add` and `bookings.update` own those (module docstring).
        await self._write(update(ShowtimeRow), [showtime_values(s) for s in showtimes if s.id in existing])

    async def delete(self, showtime_id: str) -> None:
        await self.delete_many([showtime_id])

    async def delete_many(self, showtime_ids: Sequence[str]) -> None:
        if showtime_ids:
            await self._write(delete(ShowtimeRow).where(ShowtimeRow.id.in_(showtime_ids)))


class SqlUserRepo(_SqlRepo):
    async def list_all(self) -> list[User]:
        rows = await self._all(select(UserRow).order_by(UserRow.position))
        return [User.model_validate(row, from_attributes=True) for row in rows]

    async def get(self, user_id: str) -> User | None:
        row = await self._first(select(UserRow).where(UserRow.id == user_id))
        return User.model_validate(row, from_attributes=True) if row else None


class SqlBookingRepo(_SqlRepo):
    def _query(self) -> Select[BookingRow]:
        return (
            select(BookingRow)
            .options(selectinload(BookingRow.seat_rows))
            .order_by(BookingRow.created_at.desc(), BookingRow.seq.desc())
        )

    async def _list(self, query: Select[BookingRow]) -> list[Booking]:
        return [Booking.model_validate(row, from_attributes=True) for row in await self._all(query)]

    async def add(self, booking: Booking) -> None:
        """Inserts the booking and, if it is confirmed, claims its seats in the same transaction."""
        values = booking.model_dump(by_alias=False, exclude={"seats"})
        await self._write(insert(BookingRow).values(**values))
        await self._write(
            insert(BookingSeatRow), [{"booking_id": booking.id, "seat_id": seat} for seat in booking.seats]
        )
        if booking.status != BookingStatus.CONFIRMED:
            return
        claims = [
            {"showtime_id": booking.showtime_id, "seat_id": seat, "booking_id": booking.id}
            for seat in booking.seats
        ]
        try:
            await self._write(insert(BookedSeatRow), claims)
        except IntegrityError:
            await self._session.rollback()
            # After the rollback we can see who won, to fill `details.takenSeats`.
            query = select(BookedSeatRow.seat_id).where(
                BookedSeatRow.showtime_id == booking.showtime_id, BookedSeatRow.seat_id.in_(booking.seats)
            )
            taken = set(await self._all(query))
            # Empty if the winner was cancelled since; the client still has to retry.
            raise seats_taken([s for s in booking.seats if s in taken] or booking.seats) from None

    async def get(self, booking_id: str) -> Booking | None:
        row = await self._first(self._query().where(BookingRow.id == booking_id))
        return Booking.model_validate(row, from_attributes=True) if row else None

    async def update(self, booking: Booking) -> None:
        await self._write(
            update(BookingRow)
            .where(BookingRow.id == booking.id)
            .values(status=booking.status, cancelled_at=booking.cancelled_at)
        )
        if booking.status == BookingStatus.CANCELLED:
            await self._write(delete(BookedSeatRow).where(BookedSeatRow.booking_id == booking.id))

    async def list_all(self) -> list[Booking]:
        return await self._list(self._query())

    async def list_for_user(self, user_id: str) -> list[Booking]:
        return await self._list(self._query().where(BookingRow.user_id == user_id))

    async def exists(self, booking_id: str) -> bool:
        return await self._first(select(BookingRow.seq).where(BookingRow.id == booking_id)) is not None

    async def list_for_showtime(self, showtime_id: str) -> list[Booking]:
        return await self._list(self._query().where(BookingRow.showtime_id == showtime_id))


class SqlTombstoneRepo(_SqlRepo):
    async def add(self, showtime_id: str) -> None:
        await self.remove(showtime_id)  # an upsert that works on every dialect
        await self._write(insert(ShowtimeTombstoneRow).values(showtime_id=showtime_id))

    async def remove(self, showtime_id: str) -> None:
        await self._write(delete(ShowtimeTombstoneRow).where(ShowtimeTombstoneRow.showtime_id == showtime_id))

    async def list_all(self) -> set[str]:
        return set(await self._all(select(ShowtimeTombstoneRow.showtime_id)))


class SqlOutboxRepo(_SqlRepo):
    async def add(self, email: OutboxEmail) -> None:
        await self._write(insert(OutboxRow).values(**email.model_dump(by_alias=False)))

    async def list_all(self) -> list[OutboxEmail]:
        rows = await self._all(select(OutboxRow).order_by(OutboxRow.sent_at.desc(), OutboxRow.seq.desc()))
        return [OutboxEmail.model_validate(row, from_attributes=True) for row in rows]


class SqlRepos(_SqlRepo):
    """All repositories on one session. `on_write` tells the unit of work there is something to commit."""

    def __init__(
        self, session: AsyncSession, on_write: Callable[[], Awaitable[None]] = write_directly
    ) -> None:
        super().__init__(session, on_write)
        self.movies = SqlMovieRepo(session, on_write)
        self.showtimes = SqlShowtimeRepo(session, on_write)
        self.users = SqlUserRepo(session, on_write)
        self.bookings = SqlBookingRepo(session, on_write)
        self.tombstones = SqlTombstoneRepo(session, on_write)
        self.outbox = SqlOutboxRepo(session, on_write)

    async def is_empty(self) -> bool:
        return await self._first(select(UserRow.id)) is None

    async def install(self, seed: SeedData) -> None:
        """Replaces all data: deletes children before parents, then inserts the seed. The caller commits."""
        for table in (
            OutboxRow,
            BookedSeatRow,
            BookingSeatRow,
            BookingRow,
            ShowtimeTombstoneRow,
            ShowtimeRow,
            ReviewRow,
            MovieRow,
            UserRow,
        ):
            await self._write(delete(table))

        users = [{"position": i, **user.model_dump(by_alias=False)} for i, user in enumerate(seed.users)]
        movies = [{"position": i, **movie_values(movie)} for i, movie in enumerate(seed.movies)]
        await self._write(insert(UserRow), users)
        await self._write(insert(MovieRow), movies)
        await self._write(insert(ReviewRow), [row for movie in seed.movies for row in review_values(movie)])
        await self._write(insert(ShowtimeRow), [showtime_values(s) for s in seed.showtimes])
        seats = [row for showtime in seed.showtimes for row in prebooked_values(showtime)]
        await self._write(insert(BookedSeatRow), seats)

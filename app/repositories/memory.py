"""Dict-based repositories (Phases 1–6).

Every read returns a deep copy and every write stores one, so callers can never change stored data by
mutating an object they got back (the store only changes through a repo method).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.domain.seed import SeedData
from app.schemas.booking import Booking
from app.schemas.movie import MovieRecord
from app.schemas.outbox import OutboxEmail
from app.schemas.showtime import ShowtimeRecord
from app.schemas.user import User


@dataclass
class MemoryStore:
    movies: dict[str, MovieRecord] = field(default_factory=dict)  # insertion order = seed order
    showtimes: dict[str, ShowtimeRecord] = field(default_factory=dict)
    users: dict[str, User] = field(default_factory=dict)
    bookings: dict[str, Booking] = field(default_factory=dict)  # insertion order = order added
    tombstones: set[str] = field(default_factory=set)
    outbox: list[OutboxEmail] = field(default_factory=list)


class MemoryMovieRepo:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def list_all(self, *, include_deleted: bool = False) -> list[MovieRecord]:
        return [
            m.model_copy(deep=True)
            for m in self._store.movies.values()
            if include_deleted or m.deleted_at is None
        ]

    async def get(self, movie_id: str, *, include_deleted: bool = False) -> MovieRecord | None:
        movie = self._store.movies.get(movie_id)
        if movie is None or (movie.deleted_at is not None and not include_deleted):
            return None
        return movie.model_copy(deep=True)

    async def save(self, movie: MovieRecord) -> None:
        self._store.movies[movie.id] = movie.model_copy(deep=True)


class MemoryShowtimeRepo:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def list_all(self) -> list[ShowtimeRecord]:
        return [s.model_copy(deep=True) for s in self._store.showtimes.values()]

    async def list_for_movie(self, movie_id: str) -> list[ShowtimeRecord]:
        return [s.model_copy(deep=True) for s in self._store.showtimes.values() if s.movie_id == movie_id]

    async def get(self, showtime_id: str) -> ShowtimeRecord | None:
        showtime = self._store.showtimes.get(showtime_id)
        return showtime.model_copy(deep=True) if showtime else None

    async def save(self, showtime: ShowtimeRecord) -> None:
        self._store.showtimes[showtime.id] = showtime.model_copy(deep=True)

    async def save_many(self, showtimes: Sequence[ShowtimeRecord]) -> None:
        for showtime in showtimes:
            self._store.showtimes[showtime.id] = showtime.model_copy(deep=True)

    async def delete(self, showtime_id: str) -> None:
        self._store.showtimes.pop(showtime_id, None)

    async def delete_many(self, showtime_ids: Sequence[str]) -> None:
        for showtime_id in showtime_ids:
            self._store.showtimes.pop(showtime_id, None)


class MemoryUserRepo:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def list_all(self) -> list[User]:
        return [u.model_copy(deep=True) for u in self._store.users.values()]

    async def get(self, user_id: str) -> User | None:
        user = self._store.users.get(user_id)
        return user.model_copy(deep=True) if user else None


class MemoryBookingRepo:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def add(self, booking: Booking) -> None:
        self._store.bookings[booking.id] = booking.model_copy(deep=True)

    async def get(self, booking_id: str) -> Booking | None:
        booking = self._store.bookings.get(booking_id)
        return booking.model_copy(deep=True) if booking else None

    async def update(self, booking: Booking) -> None:
        self._store.bookings[booking.id] = booking.model_copy(deep=True)

    async def list_all(self) -> list[Booking]:
        # Newest added first, then a stable sort by createdAt (ties are common with a frozen clock).
        newest_added_first = reversed(self._store.bookings.values())
        return [
            b.model_copy(deep=True)
            for b in sorted(newest_added_first, key=lambda b: b.created_at, reverse=True)
        ]

    async def list_for_user(self, user_id: str) -> list[Booking]:
        return [b for b in await self.list_all() if b.user_id == user_id]

    async def exists(self, booking_id: str) -> bool:
        return booking_id in self._store.bookings

    async def list_for_showtime(self, showtime_id: str) -> list[Booking]:
        return [
            b.model_copy(deep=True) for b in self._store.bookings.values() if b.showtime_id == showtime_id
        ]


class MemoryTombstoneRepo:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def add(self, showtime_id: str) -> None:
        self._store.tombstones.add(showtime_id)

    async def remove(self, showtime_id: str) -> None:
        self._store.tombstones.discard(showtime_id)

    async def list_all(self) -> set[str]:
        return set(self._store.tombstones)


class MemoryOutboxRepo:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def add(self, email: OutboxEmail) -> None:
        self._store.outbox.append(email.model_copy(deep=True))

    async def list_all(self) -> list[OutboxEmail]:
        newest_added_first = reversed(self._store.outbox)
        return [
            e.model_copy(deep=True) for e in sorted(newest_added_first, key=lambda e: e.sent_at, reverse=True)
        ]


class MemoryRepos:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.movies = MemoryMovieRepo(store)
        self.showtimes = MemoryShowtimeRepo(store)
        self.users = MemoryUserRepo(store)
        self.bookings = MemoryBookingRepo(store)
        self.tombstones = MemoryTombstoneRepo(store)
        self.outbox = MemoryOutboxRepo(store)

    @classmethod
    def from_seed(cls, seed: SeedData) -> "MemoryRepos":
        return cls(
            MemoryStore(
                movies={m.id: m for m in seed.movies},
                showtimes={s.id: s for s in seed.showtimes},
                users={u.id: u for u in seed.users},
            )
        )

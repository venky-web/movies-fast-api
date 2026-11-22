"""Test helpers, ported from `<mfe>/testing/test-helpers.ts`."""

from collections.abc import Awaitable, Callable
from datetime import datetime

from app.domain.rules import seat_grid
from app.repositories.memory import MemoryShowtimeRepo
from app.schemas.showtime import ShowtimeRecord
from tests.stored import stored


def pause_before_write(monkeypatch, app, pause: Callable[[], Awaitable[None]]) -> None:
    """Stands in for I/O between "check the seats" and "write": both booking and cancelling save the showtime
    as their first write, right after their checks."""
    if app.state.settings.storage_backend == "sql":
        from app.repositories.sql.repos import SqlShowtimeRepo

        repo: type = SqlShowtimeRepo
    else:
        repo = MemoryShowtimeRepo
    save = repo.save

    async def paused_save(self, showtime):
        await pause()
        await save(self, showtime)

    monkeypatch.setattr(repo, "save", paused_save)


def showtimes(app) -> list[ShowtimeRecord]:
    """Stored showtimes in start order (the equivalent of `storage.get('showtimes')`)."""
    return sorted(stored(app).showtimes().values(), key=lambda s: (s.starts_at, s.id))


def stored_showtime(app, showtime_id: str) -> ShowtimeRecord:
    return stored(app).showtime(showtime_id)


def free_seats(showtime: ShowtimeRecord) -> list[str]:
    booked = set(showtime.booked_seats)
    return [s for row in seat_grid(showtime.rows, showtime.seats_per_row) for s in row if s not in booked]


def first_future(items: list[ShowtimeRecord], now: datetime) -> ShowtimeRecord:
    return next(s for s in items if s.starts_at > now and len(free_seats(s)) >= 6)


def first_past(items: list[ShowtimeRecord], now: datetime) -> ShowtimeRecord:
    return next(s for s in items if s.starts_at < now and len(free_seats(s)) >= 2)


def booking_request(showtime: ShowtimeRecord, seats: list[str], **overrides) -> dict:
    return {
        "userId": "u-ava",
        "movieId": showtime.movie_id,
        "showtimeId": showtime.id,
        "seats": seats,
        "customerName": "Ava Patel",
        "email": "ava@example.com",
        **overrides,
    }

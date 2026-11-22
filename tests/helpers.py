"""Test helpers, ported from `<mfe>/testing/test-helpers.ts`."""

from datetime import datetime

from app.domain.rules import seat_grid
from app.schemas.showtime import ShowtimeRecord
from tests.stored import stored


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

"""Seed data, ported from `<mfe>/data/` (movies, reviews, showtimes, users).

Generated values use Python's `random.Random` seeded by id, so they are deterministic but do not match the
TypeScript PRNG output (a deliberate deviation, see the plan section 3.8).
"""

import json
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.rules import calculate_total, random_booking_id, seat_grid
from app.schemas.booking import Booking, BookingStatus
from app.schemas.movie import MovieRecord, Review
from app.schemas.showtime import ShowtimeRecord
from app.schemas.user import User

DATA_DIR = Path(__file__).parent / "data"

SLOTS: tuple[tuple[int, int], ...] = ((10, 30), (13, 15), (16, 0), (18, 45), (21, 30))
SCREEN_COUNT = 6
PAST_DAYS = 2
FUTURE_DAYS = 7  # includes today, so the window is today - 2 ... today + 6 (9 days)

REVIEW_POOL: tuple[tuple[str, str], ...] = (
    ("Film Fan Weekly", "Confident, gripping and beautifully shot."),
    ("J. Romero", "Took a while to get going, but the last act is worth it."),
    ("The Late Show Blog", "Easily one of the most fun nights at the cinema this year."),
    ("K. Nakamura", "Great performances carry a slightly predictable script."),
    ("Popcorn Critics", "Big screen, big sound: see it in a cinema."),
    ("A. Fischer", "Smart, surprising and never in a hurry."),
    ("Reel Talk", "Uneven in places, but full of heart."),
    ("M. Okonkwo", "I was still thinking about it on the way home."),
)

SEED_USERS: tuple[User, ...] = (
    User(id="u-ava", name="Ava Patel", email="ava.patel@example.com", avatar_color="#7048e8"),
    User(id="u-leo", name="Leo Martins", email="leo.martins@example.com", avatar_color="#0c7489"),
    User(id="u-mia", name="Mia Chen", email="mia.chen@example.com", avatar_color="#c2410c"),
    User(id="u-sam", name="Sam Okafor", email="sam.okafor@example.com", avatar_color="#237a36"),
)


def js_round(value: float) -> int:
    """JavaScript's `Math.round` (halves round towards +infinity)."""
    return math.floor(value + 0.5)


def pick_distinct[T](items: Sequence[T], count: int, rng: random.Random) -> list[T]:
    """Picks `count` distinct items, keeping their original order (port of `pickDistinct`)."""
    indexes = list(range(len(items)))
    for i in range(len(indexes) - 1, 0, -1):
        j = math.floor(rng.random() * (i + 1))
        indexes[i], indexes[j] = indexes[j], indexes[i]
    return [items[i] for i in sorted(indexes[:count])]


def reviews_for(movie_id: str, rating: float) -> list[Review]:
    rng = random.Random(f"reviews|{movie_id}")
    base_stars = js_round(rating / 2)
    return [
        Review(author=author, text=text, stars=min(5, max(1, base_stars + js_round(rng.random() * 2 - 1))))
        for author, text in pick_distinct(REVIEW_POOL, 3, rng)
    ]


def poster_path(movie_id: str) -> str:
    return f"/api/posters/{movie_id}.svg"


def load_movies() -> list[MovieRecord]:
    raw = json.loads((DATA_DIR / "movies.json").read_text(encoding="utf-8"))
    return [
        MovieRecord.model_validate(
            {**seed, "posterUrl": poster_path(seed["id"]), "reviews": reviews_for(seed["id"], seed["rating"])}
        )
        for seed in raw
    ]


def day_key(day: date) -> str:
    return day.strftime("%Y%m%d")


def generate_showtimes(
    movies: Sequence[MovieRecord],
    now: datetime,
    tz: ZoneInfo,
    *,
    past_days: int = PAST_DAYS,
    future_days: int = FUTURE_DAYS,
    skip_ids: Iterable[str] = (),
) -> list[ShowtimeRecord]:
    """Showtimes for every movie over the window around `now`, in the theater's time zone.

    `movies` should be the full seed list in seed order (screen numbers depend on the position);
    callers drop deleted movies afterwards. Tombstoned ids in `skip_ids` are generated (to keep the RNG
    sequence) but left out.
    """
    skip = set(skip_ids)
    today = now.astimezone(tz).date()
    showtimes: list[ShowtimeRecord] = []

    for offset in range(-past_days, future_days):
        day = today + timedelta(days=offset)
        key = day_key(day)
        for movie_index, movie in enumerate(movies):
            rng = random.Random(f"{movie.id}|{key}")
            slots = pick_distinct(SLOTS, 3 if rng.random() < 0.5 else 4, rng)
            for slot_index, (hour, minute) in enumerate(slots):
                rows = 6 + math.floor(rng.random() * 3)
                seats_per_row = 8 + math.floor(rng.random() * 5)
                occupancy = 0.1 + rng.random() * 0.4
                booked = [
                    seat for row in seat_grid(rows, seats_per_row) for seat in row if rng.random() < occupancy
                ]
                showtime_id = f"st-{movie.id}-{key}-{hour:02d}{minute:02d}"
                if showtime_id in skip:
                    continue
                showtimes.append(
                    ShowtimeRecord(
                        id=showtime_id,
                        movie_id=movie.id,
                        starts_at=datetime.combine(day, time(hour, minute), tz),
                        screen=f"Screen {((movie_index + slot_index) % SCREEN_COUNT) + 1}",
                        price=13.5 if hour >= 17 else 9.5,
                        rows=rows,
                        seats_per_row=seats_per_row,
                        booked_seats=booked,
                        source="generated",
                    )
                )
    return sort_by_start(showtimes)


@dataclass(frozen=True)
class BookingRef:
    """The parts of a booking that the showtime helpers need (Phase 3 passes real bookings)."""

    showtime_id: str
    seats: tuple[str, ...] = ()
    status: str = "CONFIRMED"


def merge_showtimes(
    generated: Sequence[ShowtimeRecord],
    stored: Sequence[ShowtimeRecord],
    bookings: Iterable[BookingRef],
    tombstones: Iterable[str] = (),
) -> list[ShowtimeRecord]:
    """Combines a freshly generated window with stored showtimes.

    Port of `mergeShowtimes`, plus the `source` and tombstone rules:

    - Generated showtimes that already exist keep their stored `bookedSeats`.
    - `manual` showtimes (admin-created or admin-edited) are kept exactly as stored.
    - Stored generated showtimes outside the window are kept only while a booking points at them.
    - Tombstoned (admin-deleted) ids never come back.
    """
    dead = set(tombstones)
    stored_by_id = {s.id: s for s in stored}
    generated_ids = {g.id for g in generated}
    referenced = {b.showtime_id for b in bookings}

    merged: list[ShowtimeRecord] = []
    for g in generated:
        if g.id in dead:
            continue
        existing = stored_by_id.get(g.id)
        if existing is None:
            merged.append(g)
        elif existing.source == "manual":
            merged.append(existing)
        else:
            merged.append(g.model_copy(update={"booked_seats": list(existing.booked_seats)}))
    for s in stored:
        if s.id in generated_ids or s.id in dead:
            continue
        if s.source == "manual" or s.id in referenced:
            merged.append(s)
    return sort_by_start(merged)


def apply_bookings_to_showtimes(
    showtimes: Sequence[ShowtimeRecord], bookings: Iterable[BookingRef]
) -> list[ShowtimeRecord]:
    """Marks the seats of every confirmed booking as booked (used when showtimes are rebuilt)."""
    seats_by_showtime: dict[str, list[str]] = {}
    for booking in bookings:
        if booking.status != "CONFIRMED":
            continue
        seats_by_showtime.setdefault(booking.showtime_id, []).extend(booking.seats)
    result = []
    for s in showtimes:
        extra = seats_by_showtime.get(s.id)
        if extra:
            s = s.model_copy(update={"booked_seats": list(dict.fromkeys([*s.booked_seats, *extra]))})
        result.append(s)
    return result


def sort_by_start(showtimes: list[ShowtimeRecord]) -> list[ShowtimeRecord]:
    return sorted(showtimes, key=lambda s: (s.starts_at, s.id))


def free_seats(showtime: ShowtimeRecord) -> list[str]:
    booked = set(showtime.booked_seats)
    grid = seat_grid(showtime.rows, showtime.seats_per_row)
    return [seat for row in grid for seat in row if seat not in booked]


def pick_different_movies(
    showtimes: Iterable[ShowtimeRecord], count: int, tz: ZoneInfo
) -> list[ShowtimeRecord]:
    """One showtime per movie and per day, so the samples spread over the week like real bookings."""
    picked: list[ShowtimeRecord] = []
    movies: set[str] = set()
    days: set[date] = set()
    for showtime in showtimes:
        day = showtime.starts_at.astimezone(tz).date()
        if showtime.movie_id in movies or day in days or len(free_seats(showtime)) < 3:
            continue
        picked.append(showtime)
        movies.add(showtime.movie_id)
        days.add(day)
        if len(picked) == count:
            break
    return picked


def create_sample_bookings(
    user: User,
    showtimes: list[ShowtimeRecord],
    now: datetime,
    tz: ZoneInfo,
    rng: random.Random,
    existing_ids: Iterable[str] = (),
) -> tuple[list[Booking], list[ShowtimeRecord]]:
    """Port of `createSampleBookings`: returns the new bookings and only the showtimes whose seats changed.

    3 upcoming confirmed (1, 2 and 3 seats), 1 upcoming cancelled and 2 past confirmed.
    """
    ordered = sort_by_start(showtimes)
    future = pick_different_movies([s for s in ordered if s.starts_at > now], 4, tz)
    past = pick_different_movies([s for s in reversed(ordered) if s.starts_at < now], 2, tz)
    plan = [
        *((s, i + 1, BookingStatus.CONFIRMED) for i, s in enumerate(future[:3])),
        *((s, 2, BookingStatus.CANCELLED) for s in future[3:4]),
        *((s, 2, BookingStatus.CONFIRMED) for s in past),
    ]

    ids = set(existing_ids)
    bookings: list[Booking] = []
    changed: list[ShowtimeRecord] = []
    for index, (showtime, count, status) in enumerate(plan):
        seats = free_seats(showtime)[:count]
        if status == BookingStatus.CONFIRMED:
            changed.append(showtime.model_copy(update={"booked_seats": [*showtime.booked_seats, *seats]}))
        while (booking_id := random_booking_id(rng)) in ids:
            pass
        ids.add(booking_id)
        bookings.append(
            Booking(
                id=booking_id,
                user_id=user.id,
                movie_id=showtime.movie_id,
                showtime_id=showtime.id,
                seats=seats,
                total_price=calculate_total(showtime.price, len(seats)),
                customer_name=user.name,
                email=user.email,
                created_at=min(now, showtime.starts_at) - timedelta(days=index + 1),
                status=status,
                cancelled_at=now if status == BookingStatus.CANCELLED else None,
            )
        )
    return bookings, changed


@dataclass
class SeedData:
    movies: list[MovieRecord]
    showtimes: list[ShowtimeRecord]
    users: list[User]


def build_seed(now: datetime, tz: ZoneInfo) -> SeedData:
    movies = load_movies()
    return SeedData(movies=movies, showtimes=generate_showtimes(movies, now, tz), users=list(SEED_USERS))

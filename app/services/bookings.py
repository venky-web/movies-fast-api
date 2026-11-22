"""Booking logic: the same rules, check order and error codes as the fake backend (plan section 3.4)."""

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.core.errors import ApiErrorCode, AppError, seats_taken
from app.domain.rules import (
    CancelBlockReason,
    calculate_total,
    check_cancellable,
    has_started,
    is_valid_seat,
    random_booking_id,
    sort_seats,
)
from app.repositories.base import Repos
from app.schemas.booking import (
    Booking,
    BookingDetails,
    BookingStatus,
    CreateBookingRequest,
    MovieSummary,
    ShowtimeSummary,
)
from app.schemas.movie import MovieRecord
from app.schemas.showtime import ShowtimeRecord
from app.services.locks import Locks


class Notifier(Protocol):
    """Sends booking emails (`EmailNotifier`: BackgroundTasks + outbox). Tests may use the no-op."""

    def booking_confirmed(self, booking: BookingDetails) -> None: ...
    def booking_cancelled(self, booking: BookingDetails) -> None: ...


class Publisher(Protocol):
    """Publishes real-time events (`BusPublisher`). Tests may use the no-op."""

    async def booking_created(self, booking: BookingDetails) -> None: ...
    async def booking_cancelled(self, booking: BookingDetails) -> None: ...
    async def showtime_changed(self, showtime: ShowtimeRecord) -> None: ...
    async def showtime_deleted(self, showtime_id: str) -> None: ...
    async def demo_reset(self) -> None: ...


class NoopNotifier:
    def booking_confirmed(self, booking: BookingDetails) -> None:
        pass

    def booking_cancelled(self, booking: BookingDetails) -> None:
        pass


class NoopPublisher:
    async def booking_created(self, booking: BookingDetails) -> None:
        pass

    async def booking_cancelled(self, booking: BookingDetails) -> None:
        pass

    async def showtime_changed(self, showtime: ShowtimeRecord) -> None:
        pass

    async def showtime_deleted(self, showtime_id: str) -> None:
        pass

    async def demo_reset(self) -> None:
        pass


@dataclass
class BookingContext:
    """Everything the booking service needs, assembled by the `get_booking_context` dependency."""

    repos: Repos
    locks: Locks
    now: datetime
    rng: random.Random
    notifier: Notifier = field(default_factory=NoopNotifier)
    publisher: Publisher = field(default_factory=NoopPublisher)


def booking_not_found(booking_id: str) -> AppError:
    return AppError(404, ApiErrorCode.NOT_FOUND, f'Booking "{booking_id}" not found')


def showtime_not_for_movie() -> AppError:
    return AppError(404, ApiErrorCode.NOT_FOUND, "That showtime does not exist for this movie")


def to_details(
    booking: Booking, movie: MovieRecord | None, showtime: ShowtimeRecord | None
) -> BookingDetails:
    """Port of `toDetails`, including its fallbacks for missing data."""
    return BookingDetails(
        **booking.model_dump(),
        movie=MovieSummary(
            id=booking.movie_id,
            title=movie.title if movie else "Unknown movie",
            poster_url=movie.poster_url if movie else "",
            duration_mins=movie.duration_mins if movie else 0,
            genres=movie.genres if movie else [],
        ),
        showtime=ShowtimeSummary(
            id=booking.showtime_id,
            starts_at=showtime.starts_at if showtime else booking.created_at,
            screen=showtime.screen if showtime else "Unknown screen",
            price=showtime.price if showtime else 0,
        ),
    )


async def details_for(repos: Repos, booking: Booking) -> BookingDetails:
    movie = await repos.movies.get(booking.movie_id, include_deleted=True)
    showtime = await repos.showtimes.get(booking.showtime_id)
    return to_details(booking, movie, showtime)


async def new_booking_id(repos: Repos, rng: random.Random) -> str:
    while True:
        booking_id = random_booking_id(rng)
        if not await repos.bookings.exists(booking_id):
            return booking_id


async def create_booking(request: CreateBookingRequest, ctx: BookingContext) -> BookingDetails:
    repos = ctx.repos
    # Step 1 (shape, seat count, name, email) already happened in Pydantic.
    if await repos.users.get(request.user_id) is None:
        raise AppError(400, ApiErrorCode.VALIDATION, "Unknown user")

    if await repos.showtimes.get(request.showtime_id) is None:
        raise showtime_not_for_movie()  # before locking, so unknown ids don't create locks

    async with ctx.locks.get(request.showtime_id):
        # Every check runs on what is read *inside* the lock: while we waited, another request may have booked
        # seats, and an admin may have moved or shrunk the showtime or deleted its movie.
        current = await repos.showtimes.get(request.showtime_id)
        if (
            current is None
            or current.movie_id != request.movie_id
            or await repos.movies.get(current.movie_id) is None  # soft-deleted
        ):
            raise showtime_not_for_movie()
        if has_started(current.starts_at, ctx.now):
            raise AppError(409, ApiErrorCode.SHOWTIME_STARTED, "This showtime has already started")
        invalid = [s for s in request.seats if not is_valid_seat(current.rows, current.seats_per_row, s)]
        if invalid:
            raise AppError(
                400,
                ApiErrorCode.VALIDATION,
                f"Unknown seats: {', '.join(invalid)}",
                {"invalidSeats": invalid},
            )
        taken = [s for s in request.seats if s in current.booked_seats]
        if taken:
            raise seats_taken(taken)

        seats = sort_seats(request.seats)
        booking = Booking(
            id=await new_booking_id(repos, ctx.rng),
            user_id=request.user_id,
            movie_id=request.movie_id,
            showtime_id=current.id,
            seats=seats,
            total_price=calculate_total(current.price, len(seats)),
            customer_name=request.customer_name,
            email=request.email,
            created_at=ctx.now,
            status=BookingStatus.CONFIRMED,
        )
        updated = current.model_copy(update={"booked_seats": [*current.booked_seats, *seats]})
        await repos.showtimes.save(updated)
        await repos.bookings.add(booking)

    details = await details_for(repos, booking)
    ctx.notifier.booking_confirmed(details)
    await ctx.publisher.booking_created(details)
    return details


async def list_bookings(
    repos: Repos, user_id: str, status: BookingStatus | None = None
) -> list[BookingDetails]:
    bookings = await repos.bookings.list_for_user(user_id)
    if status:
        bookings = [b for b in bookings if b.status == status]
    return [await details_for(repos, b) for b in bookings]


async def get_booking(repos: Repos, booking_id: str, user_id: str) -> BookingDetails:
    booking = await repos.bookings.get(booking_id)
    # Another user's booking is reported as "not found" so ids cannot be probed.
    if booking is None or booking.user_id != user_id:
        raise booking_not_found(booking_id)
    return await details_for(repos, booking)


async def cancel_booking(booking_id: str, user_id: str, ctx: BookingContext) -> BookingDetails:
    repos = ctx.repos
    booking = await repos.bookings.get(booking_id)
    if booking is None or booking.user_id != user_id:
        raise booking_not_found(booking_id)

    async with ctx.locks.get(booking.showtime_id):
        booking = await repos.bookings.get(booking_id)  # re-read: it may have been cancelled meanwhile
        if booking is None or booking.user_id != user_id:
            raise booking_not_found(booking_id)  # or a reset removed it while we waited
        showtime = await repos.showtimes.get(booking.showtime_id)
        starts_at = showtime.starts_at if showtime else booking.created_at
        match check_cancellable(booking.status, starts_at, ctx.now):
            case CancelBlockReason.ALREADY_CANCELLED:
                raise AppError(409, ApiErrorCode.ALREADY_CANCELLED, "This booking is already cancelled")
            case CancelBlockReason.SHOWTIME_STARTED:
                raise AppError(
                    409,
                    ApiErrorCode.SHOWTIME_STARTED,
                    "Bookings cannot be cancelled after the showtime starts",
                )

        cancelled = booking.model_copy(update={"status": BookingStatus.CANCELLED, "cancelled_at": ctx.now})
        if showtime:
            freed = [s for s in showtime.booked_seats if s not in booking.seats]
            showtime = showtime.model_copy(update={"booked_seats": freed})
            await repos.showtimes.save(showtime)
        await repos.bookings.update(cancelled)

    details = await details_for(repos, cancelled)
    ctx.notifier.booking_cancelled(details)
    await ctx.publisher.booking_cancelled(details)
    return details

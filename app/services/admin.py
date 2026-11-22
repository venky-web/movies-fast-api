"""Admin rules for movies, showtimes and the demo data (plan 5.7-5.9).

Every write takes the catalog lock first, and then the locks of the showtimes it changes.
"""

import random
import re
import unicodedata
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.core.errors import ApiErrorCode, AppError, not_found
from app.domain.rules import has_started, is_valid_seat
from app.domain.seed import SeedData, build_seed, create_sample_bookings, day_key, poster_path
from app.repositories.base import Repos
from app.schemas.base import Page
from app.schemas.booking import AdminBookingFilters, Booking, BookingDetails, BookingStatus
from app.schemas.movie import MovieCreate, MovieRecord, MovieReplace, MovieUpdate
from app.schemas.showtime import ShowtimeCreate, ShowtimeRecord, ShowtimeUpdate
from app.services import posters
from app.services.bookings import NoopPublisher, Publisher, details_for
from app.services.locks import CATALOG_LOCK, Locks, ShowtimeLocks


async def run_now(action: Callable[[], Awaitable[object]]) -> None:
    await action()


@dataclass
class AdminContext:
    """Everything the admin service needs, assembled by the `get_admin_context` dependency."""

    repos: Repos
    locks: Locks
    now: datetime
    tz: ZoneInfo
    rng: random.Random
    publisher: Publisher = field(default_factory=NoopPublisher)
    # For what a rollback can't undo, such as deleting files.
    after_commit: Callable[[Callable[[], Awaitable[object]]], Awaitable[None]] = run_now


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:70].rstrip("-")


def default_movie_id(title: str) -> str:
    # A title without any Latin letters or digits still needs a usable, stable id.
    return f"m-{slugify(title) or format(zlib.crc32(title.encode()), 'x')}"


def default_hue(movie_id: str) -> int:
    return zlib.crc32(movie_id.encode()) % 360  # not `hash()`, which differs in every process


async def require_movie(repos: Repos, movie_id: str) -> MovieRecord:
    movie = await repos.movies.get(movie_id)
    if movie is None:
        raise not_found("Movie", movie_id)
    return movie


async def require_showtime(repos: Repos, showtime_id: str) -> ShowtimeRecord:
    showtime = await repos.showtimes.get(showtime_id)
    if showtime is None:
        raise not_found("Showtime", showtime_id)
    return showtime


def require_future(starts_at: datetime, now: datetime) -> None:
    if has_started(starts_at, now):
        raise AppError(400, ApiErrorCode.VALIDATION, "startsAt must be in the future")


# Movies


async def create_movie(data: MovieCreate, ctx: AdminContext) -> MovieRecord:
    movie_id = data.id or default_movie_id(data.title)
    async with ctx.locks.get(CATALOG_LOCK):
        if await ctx.repos.movies.get(movie_id, include_deleted=True):  # soft-deleted ids stay taken
            raise AppError(409, ApiErrorCode.ALREADY_EXISTS, f'Movie "{movie_id}" already exists')
        movie = MovieRecord.model_validate(
            {
                **data.model_dump(by_alias=False, exclude={"id", "hue"}),
                "id": movie_id,
                "hue": default_hue(movie_id) if data.hue is None else data.hue,
                "poster_url": poster_path(movie_id),
                "source": "admin",
            }
        )
        await ctx.repos.movies.save(movie)
    return movie


async def _change_movie(movie_id: str, changes: dict[str, Any], ctx: AdminContext) -> MovieRecord:
    async with ctx.locks.get(CATALOG_LOCK):
        movie = await require_movie(ctx.repos, movie_id)
        # Not `model_copy(update=...)`: that does not validate, and would leave nested dicts as dicts.
        updated = MovieRecord.model_validate({**movie.model_dump(by_alias=False), **changes})
        await ctx.repos.movies.save(updated)
    return updated


async def replace_movie(movie_id: str, data: MovieReplace, ctx: AdminContext) -> MovieRecord:
    changes = data.model_dump(by_alias=False)
    if data.hue is None:
        del changes["hue"]
    return await _change_movie(movie_id, changes, ctx)


async def update_movie(movie_id: str, data: MovieUpdate, ctx: AdminContext) -> MovieRecord:
    return await _change_movie(movie_id, data.model_dump(by_alias=False, exclude_unset=True), ctx)


async def delete_movie(movie_id: str, ctx: AdminContext) -> None:
    """Soft delete. Upcoming showtimes are removed, except those that a cancelled booking still points at."""
    repos = ctx.repos
    async with ctx.locks.get(CATALOG_LOCK):
        movie = await require_movie(repos, movie_id)
        showtimes = await repos.showtimes.list_for_movie(movie_id)
        upcoming = {s.id for s in showtimes if not has_started(s.starts_at, ctx.now)}
        async with ctx.locks.hold_all(upcoming):
            bookings = [b for b in await repos.bookings.list_all() if b.showtime_id in upcoming]
            if any(b.status == BookingStatus.CONFIRMED for b in bookings):
                raise AppError(
                    409,
                    ApiErrorCode.MOVIE_HAS_BOOKINGS,
                    f'Movie "{movie_id}" has bookings for upcoming showtimes',
                )
            await repos.movies.save(movie.model_copy(update={"deleted_at": ctx.now}))
            removed = sorted(upcoming - {b.showtime_id for b in bookings})
            for showtime_id in removed:
                await repos.showtimes.delete(showtime_id)
                await repos.tombstones.add(showtime_id)
    # All of them: the ones that were kept are hidden with the movie, so watchers must leave those too.
    for showtime in showtimes:
        await ctx.publisher.showtime_deleted(showtime.id)


# Showtimes


async def create_showtime(data: ShowtimeCreate, ctx: AdminContext) -> ShowtimeRecord:
    repos = ctx.repos
    async with ctx.locks.get(CATALOG_LOCK):
        await require_movie(repos, data.movie_id)
        require_future(data.starts_at, ctx.now)
        local = data.starts_at.astimezone(ctx.tz)
        showtime_id = f"st-{data.movie_id}-{day_key(local.date())}-{local:%H%M}"
        if await repos.showtimes.get(showtime_id):
            raise AppError(409, ApiErrorCode.ALREADY_EXISTS, f'Showtime "{showtime_id}" already exists')
        showtime = ShowtimeRecord.model_validate(
            {**data.model_dump(by_alias=False), "id": showtime_id, "booked_seats": [], "source": "manual"}
        )
        await repos.showtimes.save(showtime)
        # The id may belong to a deleted showtime, and a tombstone would make the refresh drop the new one.
        await repos.tombstones.remove(showtime_id)
    return showtime


async def update_showtime(showtime_id: str, data: ShowtimeUpdate, ctx: AdminContext) -> ShowtimeRecord:
    repos = ctx.repos
    changes = data.model_dump(by_alias=False, exclude_unset=True)
    async with ctx.locks.get(CATALOG_LOCK):
        await require_showtime(repos, showtime_id)  # before locking, so unknown ids don't create locks
        async with ctx.locks.get(showtime_id):
            current = await require_showtime(repos, showtime_id)
            if not changes:
                return current  # nothing was edited, so it stays `generated`
            updated = ShowtimeRecord.model_validate(
                {**current.model_dump(by_alias=False), **changes, "source": "manual"}
            )
            await require_movie(repos, updated.movie_id)
            require_future(updated.starts_at, ctx.now)
            if updated.movie_id != current.movie_id and await repos.bookings.list_for_showtime(showtime_id):
                raise AppError(
                    409,
                    ApiErrorCode.SHOWTIME_HAS_BOOKINGS,
                    f'Showtime "{showtime_id}" has bookings, so its movie cannot change',
                )
            outside = [
                seat
                for seat in current.booked_seats
                if not is_valid_seat(updated.rows, updated.seats_per_row, seat)
            ]
            if outside:
                raise AppError(
                    409,
                    ApiErrorCode.LAYOUT_CONFLICT,
                    f"Booked seats would fall outside the layout: {', '.join(outside)}",
                    {"outsideSeats": outside},
                )
            await repos.showtimes.save(updated)
    await ctx.publisher.showtime_changed(updated)
    return updated


async def delete_showtime(showtime_id: str, ctx: AdminContext) -> None:
    repos = ctx.repos
    async with ctx.locks.get(CATALOG_LOCK):
        await require_showtime(repos, showtime_id)
        async with ctx.locks.get(showtime_id):
            if await repos.bookings.list_for_showtime(showtime_id):
                raise AppError(
                    409, ApiErrorCode.SHOWTIME_HAS_BOOKINGS, f'Showtime "{showtime_id}" has bookings'
                )
            await repos.showtimes.delete(showtime_id)
            # Whatever the source: an edited generated showtime is `manual`, yet the generator knows its id.
            await repos.tombstones.add(showtime_id)
    await ctx.publisher.showtime_deleted(showtime_id)


# Bookings and demo data


def _matches(booking: Booking, filters: AdminBookingFilters) -> bool:
    return (
        (filters.status is None or booking.status == filters.status)
        and (filters.movie_id is None or booking.movie_id == filters.movie_id)
        and (filters.user_id is None or booking.user_id == filters.user_id)
        and (filters.created_from is None or booking.created_at >= filters.created_from)
        and (filters.created_to is None or booking.created_at <= filters.created_to)
    )


async def list_bookings(
    repos: Repos, filters: AdminBookingFilters, *, limit: int, offset: int
) -> Page[BookingDetails]:
    found = [b for b in await repos.bookings.list_all() if _matches(b, filters)]
    items = [await details_for(repos, b) for b in found[offset : offset + limit]]
    return Page[BookingDetails](items=items, total=len(found), limit=limit, offset=offset)


async def add_sample_bookings(user_id: str, ctx: AdminContext) -> int:
    repos = ctx.repos
    user = await repos.users.get(user_id)
    if user is None:
        raise not_found("User", user_id)
    async with ctx.locks.get(CATALOG_LOCK):
        live_movies = {m.id for m in await repos.movies.list_all()}
        ids = [s.id for s in await repos.showtimes.list_all() if s.movie_id in live_movies]
        # Which showtimes get picked depends on their free seats, so hold all of them while picking.
        async with ctx.locks.hold_all(ids):
            showtimes = [s for s in await repos.showtimes.list_all() if s.movie_id in live_movies]
            existing = [b.id for b in await repos.bookings.list_all()]
            added, changed = create_sample_bookings(user, showtimes, ctx.now, ctx.tz, ctx.rng, existing)
            for showtime in changed:
                await repos.showtimes.save(showtime)
            for booking in reversed(added):  # so the first sample counts as the most recently added
                await repos.bookings.add(booking)
    await ctx.publisher.demo_reset()
    return len(added)


async def add_startup_samples(
    repos: Repos, user_id: str, *, now: datetime, tz: ZoneInfo, rng: random.Random
) -> None:
    """`SAMPLE_BOOKINGS_FOR`, added while seeding. Nothing else is running yet, so private locks will do."""
    await add_sample_bookings(user_id, AdminContext(repos, ShowtimeLocks(), now, tz, rng))


async def reset_demo(
    ctx: AdminContext, install: Callable[[SeedData], Awaitable[None]], uploads_dir: Path
) -> None:
    """Mirrors `resetDemoData()`: no bookings, seed catalogue, fresh window. Demo settings are kept.

    `install` is atomic: a swap of the store in memory, one transaction in SQL. Every showtime lock is held,
    so no booking can check its seats against the old data and write into the new.
    """
    async with ctx.locks.get(CATALOG_LOCK):
        ids = [s.id for s in await ctx.repos.showtimes.list_all()]
        async with ctx.locks.hold_all(ids):
            await install(build_seed(ctx.now, ctx.tz))
            await ctx.after_commit(partial(posters.clear_uploads, uploads_dir))
    await ctx.publisher.demo_reset()

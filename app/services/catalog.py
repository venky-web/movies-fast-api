"""Catalog reads: filtering, sorting and paging of movies and showtimes."""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.domain.rules import has_started
from app.domain.seed import BookingRef, generate_showtimes, merge_showtimes
from app.repositories.base import Repos
from app.schemas.movie import Genre, MovieRecord, MovieSort
from app.schemas.showtime import ShowtimeRecord
from app.services.locks import CATALOG_LOCK, Locks

log = logging.getLogger(__name__)


@dataclass
class MoviePage:
    items: list[MovieRecord]
    total: int


def _matches_text(movie: MovieRecord, q: str) -> bool:
    needle = q.casefold()
    haystack = [movie.title, movie.director, *movie.cast]
    return any(needle in value.casefold() for value in haystack)


def sort_movies(movies: list[MovieRecord], sort: MovieSort) -> list[MovieRecord]:
    """Stable sorts, so ties keep the seed order. Rating and release sort newest/highest first."""
    match sort:
        case MovieSort.TITLE:
            return sorted(movies, key=lambda m: m.title.casefold())
        case MovieSort.RATING:
            return sorted(movies, key=lambda m: m.rating, reverse=True)
        case MovieSort.RELEASE:
            return sorted(movies, key=lambda m: m.release_date, reverse=True)
        case MovieSort.DURATION:
            return sorted(movies, key=lambda m: m.duration_mins)


async def list_movies(
    repos: Repos,
    *,
    q: str | None = None,
    genres: list[Genre] | None = None,
    sort: MovieSort | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> MoviePage:
    movies = await repos.movies.list_all()
    if q:
        movies = [m for m in movies if _matches_text(m, q)]
    if genres:
        wanted = set(genres)
        movies = [m for m in movies if wanted.intersection(m.genres)]
    if sort:
        movies = sort_movies(movies, sort)
    total = len(movies)
    end = None if limit is None else offset + limit
    return MoviePage(items=movies[offset:end], total=total)


async def get_movie(repos: Repos, movie_id: str) -> MovieRecord | None:
    return await repos.movies.get(movie_id)


async def get_showtime(repos: Repos, showtime_id: str) -> ShowtimeRecord | None:
    """A showtime of a soft-deleted movie is hidden."""
    showtime = await repos.showtimes.get(showtime_id)
    if showtime is None or await repos.movies.get(showtime.movie_id) is None:
        return None
    return showtime


async def list_movie_showtimes(
    repos: Repos,
    movie_id: str,
    *,
    now: datetime,
    tz: ZoneInfo,
    on_date: date | None = None,
    upcoming_only: bool = False,
) -> list[ShowtimeRecord] | None:
    """Showtimes of a movie sorted by start, or None if the movie does not exist."""
    if await repos.movies.get(movie_id) is None:
        return None
    showtimes = await repos.showtimes.list_for_movie(movie_id)
    if on_date is not None:
        showtimes = [s for s in showtimes if s.starts_at.astimezone(tz).date() == on_date]
    if upcoming_only:
        showtimes = [s for s in showtimes if not has_started(s.starts_at, now)]
    return sorted(showtimes, key=lambda s: (s.starts_at, s.id))


async def refresh_showtime_window(repos: Repos, locks: Locks, *, now: datetime, tz: ZoneInfo) -> list[str]:
    """Moves the generated window to the day of `now` (plan 5.7). Returns the ids it removed.

    It reads and then writes `bookedSeats`, so it holds the catalog lock and every stored showtime's lock.
    Without them, a booking made in between would be overwritten.
    """
    async with locks.get(CATALOG_LOCK):
        ids = [s.id for s in await repos.showtimes.list_all()]
        async with locks.hold_all(ids):
            before = {s.id: s for s in await repos.showtimes.list_all()}
            movies = await repos.movies.list_all(include_deleted=True)
            live = {m.id for m in movies if m.deleted_at is None}
            tombstones = await repos.tombstones.list_all()
            # Generate from the full seed list, deleted movies included: screens depend on the position.
            seeded = [m for m in movies if m.source == "seed"]
            generated = [
                s for s in generate_showtimes(seeded, now, tz, skip_ids=tombstones) if s.movie_id in live
            ]
            referenced = [BookingRef(showtime_id=b.showtime_id) for b in await repos.bookings.list_all()]
            merged = merge_showtimes(generated, list(before.values()), referenced, tombstones)
            after = {s.id: s for s in merged}

            removed = before.keys() - after.keys()
            changed = [s for s in merged if before.get(s.id) != s]
            await repos.showtimes.delete_many(sorted(removed))
            await repos.showtimes.save_many(changed)
    log.info("Showtime window refreshed: %d saved, %d removed", len(changed), len(removed))
    return sorted(removed)

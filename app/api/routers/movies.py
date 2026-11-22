import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query, Response
from pydantic import Field

from app.api.deps import MovieIdPath, NowDep, PaginationDep, ReposDep, TheaterTzDep
from app.core.errors import NOT_FOUND_RESPONSE, not_found
from app.schemas.base import CamelModel
from app.schemas.movie import Genre, Movie, MovieSort
from app.schemas.showtime import Showtime
from app.services import catalog

router = APIRouter(prefix="/api/movies", tags=["movies"])
legacy_router = APIRouter(prefix="/api", tags=["movies"])

TOTAL_COUNT_HEADER = {
    "X-Total-Count": {"description": "Matches before paging", "schema": {"type": "integer", "minimum": 0}}
}


class MovieFilters(CamelModel):
    """Query parameter model: `?q=...&genre=Drama&genre=Comedy&sort=rating`."""

    q: str | None = Field(None, description="Case-insensitive search in title, director and cast")
    genre: list[Genre] = Field(default_factory=list, description="Repeatable; a movie matches any of them")
    sort: MovieSort | None = Field(
        None,
        description="`title`, `duration` ascending; `rating`, `release` descending. Default: seed order",
    )


class ShowtimeFilters(CamelModel):
    """`date` is a day in the theater's time zone. `upcomingOnly` uses the camelCase alias."""

    date: dt.date | None = Field(None, description="Only this day (yyyy-mm-dd) in the theater's time zone")
    upcoming_only: bool = Field(False, description="Leave out showtimes that have started")


@router.get(
    "",
    response_model=list[Movie],
    summary="List movies",
    response_description="One page of the matching movies. The body is a plain array",
    responses={200: {"headers": TOTAL_COUNT_HEADER}},
)
async def list_movies(
    filters: Annotated[MovieFilters, Query()],
    page: PaginationDep,
    repos: ReposDep,
    response: Response,
):
    """Search, filter, sort and page the catalogue. Without parameters it is the whole catalogue, in seed
    order, like the fake backend. The number of matches is in `X-Total-Count`."""
    result = await catalog.list_movies(
        repos,
        q=filters.q,
        genres=filters.genre,
        sort=filters.sort,
        limit=page.limit,  # no default limit: the fake backend returns the whole catalogue
        offset=page.offset,
    )
    response.headers["X-Total-Count"] = str(result.total)
    return result.items


@legacy_router.get(
    "/movie-list",
    response_model=list[Movie],
    deprecated=True,
    summary="List movies (old path)",
    response_description="The same as GET /api/movies",
    responses={200: {"headers": TOTAL_COUNT_HEADER}},
)
async def list_movies_legacy(
    filters: Annotated[MovieFilters, Query()],
    page: PaginationDep,
    repos: ReposDep,
    response: Response,
):
    """Use `GET /api/movies` instead. This alias exists to demonstrate `deprecated=True`."""
    return await list_movies(filters, page, repos, response)


@router.get(
    "/{movie_id}",
    response_model=Movie,
    summary="Get a movie",
    response_description="The movie",
    responses=NOT_FOUND_RESPONSE,
)
async def get_movie(movie_id: MovieIdPath, repos: ReposDep):
    """One movie with its reviews. Deleted movies are 404."""
    movie = await catalog.get_movie(repos, movie_id)
    if movie is None:
        raise not_found("Movie", movie_id)
    return movie


@router.get(
    "/{movie_id}/showtimes",
    response_model=list[Showtime],
    summary="List a movie's showtimes",
    response_description="The showtimes, sorted by start",
    responses=NOT_FOUND_RESPONSE,
)
async def list_movie_showtimes(
    movie_id: MovieIdPath,
    filters: Annotated[ShowtimeFilters, Query()],
    repos: ReposDep,
    now: NowDep,
    tz: TheaterTzDep,
):
    """Includes the past two days unless `upcomingOnly` is set. Take a `showtimeId` from here to book."""
    showtimes = await catalog.list_movie_showtimes(
        repos, movie_id, now=now, tz=tz, on_date=filters.date, upcoming_only=filters.upcoming_only
    )
    if showtimes is None:
        raise not_found("Movie", movie_id)
    return showtimes

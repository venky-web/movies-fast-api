import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query, Response
from pydantic import Field

from app.api.deps import NowDep, PaginationDep, ReposDep, TheaterTzDep
from app.core.errors import not_found
from app.schemas.base import CamelModel
from app.schemas.movie import Genre, Movie, MovieSort
from app.schemas.showtime import Showtime
from app.services import catalog

router = APIRouter(prefix="/api/movies", tags=["movies"])


class MovieFilters(CamelModel):
    """Query parameter model: `?q=...&genre=Drama&genre=Comedy&sort=rating`."""

    q: str | None = Field(None, description="Case-insensitive search in title, director and cast")
    genre: list[Genre] = Field(default_factory=list, description="Repeatable; a movie matches any of them")
    sort: MovieSort | None = None


class ShowtimeFilters(CamelModel):
    """`date` is a day in the theater's time zone. `upcomingOnly` uses the camelCase alias."""

    date: dt.date | None = None
    upcoming_only: bool = False


@router.get("", response_model=list[Movie])
async def list_movies(
    filters: Annotated[MovieFilters, Query()],
    page: PaginationDep,
    repos: ReposDep,
    response: Response,
):
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


@router.get("/{movie_id}", response_model=Movie)
async def get_movie(movie_id: str, repos: ReposDep):
    movie = await catalog.get_movie(repos, movie_id)
    if movie is None:
        raise not_found("Movie", movie_id)
    return movie


@router.get("/{movie_id}/showtimes", response_model=list[Showtime])
async def list_movie_showtimes(
    movie_id: str,
    filters: Annotated[ShowtimeFilters, Query()],
    repos: ReposDep,
    now: NowDep,
    tz: TheaterTzDep,
):
    showtimes = await catalog.list_movie_showtimes(
        repos, movie_id, now=now, tz=tz, on_date=filters.date, upcoming_only=filters.upcoming_only
    )
    if showtimes is None:
        raise not_found("Movie", movie_id)
    return showtimes

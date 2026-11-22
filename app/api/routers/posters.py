import logging
import threading
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.deps import MovieIdPath, ReposDep
from app.core.errors import NOT_FOUND_RESPONSE, not_found
from app.domain.posters import create_poster
from app.schemas.movie import MovieRecord

router = APIRouter(prefix="/api/posters", tags=["posters"])
log = logging.getLogger(__name__)


async def movie_for_poster(movie_id: MovieIdPath, repos: ReposDep) -> MovieRecord:
    """Async dependency: the repo is async, but the route itself is a plain `def`."""
    movie = await repos.movies.get(movie_id, include_deleted=True)
    if movie is None:
        raise not_found("Movie", movie_id)
    return movie


@router.get(
    "/{movie_id}.svg",
    response_class=Response,
    summary="Get a generated poster",
    response_description="The SVG poster",
    responses={
        200: {
            "content": {"image/svg+xml": {"schema": {"type": "string"}}},
            "headers": {"Cache-Control": {"description": "Cached for a day", "schema": {"type": "string"}}},
        },
        **NOT_FOUND_RESPONSE,
    },
)
def get_poster(movie: Annotated[MovieRecord, Depends(movie_for_poster)]) -> Response:
    """An SVG drawn from the title and hue. Deleted movies keep theirs, because bookings still show it."""
    # A sync `def` route runs in Starlette's threadpool, not on the event loop.
    log.debug("poster for %s rendered on thread %s", movie.id, threading.current_thread().name)
    return Response(
        content=create_poster(movie.title, movie.hue),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )

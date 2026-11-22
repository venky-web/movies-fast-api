from fastapi import APIRouter

from app.api.deps import ReposDep, ShowtimeIdPath
from app.core.errors import NOT_FOUND_RESPONSE, not_found
from app.schemas.showtime import Showtime
from app.services import catalog

router = APIRouter(prefix="/api/showtimes", tags=["showtimes"])


@router.get(
    "/{showtime_id}",
    response_model=Showtime,
    summary="Get a showtime",
    response_description="The showtime with its booked seats",
    responses=NOT_FOUND_RESPONSE,
)
async def get_showtime(showtime_id: ShowtimeIdPath, repos: ReposDep):
    """One showtime with its current seat data. For live updates use the WebSocket (see *realtime*)."""
    showtime = await catalog.get_showtime(repos, showtime_id)
    if showtime is None:
        raise not_found("Showtime", showtime_id)
    return showtime

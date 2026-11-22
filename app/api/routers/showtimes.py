from fastapi import APIRouter

from app.api.deps import ReposDep
from app.core.errors import not_found
from app.schemas.showtime import Showtime
from app.services import catalog

router = APIRouter(prefix="/api/showtimes", tags=["showtimes"])


@router.get("/{showtime_id}", response_model=Showtime)
async def get_showtime(showtime_id: str, repos: ReposDep):
    showtime = await catalog.get_showtime(repos, showtime_id)
    if showtime is None:
        raise not_found("Showtime", showtime_id)
    return showtime

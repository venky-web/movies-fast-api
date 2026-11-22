from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status

from app.api.deps import (
    AdminContextDep,
    AdminRoute,
    ConfigDep,
    InstallSeedDep,
    PaginationDep,
    ReposDep,
    require_admin,
)
from app.schemas.base import Page
from app.schemas.booking import AdminBookingFilters, BookingDetails
from app.schemas.demo import DemoSettings, SampleBookingsResult
from app.schemas.movie import MovieCreate, MovieRecord, MovieReplace, MovieUpdate
from app.schemas.outbox import OutboxEmail
from app.schemas.showtime import ShowtimeCreate, ShowtimeRecord, ShowtimeUpdate
from app.services import admin, notifications, posters

DEFAULT_PAGE_SIZE = 20

# The router-level dependency protects every route below, including ones added later.
router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)], route_class=AdminRoute
)

# Admin responses use the record models, so they show the internal fields (`source`, `hue`, ...).


@router.post(
    "/movies",
    status_code=status.HTTP_201_CREATED,
    response_model=MovieRecord,
    response_model_exclude_none=True,
)
async def create_movie(body: MovieCreate, ctx: AdminContextDep, response: Response):
    movie = await admin.create_movie(body, ctx)
    response.headers["Location"] = f"/api/movies/{movie.id}"
    return movie


@router.put("/movies/{movie_id}", response_model=MovieRecord, response_model_exclude_none=True)
async def replace_movie(movie_id: str, body: MovieReplace, ctx: AdminContextDep):
    return await admin.replace_movie(movie_id, body, ctx)


@router.patch("/movies/{movie_id}", response_model=MovieRecord, response_model_exclude_none=True)
async def update_movie(movie_id: str, body: MovieUpdate, ctx: AdminContextDep):
    return await admin.update_movie(movie_id, body, ctx)


@router.delete("/movies/{movie_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_movie(movie_id: str, ctx: AdminContextDep) -> None:
    await admin.delete_movie(movie_id, ctx)


@router.post("/movies/{movie_id}/poster", response_model=MovieRecord, response_model_exclude_none=True)
async def upload_poster(
    movie_id: str,
    file: Annotated[UploadFile, File(description="PNG, JPEG, WebP or SVG, at most 2 MB")],
    ctx: AdminContextDep,
    config: ConfigDep,
):
    return await posters.upload_poster(movie_id, file, ctx, config.uploads_dir)


@router.post("/showtimes", status_code=status.HTTP_201_CREATED, response_model=ShowtimeRecord)
async def create_showtime(body: ShowtimeCreate, ctx: AdminContextDep, response: Response):
    showtime = await admin.create_showtime(body, ctx)
    response.headers["Location"] = f"/api/showtimes/{showtime.id}"
    return showtime


@router.patch("/showtimes/{showtime_id}", response_model=ShowtimeRecord)
async def update_showtime(showtime_id: str, body: ShowtimeUpdate, ctx: AdminContextDep):
    return await admin.update_showtime(showtime_id, body, ctx)


@router.delete("/showtimes/{showtime_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_showtime(showtime_id: str, ctx: AdminContextDep) -> None:
    await admin.delete_showtime(showtime_id, ctx)


@router.get("/bookings", response_model=Page[BookingDetails], response_model_exclude_none=True)
async def list_bookings(
    filters: Annotated[AdminBookingFilters, Query()],
    page: PaginationDep,
    repos: ReposDep,
):
    return await admin.list_bookings(
        repos, filters, limit=page.limit_or(DEFAULT_PAGE_SIZE), offset=page.offset
    )


@router.get("/settings", response_model=DemoSettings)
async def get_demo_settings(request: Request):
    return request.app.state.demo_settings


@router.put("/settings", response_model=DemoSettings)
async def replace_demo_settings(body: DemoSettings, request: Request):
    request.app.state.demo_settings = body
    return body


@router.post("/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_demo(ctx: AdminContextDep, install: InstallSeedDep, config: ConfigDep) -> None:
    await admin.reset_demo(ctx, install, config.uploads_dir)


@router.post("/sample-bookings", response_model=SampleBookingsResult)
async def add_sample_bookings(user_id: Annotated[str, Query(alias="userId")], ctx: AdminContextDep):
    return SampleBookingsResult(added=await admin.add_sample_bookings(user_id, ctx))


@router.get("/outbox", response_model=list[OutboxEmail])
async def list_outbox(repos: ReposDep):
    """The "sent" emails, newest first."""
    return await notifications.list_outbox(repos)

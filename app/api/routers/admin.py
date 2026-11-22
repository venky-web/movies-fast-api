from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status

from app.api.deps import (
    AdminContextDep,
    AdminRoute,
    ConfigDep,
    InstallSeedDep,
    MovieIdPath,
    PaginationDep,
    ReposDep,
    ShowtimeIdPath,
    require_admin,
)
from app.core.errors import NOT_FOUND_RESPONSE, UNAUTHORIZED_RESPONSE, ApiErrorCode, error_responses
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
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
    route_class=AdminRoute,
    responses=UNAUTHORIZED_RESPONSE,
)

LOCATION_HEADER = {"Location": {"description": "The public URL", "schema": {"type": "string"}}}

# Admin responses use the record models, so they show the internal fields (`source`, `hue`, ...).


@router.post(
    "/movies",
    status_code=status.HTTP_201_CREATED,
    response_model=MovieRecord,
    response_model_exclude_none=True,
    summary="Create a movie",
    response_description="The new movie",
    responses={201: {"headers": LOCATION_HEADER}, **error_responses(ApiErrorCode.ALREADY_EXISTS)},
)
async def create_movie(body: MovieCreate, ctx: AdminContextDep, response: Response):
    """The id defaults to `m-{slug of the title}`. Ids of deleted movies stay taken. A created movie has no
    showtimes until you add some."""
    movie = await admin.create_movie(body, ctx)
    response.headers["Location"] = f"/api/movies/{movie.id}"
    return movie


@router.put(
    "/movies/{movie_id}",
    response_model=MovieRecord,
    response_model_exclude_none=True,
    summary="Replace a movie",
    response_description="The replaced movie",
    responses=NOT_FOUND_RESPONSE,
)
async def replace_movie(movie_id: MovieIdPath, body: MovieReplace, ctx: AdminContextDep):
    """Every field is replaced: leaving out `reviews` empties them. Only an omitted `hue` is kept."""
    return await admin.replace_movie(movie_id, body, ctx)


@router.patch(
    "/movies/{movie_id}",
    response_model=MovieRecord,
    response_model_exclude_none=True,
    summary="Update a movie",
    response_description="The updated movie",
    responses=NOT_FOUND_RESPONSE,
)
async def update_movie(movie_id: MovieIdPath, body: MovieUpdate, ctx: AdminContextDep):
    """Changes only the fields you send. Null is rejected."""
    return await admin.update_movie(movie_id, body, ctx)


@router.delete(
    "/movies/{movie_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a movie",
    response_description="Deleted",
    responses=error_responses(ApiErrorCode.NOT_FOUND, ApiErrorCode.MOVIE_HAS_BOOKINGS),
)
async def delete_movie(movie_id: MovieIdPath, ctx: AdminContextDep) -> None:
    """A soft delete: the movie and its showtimes disappear, old bookings keep their details."""
    await admin.delete_movie(movie_id, ctx)


@router.post(
    "/movies/{movie_id}/poster",
    response_model=MovieRecord,
    response_model_exclude_none=True,
    summary="Upload a poster",
    response_description="The movie, with its new posterUrl",
    responses=error_responses(
        ApiErrorCode.NOT_FOUND, ApiErrorCode.PAYLOAD_TOO_LARGE, ApiErrorCode.UNSUPPORTED_MEDIA_TYPE
    ),
)
async def upload_poster(
    movie_id: MovieIdPath,
    file: Annotated[UploadFile, File(description="PNG, JPEG, WebP or SVG, at most 2 MB")],
    ctx: AdminContextDep,
    config: ConfigDep,
):
    """PNG, JPEG, WebP or SVG, at most 2 MB. The type is read from the content and must match the part's
    content type. Replaces and deletes the previous upload."""
    return await posters.upload_poster(movie_id, file, ctx, config.uploads_dir)


@router.post(
    "/showtimes",
    status_code=status.HTTP_201_CREATED,
    response_model=ShowtimeRecord,
    summary="Create a showtime",
    response_description="The new showtime",
    responses={
        201: {"headers": LOCATION_HEADER},
        **error_responses(ApiErrorCode.VALIDATION, ApiErrorCode.NOT_FOUND, ApiErrorCode.ALREADY_EXISTS),
    },
)
async def create_showtime(body: ShowtimeCreate, ctx: AdminContextDep, response: Response):
    """Must start in the future. The id comes from the movie and the local start time, so there is one
    showtime per movie and minute."""
    showtime = await admin.create_showtime(body, ctx)
    response.headers["Location"] = f"/api/showtimes/{showtime.id}"
    return showtime


@router.patch(
    "/showtimes/{showtime_id}",
    response_model=ShowtimeRecord,
    summary="Update a showtime",
    response_description="The updated showtime",
    responses=error_responses(
        ApiErrorCode.VALIDATION,
        ApiErrorCode.NOT_FOUND,
        ApiErrorCode.LAYOUT_CONFLICT,
        ApiErrorCode.SHOWTIME_HAS_BOOKINGS,
    ),
)
async def update_showtime(showtime_id: ShowtimeIdPath, body: ShowtimeUpdate, ctx: AdminContextDep):
    """An edited showtime becomes `manual`, so the daily refresh leaves it alone. Started showtimes can't be
    edited, booked seats must stay inside the layout, and the movie can't change once there are bookings."""
    return await admin.update_showtime(showtime_id, body, ctx)


@router.delete(
    "/showtimes/{showtime_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a showtime",
    response_description="Deleted",
    responses=error_responses(ApiErrorCode.NOT_FOUND, ApiErrorCode.SHOWTIME_HAS_BOOKINGS),
)
async def delete_showtime(showtime_id: ShowtimeIdPath, ctx: AdminContextDep) -> None:
    """Only possible without bookings, cancelled ones included. The daily refresh won't bring it back."""
    await admin.delete_showtime(showtime_id, ctx)


@router.get(
    "/bookings",
    response_model=Page[BookingDetails],
    response_model_exclude_none=True,
    summary="List all bookings",
    response_description="One page, newest first",
)
async def list_bookings(
    filters: Annotated[AdminBookingFilters, Query()],
    page: PaginationDep,
    repos: ReposDep,
):
    """Every user's bookings in a page envelope, 20 per page by default."""
    return await admin.list_bookings(
        repos, filters, limit=page.limit_or(DEFAULT_PAGE_SIZE), offset=page.offset
    )


@router.get(
    "/settings",
    response_model=DemoSettings,
    summary="Get the demo settings",
    response_description="The current demo settings",
)
async def get_demo_settings(request: Request):
    """The latency and failure rate of the simulation."""
    return request.app.state.demo_settings


@router.put(
    "/settings",
    response_model=DemoSettings,
    summary="Set the demo settings",
    response_description="The new demo settings",
)
async def replace_demo_settings(body: DemoSettings, request: Request):
    """Applies from the next request on. Admin routes are never simulated, so you can always undo it."""
    request.app.state.demo_settings = body
    return body


@router.post(
    "/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reset the demo data",
    response_description="Reset",
)
async def reset_demo(ctx: AdminContextDep, install: InstallSeedDep, config: ConfigDep) -> None:
    """**Deletes all bookings, admin changes, emails and uploads**, then seeds again. Keeps demo settings."""
    await admin.reset_demo(ctx, install, config.uploads_dir)


@router.post(
    "/sample-bookings",
    response_model=SampleBookingsResult,
    summary="Add sample bookings",
    response_description="How many were added",
    responses=NOT_FOUND_RESPONSE,
)
async def add_sample_bookings(
    user_id: Annotated[str, Query(alias="userId", description="The user who gets them", examples=["u-ava"])],
    ctx: AdminContextDep,
):
    """Up to 6 bookings for the user: upcoming, cancelled and past ones."""
    return SampleBookingsResult(added=await admin.add_sample_bookings(user_id, ctx))


@router.get(
    "/outbox",
    response_model=list[OutboxEmail],
    summary="List the sent emails",
    response_description="The emails, newest first",
)
async def list_outbox(repos: ReposDep):
    """The "sent" confirmation and cancellation emails, newest first. Nothing is really sent."""
    return await notifications.list_outbox(repos)

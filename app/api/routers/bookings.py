from typing import Annotated

from fastapi import APIRouter, Body, Query, Response, status

from app.api.deps import BookingContextDep, ReposDep
from app.core.errors import ApiErrorCode, AppError
from app.schemas.booking import BookingDetails, BookingStatus, CreateBookingRequest
from app.services import bookings

router = APIRouter(prefix="/api/bookings", tags=["bookings"])

# `userId` is optional on purpose: the fake backend answers a missing one with 400 BAD_REQUEST,
# not with the 400 VALIDATION that a required parameter would give (plan 3.3).
UserIdQuery = Annotated[str | None, Query(alias="userId", description="The signed-in mock user")]


def require_user_id(user_id: str | None, message: str) -> str:
    if not user_id:
        raise AppError(400, ApiErrorCode.BAD_REQUEST, message)
    return user_id


@router.get("", response_model=list[BookingDetails], response_model_exclude_none=True)
async def list_bookings(repos: ReposDep, user_id: UserIdQuery = None, status: BookingStatus | None = None):
    """The user's bookings, newest first. An unknown user simply has none."""
    user_id = require_user_id(user_id, "The userId query parameter is required")
    return await bookings.list_bookings(repos, user_id, status)


@router.get("/{booking_id}", response_model=BookingDetails, response_model_exclude_none=True)
async def get_booking(booking_id: str, repos: ReposDep, user_id: UserIdQuery = None):
    user_id = require_user_id(user_id, "The userId query parameter is required")
    return await bookings.get_booking(repos, booking_id, user_id)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=BookingDetails,
    response_model_exclude_none=True,
)
async def create_booking(request: CreateBookingRequest, ctx: BookingContextDep, response: Response):
    booking = await bookings.create_booking(request, ctx)
    response.headers["Location"] = f"/api/bookings/{booking.id}"
    return booking


@router.patch("/{booking_id}/cancel", response_model=BookingDetails, response_model_exclude_none=True)
async def cancel_booking(
    booking_id: str,
    ctx: BookingContextDep,
    # Body(embed=True): the body is `{"userId": "..."}`, not a bare string.
    user_id: Annotated[str | None, Body(embed=True, alias="userId")] = None,
):
    user_id = require_user_id(user_id, "The userId is required")
    return await bookings.cancel_booking(booking_id, user_id, ctx)

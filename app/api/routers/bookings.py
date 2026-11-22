from typing import Annotated

from fastapi import APIRouter, Body, Query, Response, status
from fastapi.openapi.models import Example

from app.api.deps import BookingContextDep, BookingIdPath, ReposDep
from app.core.errors import ApiErrorCode, AppError, error_responses
from app.schemas.booking import EXAMPLE_SHOWTIME_ID, BookingDetails, BookingStatus, CreateBookingRequest
from app.services import bookings

router = APIRouter(prefix="/api/bookings", tags=["bookings"])

# `userId` is optional on purpose: the fake backend answers a missing one with 400 BAD_REQUEST,
# not with the 400 VALIDATION that a required parameter would give (plan 3.3).
# `x-required`: optional to FastAPI, required by the contract, and so required in the OpenAPI schema.
UserIdQuery = Annotated[
    str | None,
    Query(
        alias="userId",
        description="The signed-in mock user. Missing or empty is 400 `BAD_REQUEST`",
        examples=["u-ava"],
        json_schema_extra={"x-required": True},
    ),
]

BOOKING = {
    "userId": "u-ava",
    "movieId": "m-deep-field",
    "showtimeId": EXAMPLE_SHOWTIME_ID,
    "customerName": "Ava Patel",
    "email": "ava.patel@example.com",
}
BOOKING_EXAMPLES: dict[str, Example] = {
    "two-seats": {
        "summary": "Two seats",
        "description": "Replace `showtimeId` with one from "
        "`GET /api/movies/m-deep-field/showtimes?upcomingOnly=true`, and pick seats that aren't booked.",
        "value": BOOKING | {"seats": ["A1", "A2"]},
    },
    "six-seats": {
        "summary": "The most one booking can have",
        "value": BOOKING | {"seats": ["B1", "B2", "B3", "B4", "B5", "B6"]},
    },
}


def require_user_id(user_id: str | None, message: str) -> str:
    if not user_id:
        raise AppError(400, ApiErrorCode.BAD_REQUEST, message)
    return user_id


@router.get(
    "",
    response_model=list[BookingDetails],
    response_model_exclude_none=True,
    summary="List a user's bookings",
    response_description="The bookings, newest first",
    responses=error_responses(ApiErrorCode.BAD_REQUEST),
)
async def list_bookings(
    repos: ReposDep,
    user_id: UserIdQuery = None,
    status: Annotated[BookingStatus | None, Query(description="Only bookings with this status")] = None,
):
    """The user's bookings, newest first. An unknown user simply has none."""
    user_id = require_user_id(user_id, "The userId query parameter is required")
    return await bookings.list_bookings(repos, user_id, status)


@router.get(
    "/{booking_id}",
    response_model=BookingDetails,
    response_model_exclude_none=True,
    summary="Get a booking",
    response_description="The booking",
    responses=error_responses(ApiErrorCode.BAD_REQUEST, ApiErrorCode.NOT_FOUND),
)
async def get_booking(booking_id: BookingIdPath, repos: ReposDep, user_id: UserIdQuery = None):
    """Another user's booking is a 404, so that ids can't be probed."""
    user_id = require_user_id(user_id, "The userId query parameter is required")
    return await bookings.get_booking(repos, booking_id, user_id)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=BookingDetails,
    response_model_exclude_none=True,
    summary="Book seats",
    response_description="The booking",
    responses={
        201: {"headers": {"Location": {"description": "The booking's URL", "schema": {"type": "string"}}}},
        **error_responses(
            ApiErrorCode.VALIDATION,
            ApiErrorCode.NOT_FOUND,
            ApiErrorCode.SHOWTIME_STARTED,
            ApiErrorCode.SEATS_TAKEN,
        ),
    },
)
async def create_booking(
    request: Annotated[CreateBookingRequest, Body(openapi_examples=BOOKING_EXAMPLES)],
    ctx: BookingContextDep,
    response: Response,
):
    """Books 1 to 6 seats and sends a confirmation email to the outbox. The checks run in this order:

    1. the shape of the body, the seat count, the name and the email: 400 `VALIDATION`
    2. the user exists: 400 `VALIDATION` ("Unknown user")
    3. the showtime exists and belongs to the movie: 404 `NOT_FOUND`
    4. it hasn't started: 409 `SHOWTIME_STARTED`
    5. the seats fit the layout: 400 `VALIDATION` with `details.invalidSeats`
    6. the seats are free: 409 `SEATS_TAKEN` with `details.takenSeats`
    """
    booking = await bookings.create_booking(request, ctx)
    response.headers["Location"] = f"/api/bookings/{booking.id}"
    return booking


@router.patch(
    "/{booking_id}/cancel",
    response_model=BookingDetails,
    response_model_exclude_none=True,
    summary="Cancel a booking",
    response_description="The cancelled booking",
    responses=error_responses(
        ApiErrorCode.BAD_REQUEST,
        ApiErrorCode.NOT_FOUND,
        ApiErrorCode.ALREADY_CANCELLED,
        ApiErrorCode.SHOWTIME_STARTED,
    ),
    openapi_extra={"requestBody": {"required": True}},
)
async def cancel_booking(
    booking_id: BookingIdPath,
    ctx: BookingContextDep,
    # Body(embed=True): the body is `{"userId": "..."}`, not a bare string.
    user_id: Annotated[
        str | None,
        Body(embed=True, alias="userId", examples=["u-ava"], json_schema_extra={"x-required": True}),
    ] = None,
):
    """Frees the seats and sends a cancellation email. Not possible once the showtime has started."""
    user_id = require_user_id(user_id, "The userId is required")
    return await bookings.cancel_booking(booking_id, user_id, ctx)

from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import EmailStr, ValidatorFunctionWrapHandler, WrapValidator, field_validator, model_validator
from pydantic_core import ValidationError

from app.core.errors import business_rule
from app.domain.rules import MAX_SEATS_PER_BOOKING
from app.schemas.base import CamelModel
from app.schemas.movie import Genre
from app.schemas.types import Money, UtcDatetime


class BookingStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class Booking(CamelModel):
    id: str
    user_id: str
    movie_id: str
    showtime_id: str
    seats: list[str]
    total_price: Money
    customer_name: str
    email: str
    created_at: UtcDatetime
    status: BookingStatus
    cancelled_at: UtcDatetime | None = None


class MovieSummary(CamelModel):
    id: str
    title: str
    poster_url: str
    duration_mins: int
    genres: list[Genre]


class ShowtimeSummary(CamelModel):
    id: str
    starts_at: UtcDatetime
    screen: str
    price: Money


class BookingDetails(Booking):
    """What the bookings API returns: the stored booking joined with its movie and showtime."""

    movie: MovieSummary
    showtime: ShowtimeSummary


def _email_with_contract_message(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    """Wraps `EmailStr`: trims first, and replaces Pydantic's message with the fake backend's one."""
    if isinstance(value, str):
        value = value.strip()
    try:
        return handler(value)
    except ValidationError:
        raise business_rule("A valid email is required") from None


ContractEmail = Annotated[EmailStr, WrapValidator(_email_with_contract_message)]


class CreateBookingRequest(CamelModel):
    """Body of `POST /api/bookings`. Checks that need data (user, showtime, layout) live in the service."""

    user_id: str
    movie_id: str
    showtime_id: str
    seats: list[str]
    customer_name: str
    email: ContractEmail

    @field_validator("seats")
    @classmethod
    def seats_count_and_unique(cls, seats: list[str]) -> list[str]:
        if not 1 <= len(seats) <= MAX_SEATS_PER_BOOKING:
            raise business_rule(f"Select between 1 and {MAX_SEATS_PER_BOOKING} seats")
        if len(set(seats)) != len(seats):
            raise business_rule("Seats must not repeat")
        return seats

    @field_validator("customer_name", mode="before")
    @classmethod
    def trim_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("customer_name")
    @classmethod
    def name_long_enough(cls, value: str) -> str:
        if len(value) < 2:
            raise business_rule("A customer name of at least 2 characters is required")
        return value


class AdminBookingFilters(CamelModel):
    """Query parameter model of `GET /api/admin/bookings`. Both date bounds are inclusive."""

    status: BookingStatus | None = None
    movie_id: str | None = None
    user_id: str | None = None
    created_from: UtcDatetime | None = None
    created_to: UtcDatetime | None = None

    @model_validator(mode="after")
    def range_in_order(self) -> Self:
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise business_rule("createdFrom must not be after createdTo")
        return self

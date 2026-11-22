from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    ConfigDict,
    EmailStr,
    Field,
    ValidatorFunctionWrapHandler,
    WrapValidator,
    field_validator,
    model_validator,
)
from pydantic.config import JsonDict
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import ValidationError

from app.core.errors import business_rule
from app.domain.rules import MAX_SEATS_PER_BOOKING
from app.schemas.base import CamelModel, RequestModel
from app.schemas.movie import Genre
from app.schemas.types import Money, UtcDatetime

EXAMPLE_SHOWTIME_ID = "st-m-deep-field-20351201-1845"

BOOKING_EXAMPLE: JsonDict = {
    "id": "BK-7Q2M4X",
    "userId": "u-ava",
    "movieId": "m-deep-field",
    "showtimeId": EXAMPLE_SHOWTIME_ID,
    "seats": ["A1", "A2"],
    "totalPrice": 27.0,
    "customerName": "Ava Patel",
    "email": "ava.patel@example.com",
    "createdAt": "2035-11-30T09:12:44.120Z",
    "status": "CONFIRMED",
    "movie": {
        "id": "m-deep-field",
        "title": "Deep Field",
        "posterUrl": "/api/posters/m-deep-field.svg",
        "durationMins": 147,
        "genres": ["Sci-Fi", "Adventure"],
    },
    "showtime": {
        "id": EXAMPLE_SHOWTIME_ID,
        "startsAt": "2035-12-01T18:45:00.000Z",
        "screen": "Screen 2",
        "price": 13.5,
    },
}


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
    cancelled_at: UtcDatetime | SkipJsonSchema[None] = None  # left out until cancelled, never null


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

    model_config = ConfigDict(json_schema_extra={"examples": [BOOKING_EXAMPLE]})

    movie: MovieSummary
    showtime: ShowtimeSummary


# What JavaScript's `trim()` removes. Python's `strip()` differs: it keeps U+FEFF and removes U+001C-U+001F.
JS_WHITESPACE = (
    "".join(
        map(
            chr, (9, 10, 11, 12, 13, 32, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F)
        )
    )
    + chr(0x3000)
    + chr(0xFEFF)
)


def js_trim(value: Any) -> Any:
    return value.strip(JS_WHITESPACE) if isinstance(value, str) else value


def js_length(value: str) -> int:
    """JavaScript's `.length` counts UTF-16 code units, so most emoji count twice."""
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def _email_with_contract_message(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    """Wraps `EmailStr`: trims first, and replaces Pydantic's message with the fake backend's one."""
    value = js_trim(value)
    # `EmailStr` also takes "Ava <ava@example.com>" and keeps only the address. The contract says invalid.
    if isinstance(value, str) and ("<" in value or ">" in value):
        raise business_rule("A valid email is required")
    try:
        return handler(value)
    except ValidationError:
        raise business_rule("A valid email is required") from None


ContractEmail = Annotated[EmailStr, WrapValidator(_email_with_contract_message)]


class CreateBookingRequest(RequestModel):
    """Body of `POST /api/bookings`. The name and the email are trimmed."""

    # The rules below are validators with the contract's messages, so their schema is spelled out by hand.
    user_id: str = Field(examples=["u-ava"])
    movie_id: str = Field(examples=["m-deep-field"])
    showtime_id: str = Field(description="An upcoming showtime of that movie", examples=[EXAMPLE_SHOWTIME_ID])
    seats: list[str] = Field(
        description="1 to 6 different seat ids such as `A1` or `C12` (no leading zeros)",
        examples=[["A1", "A2"]],
        json_schema_extra={"minItems": 1, "maxItems": MAX_SEATS_PER_BOOKING, "uniqueItems": True},
    )
    customer_name: str = Field(
        description="At least 2 characters after trimming",
        examples=["Ava Patel"],
        json_schema_extra={"minLength": 2},
    )
    email: ContractEmail = Field(
        description="An address only, without a display name", examples=["ava.patel@example.com"]
    )

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
        return js_trim(value)

    @field_validator("customer_name")
    @classmethod
    def name_long_enough(cls, value: str) -> str:
        if js_length(value) < 2:
            raise business_rule("A customer name of at least 2 characters is required")
        return value


class AdminBookingFilters(CamelModel):
    """Query parameter model of `GET /api/admin/bookings`. Both date bounds are inclusive."""

    status: BookingStatus | None = Field(None, description="Only bookings with this status")
    movie_id: str | None = Field(None, description="Only bookings of this movie")
    user_id: str | None = Field(None, description="Only this user's bookings")
    created_from: UtcDatetime | None = Field(None, description="Created at or after, with a time zone")
    created_to: UtcDatetime | None = Field(None, description="Created at or before, with a time zone")

    @model_validator(mode="after")
    def range_in_order(self) -> Self:
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise business_rule("createdFrom must not be after createdTo")
        return self

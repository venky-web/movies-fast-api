from typing import Annotated, Literal

from pydantic import ConfigDict, Field, computed_field
from pydantic.config import JsonDict

from app.schemas.base import CamelModel
from app.schemas.patch import PatchModel
from app.schemas.types import Money, Price, Text, UtcDatetime


class Showtime(CamelModel):
    """Public showtime, same shape as the TypeScript `Showtime` plus `seatsRemaining`."""

    id: str
    movie_id: str
    starts_at: UtcDatetime
    screen: str
    price: Money
    rows: int = Field(ge=1, le=26)
    seats_per_row: int = Field(ge=1, le=30)
    booked_seats: list[str]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def seats_remaining(self) -> int:
        return self.rows * self.seats_per_row - len(self.booked_seats)


class ShowtimeRecord(Showtime):
    """Stored showtime. `manual` showtimes are never touched by the daily window refresh."""

    source: Literal["generated", "manual"] = "generated"


Rows = Annotated[int, Field(ge=1, le=26)]  # rows are lettered A-Z
SeatsPerRow = Annotated[int, Field(ge=1, le=30)]

SHOWTIME_EXAMPLE: JsonDict = {
    "movieId": "m-deep-field",
    "startsAt": "2027-01-15T19:00:00Z",
    "screen": "Screen 2",
    "price": 12.5,
    "rows": 8,
    "seatsPerRow": 10,
}


class ShowtimeCreate(CamelModel):
    """Shape and ranges only. "Not in the past" needs the clock, so it is checked in the service."""

    model_config = ConfigDict(json_schema_extra={"examples": [SHOWTIME_EXAMPLE]})

    movie_id: str
    starts_at: UtcDatetime
    screen: Text
    price: Price
    rows: Rows
    seats_per_row: SeatsPerRow


class ShowtimeUpdate(PatchModel):
    movie_id: str | None = None
    starts_at: UtcDatetime | None = None
    screen: Text | None = None
    price: Price | None = None
    rows: Rows | None = None
    seats_per_row: SeatsPerRow | None = None

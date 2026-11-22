from typing import Annotated, Literal

from pydantic import ConfigDict, Field, computed_field
from pydantic.config import JsonDict
from pydantic.json_schema import SkipJsonSchema

from app.schemas.base import CamelModel, RequestModel
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
    """A showtime as admins see it. The daily refresh never touches `manual` showtimes."""

    source: Literal["generated", "manual"] = "generated"


Rows = Annotated[int, Field(ge=1, le=26)]  # rows are lettered A-Z
SeatsPerRow = Annotated[int, Field(ge=1, le=30)]

SHOWTIME_EXAMPLE: JsonDict = {
    "movieId": "m-deep-field",
    "startsAt": "2035-06-01T19:00:00Z",
    "screen": "Screen 2",
    "price": 12.5,
    "rows": 8,
    "seatsPerRow": 10,
}


class ShowtimeCreate(RequestModel):
    """A new showtime. `startsAt` must be in the future."""

    model_config = ConfigDict(json_schema_extra={"examples": [SHOWTIME_EXAMPLE]})

    movie_id: str
    starts_at: UtcDatetime
    screen: Text
    price: Price
    rows: Rows
    seats_per_row: SeatsPerRow


class ShowtimeUpdate(PatchModel):
    """Send only the fields to change. A field can be left out, but not set to null."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"price": 11.5, "screen": "Screen 4"}]})

    movie_id: str | SkipJsonSchema[None] = None
    starts_at: UtcDatetime | SkipJsonSchema[None] = None
    screen: Text | SkipJsonSchema[None] = None
    price: Price | SkipJsonSchema[None] = None
    rows: Rows | SkipJsonSchema[None] = None
    seats_per_row: SeatsPerRow | SkipJsonSchema[None] = None

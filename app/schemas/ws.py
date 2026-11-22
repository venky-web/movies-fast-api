"""WebSocket messages of the seat map (plan 3.6), discriminated on `type`."""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from app.core.errors import ApiErrorCode
from app.domain.rules import MAX_SEATS_PER_BOOKING
from app.schemas.base import CamelModel

# Server -> client


class Snapshot(CamelModel):
    type: Literal["snapshot"] = "snapshot"
    showtime_id: str
    rows: int
    seats_per_row: int
    booked_seats: list[str]
    held_seats: list[str] = Field(default_factory=list)  # filled by the seat-hold stretch goal


class SeatsBooked(CamelModel):
    type: Literal["seats-booked"] = "seats-booked"
    seats: list[str]


class SeatsReleased(CamelModel):
    type: Literal["seats-released"] = "seats-released"
    seats: list[str]


class WsError(CamelModel):
    type: Literal["error"] = "error"
    code: ApiErrorCode
    message: str


ServerMessage = Annotated[Snapshot | SeatsBooked | SeatsReleased | WsError, Field(discriminator="type")]
server_message: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)

# Client -> server. Validated, but holds themselves are a stretch goal: the hub answers with an error.

HoldList = Annotated[list[str], Field(min_length=1, max_length=MAX_SEATS_PER_BOOKING)]


class HoldSeats(CamelModel):
    type: Literal["hold"]
    seats: HoldList


class ReleaseSeats(CamelModel):
    type: Literal["release"]
    seats: HoldList


ClientMessage = Annotated[HoldSeats | ReleaseSeats, Field(discriminator="type")]
client_message: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)

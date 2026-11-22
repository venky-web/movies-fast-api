"""Events on the in-process bus (plan 5.5)."""

from app.schemas.base import CamelModel
from app.schemas.booking import BookingDetails


class BookingCreated(CamelModel):
    booking: BookingDetails


class BookingCancelled(CamelModel):
    booking: BookingDetails


class ShowtimeChanged(CamelModel):
    showtime_id: str


class ShowtimeDeleted(CamelModel):
    """The showtime can no longer be seen: it was deleted, hidden with its movie, or left the window."""

    showtime_id: str


class DemoReset(CamelModel):
    """The data changed wholesale (reset, sample bookings), so clients should reload."""


Event = BookingCreated | BookingCancelled | ShowtimeChanged | ShowtimeDeleted | DemoReset

"""Business rules, ported from `<mfe>/models/booking-rules.ts`.

`isValidEmail` is not ported: the API validates emails with Pydantic's `EmailStr` (stricter, see plan 3.8).
"""

import random
import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.schemas.types import round_money

MAX_SEATS_PER_BOOKING = 6
SEAT_HOLD_MS = 5 * 60 * 1000
ROW_LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
BOOKING_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# Exactly one spelling per seat, because "already booked" compares strings. `\d` would also accept non-ASCII
# digits, `$` a trailing newline, and a leading zero would make "A02" a second name for "A2".
SEAT_PATTERN = re.compile(r"([A-Z])([1-9][0-9]?)")


def seat_id(row_index: int, seat_number: int) -> str:
    """`seat_id(0, 1)` returns `"A1"`. Rows are zero-based, seat numbers one-based."""
    return f"{ROW_LABELS[row_index]}{seat_number}"


def seat_grid(rows: int, seats_per_row: int) -> list[list[str]]:
    """All seat ids of a layout, row by row."""
    return [[seat_id(row, seat + 1) for seat in range(seats_per_row)] for row in range(rows)]


def is_valid_seat(rows: int, seats_per_row: int, seat: str) -> bool:
    match = SEAT_PATTERN.fullmatch(seat)
    if not match:
        return False
    row = ROW_LABELS.index(match[1])
    number = int(match[2])
    return row < rows and 1 <= number <= seats_per_row


def total_seats(rows: int, seats_per_row: int) -> int:
    return rows * seats_per_row


def has_started(starts_at: datetime, now: datetime) -> bool:
    """The start time itself counts as started."""
    return starts_at <= now


def calculate_total(price_per_seat: Decimal, seat_count: int) -> Decimal:
    return round_money(price_per_seat * seat_count)


def seat_sort_key(seat: str) -> tuple[str, int]:
    """Row letter, then seat number: `A2, A10, B1` (port of `compareSeats`)."""
    return seat[0], int(seat[1:])


def sort_seats(seats: list[str]) -> list[str]:
    return sorted(seats, key=seat_sort_key)


def random_booking_id(rng: random.Random) -> str:
    return "BK-" + "".join(rng.choice(BOOKING_ID_ALPHABET) for _ in range(6))


class CancelBlockReason(StrEnum):
    ALREADY_CANCELLED = "ALREADY_CANCELLED"
    SHOWTIME_STARTED = "SHOWTIME_STARTED"


def check_cancellable(status: str, starts_at: datetime, now: datetime) -> CancelBlockReason | None:
    """None if the booking can be cancelled: it is confirmed and its showtime has not started."""
    if status == "CANCELLED":
        return CancelBlockReason.ALREADY_CANCELLED
    if has_started(starts_at, now):
        return CancelBlockReason.SHOWTIME_STARTED
    return None

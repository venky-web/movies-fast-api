"""Port of `<mfe>/models/booking-rules.spec.ts`."""

from datetime import UTC, datetime
from decimal import Decimal

from app.domain.rules import (
    CancelBlockReason,
    calculate_total,
    check_cancellable,
    has_started,
    is_valid_seat,
    seat_grid,
    seat_id,
    sort_seats,
)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def test_builds_seat_ids_row_by_row():
    assert seat_id(0, 1) == "A1"
    assert seat_grid(3, 4) == [
        ["A1", "A2", "A3", "A4"],
        ["B1", "B2", "B3", "B4"],
        ["C1", "C2", "C3", "C4"],
    ]


def test_validates_seats_against_the_layout():
    assert is_valid_seat(3, 4, "C4")
    assert not is_valid_seat(3, 4, "D1")
    assert not is_valid_seat(3, 4, "A5")
    assert not is_valid_seat(3, 4, "A0")
    assert not is_valid_seat(3, 4, "nonsense")
    assert not is_valid_seat(3, 4, "a1")
    assert not is_valid_seat(3, 4, "A100")


def test_treats_the_start_time_itself_as_started():
    assert has_started(NOW, NOW)
    assert not has_started(datetime(2026, 11, 19, 12, 0, 1, tzinfo=UTC), NOW)


def test_rounds_totals_to_cents():
    assert calculate_total(Decimal("9.5"), 3) == Decimal("28.50")
    assert calculate_total(Decimal("13.33"), 3) == Decimal("39.99")
    assert calculate_total(Decimal("0.335"), 1) == Decimal("0.34")  # half up, unlike float round()


def test_sorts_seats_by_row_then_number():
    assert sort_seats(["B1", "A10", "A2"]) == ["A2", "A10", "B1"]


def test_allows_cancelling_only_confirmed_bookings_whose_showtime_has_not_started():
    future = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
    past = datetime(2026, 11, 18, 18, 0, tzinfo=UTC)

    assert check_cancellable("CONFIRMED", future, NOW) is None
    assert check_cancellable("CONFIRMED", past, NOW) == CancelBlockReason.SHOWTIME_STARTED
    assert check_cancellable("CANCELLED", future, NOW) == CancelBlockReason.ALREADY_CANCELLED

"""Regressions found while preparing Phase 4: seat aliases, and bad input that crashed instead of a 400."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from app.schemas.types import Money, UtcDatetime
from tests.conftest import TEST_NOW
from tests.helpers import booking_request, showtimes

JSON = {"content-type": "application/json"}


class Typed(BaseModel):
    at: UtcDatetime
    price: Money


@pytest.mark.parametrize(
    "alias",
    [
        pytest.param("A02", id="leading-zero"),
        pytest.param("A2\n", id="trailing-newline"),
        pytest.param("A٢", id="non-ascii-digit"),
    ],
)
def test_a_booked_seat_cannot_be_sold_again_under_another_spelling(app, client, alias):
    showtime = next(s for s in showtimes(app) if s.starts_at > TEST_NOW and "A2" in s.booked_seats)

    response = client.post("/api/bookings", json=booking_request(showtime, [alias]))

    assert response.status_code == 400
    assert response.json()["details"]["invalidSeats"] == [alias]


@pytest.mark.parametrize(
    ("content", "headers"),
    [
        pytest.param(b"\xff\xfe", {"content-type": "application/octet-stream"}, id="not-utf8"),
        pytest.param(b'{"seats": 1e400}', JSON, id="infinite-number"),
        pytest.param(b'{"seats": "\\ud800"}', JSON, id="lone-surrogate"),
    ],
)
def test_unserialisable_bad_input_is_still_a_400(app, content, headers):
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/bookings", content=content, headers=headers)

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"at": "9999-12-31T23:59:00-05:00"}, id="datetime-overflow"),
        pytest.param({"price": Decimal("1e30")}, id="money-overflow"),
    ],
)
def test_out_of_range_values_are_validation_errors(overrides):
    with pytest.raises(ValidationError):
        Typed(**({"at": "2026-11-19T12:00:00Z", "price": 1} | overrides))

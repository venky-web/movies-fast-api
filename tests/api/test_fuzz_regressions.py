"""Found by fuzzing every operation in Phase 8. Each of these was a 500, some left broken data behind."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.movie import MOVIE_EXAMPLE
from tests.conftest import ADMIN_KEY, TEST_NOW
from tests.helpers import booking_request, first_future, free_seats, showtimes, stored_showtime
from tests.stored import stored

HEADERS = {"content-type": "application/json", "X-Admin-Key": ADMIN_KEY}
SURROGATE = chr(0xD800)  # valid in a JSON string as an escape, impossible to encode as UTF-8

NEW_SHOWTIME = {
    "movieId": "m-deep-field",
    "startsAt": "2035-06-01T19:00:00Z",
    "screen": "Screen 1",
    "price": 10,
    "rows": 5,
    "seatsPerRow": 8,
}


def send(client, method, url, body):
    # json.dumps escapes the surrogate, which is how a client can send one.
    return client.request(method, url, headers=HEADERS, content=json.dumps(body).encode())


@pytest.fixture
def forgiving(app):
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize("depth", [20, 100, 500, 1000])
def test_deeply_nested_input_is_a_400(forgiving, depth):
    body = b'{"userId":' + b"[" * depth + b"]" * depth + b"}"

    response = forgiving.post("/api/bookings", content=body, headers=HEADERS)

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION"


@pytest.mark.parametrize("field", ["userId", "movieId", "showtimeId", "seats", "customerName", "email"])
def test_a_lone_surrogate_in_a_booking_is_a_400_and_stores_nothing(app, forgiving, field):
    showtime = first_future(showtimes(app), TEST_NOW)
    body = booking_request(showtime, free_seats(showtime)[:1])
    body[field] = [f"A1{SURROGATE}"] if field == "seats" else body[field] + SURROGATE

    response = send(forgiving, "POST", "/api/bookings", body)

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION"
    assert stored(app).bookings() == {}
    assert stored_showtime(app, showtime.id) == showtime
    assert forgiving.get("/api/bookings", params={"userId": "u-ava"}).status_code == 200


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        (
            "PATCH",
            "/api/admin/movies/m-deep-field",
            {"reviews": [{"author": SURROGATE, "stars": 3, "text": "t"}]},
        ),
        (
            "PATCH",
            "/api/admin/movies/m-deep-field",
            {"reviews": [{"author": "a", "stars": 3, "text": SURROGATE}]},
        ),
        ("PATCH", "/api/admin/movies/m-deep-field", {"cast": [f"A{SURROGATE}"]}),
        ("PUT", "/api/admin/movies/m-deep-field", MOVIE_EXAMPLE | {"title": f"T{SURROGATE}"}),
        ("POST", "/api/admin/movies", MOVIE_EXAMPLE | {"id": f"m-{SURROGATE}"}),
        ("POST", "/api/admin/showtimes", NEW_SHOWTIME | {"movieId": f"m{SURROGATE}"}),
        ("POST", "/api/admin/showtimes", NEW_SHOWTIME | {"screen": f"S{SURROGATE}"}),
    ],
    ids=["review-author", "review-text", "cast", "title", "movie-id", "showtime-movie", "screen"],
)
def test_a_lone_surrogate_from_an_admin_cannot_break_the_catalogue(forgiving, method, url, body):
    response = send(forgiving, method, url, body)

    assert response.status_code == 400
    assert forgiving.get("/api/movies").status_code == 200


def test_a_lone_surrogate_in_a_cancel_is_not_a_500(forgiving):
    response = send(forgiving, "PATCH", "/api/bookings/BK-NOPE00/cancel", {"userId": SURROGATE})

    assert response.status_code in (400, 404)


@pytest.mark.parametrize("starts_at", ["9999-12-31T23:59:59Z", "9999-12-31T20:00:00Z"])
def test_a_date_at_the_edge_of_the_calendar_is_a_400_east_of_utc(settings, frozen_clock, starts_at):
    tokyo = settings.model_copy(update={"theater_tz": "Asia/Tokyo"})
    with TestClient(create_app(tokyo, frozen_clock), raise_server_exceptions=False) as client:
        response = send(client, "POST", "/api/admin/showtimes", NEW_SHOWTIME | {"startsAt": starts_at})

    assert response.status_code == 400

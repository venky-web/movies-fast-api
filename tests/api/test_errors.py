"""Every error is an `ApiError` (plan 3.2 / 3.3)."""

import pytest
from fastapi.testclient import TestClient

from app.core.errors import format_location


def assert_api_error(response, status, code):
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert isinstance(body["message"], str) and body["message"]
    assert "detail" not in body
    return body


def test_unknown_route_is_not_found(client):
    body = assert_api_error(client.get("/api/nope"), 404, "NOT_FOUND")

    assert body["message"] == "No endpoint for GET /api/nope"
    assert "details" not in body  # omitted, not null


def test_wrong_method_is_method_not_allowed(client):
    response = client.delete("/api/movies")

    assert_api_error(response, 405, "METHOD_NOT_ALLOWED")
    assert "GET" in response.headers["allow"]


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/api/movies/nope", 'Movie "nope" not found'),
        ("/api/movies/nope/showtimes", 'Movie "nope" not found'),
        ("/api/showtimes/nope", 'Showtime "nope" not found'),
        ("/api/users/nope", 'User "nope" not found'),
        ("/api/posters/nope.svg", 'Movie "nope" not found'),
    ],
)
def test_missing_resources_use_the_fake_backend_wording(client, path, message):
    assert assert_api_error(client.get(path), 404, "NOT_FOUND")["message"] == message


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"limit": 0}, "limit"),
        ({"offset": -1}, "offset"),
        ({"sort": "x"}, "sort"),
        ({"genre": "Western"}, "genre"),
    ],
)
def test_bad_query_values_are_400_validation(client, params, field):
    body = assert_api_error(client.get("/api/movies", params=params), 400, "VALIDATION")

    assert body["message"].startswith(f"{field}")
    assert body["details"]["errors"][0]["loc"][0] == "query"
    assert "url" not in body["details"]["errors"][0]


def test_camel_case_paths_in_validation_messages(client):
    body = assert_api_error(
        client.get("/api/movies/m-last-lighthouse/showtimes", params={"upcomingOnly": "maybe"}),
        400,
        "VALIDATION",
    )

    assert body["message"].startswith("upcomingOnly: ")


def test_format_location_drops_the_prefix_and_indexes_lists():
    assert format_location(("body", "seats", 0)) == "seats[0]"
    assert format_location(("body", "movie", "title")) == "movie.title"
    assert format_location(("query", "userId")) == "userId"


def test_unhandled_exceptions_are_500_internal(app):
    @app.get("/api/boom")
    async def boom():
        raise RuntimeError("kaboom")

    with TestClient(app, raise_server_exceptions=False) as client:
        body = assert_api_error(client.get("/api/boom"), 500, "INTERNAL")

    assert "kaboom" not in body["message"]  # internals are not leaked

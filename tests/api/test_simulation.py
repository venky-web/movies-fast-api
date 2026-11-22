"""Port of the "demo settings" part of the fake backend's spec, plus the plan's exclusions (5.6)."""

import random

import pytest

from app.middleware.simulation import is_simulated
from app.schemas.demo import DemoSettings
from tests.conftest import ADMIN_KEY, TEST_NOW
from tests.helpers import booking_request, first_future, free_seats, showtimes
from tests.stored import stored

ADMIN = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture
def slept(app, client):
    delays = []

    async def record(seconds):
        delays.append(seconds)

    app.state.sleep = record
    return delays


def simulate(app, latency_ms=0, failure_rate=0.0):
    app.state.demo_settings = DemoSettings(latency_ms=latency_ms, failure_rate=failure_rate)


def test_simulates_failures(app, client):
    simulate(app, failure_rate=1)

    response = client.get("/api/movies")

    assert response.status_code == 503
    assert response.json() == {
        "code": "SIMULATED_FAILURE",
        "message": "Simulated server failure (see demo tools)",
    }


def test_adds_the_configured_latency(app, client, slept):
    simulate(app, latency_ms=300)

    assert client.get("/api/movies").status_code == 200
    assert slept == [0.3]


def test_no_latency_means_no_sleep(client, slept):
    client.get("/api/movies")

    assert slept == []


def test_latency_comes_before_the_failure(app, client, slept):
    simulate(app, latency_ms=100, failure_rate=1)

    assert client.get("/api/movies").status_code == 503
    assert slept == [0.1]


def test_a_failure_rate_of_one_half_follows_the_seeded_rng(app, client):
    simulate(app, failure_rate=0.5)
    app.state.rng = random.Random(7)
    expected_rng = random.Random(7)
    expected = [503 if expected_rng.random() < 0.5 else 200 for _ in range(20)]

    assert [client.get("/api/users").status_code for _ in range(20)] == expected
    assert set(expected) == {200, 503}


def test_unknown_api_routes_are_delayed_and_can_fail_too(app, client, slept):
    simulate(app, latency_ms=50)
    assert client.get("/api/unknown").status_code == 404

    simulate(app, latency_ms=50, failure_rate=1)
    assert client.get("/api/unknown").status_code == 503
    assert slept == [0.05, 0.05]


@pytest.mark.parametrize(
    "path", ["/api/admin/settings", "/api/posters/m-deep-field.svg", "/health", "/openapi.json", "/docs"]
)
def test_excluded_paths_are_unaffected(app, client, slept, path):
    simulate(app, latency_ms=500, failure_rate=1)

    assert client.get(path, headers=ADMIN).status_code == 200
    assert slept == []


@pytest.mark.parametrize(
    ("path", "simulated"),
    [
        ("/api/movies", True),
        ("/api/administrators", True),
        ("/api/events-archive", True),
        ("/api/admin", False),
        ("/api/admin/reset", False),
        ("/api/events", False),
        ("/api", False),
        ("/apiary", False),
        ("/uploads/posters/x.png", False),
        ("/ws/showtimes/st-1", False),
    ],
)
def test_which_paths_are_simulated(path, simulated):
    assert is_simulated(path) is simulated


def test_changing_the_settings_applies_immediately(client):
    url = "/api/admin/settings"
    assert client.put(url, json={"latencyMs": 0, "failureRate": 1}, headers=ADMIN).status_code == 200
    assert client.get("/api/movies").status_code == 503

    assert client.put(url, json={"latencyMs": 0, "failureRate": 0}, headers=ADMIN).status_code == 200
    assert client.get("/api/movies").status_code == 200


def test_a_failed_request_changes_nothing(app, client):
    showtime = first_future(showtimes(app), TEST_NOW)
    simulate(app, failure_rate=1)

    response = client.post("/api/bookings", json=booking_request(showtime, free_seats(showtime)[:1]))

    assert response.status_code == 503
    assert stored(app).bookings() == {}
    assert stored(app).outbox() == []

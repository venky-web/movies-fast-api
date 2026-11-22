import asyncio
import random
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import background
from app.background import refresh_showtimes, theater_day
from app.domain.seed import generate_showtimes, load_movies
from app.main import create_app
from tests.conftest import ADMIN_KEY, TEST_NOW
from tests.helpers import booking_request, first_future, first_past, free_seats, showtimes, stored_showtime
from tests.stored import stored

ADMIN = {"X-Admin-Key": ADMIN_KEY}
LONDON = ZoneInfo("Europe/London")


def refresh_tomorrow(app, client, frozen_clock, days=1):
    frozen_clock.advance(days=days)
    client.portal.call(refresh_showtimes, app)
    return stored(app).showtimes()


def days_in(items):
    return sorted({s.starts_at.astimezone(LONDON).date().isoformat() for s in items})


def test_the_window_moves_by_a_day(app, client, frozen_clock):
    after = refresh_tomorrow(app, client, frozen_clock)

    assert days_in(after.values()) == [f"2026-11-{day}" for day in range(18, 27)]


def test_the_same_day_changes_nothing(app, client):
    before = dict(stored(app).showtimes())

    client.portal.call(refresh_showtimes, app)

    assert stored(app).showtimes() == before


def test_booked_seats_survive(app, client, frozen_clock):
    showtime = next(
        s
        for s in showtimes(app)
        if s.starts_at.day == 22 and len(free_seats(s)) >= 2 and s.starts_at > TEST_NOW
    )
    seats = free_seats(showtime)[:2]
    assert client.post("/api/bookings", json=booking_request(showtime, seats)).status_code == 201

    after = refresh_tomorrow(app, client, frozen_clock)

    assert after[showtime.id].booked_seats == [*showtime.booked_seats, *seats]


def test_cancelled_seats_stay_free(app, client, frozen_clock):
    showtime = first_future(showtimes(app), TEST_NOW)
    body = booking_request(showtime, free_seats(showtime)[:1])
    booking = client.post("/api/bookings", json=body).json()
    client.patch(f"/api/bookings/{booking['id']}/cancel", json={"userId": "u-ava"})

    after = refresh_tomorrow(app, client, frozen_clock)

    assert after[showtime.id].booked_seats == showtime.booked_seats


def test_old_showtimes_stay_only_while_a_booking_points_at_them(app, client, frozen_clock):
    showtime = first_future(showtimes(app), TEST_NOW)
    unreferenced = first_past(showtimes(app), TEST_NOW)
    body = booking_request(showtime, free_seats(showtime)[:1])
    booking = client.post("/api/bookings", json=body).json()

    after = refresh_tomorrow(app, client, frozen_clock, days=10)

    assert showtime.id in after
    assert unreferenced.id not in after
    details = client.get(f"/api/bookings/{booking['id']}", params={"userId": "u-ava"}).json()
    assert details["showtime"] == booking["showtime"]


def test_manual_showtimes_survive_even_outside_the_window(app, client, frozen_clock):
    edited = first_future(showtimes(app), TEST_NOW)
    url = f"/api/admin/showtimes/{edited.id}"
    assert client.patch(url, json={"price": 5}, headers=ADMIN).status_code == 200

    after = refresh_tomorrow(app, client, frozen_clock, days=20)

    assert (after[edited.id].source, float(after[edited.id].price)) == ("manual", 5)


def test_deleted_showtimes_stay_deleted(app, client, frozen_clock):
    deleted = next(s for s in showtimes(app) if s.starts_at.day == 23)
    assert client.delete(f"/api/admin/showtimes/{deleted.id}", headers=ADMIN).status_code == 204

    assert deleted.id not in refresh_tomorrow(app, client, frozen_clock)


def test_deleted_movies_get_no_showtimes_and_do_not_shift_the_others(app, client, frozen_clock):
    movies = load_movies()
    deleted = movies[3].id
    assert client.delete(f"/api/admin/movies/{deleted}", headers=ADMIN).status_code == 204

    after = refresh_tomorrow(app, client, frozen_clock)

    full = generate_showtimes(movies, frozen_clock.now(), LONDON)
    assert not [s for s in after.values() if s.movie_id == deleted and s.starts_at.day == 26]
    # Screens depend on the movie's position in the seed list.
    assert {i: s.screen for i, s in after.items() if s.movie_id != deleted} == {
        s.id: s.screen for s in full if s.movie_id != deleted
    }


def test_admin_created_movies_get_no_generated_showtimes(app, client, frozen_clock):
    movie = load_movies()[0].model_dump(mode="json", exclude={"hue", "source"}) | {"id": "m-admin-made"}
    assert client.post("/api/admin/movies", json=movie, headers=ADMIN).status_code == 201

    after = refresh_tomorrow(app, client, frozen_clock)

    assert "m-admin-made" not in {s.movie_id for s in after.values()}


def test_the_theater_decides_when_a_day_starts(app, frozen_clock):
    frozen_clock.set(datetime(2027, 7, 1, 23, 30, tzinfo=UTC))  # 00:30 on 2 July in London

    assert theater_day(app).isoformat() == "2027-07-02"


@pytest.mark.anyio
async def test_a_refresh_does_not_overwrite_a_booking_it_waited_for(app, aclient):
    showtime = first_future(showtimes(app), TEST_NOW)
    seat = free_seats(showtime)[0]
    lock = app.state.locks.get(showtime.id)

    async def queued(count):
        async with asyncio.timeout(5):
            while len(lock._waiters or ()) < count:  # noqa: ASYNC110 (no event to wait on)
                await asyncio.sleep(0.01)

    async with lock:
        booking = asyncio.create_task(aclient.post("/api/bookings", json=booking_request(showtime, [seat])))
        await queued(1)
        refresh = asyncio.create_task(refresh_showtimes(app))
        await queued(2)

    assert (await booking).status_code == 201
    await refresh
    assert seat in stored_showtime(app, showtime.id).booked_seats


class TestLoop:
    @pytest.fixture
    def looping(self, settings, frozen_clock):
        fast = settings.model_copy(update={"showtime_refresh_seconds": 0.01})
        return create_app(fast, frozen_clock, random.Random(1))

    @staticmethod
    def wait_until(condition):
        deadline = time.monotonic() + 5
        while not condition():
            assert time.monotonic() < deadline, "the loop did not get there"
            time.sleep(0.01)

    def test_refreshes_when_the_day_changes(self, looping, frozen_clock):
        with TestClient(looping):
            frozen_clock.advance(days=1)

            self.wait_until(lambda: days_in(list(stored(looping).showtimes().values()))[-1] == "2026-11-26")

    def test_does_not_refresh_within_the_same_day(self, looping, frozen_clock, monkeypatch):
        calls = []

        async def record(app):
            calls.append(app)

        monkeypatch.setattr(background, "refresh_showtimes", record)
        with TestClient(looping):
            frozen_clock.advance(hours=11)
            time.sleep(0.1)

        assert calls == []

    def test_retries_after_a_failure(self, looping, frozen_clock, monkeypatch):
        calls = []

        async def fail_once(app):
            calls.append(app)
            if len(calls) == 1:
                raise RuntimeError("storage hiccup")

        monkeypatch.setattr(background, "refresh_showtimes", fail_once)
        with TestClient(looping):
            frozen_clock.advance(days=1)

            self.wait_until(lambda: len(calls) >= 2)
            time.sleep(0.1)

        assert len(calls) == 2  # retried, then done for that day

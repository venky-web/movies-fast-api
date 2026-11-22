import asyncio

import pytest

from tests.conftest import TEST_NOW
from tests.helpers import booking_request, first_future, free_seats, showtimes, stored_showtime
from tests.stored import stored


class TestBookingRoutes:
    def test_location_header_points_to_the_created_booking(self, app, client):
        showtime = first_future(showtimes(app), TEST_NOW)
        response = client.post("/api/bookings", json=booking_request(showtime, free_seats(showtime)[:1]))

        location = response.headers["Location"]
        assert location == f"/api/bookings/{response.json()['id']}"
        assert client.get(location, params={"userId": "u-ava"}).json() == response.json()

    def test_get_booking_needs_a_user_id(self, client):
        assert client.get("/api/bookings/BK-NOPE").json()["code"] == "BAD_REQUEST"
        assert client.get("/api/bookings/BK-NOPE", params={"userId": "u-ava"}).json()["code"] == "NOT_FOUND"

    def test_list_is_newest_first_and_filters_by_status(self, app, client, frozen_clock):
        showtime = first_future(showtimes(app), TEST_NOW)
        seats = free_seats(showtime)
        first = client.post("/api/bookings", json=booking_request(showtime, seats[:1])).json()
        second = client.post(
            "/api/bookings", json=booking_request(showtime, seats[1:2])
        ).json()  # same createdAt
        frozen_clock.advance(minutes=1)
        third = client.post("/api/bookings", json=booking_request(showtime, seats[2:3])).json()
        client.patch(f"/api/bookings/{first['id']}/cancel", json={"userId": "u-ava"})

        listed = client.get("/api/bookings", params={"userId": "u-ava"}).json()
        cancelled = client.get("/api/bookings", params={"userId": "u-ava", "status": "CANCELLED"}).json()

        assert [b["id"] for b in listed] == [third["id"], second["id"], first["id"]]
        assert [b["id"] for b in cancelled] == [first["id"]]

    def test_total_price_is_a_json_number(self, app, client):
        showtime = first_future(showtimes(app), TEST_NOW)
        response = client.post("/api/bookings", json=booking_request(showtime, free_seats(showtime)[:3]))

        assert '"totalPrice":' + str(float(showtime.price) * 3) in response.text

    def test_snake_case_fields_are_accepted_too(self, app, client):
        showtime = first_future(showtimes(app), TEST_NOW)
        body = {
            "user_id": "u-ava",
            "movie_id": showtime.movie_id,
            "showtime_id": showtime.id,
            "seats": free_seats(showtime)[:1],
            "customer_name": "Ava",
            "email": "ava@example.com",
        }

        assert client.post("/api/bookings", json=body).status_code == 201


async def pause_for_io() -> None:
    """Stands in for real I/O between "check seats" and "write" (plan 5.4)."""
    await asyncio.sleep(0)


@pytest.mark.anyio
class TestConcurrency:
    @pytest.mark.backends("memory")  # in SQL the unique constraint prevents it (tests/api/test_sql.py)
    async def test_race_without_the_lock_double_books(self, app, aclient):
        """The bug the lock fixes: both requests pass the check before either one writes."""
        app.state.locks.enabled = False
        app.state.booking_pause = pause_for_io
        showtime = first_future(showtimes(app), TEST_NOW)
        body = booking_request(showtime, free_seats(showtime)[:2])

        responses = await asyncio.gather(*(aclient.post("/api/bookings", json=body) for _ in range(2)))

        assert [r.status_code for r in responses] == [201, 201]
        assert len(stored(app).bookings()) == 2  # the same seats were sold twice

    async def test_lock_gives_one_201_and_one_409(self, app, aclient):
        app.state.booking_pause = pause_for_io
        showtime = first_future(showtimes(app), TEST_NOW)
        body = booking_request(showtime, free_seats(showtime)[:2])

        responses = await asyncio.gather(*(aclient.post("/api/bookings", json=body) for _ in range(2)))

        assert sorted(r.status_code for r in responses) == [201, 409]
        assert len(stored(app).bookings()) == 1

    async def test_concurrent_cancel_and_create_keep_booked_seats_consistent(self, app, aclient):
        app.state.booking_pause = pause_for_io
        showtime = first_future(showtimes(app), TEST_NOW)
        seats = free_seats(showtime)
        original = set(showtime.booked_seats)
        first = (await aclient.post("/api/bookings", json=booking_request(showtime, seats[:2]))).json()

        cancel, create = await asyncio.gather(
            aclient.patch(f"/api/bookings/{first['id']}/cancel", json={"userId": "u-ava"}),
            aclient.post("/api/bookings", json=booking_request(showtime, seats[2:4])),
        )

        assert (cancel.status_code, create.status_code) == (200, 201)
        assert set(stored_showtime(app, showtime.id).booked_seats) == original | set(seats[2:4])

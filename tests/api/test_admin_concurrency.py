"""Admin writes racing bookings. The test holds the showtime lock to fix the order in which requests run."""

import asyncio

import pytest

from tests.conftest import ADMIN_KEY, TEST_NOW
from tests.helpers import booking_request, first_future, free_seats, showtimes, stored_showtime
from tests.stored import stored

ADMIN = {"X-Admin-Key": ADMIN_KEY}


async def run_in_order(app, showtime_id, first, second):
    """Both requests queue on the showtime lock (which is fair), `first` ahead of `second`."""
    lock = app.state.locks.get(showtime_id)

    async def queued(count):
        async with asyncio.timeout(5):
            while len(lock._waiters or ()) < count:  # noqa: ASYNC110 (no event to wait on)
                await asyncio.sleep(0.01)

    async with lock:
        first_task = asyncio.create_task(first)
        await queued(1)
        second_task = asyncio.create_task(second)
        await queued(2)
    return await asyncio.gather(first_task, second_task)


def empty_showtime(app):
    """A future showtime without booked seats, so its layout can shrink."""
    showtime = first_future(showtimes(app), TEST_NOW).model_copy(update={"booked_seats": []})
    stored(app).put_showtime(showtime)
    return showtime


@pytest.mark.anyio
async def test_booking_that_waited_sees_the_shrunk_layout(app, aclient):
    showtime = empty_showtime(app)

    edit, booking = await run_in_order(
        app,
        showtime.id,
        aclient.patch(f"/api/admin/showtimes/{showtime.id}", json={"rows": 1}, headers=ADMIN),
        aclient.post("/api/bookings", json=booking_request(showtime, ["B1"])),
    )

    assert edit.status_code == 200
    assert booking.status_code == 400, booking.text
    assert stored_showtime(app, showtime.id).booked_seats == []


@pytest.mark.anyio
async def test_booking_that_waited_sees_the_new_movie(app, aclient):
    showtime = empty_showtime(app)
    other = next(m for m in stored(app).movies() if m != showtime.movie_id)

    edit, booking = await run_in_order(
        app,
        showtime.id,
        aclient.patch(f"/api/admin/showtimes/{showtime.id}", json={"movieId": other}, headers=ADMIN),
        aclient.post("/api/bookings", json=booking_request(showtime, ["A1"])),
    )

    assert edit.status_code == 200
    assert booking.status_code == 404, booking.text


@pytest.mark.anyio
async def test_layout_edit_that_waited_sees_the_new_booking(app, aclient):
    showtime = empty_showtime(app)

    booking, edit = await run_in_order(
        app,
        showtime.id,
        aclient.post("/api/bookings", json=booking_request(showtime, ["B1"])),
        aclient.patch(f"/api/admin/showtimes/{showtime.id}", json={"rows": 1}, headers=ADMIN),
    )

    assert booking.status_code == 201
    assert edit.json()["code"] == "LAYOUT_CONFLICT"


@pytest.mark.anyio
async def test_movie_delete_that_waited_sees_the_new_booking(app, aclient):
    showtime = first_future(showtimes(app), TEST_NOW)

    booking, delete = await run_in_order(
        app,
        showtime.id,
        aclient.post("/api/bookings", json=booking_request(showtime, free_seats(showtime)[:1])),
        aclient.delete(f"/api/admin/movies/{showtime.movie_id}", headers=ADMIN),
    )

    assert booking.status_code == 201
    assert delete.json()["code"] == "MOVIE_HAS_BOOKINGS"

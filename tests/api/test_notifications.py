import json
import logging

import pytest

from app.core.logging import RequestIdFilter
from app.repositories.memory import MemoryOutboxRepo
from app.repositories.sql.repos import SqlOutboxRepo
from app.schemas.booking import BookingDetails
from app.schemas.outbox import EmailKind
from app.services.notifications import send_email
from tests.conftest import ADMIN_KEY, TEST_NOW
from tests.helpers import booking_request, first_future, free_seats, showtimes
from tests.stored import stored

ADMIN = {"X-Admin-Key": ADMIN_KEY}


def book(app, client, **kwargs):
    showtime = first_future(showtimes(app), TEST_NOW)
    body = booking_request(showtime, free_seats(showtime)[:2])
    response = client.post("/api/bookings", json=body, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def cancel(client, booking):
    return client.patch(f"/api/bookings/{booking['id']}/cancel", json={"userId": booking["userId"]})


def outbox(client):
    response = client.get("/api/admin/outbox", headers=ADMIN)
    assert response.status_code == 200
    return response.json()


def test_a_booking_adds_a_confirmation_email(app, client):
    booking = book(app, client)

    [email] = outbox(client)  # TestClient runs background tasks before it returns

    assert (email["kind"], email["bookingId"], email["to"]) == (
        "confirmation",
        booking["id"],
        booking["email"],
    )
    assert email["subject"] == f"Booking confirmed: {booking['movie']['title']}"
    assert email["sentAt"] == "2026-11-19T12:00:00.000Z"
    for part in (booking["id"], booking["showtime"]["screen"], ", ".join(booking["seats"])):
        assert part in email["body"]
    assert f"Total: {booking['totalPrice']:.2f}" in email["body"]


def test_a_cancellation_adds_its_email_newest_first(app, client):
    booking = book(app, client)

    cancel(client, booking)  # same frozen time, so "most recently added" breaks the tie

    assert [e["kind"] for e in outbox(client)] == ["cancellation", "confirmation"]


def test_the_show_time_is_in_the_theater_time_zone(app, client):
    showtime = first_future(showtimes(app), TEST_NOW).model_copy(
        update={"starts_at": TEST_NOW.replace(year=2027, month=7, hour=20, minute=15)}  # BST
    )
    stored(app).put_showtime(showtime)
    body = booking_request(showtime, free_seats(showtime)[:1])

    assert client.post("/api/bookings", json=body).status_code == 201

    assert "When: Mon 19 Jul 2027, 20:15" in outbox(client)[0]["body"]


def test_failed_requests_send_nothing(app, client):
    showtime = first_future(showtimes(app), TEST_NOW)
    taken = booking_request(showtime, showtime.booked_seats[:1])

    assert client.post("/api/bookings", json=taken).status_code == 409
    assert client.patch("/api/bookings/BK-NOPE/cancel", json={"userId": "u-ava"}).status_code == 404

    assert outbox(client) == []


def test_cancelling_twice_sends_one_cancellation(app, client):
    booking = book(app, client)

    cancel(client, booking)
    assert cancel(client, booking).status_code == 409

    assert [e["kind"] for e in outbox(client)].count("cancellation") == 1


def test_reset_clears_the_outbox(app, client):
    book(app, client)

    assert client.post("/api/admin/reset", headers=ADMIN).status_code == 204

    assert outbox(client) == []


def test_a_failing_email_is_logged_and_does_not_break_the_booking(app, client, caplog, monkeypatch):
    async def fail(self, email):
        raise RuntimeError("smtp down")

    for repo in (MemoryOutboxRepo, SqlOutboxRepo):
        monkeypatch.setattr(repo, "add", fail)
    caplog.handler.addFilter(RequestIdFilter())

    with caplog.at_level(logging.INFO, logger="app"):
        booking = book(app, client, headers={"X-Request-ID": "trace-email"})

    [record] = [r for r in caplog.records if r.levelname == "ERROR"]
    assert booking["id"] in record.getMessage()
    assert record.request_id == "trace-email"  # background tasks still see the request's context


def test_an_email_for_data_that_was_reset_meanwhile_is_not_sent(app, client, frozen_clock):
    booking = BookingDetails.model_validate(book(app, client))
    assert client.post("/api/admin/reset", headers=ADMIN).status_code == 204

    client.portal.call(
        send_email,
        EmailKind.CONFIRMATION,
        booking,
        app.state.storage.open_repos,
        frozen_clock,
        TEST_NOW.tzinfo,
    )

    assert outbox(client) == []


@pytest.mark.anyio
async def test_the_email_is_written_after_the_response_was_sent(app):
    async with app.router.lifespan_context(app):
        showtime = first_future(showtimes(app), TEST_NOW)
        body = json.dumps(booking_request(showtime, free_seats(showtime)[:1])).encode()
        emails_when_sent = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body" and not message.get("more_body"):
                emails_when_sent.append(len(stored(app).outbox()))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/bookings",
            "raw_path": b"/api/bookings",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        }
        await app(scope, receive, send)

        assert emails_when_sent == [0]
        assert len(stored(app).outbox()) == 1

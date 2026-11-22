"""Port of the booking parts of `<mfe>/fake-backend/fake-backend.interceptor.spec.ts`."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.schemas.booking import Booking, BookingStatus
from tests.conftest import TEST_NOW
from tests.helpers import booking_request, first_future, first_past, free_seats, showtimes, stored_showtime
from tests.stored import stored


def book(client, body):
    response = client.post("/api/bookings", json=body)
    assert response.status_code == 201, response.json()
    return response.json()


def failure(response, status):
    assert response.status_code == status, response.json()
    return response.json()


class TestCreatingBookings:
    def test_saves_the_booking_marks_the_seats_as_booked_and_prices_it(self, app, client):
        showtime = first_future(showtimes(app), TEST_NOW)
        seats = free_seats(showtime)[:2]

        booking = book(client, booking_request(showtime, seats))

        assert booking["id"].startswith("BK-") and len(booking["id"]) == 9
        assert booking["status"] == "CONFIRMED"
        assert booking["totalPrice"] == float(showtime.price) * 2
        assert booking["movie"]["id"] == showtime.movie_id
        assert (
            booking["showtime"]["startsAt"] == client.get(f"/api/showtimes/{showtime.id}").json()["startsAt"]
        )
        assert "cancelledAt" not in booking
        assert list(stored(app).bookings()) == [booking["id"]]
        assert set(seats) <= set(stored_showtime(app, showtime.id).booked_seats)

    def test_re_checks_availability_so_the_same_seats_cannot_be_booked_twice(self, app, client):
        showtime = first_future(showtimes(app), TEST_NOW)
        seats = free_seats(showtime)[:2]
        book(client, booking_request(showtime, seats))

        error = failure(
            client.post("/api/bookings", json=booking_request(showtime, [seats[1], free_seats(showtime)[2]])),
            409,
        )

        assert error["code"] == "SEATS_TAKEN"
        assert error["details"]["takenSeats"] == [seats[1]]
        assert len(stored(app).bookings()) == 1

    def test_rejects_more_than_6_seats_unknown_seats_and_invalid_customer_data(self, app, client):
        showtime = first_future(showtimes(app), TEST_NOW)
        seven = free_seats(showtime)[:7]

        assert (
            failure(client.post("/api/bookings", json=booking_request(showtime, seven)), 400)["code"]
            == "VALIDATION"
        )
        unknown = failure(client.post("/api/bookings", json=booking_request(showtime, ["Z99"])), 400)
        assert unknown["details"]["invalidSeats"] == ["Z99"]
        no_email = booking_request(showtime, seven[:1], email="nope")
        assert failure(client.post("/api/bookings", json=no_email), 400)["code"] == "VALIDATION"

    def test_rejects_a_showtime_that_has_already_started(self, app, client):
        past = first_past(showtimes(app), TEST_NOW)

        error = failure(client.post("/api/bookings", json=booking_request(past, free_seats(past)[:1])), 409)

        assert error["code"] == "SHOWTIME_STARTED"


class TestListingAndCancellingBookings:
    def test_get_bookings_needs_a_user_id_and_returns_only_that_users_bookings(self, app, client):
        showtime = first_future(showtimes(app), TEST_NOW)
        book(client, booking_request(showtime, free_seats(showtime)[:1]))
        book(client, booking_request(showtime, free_seats(showtime)[1:2], userId="u-leo"))

        assert failure(client.get("/api/bookings"), 400)["code"] == "BAD_REQUEST"
        mine = client.get("/api/bookings", params={"userId": "u-ava"}).json()
        assert len(mine) == 1
        assert mine[0]["userId"] == "u-ava"

    def test_cancels_frees_the_seats_and_sets_the_status(self, app, client):
        showtime = first_future(showtimes(app), TEST_NOW)
        seats = free_seats(showtime)[:3]
        booking = book(client, booking_request(showtime, seats))

        cancelled = client.patch(f"/api/bookings/{booking['id']}/cancel", json={"userId": "u-ava"}).json()

        assert cancelled["status"] == "CANCELLED"
        assert cancelled["cancelledAt"] == "2026-11-19T12:00:00.000Z"
        assert not set(seats) & set(stored_showtime(app, showtime.id).booked_seats)
        again = failure(client.patch(f"/api/bookings/{booking['id']}/cancel", json={"userId": "u-ava"}), 409)
        assert again["code"] == "ALREADY_CANCELLED"

    def test_hides_other_users_bookings_behind_a_404(self, app, client):
        showtime = first_future(showtimes(app), TEST_NOW)
        booking = book(client, booking_request(showtime, free_seats(showtime)[:1]))

        failure(client.patch(f"/api/bookings/{booking['id']}/cancel", json={"userId": "u-leo"}), 404)
        failure(client.get(f"/api/bookings/{booking['id']}", params={"userId": "u-leo"}), 404)

    def test_refuses_to_cancel_once_the_showtime_has_started(self, app, client):
        past = first_past(showtimes(app), TEST_NOW)
        past_booking = Booking(
            id="BK-PAST",
            user_id="u-ava",
            movie_id=past.movie_id,
            showtime_id=past.id,
            seats=["A1"],
            total_price=past.price,
            customer_name="Ava",
            email="ava@example.com",
            created_at=datetime(2026, 11, 15, 10, 0, tzinfo=UTC),
            status=BookingStatus.CONFIRMED,
        )
        stored(app).put_booking(past_booking)

        error = failure(client.patch("/api/bookings/BK-PAST/cancel", json={"userId": "u-ava"}), 409)

        assert error["code"] == "SHOWTIME_STARTED"


class TestExtraParityRules:
    """Rules of `parseCreateRequest` / `cancelBooking` that the Angular spec does not cover directly."""

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"seats": ["A1", "A1"]}, "Seats must not repeat"),
            ({"seats": []}, "Select between 1 and 6 seats"),
            ({"customerName": "  A  "}, "A customer name of at least 2 characters is required"),
            ({"email": "nope"}, "A valid email is required"),
            ({"userId": "u-nobody"}, "Unknown user"),
        ],
    )
    def test_validation_messages_match_the_fake_backend(self, app, client, overrides, message):
        showtime = first_future(showtimes(app), TEST_NOW)
        body = booking_request(showtime, free_seats(showtime)[:1]) | overrides

        error = failure(client.post("/api/bookings", json=body), 400)

        assert (error["code"], error["message"]) == ("VALIDATION", message)

    def test_showtime_of_another_movie_is_404(self, app, client):
        showtime = first_future(showtimes(app), TEST_NOW)
        other_movie = next(m for m in stored(app).movies() if m != showtime.movie_id)
        body = booking_request(showtime, free_seats(showtime)[:1], movieId=other_movie)

        assert failure(client.post("/api/bookings", json=body), 404)["message"] == (
            "That showtime does not exist for this movie"
        )

    def test_stores_trimmed_name_and_email_and_sorted_seats(self, app, client):
        showtime = next(
            s
            for s in showtimes(app)
            if s.starts_at > TEST_NOW
            and {"A2", "A10", "B1"}.isdisjoint(s.booked_seats)
            and s.seats_per_row >= 10
        )
        body = booking_request(
            showtime, ["B1", "A10", "A2"], customerName="  Ava  ", email=" ava@example.com "
        )

        booking = book(client, body)

        assert booking["seats"] == ["A2", "A10", "B1"]
        assert (booking["customerName"], booking["email"]) == ("Ava", "ava@example.com")
        assert Decimal(str(booking["totalPrice"])) == showtime.price * 3

    @pytest.mark.parametrize("body", [None, {}, {"userId": ""}])
    def test_cancel_without_user_id_is_bad_request(self, app, client, body):
        showtime = first_future(showtimes(app), TEST_NOW)
        booking = book(client, booking_request(showtime, free_seats(showtime)[:1]))

        error = failure(client.patch(f"/api/bookings/{booking['id']}/cancel", json=body), 400)

        assert error == {"code": "BAD_REQUEST", "message": "The userId is required"}

    def test_cancel_after_the_start_using_the_frozen_clock(self, app, client, frozen_clock):
        showtime = first_future(showtimes(app), TEST_NOW)
        booking = book(client, booking_request(showtime, free_seats(showtime)[:1]))

        frozen_clock.set(showtime.starts_at)

        error = failure(client.patch(f"/api/bookings/{booking['id']}/cancel", json={"userId": "u-ava"}), 409)
        assert error["message"] == "Bookings cannot be cancelled after the showtime starts"

    def test_unknown_user_has_no_bookings(self, client):
        assert client.get("/api/bookings", params={"userId": "u-nobody"}).json() == []


class TestParityFoundByTheDifferentialTest:
    """Differences from the real fake backend, found by sending both the same requests (Phase 8)."""

    @pytest.mark.parametrize("email", ["Ava <ava@example.com>", "<ava@example.com>", "Ava<ava@example.com>"])
    def test_a_display_name_is_not_an_email(self, app, client, email):
        showtime = first_future(showtimes(app), TEST_NOW)
        body = booking_request(showtime, free_seats(showtime)[:1], email=email)

        assert failure(client.post("/api/bookings", json=body), 400)["message"] == "A valid email is required"

    def test_name_length_counts_like_javascript(self, app, client):
        showtime = first_future(showtimes(app), TEST_NOW)
        emoji = chr(0x1F600)  # one code point, but `.length === 2` in JavaScript

        assert book(client, booking_request(showtime, free_seats(showtime)[:1], customerName=emoji))

    def test_trimming_works_like_javascript(self, app, client):
        showtime = first_future(showtimes(app), TEST_NOW)
        name = chr(0xFEFF) + "A"  # JavaScript's trim() removes a byte order mark, Python's strip() doesn't
        body = booking_request(showtime, free_seats(showtime)[:1], customerName=name)

        error = failure(client.post("/api/bookings", json=body), 400)
        assert error["message"] == "A customer name of at least 2 characters is required"

    def test_broken_json_has_a_readable_message(self, client):
        response = client.post(
            "/api/bookings", content=b"{oops", headers={"content-type": "application/json"}
        )

        assert failure(response, 400)["message"] == "Request body: JSON decode error"

    def test_invalid_unicode_has_a_readable_message(self, client):
        body = b'{"customerName": "Ava\\ud800"}'
        response = client.post("/api/bookings", content=body, headers={"content-type": "application/json"})

        messages = [e["msg"] for e in failure(response, 400)["details"]["errors"]]
        assert "Text must be valid Unicode (no lone surrogates)" in messages

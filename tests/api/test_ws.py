"""`WS /ws/showtimes/{id}` (plan 3.6)."""

import asyncio
import json
import time

import anyio
import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.background import refresh_showtimes
from app.realtime import seat_hub
from app.schemas.events import BookingCreated, ShowtimeChanged
from app.schemas.ws import SeatsBooked, server_message
from tests.conftest import ADMIN_KEY, TEST_NOW
from tests.helpers import booking_request, first_future, first_past, free_seats, showtimes, stored_showtime
from tests.stored import stored

ADMIN = {"X-Admin-Key": ADMIN_KEY}


def receive(ws, seconds=5):
    """`ws.receive_json()` with a timeout, so a missing message fails the test instead of hanging it."""

    async def next_message():
        with anyio.fail_after(seconds):
            return await ws._send_rx.receive()

    message = ws.portal.call(next_message)
    ws._raise_on_close(message)
    server_message.validate_json(message["text"])  # every message is one of the declared types
    return json.loads(message["text"])


def closed_with(ws):
    with pytest.raises(WebSocketDisconnect) as closed:
        receive(ws)
    return closed.value.code


def assert_nothing_pending(ws):
    """Messages keep their order, so whatever was queued would arrive before the answer to this."""
    ws.send_text("marker")
    assert receive(ws)["type"] == "error"


def wait_until(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def book(client, showtime, seats):
    response = client.post("/api/bookings", json=booking_request(showtime, seats))
    assert response.status_code == 201, response.text
    return response.json()


def cancel(client, booking):
    response = client.patch(f"/api/bookings/{booking['id']}/cancel", json={"userId": booking["userId"]})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def showtime(app, client):
    return first_future(showtimes(app), TEST_NOW)


@pytest.fixture
def url(showtime):
    return f"/ws/showtimes/{showtime.id}"


class TestConnecting:
    def test_gets_a_snapshot(self, client, showtime, url):
        with client.websocket_connect(url) as ws:
            assert receive(ws) == {
                "type": "snapshot",
                "showtimeId": showtime.id,
                "rows": showtime.rows,
                "seatsPerRow": showtime.seats_per_row,
                "bookedSeats": showtime.booked_seats,
                "heldSeats": [],
            }

    def test_the_snapshot_includes_earlier_bookings(self, client, showtime, url):
        seats = free_seats(showtime)[:2]
        book(client, showtime, seats)

        with client.websocket_connect(url) as ws:
            assert set(seats) <= set(receive(ws)["bookedSeats"])

    def test_unknown_showtime_gets_an_error_and_4404(self, client):
        with client.websocket_connect("/ws/showtimes/nope") as ws:
            assert receive(ws) == {
                "type": "error",
                "code": "NOT_FOUND",
                "message": 'Showtime "nope" not found',
            }
            assert closed_with(ws) == 4404

    def test_a_deleted_movies_showtime_is_unknown(self, app, client):
        past = first_past(showtimes(app), TEST_NOW)
        assert client.delete(f"/api/admin/movies/{past.movie_id}", headers=ADMIN).status_code == 204

        with client.websocket_connect(f"/ws/showtimes/{past.id}") as ws:
            assert receive(ws)["code"] == "NOT_FOUND"
            assert closed_with(ws) == 4404

    def test_disconnecting_unregisters(self, app, client, url):
        with client.websocket_connect(url) as ws:
            receive(ws)
            assert app.state.seat_hub.connection_count == 1

        wait_until(lambda: app.state.seat_hub.connection_count == 0)

    def test_the_simulation_does_not_apply(self, client, showtime, url):
        settings = {"latencyMs": 0, "failureRate": 1}
        assert client.put("/api/admin/settings", json=settings, headers=ADMIN).status_code == 200

        with client.websocket_connect(url) as ws:
            assert receive(ws)["type"] == "snapshot"


class TestLiveSeats:
    def test_two_clients_see_bookings_and_cancellations(self, client, showtime, url):
        seats = free_seats(showtime)[:2]
        with client.websocket_connect(url) as first, client.websocket_connect(url) as second:
            receive(first)
            receive(second)

            booking = book(client, showtime, seats)
            for ws in (first, second):
                assert receive(ws) == {"type": "seats-booked", "seats": booking["seats"]}

            cancel(client, booking)
            for ws in (first, second):
                assert receive(ws) == {"type": "seats-released", "seats": booking["seats"]}

    def test_another_showtime_receives_nothing(self, app, client, showtime, url):
        other = next(s for s in showtimes(app) if s.id != showtime.id)
        with client.websocket_connect(url) as watcher:
            with client.websocket_connect(f"/ws/showtimes/{other.id}") as bystander:
                receive(watcher)
                receive(bystander)

                book(client, showtime, free_seats(showtime)[:1])
                assert receive(watcher)["type"] == "seats-booked"  # so the hub has handled the event

                assert_nothing_pending(bystander)

    def test_a_client_leaving_does_not_affect_the_others(self, app, client, showtime, url):
        with client.websocket_connect(url) as staying:
            receive(staying)
            with client.websocket_connect(url) as leaving:
                receive(leaving)
            wait_until(lambda: app.state.seat_hub.connection_count == 1)

            book(client, showtime, free_seats(showtime)[:1])

            assert receive(staying)["type"] == "seats-booked"

    def test_an_event_already_in_the_snapshot_is_not_repeated(self, app, client, showtime, url):
        booking = book(client, showtime, free_seats(showtime)[:1])
        with client.websocket_connect(url) as ws:
            receive(ws)

            event = BookingCreated.model_validate({"booking": booking})
            client.portal.call(app.state.bus.publish, event)
            client.portal.call(asyncio.sleep, 0.05)

            assert_nothing_pending(ws)

    def test_a_late_cancel_event_cannot_free_a_rebooked_seat(self, app, client, showtime, url):
        seats = free_seats(showtime)[:1]
        held_back = []

        class LatePublisher:
            async def booking_created(self, booking):
                await real.booking_created(booking)

            async def booking_cancelled(self, booking):
                held_back.append(booking)

        real = app.state.publisher
        booking = book(client, showtime, seats)
        with client.websocket_connect(url) as ws:
            receive(ws)
            app.state.publisher = LatePublisher()
            cancel(client, booking)
            book(client, showtime, seats)

            client.portal.call(real.booking_cancelled, held_back[0])  # arrives after the rebooking
            client.portal.call(asyncio.sleep, 0.05)

            assert_nothing_pending(ws)


class TestAdminChanges:
    def test_a_layout_change_sends_a_new_snapshot(self, client, showtime, url):
        with client.websocket_connect(url) as ws:
            receive(ws)

            response = client.patch(f"/api/admin/showtimes/{showtime.id}", json={"rows": 20}, headers=ADMIN)
            assert response.status_code == 200

            message = receive(ws)
            assert (message["type"], message["rows"]) == ("snapshot", 20)

    def test_deleting_the_showtime_closes_with_4410(self, client, showtime, url):
        with client.websocket_connect(url) as ws:
            receive(ws)

            assert client.delete(f"/api/admin/showtimes/{showtime.id}", headers=ADMIN).status_code == 204

            error = receive(ws)
            assert (error["type"], error["code"]) == ("error", "NOT_FOUND")
            assert closed_with(ws) == 4410

    @pytest.mark.parametrize("kept_because", ["past", "cancelled-booking"])
    def test_deleting_the_movie_closes_hidden_showtimes_too(self, app, client, kept_because):
        if kept_because == "past":
            watched = first_past(showtimes(app), TEST_NOW)
        else:
            watched = first_future(showtimes(app), TEST_NOW)
            cancel(client, book(client, watched, free_seats(watched)[:1]))
        with client.websocket_connect(f"/ws/showtimes/{watched.id}") as ws:
            receive(ws)

            assert client.delete(f"/api/admin/movies/{watched.movie_id}", headers=ADMIN).status_code == 204

            assert receive(ws)["code"] == "NOT_FOUND"
            assert closed_with(ws) == 4410
        assert watched.id in stored(app).showtimes()  # kept, only hidden

    def test_the_daily_refresh_closes_showtimes_it_removes(self, app, client, frozen_clock):
        oldest = showtimes(app)[0]
        with client.websocket_connect(f"/ws/showtimes/{oldest.id}") as ws:
            receive(ws)

            frozen_clock.advance(days=1)
            client.portal.call(refresh_showtimes, app)

            assert receive(ws)["code"] == "NOT_FOUND"
            assert closed_with(ws) == 4410

    def test_reset_sends_a_new_snapshot(self, client, showtime, url):
        book(client, showtime, free_seats(showtime)[:1])
        with client.websocket_connect(url) as ws:
            receive(ws)

            assert client.post("/api/admin/reset", headers=ADMIN).status_code == 204

            message = receive(ws)
            assert message["type"] == "snapshot"
            assert message["bookedSeats"] == showtime.booked_seats

    def test_reset_closes_showtimes_that_no_longer_exist(self, client):
        body = {
            "movieId": "m-deep-field",
            "startsAt": "2026-12-10T19:15:00Z",
            "screen": "Screen 2",
            "price": 12.5,
            "rows": 8,
            "seatsPerRow": 10,
        }
        created = client.post("/api/admin/showtimes", json=body, headers=ADMIN).json()
        with client.websocket_connect(f"/ws/showtimes/{created['id']}") as ws:
            receive(ws)

            assert client.post("/api/admin/reset", headers=ADMIN).status_code == 204

            assert receive(ws)["code"] == "NOT_FOUND"
            assert closed_with(ws) == 4410

    def test_sample_bookings_send_a_new_snapshot(self, client, showtime, url):
        with client.websocket_connect(url) as ws:
            receive(ws)

            response = client.post("/api/admin/sample-bookings?userId=u-ava", headers=ADMIN)
            assert response.status_code == 200

            assert receive(ws)["type"] == "snapshot"


class TestClientMessages:
    @pytest.mark.parametrize(
        "text",
        [
            "not json",
            "{}",
            "[]",
            "null",
            '{"type": "explode"}',
            '{"type": "hold"}',
            '{"type": "hold", "seats": []}',
            '{"type": "hold", "seats": ["A1","A2","A3","A4","A5","A6","A7"]}',
            '{"type": "hold", "seats": ["\\ud800"]}',
        ],
    )
    def test_invalid_messages_keep_the_socket_usable(self, client, showtime, url, text):
        with client.websocket_connect(url) as ws:
            receive(ws)

            ws.send_text(text)
            assert receive(ws)["code"] == "VALIDATION"

            book(client, showtime, free_seats(showtime)[:1])
            assert receive(ws)["type"] == "seats-booked"

    def test_binary_is_invalid(self, client, url):
        with client.websocket_connect(url) as ws:
            receive(ws)

            ws.send_bytes(b'{"type": "hold", "seats": ["A1"]}')

            assert receive(ws)["code"] == "VALIDATION"

    def test_errors_do_not_echo_the_input(self, client, url):
        with client.websocket_connect(url) as ws:
            receive(ws)

            ws.send_text(json.dumps({"type": "x" * 100_000}))

            assert len(json.dumps(receive(ws))) < 300

    @pytest.mark.parametrize("kind", ["hold", "release"])
    def test_holds_need_a_user_id(self, client, url, kind):
        with client.websocket_connect(url) as ws:
            receive(ws)

            ws.send_json({"type": kind, "seats": ["A1"]})

            assert receive(ws) == {
                "type": "error",
                "code": "BAD_REQUEST",
                "message": f"userId is required to {kind} seats",
            }

    def test_holds_say_they_are_not_supported_yet(self, client, url):
        with client.websocket_connect(f"{url}?userId=u-ava") as ws:
            receive(ws)

            ws.send_json({"type": "hold", "seats": ["A1"]})

            assert receive(ws)["message"] == "Seat holds are not supported yet"


class TestResilience:
    def test_shutdown_closes_with_1001(self, app, client, url):
        with client.websocket_connect(url) as ws:
            receive(ws)

            client.portal.call(app.state.shutdown_event.set)

            assert closed_with(ws) == 1001

    def test_connecting_during_shutdown_gets_1001(self, app, client, url):
        client.portal.call(app.state.shutdown_event.set)
        client.portal.call(asyncio.sleep, 0.05)

        with client.websocket_connect(url) as ws:
            assert closed_with(ws) == 1001

    def test_a_hub_that_fell_behind_resyncs_and_carries_on(self, app, client, showtime, url):
        with client.websocket_connect(url) as ws:
            receive(ws)

            async def flood():
                for _ in range(101):
                    app.state.bus.publish(ShowtimeChanged(showtime_id="st-nobody-watches"))

            client.portal.call(flood)

            assert receive(ws)["type"] == "snapshot"
            book(client, showtime, free_seats(showtime)[:1])
            assert receive(ws)["type"] == "seats-booked"

    def test_one_failing_event_does_not_stop_the_hub(self, app, client, showtime, url, monkeypatch):
        seats = free_seats(showtime)
        with client.websocket_connect(url) as ws:
            receive(ws)
            real_find = app.state.seat_hub._find
            calls = []

            async def fail_once(showtime_id):
                calls.append(showtime_id)
                if len(calls) == 1:
                    raise RuntimeError("storage hiccup")
                return await real_find(showtime_id)

            monkeypatch.setattr(app.state.seat_hub, "_find", fail_once)
            book(client, showtime, seats[:1])
            wait_until(lambda: calls)
            book(client, showtime, seats[1:2])

            assert set(receive(ws)["seats"]) == set(seats[:2])  # the missed change is caught up too

    def test_a_restarted_app_has_a_working_hub(self, app):
        for _ in range(2):
            with TestClient(app) as client:
                showtime = first_future(showtimes(app), TEST_NOW)
                with client.websocket_connect(f"/ws/showtimes/{showtime.id}") as ws:
                    receive(ws)
                    book(client, showtime, free_seats(showtime)[:1])
                    assert receive(ws)["type"] == "seats-booked"
                assert stored_showtime(app, showtime.id)


class FakeSocket:
    """A socket whose client may have stopped reading."""

    def __init__(self, *, stalled):
        self.stalled = stalled
        self.sent = []
        self.closed = None

    async def receive(self):
        await asyncio.Event().wait()

    async def send_text(self, text):
        if self.stalled:
            await asyncio.Event().wait()
        self.sent.append(json.loads(text))

    async def close(self, code, reason):
        self.closed = code


@pytest.mark.anyio
class TestConnection:
    async def test_a_stalled_client_is_given_up_on(self, monkeypatch):
        monkeypatch.setattr(seat_hub, "CLOSE_GRACE_SECONDS", 0.05)
        connection = seat_hub.SeatConnection(FakeSocket(stalled=True), "st-1", None)
        running = asyncio.create_task(connection.run())

        for _ in range(seat_hub.OUTBOX_SIZE + 2):
            connection._send(SeatsBooked(seats=["A1"]))
            await asyncio.sleep(0)

        await asyncio.wait_for(running, 2)
        assert connection._close_code == seat_hub.CloseCode.TOO_SLOW

    async def test_too_slow_discards_the_backlog_and_closes_with_1013(self):
        socket = FakeSocket(stalled=False)
        connection = seat_hub.SeatConnection(socket, "st-1", None)

        for _ in range(seat_hub.OUTBOX_SIZE + 1):
            connection._send(SeatsBooked(seats=["A1"]))
        await asyncio.wait_for(connection.run(), 2)

        assert (socket.sent, socket.closed) == ([], 1013)

    async def test_a_normal_close_delivers_what_was_queued_first(self):
        socket = FakeSocket(stalled=False)
        connection = seat_hub.SeatConnection(socket, "st-1", None)

        connection._send(SeatsBooked(seats=["A1"]))
        connection.close(4410, "Showtime deleted")
        await asyncio.wait_for(connection.run(), 2)

        assert (socket.sent, socket.closed) == ([{"type": "seats-booked", "seats": ["A1"]}], 4410)

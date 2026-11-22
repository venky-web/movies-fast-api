"""`GET /api/events`.

`ASGITransport` returns only when the response is complete (pitfall 13), so every stream here is ended: by
`maxEvents`, by the shutdown event, by dropping it, or by disconnecting.
"""

import asyncio
import json

import pytest

from app.schemas.demo import DemoSettings
from app.schemas.events import DemoReset, ShowtimeChanged
from tests.conftest import ADMIN_KEY, TEST_NOW
from tests.helpers import booking_request, first_future, free_seats, showtimes

pytestmark = pytest.mark.anyio

ADMIN = {"X-Admin-Key": ADMIN_KEY}


async def until(condition, seconds=5):
    async with asyncio.timeout(seconds):
        while not condition():  # noqa: ASYNC110 (no event to wait on)
            await asyncio.sleep(0.005)


async def open_stream(app, client, query="", headers=None):
    """Starts the request and waits until it is subscribed (the seat hub is a subscriber too)."""
    before = app.state.bus.subscriber_count
    task = asyncio.create_task(client.get(f"/api/events{query}", headers=headers))
    await until(lambda: app.state.bus.subscriber_count == before + 1)
    return task


async def finished(task):
    return await asyncio.wait_for(task, 5)


def events(response):
    found = []
    for block in response.text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if line and not line.startswith(":"))
        if "event" in fields:
            found.append(fields | {"data": json.loads(fields["data"])})
    return found


async def book(app, client):
    showtime = first_future(showtimes(app), TEST_NOW)
    response = await client.post("/api/bookings", json=booking_request(showtime, free_seats(showtime)[:1]))
    assert response.status_code == 201
    return response.json()


async def test_receives_booking_created_like_the_rest_response(app, aclient):
    stream = await open_stream(app, aclient, "?maxEvents=1")

    booking = await book(app, aclient)

    [event] = events(await finished(stream))
    assert (event["event"], event["data"]) == ("booking-created", booking)


async def test_receives_cancellation_and_reset_with_increasing_ids(app, aclient):
    stream = await open_stream(app, aclient, "?maxEvents=3")

    booking = await book(app, aclient)
    url = f"/api/bookings/{booking['id']}/cancel"
    cancelled = (await aclient.patch(url, json={"userId": "u-ava"})).json()
    await aclient.post("/api/admin/reset", headers=ADMIN)

    received = events(await finished(stream))
    assert [(e["event"], e["data"]) for e in received] == [
        ("booking-created", booking),
        ("booking-cancelled", cancelled),
        ("demo-reset", {}),
    ]
    ids = [int(e["id"]) for e in received]
    assert ids == sorted(set(ids))


async def test_sample_bookings_send_demo_reset(app, aclient):
    stream = await open_stream(app, aclient, "?maxEvents=1")

    await aclient.post("/api/admin/sample-bookings?userId=u-leo", headers=ADMIN)

    assert events(await finished(stream))[0]["event"] == "demo-reset"


async def test_every_client_gets_every_event(app, aclient):
    first = await open_stream(app, aclient, "?maxEvents=1")
    second = await open_stream(app, aclient, "?maxEvents=1")

    await book(app, aclient)

    assert events(await finished(first)) == events(await finished(second))


async def test_a_failed_booking_sends_nothing(app, aclient):
    stream = await open_stream(app, aclient, "?maxEvents=1")
    showtime = first_future(showtimes(app), TEST_NOW)

    taken = await aclient.post("/api/bookings", json=booking_request(showtime, showtime.booked_seats[:1]))
    assert taken.status_code == 409
    booking = await book(app, aclient)

    assert events(await finished(stream))[0]["data"] == booking


async def test_internal_events_neither_show_up_nor_drop_the_stream(app, aclient):
    stream = await open_stream(app, aclient, "?maxEvents=1")

    for _ in range(150):
        app.state.bus.publish(ShowtimeChanged(showtime_id="st-1"))
    app.state.bus.publish(DemoReset())

    assert [e["event"] for e in events(await finished(stream))] == ["demo-reset"]


async def test_headers_retry_and_no_compression(app, aclient):
    headers = {"Accept-Encoding": "gzip", "Origin": "http://localhost:4200"}
    stream = await open_stream(app, aclient, "?maxEvents=1", headers)
    await book(app, aclient)

    response = await finished(stream)

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["access-control-allow-origin"] == "http://localhost:4200"
    assert response.headers["x-request-id"]
    assert "content-encoding" not in response.headers
    assert response.text.startswith("retry: 3000\n\n")


async def test_heartbeats_do_not_count_towards_max_events(app, aclient):
    stream = await open_stream(app, aclient, "?maxEvents=1")
    await asyncio.sleep(0.2)  # the test heartbeat is 0.05 s
    assert not stream.done()

    await book(app, aclient)

    response = await finished(stream)
    assert ": ping\n\n" in response.text
    assert len(events(response)) == 1


async def test_shutdown_ends_the_stream_at_once(app, aclient):
    stream = await open_stream(app, aclient)

    app.state.shutdown_event.set()

    assert (await asyncio.wait_for(stream, 1)).status_code == 200


async def test_a_stream_that_falls_behind_is_ended(app, aclient):
    stream = await open_stream(app, aclient)

    for _ in range(101):
        app.state.bus.publish(DemoReset())

    assert (await finished(stream)).status_code == 200


async def test_finished_streams_unsubscribe(app, aclient):
    before = app.state.bus.subscriber_count
    stream = await open_stream(app, aclient, "?maxEvents=1")

    await book(app, aclient)
    await finished(stream)

    assert app.state.bus.subscriber_count == before


async def test_the_simulation_leaves_the_stream_alone(app, aclient):
    app.state.demo_settings = DemoSettings(latency_ms=0, failure_rate=1)
    stream = await open_stream(app, aclient, "?maxEvents=1")

    app.state.bus.publish(DemoReset())

    assert (await finished(stream)).status_code == 200


@pytest.mark.parametrize("value", ["0", "-1", "many"])
async def test_a_bad_max_events_is_400_and_unsubscribes(app, aclient, value):
    before = app.state.bus.subscriber_count

    response = await aclient.get(f"/api/events?maxEvents={value}")

    assert (response.status_code, response.json()["code"]) == (400, "VALIDATION")
    assert app.state.bus.subscriber_count == before


@pytest.mark.parametrize("stalled", [False, True], ids=["reading", "stalled"])
async def test_a_disconnect_unsubscribes_without_waiting_for_a_write(app, settings, stalled):
    app.state.settings = settings.model_copy(update={"sse_heartbeat_seconds": 60})  # nothing gets written
    gone = asyncio.Event()

    async def receive():
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if stalled and message["type"] == "http.response.body":
            await asyncio.Event().wait()  # a client that never reads

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},  # what uvicorn sends
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/events",
        "raw_path": b"/api/events",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    async with app.router.lifespan_context(app):
        before = app.state.bus.subscriber_count
        request = asyncio.create_task(app(scope, receive, send))
        await until(lambda: app.state.bus.subscriber_count == before + 1)
        await asyncio.sleep(0.05)

        gone.set()

        await asyncio.wait_for(request, 1)
        assert app.state.bus.subscriber_count == before

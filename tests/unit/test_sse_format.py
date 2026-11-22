"""The bytes on the wire: our events through FastAPI's own encoder."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from fastapi import sse as fastapi_sse
from fastapi.sse import format_sse_event

from app.realtime import sse
from app.realtime.bus import EventBus, Published
from app.realtime.sse import event_stream, to_sse
from app.schemas.booking import BookingDetails
from app.schemas.events import BookingCancelled, BookingCreated, DemoReset, ShowtimeChanged

AT = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
BOOKING = BookingDetails.model_validate(
    {
        "id": "BK-ABC123",
        "userId": "u-ava",
        "movieId": "m-1",
        "showtimeId": "st-1",
        "seats": ["A1"],
        "totalPrice": 9.5,
        "customerName": "Ava\r\ndata: forged",
        "email": "ava@example.com",
        "createdAt": AT,
        "status": "CONFIRMED",
        "movie": {"id": "m-1", "title": "One", "posterUrl": "/p", "durationMins": 90, "genres": ["Drama"]},
        "showtime": {"id": "st-1", "startsAt": AT, "screen": "Screen 1", "price": 9.5},
    }
)


def wire(item):
    return format_sse_event(
        data_str=item.raw_data, event=item.event, id=item.id, retry=item.retry, comment=item.comment
    ).decode()


def test_a_booking_is_one_camel_case_json_line():
    text = wire(to_sse(Published(7, BookingCreated(booking=BOOKING))))

    event, data, event_id, *end = text.split("\n")
    assert (event, event_id, end) == ("event: booking-created", "id: 7", ["", ""])
    body = json.loads(data.removeprefix("data: "))
    assert body["customerName"] == "Ava\r\ndata: forged"  # a client's newline cannot start a field
    assert (body["totalPrice"], body["createdAt"]) == (9.5, "2026-11-19T12:00:00.000Z")
    assert "cancelledAt" not in body


def test_a_cancellation_has_its_own_name():
    cancelled = BOOKING.model_copy(update={"cancelled_at": AT})

    item = to_sse(Published(8, BookingCancelled(booking=cancelled)))

    assert item.event == "booking-cancelled"
    assert json.loads(item.raw_data)["cancelledAt"] == "2026-11-19T12:00:00.000Z"


def test_demo_reset_has_data_or_browsers_would_ignore_it():
    assert wire(to_sse(Published(3, DemoReset()))) == "event: demo-reset\ndata: {}\nid: 3\n\n"


def test_internal_events_are_not_sent():
    with pytest.raises(ValueError):
        to_sse(Published(1, ShowtimeChanged(showtime_id="st-1")))


@pytest.mark.anyio
async def test_the_stream_starts_with_retry_then_heartbeats():
    async with EventBus().subscribe() as subscription:
        stream = event_stream(subscription, heartbeat=0.01)

        assert wire(await anext(stream)) == "retry: 3000\n\n"
        assert wire(await asyncio.wait_for(anext(stream), 2)) == ": ping\n\n"
        await stream.aclose()


def test_the_built_in_ping_interval_is_what_we_assume():
    # Private in FastAPI, so a version upgrade could change it.
    assert fastapi_sse._PING_INTERVAL == sse.BUILTIN_PING_SECONDS


@pytest.mark.anyio
async def test_long_heartbeats_are_left_to_fastapi(monkeypatch):
    monkeypatch.setattr(sse, "BUILTIN_PING_SECONDS", 0.01)
    async with EventBus().subscribe() as subscription:
        stream = event_stream(subscription, heartbeat=0.01)
        await anext(stream)  # retry

        with pytest.raises(TimeoutError):  # no ping of our own, which would double FastAPI's
            await asyncio.wait_for(anext(stream), 0.1)

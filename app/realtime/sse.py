"""The `/api/events` stream, encoded by FastAPI's built-in SSE support (`fastapi.sse`)."""

import asyncio
from collections.abc import AsyncIterator

from fastapi.sse import ServerSentEvent

from app.realtime.bus import Published, Subscription
from app.schemas.events import BookingCancelled, BookingCreated, DemoReset, Event

SSE_EVENT_TYPES: tuple[type[Event], ...] = (BookingCreated, BookingCancelled, DemoReset)
RETRY_MS = 3000
# FastAPI pings an idle stream by itself this often (not configurable), so we only add shorter heartbeats:
# with the default 15 s every ping went out twice.
BUILTIN_PING_SECONDS = 15.0


def to_sse(published: Published) -> ServerSentEvent:
    match published.event:
        case BookingCreated(booking=booking):
            name, data = "booking-created", booking.model_dump_json(exclude_none=True)
        case BookingCancelled(booking=booking):
            name, data = "booking-cancelled", booking.model_dump_json(exclude_none=True)
        case DemoReset():
            # Not empty: EventSource silently ignores an event without a data line.
            name, data = "demo-reset", "{}"
        case other:
            raise ValueError(f"{type(other).__name__} is not sent over SSE")
    # raw_data: already JSON, shaped like the REST responses (camelCase, no null cancelledAt)
    return ServerSentEvent(event=name, id=str(published.id), raw_data=data)


async def event_stream(
    subscription: Subscription, *, heartbeat: float, max_events: int | None = None
) -> AsyncIterator[ServerSentEvent]:
    """Ends after `max_events`, or when the subscription ends (too slow, or the app is shutting down).

    A client disconnect needs no check here: Starlette watches for it and cancels the stream.
    """
    yield ServerSentEvent(retry=RETRY_MS)
    wait = heartbeat if heartbeat < BUILTIN_PING_SECONDS else None
    sent = 0
    while max_events is None or sent < max_events:
        try:
            published = await asyncio.wait_for(subscription.get(), timeout=wait)
        except TimeoutError:
            yield ServerSentEvent(comment="ping")
            continue
        except asyncio.QueueShutDown:
            return
        yield to_sse(published)
        sent += 1

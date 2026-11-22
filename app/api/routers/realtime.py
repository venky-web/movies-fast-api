from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket
from fastapi.sse import EventSourceResponse, ServerSentEvent

from app.api.deps import ConfigDep, EventSubscriptionDep, SeatHubDep
from app.realtime import sse

router = APIRouter(prefix="/api", tags=["realtime"])
# A separate router: the audit dependency takes a `Request`, which a WebSocket route doesn't have.
ws_router = APIRouter(prefix="/ws", tags=["realtime"])


@router.get(
    "/events",
    response_class=EventSourceResponse,
    summary="Stream booking events (SSE)",
    response_description="An event stream that stays open",
)
async def stream_events(
    subscription: EventSubscriptionDep,
    config: ConfigDep,
    max_events: Annotated[
        int | None, Query(alias="maxEvents", ge=1, description="End the stream after this many events")
    ] = None,
) -> AsyncIterator[ServerSentEvent]:
    """Events: `booking-created`, `booking-cancelled` (BookingDetails) and `demo-reset` (`{}`).

    The connection stays open: set `maxEvents` to try it from this page. Details are in the tag description.
    """
    events = sse.event_stream(subscription, heartbeat=config.sse_heartbeat_seconds, max_events=max_events)
    async for item in events:
        yield item


@ws_router.websocket("/showtimes/{showtime_id}")
async def watch_seats(
    websocket: WebSocket,
    showtime_id: str,
    hub: SeatHubDep,
    user_id: Annotated[str | None, Query(alias="userId")] = None,
) -> None:
    await hub.watch(websocket, showtime_id, user_id)

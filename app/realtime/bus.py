"""In-process event bus: every subscriber gets its own bounded queue.

It only works within one process, so run one worker.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NamedTuple

from app.schemas.booking import BookingDetails
from app.schemas.events import (
    BookingCancelled,
    BookingCreated,
    DemoReset,
    Event,
    ShowtimeChanged,
    ShowtimeDeleted,
)
from app.schemas.showtime import ShowtimeRecord

log = logging.getLogger(__name__)

QUEUE_SIZE = 100


class Published(NamedTuple):
    id: int  # increases with every event of this process
    event: Event


class Subscription:
    """`get()` raises `asyncio.QueueShutDown` once the subscriber was dropped or the bus closed."""

    def __init__(self, types: tuple[type[Event], ...], maxsize: int) -> None:
        self._types = types
        self._queue: asyncio.Queue[Published] = asyncio.Queue(maxsize)
        self.dropped = False

    def wants(self, event: Event) -> bool:
        return not self._types or isinstance(event, self._types)

    def offer(self, published: Published) -> bool:
        """False if the queue was full: the subscriber is then dropped, and its backlog freed."""
        try:
            self._queue.put_nowait(published)
        except asyncio.QueueFull:
            self.dropped = True
            self.close()
            return False
        return True

    def close(self) -> None:
        self._queue.shutdown(immediate=True)  # Python 3.13: also wakes a waiting `get()`

    async def get(self) -> Published:
        return await self._queue.get()


class EventBus:
    def __init__(self, maxsize: int = QUEUE_SIZE) -> None:
        self._maxsize = maxsize
        self._subscriptions: set[Subscription] = set()
        self._last_id = 0
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    @asynccontextmanager
    async def subscribe(self, *types: type[Event]) -> AsyncIterator[Subscription]:
        """Only the given event types count towards the queue limit (no types: all events)."""
        subscription = Subscription(types, self._maxsize)
        if self._closed:
            subscription.close()
        else:
            self._subscriptions.add(subscription)
        try:
            yield subscription
        finally:
            # No `await` in here, so this also completes when the subscriber's task is being cancelled.
            self._subscriptions.discard(subscription)

    def publish(self, event: Event) -> None:
        """Never waits, so a slow subscriber cannot slow down a booking."""
        self._last_id += 1
        published = Published(self._last_id, event)
        for subscription in list(self._subscriptions):
            if subscription.wants(event) and not subscription.offer(published):
                self._subscriptions.discard(subscription)
                log.warning("Dropped a subscriber that was %d events behind", self._maxsize)

    def close(self) -> None:
        self._closed = True
        for subscription in self._subscriptions:
            subscription.close()
        self._subscriptions.clear()


class BusPublisher:
    """The services' `Publisher`. They call it after the write succeeded."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def booking_created(self, booking: BookingDetails) -> None:
        self._bus.publish(BookingCreated(booking=booking))

    async def booking_cancelled(self, booking: BookingDetails) -> None:
        self._bus.publish(BookingCancelled(booking=booking))

    async def showtime_changed(self, showtime: ShowtimeRecord) -> None:
        self._bus.publish(ShowtimeChanged(showtime_id=showtime.id))

    async def showtime_deleted(self, showtime_id: str) -> None:
        self._bus.publish(ShowtimeDeleted(showtime_id=showtime_id))

    async def demo_reset(self) -> None:
        self._bus.publish(DemoReset())

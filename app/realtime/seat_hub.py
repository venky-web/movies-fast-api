"""Live seat maps over WebSocket (plan 3.6).

The hub never forwards an event's seats. An event only says "look again": the hub reads the showtime and sends
each socket the difference from what that socket was last told. That stays correct when events arrive out of
order, or when a booking lands while a client is connecting.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from enum import IntEnum
from functools import partial

import anyio
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from starlette.types import Message

from app.core.errors import ApiErrorCode
from app.domain.rules import sort_seats
from app.realtime.bus import EventBus
from app.repositories.base import Repos
from app.schemas.events import (
    BookingCancelled,
    BookingCreated,
    DemoReset,
    Event,
    ShowtimeChanged,
    ShowtimeDeleted,
)
from app.schemas.showtime import ShowtimeRecord
from app.schemas.ws import SeatsBooked, SeatsReleased, ServerMessage, Snapshot, WsError, client_message
from app.services import catalog

log = logging.getLogger(__name__)

OUTBOX_SIZE = 100
CLOSE_GRACE_SECONDS = 2.0  # how long a client gets to accept the last messages and the close


class CloseCode(IntEnum):
    GOING_AWAY = 1001  # server shutting down
    TOO_SLOW = 1013  # "try again later"
    UNKNOWN_SHOWTIME = 4404
    SHOWTIME_DELETED = 4410


class SeatConnection:
    """One socket. Only its writer task sends, so a slow client never blocks the hub or the other clients."""

    def __init__(self, websocket: WebSocket, showtime_id: str, user_id: str | None) -> None:
        self.showtime_id = showtime_id
        self._websocket = websocket
        self._user_id = user_id
        self._outbox: asyncio.Queue[ServerMessage] = asyncio.Queue(OUTBOX_SIZE)
        self._booked: set[str] = set()  # what this client was last told
        self._closing = asyncio.Event()
        self._close_code = 1000
        self._close_reason = ""

    def send_snapshot(self, showtime: ShowtimeRecord) -> None:
        self._booked = set(showtime.booked_seats)
        self._send(
            Snapshot(
                showtime_id=showtime.id,
                rows=showtime.rows,
                seats_per_row=showtime.seats_per_row,
                booked_seats=showtime.booked_seats,
            )
        )

    def send_changes(self, showtime: ShowtimeRecord) -> None:
        booked = set(showtime.booked_seats)
        released, added = self._booked - booked, booked - self._booked
        self._booked = booked
        if released:
            self._send(SeatsReleased(seats=sort_seats(list(released))))
        if added:
            self._send(SeatsBooked(seats=sort_seats(list(added))))

    def _send(self, message: ServerMessage) -> None:
        if self._closing.is_set():
            return
        try:
            self._outbox.put_nowait(message)
        except asyncio.QueueFull:
            log.warning("Closing a seat map socket that was %d messages behind", self._outbox.maxsize)
            self.close(CloseCode.TOO_SLOW, "Too slow", discard_backlog=True)

    def close(
        self, code: int, reason: str, *, error: WsError | None = None, discard_backlog: bool = False
    ) -> None:
        """Queued messages (and `error`) still go out before the close, unless the backlog is discarded."""
        if self._closing.is_set():
            return
        if error:
            self._send(error)
        self._close_code, self._close_reason = code, reason
        self._closing.set()
        self._outbox.shutdown(immediate=discard_backlog)

    async def run(self) -> None:
        async with anyio.create_task_group() as tasks:

            async def read() -> None:
                await self._read()
                tasks.cancel_scope.cancel()

            async def write() -> None:
                await self._write()
                tasks.cancel_scope.cancel()

            async def give_up() -> None:
                # A client that stopped reading blocks the writer, which then never gets to send the close.
                await self._closing.wait()
                await asyncio.sleep(CLOSE_GRACE_SECONDS)
                tasks.cancel_scope.cancel()

            tasks.start_soon(read)
            tasks.start_soon(write)
            tasks.start_soon(give_up)

    async def _read(self) -> None:
        while True:
            message = await self._websocket.receive()
            if message["type"] == "websocket.disconnect":
                if self._closing.is_set():
                    await anyio.sleep_forever()  # our own close: let the writer finish it
                return
            try:
                # Waits for space, so a client flooding us is slowed down instead of filling memory.
                await self._outbox.put(self._answer(message))
            except asyncio.QueueShutDown:
                continue

    def _answer(self, message: Message) -> WsError:
        text = message.get("text")
        if text is None:
            return WsError(code=ApiErrorCode.VALIDATION, message="Send JSON as a text message")
        try:
            parsed = client_message.validate_json(text)
        except ValidationError:
            # A fixed text: Pydantic's would echo the input, and a message can be up to 16 MB.
            return WsError(
                code=ApiErrorCode.VALIDATION,
                message='Expected {"type": "hold" | "release", "seats": [1 to 6 seat ids]}',
            )
        if not self._user_id:
            message_text = f"userId is required to {parsed.type} seats"
            return WsError(code=ApiErrorCode.BAD_REQUEST, message=message_text)
        return WsError(code=ApiErrorCode.BAD_REQUEST, message="Seat holds are not supported yet")

    async def _write(self) -> None:
        try:
            try:
                while True:
                    message = await self._outbox.get()
                    await self._websocket.send_text(message.model_dump_json())
            except asyncio.QueueShutDown:
                await self._websocket.close(self._close_code, self._close_reason)
        except (WebSocketDisconnect, RuntimeError) as exc:
            # The client is gone. Uvicorn reports some of these cases as a RuntimeError.
            log.debug("Seat map socket ended while sending: %r", exc)


class SeatHub:
    def __init__(self, bus: EventBus, open_repos: Callable[[], AbstractAsyncContextManager[Repos]]) -> None:
        self._bus = bus
        self._open_repos = open_repos
        self._connections: dict[str, set[SeatConnection]] = {}
        # Held while reading and queueing, so a connecting client cannot get a snapshot older than the
        # changes the others were just sent.
        self._lock = asyncio.Lock()
        self._closed = False
        self._open = 0  # every socket being served, including those already told to close
        self._none_open = asyncio.Event()
        self._none_open.set()
        self.ready = asyncio.Event()

    @property
    def connection_count(self) -> int:
        return sum(len(group) for group in self._connections.values())

    async def watch(self, websocket: WebSocket, showtime_id: str, user_id: str | None) -> None:
        """Serves one socket until either side closes it."""
        connection = SeatConnection(websocket, showtime_id, user_id)
        self._open += 1
        self._none_open.clear()
        try:
            await websocket.accept()
            async with self._lock:
                showtime = await self._find(showtime_id)
                if self._closed:
                    connection.close(CloseCode.GOING_AWAY, "Server shutting down")
                elif showtime is None:
                    error = WsError(
                        code=ApiErrorCode.NOT_FOUND, message=f'Showtime "{showtime_id}" not found'
                    )
                    connection.close(CloseCode.UNKNOWN_SHOWTIME, "Unknown showtime", error=error)
                else:
                    connection.send_snapshot(showtime)
                    self._connections.setdefault(showtime_id, set()).add(connection)
            await connection.run()
        finally:
            self._forget(connection)
            self._open -= 1
            if not self._open:
                self._none_open.set()

    async def wait_until_all_closed(self) -> None:
        """Returns when every socket has had its close written. Used to say goodbye before uvicorn does."""
        await self._none_open.wait()

    async def run(self) -> None:
        """The lifespan task. When the bus closes (shutdown), every socket is closed with 1001."""
        try:
            while True:
                async with self._bus.subscribe() as subscription:
                    self.ready.set()
                    await self._safely(self._resync)  # catches up if the last subscription was dropped
                    while True:
                        try:
                            event = (await subscription.get()).event
                        except asyncio.QueueShutDown:
                            break
                        await self._safely(partial(self._handle, event))
                if not subscription.dropped:
                    return
                log.warning("The seat hub fell behind: sending every client a fresh snapshot")
        finally:
            self._closed = True
            for group in self._connections.values():
                for connection in group:
                    connection.close(CloseCode.GOING_AWAY, "Server shutting down")
            self._connections.clear()

    async def _safely(self, action: Callable) -> None:
        try:
            await action()
        except Exception:  # one bad event must not end live updates for everybody
            log.exception("The seat hub could not process an event")

    async def _handle(self, event: Event) -> None:
        match event:
            case BookingCreated(booking=booking) | BookingCancelled(booking=booking):
                await self._update(booking.showtime_id)
            case ShowtimeChanged(showtime_id=showtime_id):
                await self._update(showtime_id, snapshot=True)
            case ShowtimeDeleted(showtime_id=showtime_id):
                async with self._lock:
                    self._close_deleted(showtime_id)
            case DemoReset():
                await self._resync()

    async def _update(self, showtime_id: str, *, snapshot: bool = False) -> None:
        async with self._lock:
            if showtime_id not in self._connections:
                return  # nobody is watching
            showtime = await self._find(showtime_id)
            if showtime is None:
                self._close_deleted(showtime_id)
                return
            for connection in self._connections.get(showtime_id, set()):
                if snapshot:
                    connection.send_snapshot(showtime)
                else:
                    connection.send_changes(showtime)

    async def _resync(self) -> None:
        for showtime_id in list(self._connections):
            await self._update(showtime_id, snapshot=True)

    async def _find(self, showtime_id: str) -> ShowtimeRecord | None:
        # A unit of work per read: a socket must not hold a connection, and a reset swaps the memory store.
        async with self._open_repos() as repos:
            return await catalog.get_showtime(repos, showtime_id)

    def _close_deleted(self, showtime_id: str) -> None:
        error = WsError(code=ApiErrorCode.NOT_FOUND, message=f'Showtime "{showtime_id}" was deleted')
        for connection in self._connections.pop(showtime_id, set()):
            connection.close(CloseCode.SHOWTIME_DELETED, "Showtime deleted", error=error)

    def _forget(self, connection: SeatConnection) -> None:
        group = self._connections.get(connection.showtime_id)
        if group is not None:
            group.discard(connection)
            if not group:
                del self._connections[connection.showtime_id]

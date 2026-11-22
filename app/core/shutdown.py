"""Announces a shutdown when it begins, not when it is almost over.

Uvicorn waits for open connections *before* it runs lifespan shutdown, so an event set there can never end a
stream: the stream waits for the event and the server waits for the stream (`fastapi dev` then hangs on
reload). Setting the event from the exit signal breaks that circle.

Uvicorn also closes every WebSocket with 1012 within 0.1 s of the signal, which raced the app's own 1001. So
the signal is handed on to uvicorn only when the app has said goodbye, or after `GOODBYE_SECONDS`.
"""

import asyncio
import signal
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from types import FrameType

EXIT_SIGNALS = [getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, name)]
GOODBYE_SECONDS = 1.0


@contextmanager
def set_on_exit_signal(event: asyncio.Event, say_goodbye: Callable[[], Awaitable[None]]) -> Iterator[None]:
    """While active, an exit signal sets `event`, awaits `say_goodbye()` and then runs the handler that was
    there before (uvicorn's). A second signal skips the wait, so a double Ctrl+C still forces the exit.

    Signal handlers can only be installed on the main thread, so under `TestClient` this does nothing.
    A server stopped from code (`server.should_exit = True`) sends no signal either.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    loop = asyncio.get_running_loop()
    previous: dict[int, Callable[[int, FrameType | None], object]] = {}
    handovers: list[asyncio.Task[None]] = []  # the loop itself keeps only weak references to tasks
    announced = False

    async def hand_over(signum: int) -> None:
        try:
            with suppress(TimeoutError):
                await asyncio.wait_for(say_goodbye(), GOODBYE_SECONDS)
        finally:
            previous[signum](signum, None)

    def begin(signum: int) -> None:
        event.set()
        handovers.append(loop.create_task(hand_over(signum), name="shutdown-handover"))

    def on_signal(signum: int, frame: FrameType | None) -> None:
        nonlocal announced
        if announced or loop.is_closed():
            previous[signum](signum, frame)
            return
        announced = True
        loop.call_soon_threadsafe(begin, signum)

    for exit_signal in EXIT_SIGNALS:
        handler = signal.getsignal(exit_signal)
        if callable(handler):  # SIG_DFL and SIG_IGN are not
            previous[exit_signal] = handler
            signal.signal(exit_signal, on_signal)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            if signal.getsignal(signum) is on_signal:
                signal.signal(signum, handler)

import asyncio
import signal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core import shutdown
from app.core.config import Settings


def test_shutdown_stops_the_refresh_task(app):
    with TestClient(app):
        task = app.state.refresh_task
        assert not task.done()
        assert not app.state.shutdown_event.is_set()

    assert task.cancelled()
    assert app.state.shutdown_event.is_set()


@pytest.mark.anyio
async def test_shutdown_leaves_no_tasks_running(app):
    before = asyncio.all_tasks()

    async with app.router.lifespan_context(app):
        names = {task.get_name() for task in asyncio.all_tasks() - before}
        assert names == {"showtime-refresh", "seat-hub", "bus-closer"}

    assert asyncio.all_tasks() - before == set()


@pytest.mark.anyio
async def test_an_exit_signal_sets_the_shutdown_event_before_lifespan_shutdown(app):
    """Uvicorn waits for open streams before lifespan shutdown, so that would be too late to end them."""
    received = []

    def server_handler(signum, frame):
        received.append(signum)

    original = signal.signal(signal.SIGINT, server_handler)
    try:
        async with app.router.lifespan_context(app):
            signal.raise_signal(signal.SIGINT)
            await asyncio.wait_for(app.state.shutdown_event.wait(), 2)
            async with asyncio.timeout(2):
                while not received:  # noqa: ASYNC110 (no event to wait on)
                    await asyncio.sleep(0.005)

            assert received == [signal.SIGINT]  # the server's own handler still runs

        assert signal.getsignal(signal.SIGINT) is server_handler  # put back on shutdown
    finally:
        signal.signal(signal.SIGINT, original)


def test_each_startup_gets_a_fresh_event(app):
    with TestClient(app):
        first = app.state.shutdown_event
    with TestClient(app):
        assert app.state.shutdown_event is not first
        assert not app.state.shutdown_event.is_set()


@pytest.mark.parametrize("seconds", [0, -1])
def test_the_refresh_interval_must_be_positive(seconds):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, showtime_refresh_seconds=seconds)


@pytest.mark.anyio
class TestHandingTheSignalOn:
    """Uvicorn's handler must wait for the WebSocket closes, or clients get its 1012 instead of our 1001."""

    @pytest.fixture
    def received(self):
        received = []
        original = signal.signal(signal.SIGINT, lambda signum, frame: received.append(signum))
        yield received
        signal.signal(signal.SIGINT, original)

    async def test_waits_for_the_goodbye(self, received):
        event, said = asyncio.Event(), asyncio.Event()
        with shutdown.set_on_exit_signal(event, said.wait):
            signal.raise_signal(signal.SIGINT)
            await asyncio.wait_for(event.wait(), 2)
            await asyncio.sleep(0.05)
            assert received == []

            said.set()
            await asyncio.sleep(0.05)

        assert received == [signal.SIGINT]

    async def test_does_not_wait_for_ever(self, received, monkeypatch):
        monkeypatch.setattr(shutdown, "GOODBYE_SECONDS", 0.02)
        with shutdown.set_on_exit_signal(asyncio.Event(), asyncio.Event().wait):
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0.2)

        assert received == [signal.SIGINT]

    async def test_a_second_signal_skips_the_wait(self, received):
        said = asyncio.Event()
        with shutdown.set_on_exit_signal(asyncio.Event(), said.wait):
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0.02)
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0.02)

            assert received == [signal.SIGINT]
            said.set()
            await asyncio.sleep(0.02)

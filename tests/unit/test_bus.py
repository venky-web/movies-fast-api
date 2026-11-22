import asyncio

import pytest

from app.realtime.bus import QUEUE_SIZE, EventBus
from app.schemas.events import DemoReset, ShowtimeChanged

pytestmark = pytest.mark.anyio


async def test_fans_out_to_every_subscriber():
    bus = EventBus()
    async with bus.subscribe() as first, bus.subscribe() as second:
        bus.publish(DemoReset())

        assert await first.get() == await second.get()


async def test_ids_increase():
    bus = EventBus()
    async with bus.subscribe() as subscription:
        for _ in range(3):
            bus.publish(DemoReset())

        assert [(await subscription.get()).id for _ in range(3)] == [1, 2, 3]


async def test_the_queue_holds_exactly_100():
    bus = EventBus()
    async with bus.subscribe() as subscription:
        for _ in range(QUEUE_SIZE):
            bus.publish(DemoReset())

        assert not subscription.dropped
        assert (await subscription.get()).id == 1


async def test_a_full_queue_drops_only_that_subscriber():
    bus = EventBus(maxsize=2)
    async with bus.subscribe() as slow, bus.subscribe() as fast:
        for expected in range(1, 4):
            bus.publish(DemoReset())
            assert (await fast.get()).id == expected

        assert slow.dropped and not fast.dropped
        assert bus.subscriber_count == 1
        with pytest.raises(asyncio.QueueShutDown):  # its backlog is freed too
            await slow.get()


async def test_unwanted_types_do_not_fill_the_queue():
    bus = EventBus(maxsize=1)
    async with bus.subscribe(DemoReset) as subscription:
        for _ in range(5):
            bus.publish(ShowtimeChanged(showtime_id="st-1"))
        bus.publish(DemoReset())

        assert isinstance((await subscription.get()).event, DemoReset)
        assert not subscription.dropped


async def test_leaving_unsubscribes_even_when_cancelled():
    bus = EventBus()

    async def subscriber():
        async with bus.subscribe() as subscription:
            await subscription.get()

    task = asyncio.create_task(subscriber())
    await asyncio.sleep(0)
    assert bus.subscriber_count == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bus.subscriber_count == 0


async def test_close_wakes_waiters_and_refuses_new_subscribers():
    bus = EventBus()
    async with bus.subscribe() as subscription:
        waiting = asyncio.create_task(subscription.get())
        await asyncio.sleep(0)

        bus.close()

        with pytest.raises(asyncio.QueueShutDown):
            await waiting
        assert not subscription.dropped
    async with bus.subscribe() as late:
        with pytest.raises(asyncio.QueueShutDown):
            await late.get()

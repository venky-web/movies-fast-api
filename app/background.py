"""Lifespan background work: keeps the showtime window around today."""

import asyncio
import logging
from datetime import date
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from app.services import catalog

log = logging.getLogger(__name__)


def theater_day(app: FastAPI) -> date:
    return app.state.clock.now().astimezone(ZoneInfo(app.state.settings.theater_tz)).date()


async def refresh_showtimes(app: FastAPI) -> None:
    """One refresh. A plain function, so tests can call it after moving the clock instead of waiting."""
    async with app.state.storage.unit_of_work(app.state.publisher) as uow:
        removed = await catalog.refresh_showtime_window(
            uow.repos,
            uow.locks,
            now=app.state.clock.now(),
            tz=ZoneInfo(app.state.settings.theater_tz),
        )
        for showtime_id in removed:
            await uow.publisher.showtime_deleted(showtime_id)


async def refresh_loop(app: FastAPI, seeded_on: date) -> None:
    """Refreshes when the theater's day changes.

    `seeded_on` comes from the caller: reading the day here would miss a midnight between seeding and the
    first run of this task. A failed refresh is retried at the next tick.
    """
    last_day = seeded_on
    while True:
        await asyncio.sleep(app.state.settings.showtime_refresh_seconds)
        try:
            today = theater_day(app)
            if today != last_day:
                await refresh_showtimes(app)
                last_day = today
        except Exception:  # CancelledError is not an Exception, so shutdown still gets through
            log.exception("Showtime refresh failed, retrying at the next tick")

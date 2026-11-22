"""Fake emails, "sent" into the outbox by background tasks that run after the response."""

import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.clock import Clock
from app.repositories.base import Repos
from app.schemas.booking import BookingDetails
from app.schemas.outbox import EmailKind, OutboxEmail

log = logging.getLogger(__name__)

OpenRepos = Callable[[], AbstractAsyncContextManager[Repos]]


def render_email(kind: EmailKind, booking: BookingDetails, sent_at: datetime, tz: ZoneInfo) -> OutboxEmail:
    confirmed = kind == EmailKind.CONFIRMATION
    lines = [
        f"Hi {booking.customer_name},",
        "",
        f"Your booking {booking.id} is {'confirmed' if confirmed else 'cancelled'}.",
        "",
        f"Movie: {booking.movie.title}",
        f"When: {booking.showtime.starts_at.astimezone(tz):%a %d %b %Y, %H:%M}",
        f"Where: {booking.showtime.screen}",
        f"Seats: {', '.join(booking.seats)}",
        f"Total: {booking.total_price:.2f}",
    ]
    return OutboxEmail(
        id=f"EM-{uuid.uuid4().hex[:12]}",  # not `app.state.rng`, which would shift the seeded booking ids
        to=booking.email,
        subject=f"Booking {'confirmed' if confirmed else 'cancelled'}: {booking.movie.title}",
        body="\n".join(lines),
        booking_id=booking.id,
        kind=kind,
        sent_at=sent_at,
    )


async def send_email(
    kind: EmailKind,
    booking: BookingDetails,
    open_repos: OpenRepos,
    clock: Clock,
    tz: ZoneInfo,
) -> None:
    """The background task. It takes nothing from the request and never raises (the response is out)."""
    try:
        async with open_repos() as repos:  # its own unit of work: the request's ended before the response
            if not await repos.bookings.exists(booking.id):
                log.info("No %s email for %s: the demo data was reset meanwhile", kind, booking.id)
                return
            await repos.outbox.add(render_email(kind, booking, clock.now(), tz))
        log.info("Sent %s email for %s", kind, booking.id)
    except Exception:
        log.exception("Could not send the %s email for %s", kind, booking.id)


class EmailNotifier:
    """The booking service's `Notifier`. `schedule` is `BackgroundTasks.add_task`."""

    def __init__(
        self, schedule: Callable[..., None], open_repos: OpenRepos, clock: Clock, tz: ZoneInfo
    ) -> None:
        self._schedule = schedule
        self._open_repos = open_repos
        self._clock = clock
        self._tz = tz

    def booking_confirmed(self, booking: BookingDetails) -> None:
        self._schedule(send_email, EmailKind.CONFIRMATION, booking, self._open_repos, self._clock, self._tz)

    def booking_cancelled(self, booking: BookingDetails) -> None:
        self._schedule(send_email, EmailKind.CANCELLATION, booking, self._open_repos, self._clock, self._tz)


async def list_outbox(repos: Repos) -> list[OutboxEmail]:
    return await repos.outbox.list_all()

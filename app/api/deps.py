"""Shared dependencies and their `Annotated` aliases. Routers import only the aliases."""

import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, Path, Query, Request, Response, Security, WebSocket
from fastapi.routing import APIRoute
from fastapi.security import APIKeyHeader

from app.core.config import Settings
from app.core.errors import ApiErrorCode, AppError, not_found
from app.domain.seed import SeedData
from app.realtime.bus import Subscription
from app.realtime.seat_hub import SeatHub
from app.realtime.sse import SSE_EVENT_TYPES
from app.repositories.base import Repos
from app.schemas.user import User
from app.services.admin import AdminContext
from app.services.bookings import BookingContext
from app.services.locks import Locks
from app.services.notifications import EmailNotifier
from app.storage import UnitOfWork

log = logging.getLogger("app.audit")

MovieIdPath = Annotated[str, Path(description="A movie id", examples=["m-deep-field"])]
ShowtimeIdPath = Annotated[
    str, Path(description="A showtime id, from `GET /api/movies/{movie_id}/showtimes`")
]
BookingIdPath = Annotated[str, Path(description="A booking id such as `BK-7Q2M4X`")]
UserIdPath = Annotated[str, Path(description="A mock user id", examples=["u-ava"])]


def get_config(request: Request) -> Settings:
    """Settings live on the app (passed to `create_app`), so tests never patch the environment."""
    return request.app.state.settings


def get_now(request: Request) -> datetime:
    """Current time from the app-level clock. Tests can also override this one dependency."""
    return request.app.state.clock.now()


async def get_uow(request: Request) -> AsyncIterator[UnitOfWork]:
    """The unit of work: commits after the route, rolls back if it raised.

    It must be used with `scope="function"`. The default scope exits after the response was sent, so a failed
    commit would still be a 201 for the client, and background tasks would run before the commit (pitfall 14).
    """
    async with request.app.state.storage.unit_of_work(request.app.state.publisher) as uow:
        yield uow


UowDep = Annotated[UnitOfWork, Depends(get_uow, scope="function")]


async def get_repos(uow: UowDep) -> Repos:
    """The unit of work's repositories: in memory or SQL, whatever `settings.storage_backend` chose."""
    return uow.repos


async def get_locks(uow: UowDep) -> Locks:
    return uow.locks


ConfigDep = Annotated[Settings, Depends(get_config)]
NowDep = Annotated[datetime, Depends(get_now)]
ReposDep = Annotated[Repos, Depends(get_repos)]


def get_theater_tz(config: ConfigDep) -> ZoneInfo:
    """A sub-dependency: it depends on `get_config`."""
    return ZoneInfo(config.theater_tz)


TheaterTzDep = Annotated[ZoneInfo, Depends(get_theater_tz)]
LocksDep = Annotated[Locks, Depends(get_locks)]


async def get_event_subscription(request: Request) -> AsyncIterator[Subscription]:
    """A request-scoped `yield` dependency, the right scope for a stream: it ends after the last byte.

    It subscribes before the response starts, so no event is missed, and it always unsubscribes, whereas a
    `finally` inside the generator only runs when a suspended generator gets closed.
    """
    async with request.app.state.bus.subscribe(*SSE_EVENT_TYPES) as subscription:
        yield subscription


async def get_seat_hub(websocket: WebSocket) -> SeatHub:
    """WebSocket routes have no `Request`, so this takes the `WebSocket`.

    `async` although it awaits nothing: a `def` dependency would cost each socket a trip to the threadpool.
    """
    return websocket.app.state.seat_hub


EventSubscriptionDep = Annotated[Subscription, Depends(get_event_subscription)]
SeatHubDep = Annotated[SeatHub, Depends(get_seat_hub)]


def get_booking_context(
    request: Request,
    background_tasks: BackgroundTasks,
    uow: UowDep,
    repos: ReposDep,
    locks: LocksDep,
    now: NowDep,
    tz: TheaterTzDep,
) -> BookingContext:
    """Groups the booking service's collaborators. A dependency can take `BackgroundTasks`, like a route."""
    app = request.app
    # A factory, not `repos`: the task runs after this request's unit of work has ended.
    notifier = EmailNotifier(background_tasks.add_task, app.state.storage.open_repos, app.state.clock, tz)
    return BookingContext(
        repos=repos,
        locks=locks,
        now=now,
        rng=request.app.state.rng,
        notifier=notifier,
        publisher=uow.publisher,
    )


BookingContextDep = Annotated[BookingContext, Depends(get_booking_context)]


def get_admin_context(
    request: Request, uow: UowDep, repos: ReposDep, locks: LocksDep, now: NowDep, tz: TheaterTzDep
) -> AdminContext:
    return AdminContext(
        repos=repos,
        locks=locks,
        now=now,
        tz=tz,
        rng=request.app.state.rng,
        publisher=uow.publisher,
        after_commit=uow.after_commit,
    )


AdminContextDep = Annotated[AdminContext, Depends(get_admin_context)]


async def get_install_seed(uow: UowDep) -> Callable[[SeedData], Awaitable[None]]:
    """How a reset replaces the data: a swap of the store in memory, one transaction in SQL."""
    return uow.install


InstallSeedDep = Annotated[Callable[[SeedData], Awaitable[None]], Depends(get_install_seed)]

# auto_error=False: we raise the 401 ourselves, so it is an `ApiError` whatever FastAPI's default is.
admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def check_admin_key(key: str | None, config: Settings) -> None:
    # Compared as bytes: `compare_digest` raises TypeError for non-ASCII strings.
    if not key or not secrets.compare_digest(key.encode(), config.admin_api_key.encode()):
        raise AppError(
            401,
            ApiErrorCode.UNAUTHORIZED,
            "A valid X-Admin-Key header is required",
            headers={"WWW-Authenticate": "APIKey"},
        )


async def require_admin(
    config: ConfigDep, key: Annotated[str | None, Security(admin_key_header)] = None
) -> None:
    """The router-level dependency. It also puts the key into OpenAPI (Swagger's "Authorize" button)."""
    check_admin_key(key, config)


class AdminRoute(APIRoute):
    """Checks the key before the body is read.

    FastAPI parses the body *before* it runs any dependency. With `require_admin` alone, malformed JSON
    without a key was a 400, and an anonymous upload was received in full before its 401.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def check_key_first(request: Request) -> Response:
            check_admin_key(request.headers.get(admin_key_header.model.name), request.app.state.settings)
            return await handle(request)

        return check_key_first


async def get_user(user_id: UserIdPath, repos: ReposDep) -> User:
    """Resolves the `{user_id}` path parameter to a user, or 404. Only `/api/users/{user_id}` uses it:
    the booking routes follow the fake backend's own rules (plan 3.3)."""
    user = await repos.users.get(user_id)
    if user is None:
        raise not_found("User", user_id)
    return user


UserDep = Annotated[User, Depends(get_user)]


class Pagination:
    """Class-based dependency: FastAPI reads `__init__`'s parameters as query parameters.

    `limit` is optional so each route chooses its default: `/api/movies` returns everything,
    admin lists use 20.
    """

    def __init__(
        self,
        limit: Annotated[int | None, Query(ge=1, le=100, description="Page size (max 100)")] = None,
        offset: Annotated[int, Query(ge=0, description="How many matches to skip")] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset

    def limit_or(self, default: int) -> int:
        return default if self.limit is None else self.limit


PaginationDep = Annotated[Pagination, Depends()]


async def request_audit(request: Request) -> AsyncIterator[None]:
    """`yield` dependency: code before `yield` runs before the route, code after it runs afterwards.

    An exception raised by the route is re-raised at the `yield`; we log it and raise it again so the
    exception handlers still turn it into an `ApiError`.
    """
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except Exception as exc:
        outcome = type(exc).__name__
        raise
    finally:
        ms = (time.perf_counter() - started) * 1000
        log.info("%s %s -> %s in %.1f ms", request.method, request.url.path, outcome, ms)


AuditDep = Depends(request_audit)

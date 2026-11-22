import asyncio
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.deps import AuditDep
from app.api.openapi import DESCRIPTION, TAGS, install_openapi, operation_id
from app.api.routers import admin, bookings, meta, movies, posters, realtime, showtimes, users
from app.background import refresh_loop, refresh_showtimes
from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.shutdown import set_on_exit_signal
from app.domain.seed import build_seed
from app.middleware.request_context import RequestContextMiddleware
from app.middleware.simulation import SimulationMiddleware
from app.realtime.bus import BusPublisher, EventBus
from app.realtime.seat_hub import SeatHub
from app.schemas.demo import DemoSettings
from app.services import admin as admin_service
from app.services.locks import ShowtimeLocks
from app.services.posters import posters_dir
from app.storage import MemoryStorage

log = logging.getLogger(__name__)

# Headers that browser code may read on a cross-origin response.
EXPOSED_HEADERS = ["X-Total-Count", "Location", "X-Request-ID", "X-Process-Time", "ETag"]


async def close_bus_on_shutdown(shutdown: asyncio.Event, bus: EventBus) -> None:
    """Closing the bus ends every SSE stream and makes the seat hub close its sockets, all without polling."""
    await shutdown.wait()
    bus.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings: Settings = app.state.settings
    # Seed with the app-level clock: dependency_overrides would not reach this code (pitfall 11).
    now = app.state.clock.now()
    tz = ZoneInfo(settings.theater_tz)
    app.state.locks = ShowtimeLocks(enabled=settings.use_showtime_locks)
    app.state.bus = bus = EventBus()  # a fresh one per startup, because shutdown closes it
    app.state.publisher = BusPublisher(bus)
    app.state.demo_settings = DemoSettings(
        latency_ms=settings.demo_latency_ms, failure_rate=settings.demo_failure_rate
    )
    # StaticFiles(check_dir=False) still fails on the first request if the folder is missing.
    posters_dir(settings.uploads_dir).mkdir(parents=True, exist_ok=True)
    if settings.storage_backend == "sql":
        await start_sql(app)
    else:
        await start_memory(app, now, tz)
    try:
        async with running_tasks(app, bus, now.astimezone(tz).date()):
            yield
    finally:
        await app.state.storage.close()


async def start_memory(app: FastAPI, now: datetime, tz: ZoneInfo) -> None:
    app.state.storage = storage = MemoryStorage(build_seed(now, tz), app.state.locks)
    if user_id := app.state.settings.sample_bookings_for:
        await admin_service.add_startup_samples(storage.repos, user_id, now=now, tz=tz, rng=app.state.rng)


async def start_sql(app: FastAPI) -> None:
    # Imported here, so that memory mode never needs SQLAlchemy.
    from app.repositories.sql.storage import SqlStorage
    from app.seed import seed_if_empty

    settings: Settings = app.state.settings
    app.state.storage = storage = await SqlStorage.connect(settings.database_url, app.state.locks)
    try:
        if not await seed_if_empty(storage, app.state.clock, settings, app.state.rng):
            await refresh_showtimes(app)  # the data may be days old
    except BaseException:
        await storage.close()
        raise


@asynccontextmanager
async def running_tasks(app: FastAPI, bus: EventBus, seeded_on: date) -> AsyncIterator[None]:
    """The lifespan's background tasks. They end before storage closes."""
    app.state.shutdown_event = asyncio.Event()
    app.state.seat_hub = hub = SeatHub(bus, app.state.storage.open_repos)
    hub_task = asyncio.create_task(hub.run(), name="seat-hub")
    await hub.ready.wait()  # don't take requests whose events the hub would miss
    closer_task = asyncio.create_task(close_bus_on_shutdown(app.state.shutdown_event, bus), name="bus-closer")
    refresh_task = asyncio.create_task(refresh_loop(app, seeded_on), name="showtime-refresh")
    app.state.refresh_task = refresh_task
    try:
        with set_on_exit_signal(app.state.shutdown_event, hub.wait_until_all_closed):
            yield
    finally:
        app.state.shutdown_event.set()
        refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await refresh_task
        await closer_task
        await hub_task  # ends by itself when the bus closes, after closing every socket
        log.info("Background tasks stopped")


def add_middleware(app: FastAPI, settings: Settings) -> None:
    """The middleware added last runs first: CORS, GZip, request context, simulation, routes (5.12)."""
    app.add_middleware(SimulationMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Admin-Key", "X-Request-ID", "If-None-Match"],
        expose_headers=EXPOSED_HEADERS,
    )


def create_app(
    settings: Settings | None = None,
    clock: Clock | None = None,
    rng: random.Random | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Movies API",
        version="0.1.0",
        description=DESCRIPTION,
        openapi_tags=TAGS,
        generate_unique_id_function=operation_id,  # set before including routers, which inherit it
        lifespan=lifespan,
    )
    app.state.settings = settings or get_settings()
    app.state.clock = clock or SystemClock()
    app.state.rng = rng or random.Random()

    app.state.sleep = asyncio.sleep  # used by the simulation middleware; tests replace it

    register_exception_handlers(app)
    add_middleware(app, app.state.settings)

    app.include_router(meta.router)
    for api_router in (
        movies.router,
        movies.legacy_router,
        showtimes.router,
        users.router,
        posters.router,
        bookings.router,
        admin.router,
        realtime.router,
    ):
        # Router-level dependency: every API route runs inside the `request_audit` yield dependency.
        app.include_router(api_router, dependencies=[AuditDep])
    app.include_router(realtime.ws_router)
    # check_dir=False: importing the module must not need (or create) the folder; lifespan creates it.
    app.mount(
        "/uploads", StaticFiles(directory=app.state.settings.uploads_dir, check_dir=False), name="uploads"
    )
    install_openapi(app)
    return app


app = create_app()

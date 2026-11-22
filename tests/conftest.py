"""Every test that uses the app runs on both storage backends.

- `pytest -m "not sql"` is the quick loop.
- `@pytest.mark.backends("memory")` limits a test to the backends it is about.
"""

import os
import random
import shutil
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.clock import FrozenClock
from app.core.config import Settings
from app.main import create_app

# Same instant as the Angular specs: new Date(2026, 10, 19, 12, 0, 0) in the theater's time zone.
TEST_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=ZoneInfo("Europe/London"))
ADMIN_KEY = "test-admin-key"
BACKENDS = ("memory", "sql")


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "backend" not in metafunc.fixturenames:
        return  # doesn't touch the app, so once is enough
    marker = metafunc.definition.get_closest_marker("backends")
    chosen = marker.args if marker else BACKENDS
    params = [pytest.param(b, marks=pytest.mark.sql if b == "sql" else ()) for b in chosen]
    metafunc.parametrize("backend", params, indirect=True)


@pytest.fixture(autouse=True)
def no_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The README has people set `STORAGE_BACKEND` and friends in their shell. Keep that out of the tests."""
    for name in {*os.environ}:
        if name.lower() in Settings.model_fields:
            monkeypatch.delenv(name)


@pytest.fixture
def backend(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(TEST_NOW)


@pytest.fixture(scope="session")
def schema_template(tmp_path_factory: pytest.TempPathFactory):
    """An empty database with the schema. Copying a file is much faster than `create_all` in every test."""
    from sqlalchemy import create_engine

    from app.repositories.sql.models import Base

    path = tmp_path_factory.mktemp("schema") / "template.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture
def database_url(request: pytest.FixtureRequest, backend: str, tmp_path) -> str:
    path = tmp_path / "test.db"
    if backend == "sql":
        shutil.copy(request.getfixturevalue("schema_template"), path)  # a fresh file per test
    return f"sqlite+aiosqlite:///{path}"


@pytest.fixture
def settings(tmp_path, backend: str, database_url: str) -> Settings:
    # _env_file=None: a developer's local .env never leaks into tests.
    return Settings(
        _env_file=None,
        app_env="test",
        admin_api_key=ADMIN_KEY,
        storage_backend=backend,
        database_url=database_url,  # always a temp file, so a test can never reach a real movies.db
        uploads_dir=tmp_path / "uploads",
        sse_heartbeat_seconds=0.05,
    )


@pytest.fixture
def app(settings: Settings, frozen_clock: FrozenClock) -> FastAPI:
    return create_app(settings, frozen_clock, random.Random(1234))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:  # the `with` block runs lifespan
        yield c


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def aclient(app: FastAPI):
    """Async client for concurrency tests. `ASGITransport` does not run lifespan, so drive it by hand."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

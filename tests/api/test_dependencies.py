import logging
from datetime import UTC, datetime

from app.api.deps import get_now, get_repos
from app.core.config import get_settings
from app.domain.seed import load_movies


def test_dependency_override_replaces_the_clock_for_requests(app, client):
    app.dependency_overrides[get_now] = lambda: datetime(2030, 1, 1, tzinfo=UTC)
    try:
        response = client.get("/api/movies/m-last-lighthouse/showtimes", params={"upcomingOnly": "true"})
    finally:
        app.dependency_overrides.clear()

    # In 2030 every seeded showtime is in the past. The seed itself still used the app clock (2026), because
    # dependency_overrides only reach request handling, not lifespan (pitfall 11).
    assert response.json() == []
    assert client.get("/api/movies/m-last-lighthouse/showtimes", params={"upcomingOnly": "true"}).json()


class StubMovies:
    def __init__(self):
        self.movie = load_movies()[0].model_copy(update={"title": "Stubbed"})

    async def list_all(self, *, include_deleted=False):
        return [self.movie]

    async def get(self, movie_id, *, include_deleted=False):
        return self.movie if movie_id == self.movie.id else None


class StubRepos:
    movies = StubMovies()


def test_dependency_override_tests_a_router_with_a_stub_repo(app, client):
    app.dependency_overrides[get_repos] = StubRepos
    try:
        movies = client.get("/api/movies").json()
    finally:
        app.dependency_overrides.clear()

    assert [m["title"] for m in movies] == ["Stubbed"]


def test_pagination_dependency_reads_limit_and_offset(client):
    response = client.get("/api/movies", params={"limit": 2, "offset": 1})

    assert len(response.json()) == 2
    assert response.headers["X-Total-Count"] == "12"


def test_request_audit_yield_dependency_logs_after_the_route(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.audit"):
        client.get("/api/movies/m-last-lighthouse")
        client.get("/api/movies/nope")

    messages = [r.getMessage() for r in caplog.records if r.name == "app.audit"]
    assert any(m.startswith("GET /api/movies/m-last-lighthouse -> ok in ") for m in messages)
    assert any(m.startswith("GET /api/movies/nope -> AppError in ") for m in messages)


def test_get_settings_is_cached_until_cache_clear(monkeypatch):
    get_settings.cache_clear()
    try:
        monkeypatch.setenv("THEATER_TZ", "Europe/Paris")
        assert get_settings().theater_tz == "Europe/Paris"

        monkeypatch.setenv("THEATER_TZ", "Asia/Tokyo")
        assert get_settings().theater_tz == "Europe/Paris"  # cached

        get_settings.cache_clear()
        assert get_settings().theater_tz == "Asia/Tokyo"
    finally:
        get_settings.cache_clear()
